#!/usr/bin/env python3
"""Build SAS Packages Framework archives through a remote SASPy session."""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import os
import shutil
import traceback
from datetime import datetime
from pathlib import Path

SPF_URL = 'https://raw.githubusercontent.com/yabwon/SAS_PACKAGES/main/SPF/SPFinit.sas'


def sas_quote(value: str) -> str:
    """SAS double-quoted string; reject control characters."""
    if any(ord(c) < 32 for c in value):
        raise ValueError('Control characters are not permitted in SAS paths.')
    return '"' + value.replace('"', '""') + '"'


def check_log(log: str, stage: str) -> None:
    # Exclude ordinary text containing the word ERROR; inspect SAS log line starts.
    matches = re.findall(r'^\s*(?:ERROR(?:\s+\d+-\d+)?|FATAL):.*$', log, re.I | re.M)
    if matches:
        raise RuntimeError(f'{stage} failed:\n' + '\n'.join(matches[:12]))


def submit(sas, code: str, stage: str, logs: list[str]) -> str:
    result = sas.submit(code, results='text')
    log = result.get('LOG', '')
    logs.append(f'\n===== {stage} =====\n{log}\n')
    check_log(log, stage)
    return log


def parse_metadata(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding='utf-8-sig')
    name = re.search(r'^\s*Package:\s*(\S+)\s*$', text, re.M | re.I)
    version = re.search(r'^\s*Version:\s*(\S+)\s*$', text, re.M | re.I)
    if not name or not version:
        raise ValueError('sas/description.sas must contain Package: and Version: metadata.')
    package, ver = name.group(1), version.group(1)
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', package):
        raise ValueError(f'Invalid SPF package name: {package!r}')
    return package, ver


def list_sources(root: Path) -> list[Path]:
    sas_root = root / 'sas'
    if not (sas_root / 'description.sas').is_file():
        raise FileNotFoundError(f'Missing {sas_root / "description.sas"}')
    macros = list((sas_root / '001_macro').glob('*.sas'))
    if not macros:
        raise ValueError('No .sas macros found under sas/001_macro.')
    # SPF input: description and numbered package directories only.
    sources = [p for p in sas_root.rglob('*') if p.is_file()
               and (p.name.lower() == 'description.sas' or
                    (p.relative_to(sas_root).parts[0][:1].isdigit()
                     and p.relative_to(sas_root).parts[0] != '999_code'))]
    return sorted(sources)


def mkdir_remote(sas, path: str, logs: list[str]) -> None:
    # Paths are generated from the SAS WORK location and validated local relative names.
    parent, name = path.rsplit('/', 1)
    code = f'''data _null_;
 length created $1024;
 created=dcreate({sas_quote(name)}, {sas_quote(parent)});
 if missing(created) and not fileexist({sas_quote(path)}) then
     put 'ERROR: Cannot create remote directory: ' {sas_quote(path)};
run;'''
    submit(sas, code, f'Make remote directory {name}', logs)


def remote_exists(sas, remote: str, logs: list[str]) -> bool:
    # A SAS PUT statement cannot print FILEEXIST() directly; assign it first.
    code = f'''data _null_;
 exists = fileexist({sas_quote(remote)});
 put "SPF_FILE_EXISTS=" exists;
run;'''
    log = submit(sas, code, 'Check remote artifact', logs)
    match = re.search(r'^SPF_FILE_EXISTS=\s*([01])\s*$', log, re.M)
    if not match:
        raise RuntimeError('Could not determine whether the remote artifact exists. Check the build log.')
    return match.group(1) == '1'


def transfer_ok(result, direction: str) -> None:
    # SASPy upload/download normally returns a dict with Success and LOG.
    if isinstance(result, dict) and result.get('Success') is False:
        raise RuntimeError(f'{direction} failed: {result.get("LOG", result)}')


def main() -> int:
    parser = argparse.ArgumentParser(description='Build an SPF package via SASPy and SAS OnDemand.')
    parser.add_argument('--repo', required=True, help='Local SPF repository root')
    parser.add_argument('--package', help='Expected package name (defaults to description.sas)')
    parser.add_argument('--markdown', choices=['yes', 'no'], default='yes')
    parser.add_argument('--verify', choices=['yes', 'no'], default='yes')
    parser.add_argument('--config', default=str(Path(__file__).parent / 'sas_config.json'), help='CodeVista-compatible sas_config.json')
    args = parser.parse_args()
    repo = Path(args.repo).expanduser().resolve()
    logs: list[str] = []
    log_dir = repo / 'build_logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f'build_{datetime.now():%Y%m%d_%H%M%S}.log'
    sas = None
    tempdir = None
    try:
        if not repo.is_dir():
            raise FileNotFoundError(f'Repository not found: {repo}')
        sources = list_sources(repo)
        name, version = parse_metadata(repo / 'sas' / 'description.sas')
        if repo.name.lower() != name.lower():
            raise ValueError(f'Repository folder {repo.name!r} must match SPF Package metadata {name!r}.')
        if args.package and args.package.lower() != name.lower():
            raise ValueError(f'Package mismatch: requested {args.package}, metadata declares {name}')
        config_path = Path(args.config).expanduser().resolve()
        cfg = json.loads(config_path.read_text(encoding='utf-8-sig'))
        cfgname = cfg.get('config_name', 'oda')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', cfgname):
            raise ValueError('Invalid SASPy configuration name')
        java = cfg.get('java_path', '')
        host = cfg.get('iom_host', '')
        port = int(cfg.get('iom_port', 8591))
        authkey = cfg.get('authkey', 'oda')
        encoding = cfg.get('encoding', 'utf-8')
        if not java or not Path(java).is_file():
            raise FileNotFoundError(f'Java executable not found: {java!r}')
        if not host:
            raise ValueError('sas_config.json requires iom_host')
        authfile = Path(cfg.get('authinfo', '_authinfo'))
        if not authfile.is_absolute():
            authfile = config_path.parent / authfile
        if not authfile.is_file():
            raise FileNotFoundError(f'Authentication file not found: {authfile}')
        # SASPy IOM can ignore the authinfo configuration path and search
        # the user's home directory. Provide a private temporary home with
        # the existing _authinfo, without modifying the user's real home.
        tempdir = tempfile.TemporaryDirectory(prefix='spf_builder_')
        temp_home = Path(tempdir.name) / 'home'
        temp_home.mkdir()
        shutil.copyfile(authfile, temp_home / '_authinfo')
        shutil.copyfile(authfile, temp_home / '.authinfo')
        cfgfile = Path(tempdir.name) / 'saspy_cfg.py'
        cfgtext = ("SAS_config_names = [" + repr(cfgname) + "]\n"
                   + cfgname + " = " + repr({
                       'java': java, 'iomhost': host, 'iomport': port,
                       'authkey': authkey, 'encoding': encoding,
                       'authinfo': str(authfile)
                   }) + "\n")
        cfgfile.write_text(cfgtext, encoding='utf-8')
        print(f'Package: {name} v{version}\nRepository: {repo}\nSource files: {len(sources)}')
        import saspy
        print('Connecting to SAS...')
        # SASPy uses expanduser('~') for its default authinfo lookup.
        # Scope HOME/USERPROFILE overrides to session creation only.
        old_home = os.environ.get('HOME')
        old_profile = os.environ.get('USERPROFILE')
        try:
            os.environ['HOME'] = str(temp_home)
            os.environ['USERPROFILE'] = str(temp_home)
            sas = saspy.SASsession(cfgfile=str(cfgfile), cfgname=cfgname)
        finally:
            if old_home is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = old_home
            if old_profile is None:
                os.environ.pop('USERPROFILE', None)
            else:
                os.environ['USERPROFILE'] = old_profile
        if sas is None:
            raise RuntimeError('SASPy did not return a session object.')
        # SASPy session objects may evaluate as False despite a successful IOM connection.
        # Confirm connectivity by submitting a real SAS statement instead.
        print('Checking SAS execution...')
        connection_log = submit(sas, '%put NOTE: SPF_BUILDER_CONNECTION_OK;',
                                'Verify SAS connection', logs)
        if 'SPF_BUILDER_CONNECTION_OK' not in connection_log:
            raise RuntimeError('SAS session opened but execution check did not return its marker.')
        print('SAS session verified successfully.')
        # Retrieve WORK path through SAS log (no reliance on SASPy macro expansion).
        work_log = submit(sas, '%put SPF_WORK_DIR=%sysfunc(pathname(work));', 'Find SAS WORK', logs)
        found = re.search(r'^SPF_WORK_DIR=(.+)$', work_log, re.M)
        if not found:
            raise RuntimeError('Could not resolve the SAS WORK directory.')
        work = found.group(1).strip().replace('\\', '/')
        remote_root = work.rstrip('/') + '/spf_' + name.lower()
        mkdir_remote(sas, remote_root, logs)
        mkdir_remote(sas, remote_root + '/sas', logs)
        # Create each relative directory before uploading the source files.
        relative_dirs = {p.relative_to(repo / 'sas').parent for p in sources}
        dirs = sorted({str(d).replace('\\', '/') for d in relative_dirs if str(d) != '.'},
                      key=lambda d: (d.count('/'), d))
        created_dirs: set[str] = set()
        for directory in dirs:
            segments = directory.split('/')
            for i in range(1, len(segments) + 1):
                target = remote_root + '/sas/' + '/'.join(segments[:i])
                if target not in created_dirs:
                    mkdir_remote(sas, target, logs)
                    created_dirs.add(target)
        for src in sources:
            rel = src.relative_to(repo / 'sas').as_posix()
            dst = remote_root + '/sas/' + rel
            print(f'Uploading: {rel}')
            transfer_ok(sas.upload(str(src), dst, overwrite=True), 'Upload ' + rel)
            if not remote_exists(sas, dst, logs):
                raise RuntimeError(f'Upload not found on SAS server: {rel}')
        print('Initializing SPF from GitHub...')
        submit(sas, f'filename spfinit url {sas_quote(cfg.get("spf_init_url", SPF_URL))};\n%include spfinit;',
               'Initialize SPF', logs)
        print('Generating package...')
        markdown = '1' if args.markdown == 'yes' else '0'
        build_log = submit(sas,
            f'%generatePackage(filesLocation={remote_root}/sas, buildLocation={remote_root}, markdownDoc={markdown});',
            'Generate SPF package', logs)
        # Confirm expected artifacts rather than assuming successful macro return.
        expected = [name.lower() + '.zip']
        if args.markdown == 'yes':
            expected.append(name.lower() + '.md')
        for filename in expected:
            remote = remote_root + '/' + filename
            if not remote_exists(sas, remote, logs):
                raise RuntimeError(f'SPF did not produce expected artifact: {filename}')
            target = repo / filename
            print(f'Downloading: {filename}')
            transfer_ok(sas.download(str(target), remote, overwrite=True), 'Download ' + filename)
            if not target.is_file() or target.stat().st_size == 0:
                raise RuntimeError(f'Download missing or empty: {target}')
        if args.verify == 'yes':
            print('Verifying package load in SAS...')
            # Use SPF's explicit path= argument to load the freshly built ZIP.
            # Do not reassign the PACKAGES fileref: SPF may already manage it.
            verify_code = f"""
options mprint mlogic symbolgen source2;

%loadPackage(
    {name},
    requiredVersion={version},
    path={remote_root}
);

%put NOTE: SPF_BUILDER_LOADED_PACKAGES=&SYSLOADEDPACKAGES;

options nomprint nomlogic nosymbolgen nosource2;
"""
            verify_log = submit(sas, verify_code, 'Verify package load', logs)
            # SPF reports the loaded package and version in SYSLOADEDPACKAGES.
            # Require that evidence rather than treating an error-free log alone
            # as proof that the requested version was loaded.
            loaded = re.search(
                rf'INFO:\s*\[SYSLOADEDPACKAGES\].*?\b{re.escape(name)}\({re.escape(version)}\)',
                verify_log, re.IGNORECASE | re.DOTALL
            )
            if not loaded:
                raise RuntimeError(
                    f'SPF did not confirm loading {name}({version}); inspect build log.'
                )
            print(f'Package load verified: {name} v{version}')
        print(f'BUILD COMPLETE: {repo / expected[0]}')
        return 0
    except Exception as exc:
        detail = traceback.format_exc()
        logs.append(f'\nBUILD FAILED: {exc}\n{detail}\n')
        print(f'BUILD FAILED: {exc}\n{detail}', file=sys.stderr)
        return 1
    finally:
        try:
            log_path.write_text('\n'.join(logs), encoding='utf-8')
            print(f'Build log: {log_path}')
        except OSError as exc:
            print(f'Could not write build log: {exc}', file=sys.stderr)
        if sas is not None:
            try:
                sas.endsas()
            except Exception:
                pass
        if tempdir is not None:
            tempdir.cleanup()


if __name__ == '__main__':
    sys.exit(main())
