# SAS Package Builder

A standalone Windows BAT + Python CLI for building SAS Packages Framework (SPF) archives via SASPy and SAS OnDemand. No GUI or virtual environment required.

## Setup

Install SASPy into the installed Python 3.15 if necessary:

```powershell
py -3.15 -m pip install -r requirements.txt
```

Copy the **working** CodeVista `sas_config.json` and `_authinfo` into this project's root. The builder reads `config_name`, `java_path`, `iom_host`, `iom_port`, `authkey`, `authinfo`, and `encoding`. It resolves a relative `authinfo` path against the JSON directory. Do not commit credentials. `sas_config.json` may be committed only after reviewing it for secrets and machine-specific information; this project's `.gitignore` does not exclude it.

The builder makes temporary copies of `_authinfo` in a private temporary directory to support SASPy's home-directory credential lookup, and removes them after the build. It does not print credentials.

## Run

```powershell
.\build_package.bat "D:\Work\MyGithub\validation_utils"
```

Or double-click `build_package.bat` and enter the repository path. Package name comes from `sas/description.sas` and must match the repository folder name. Markdown generation and package load verification are both enabled by default.

For advanced options, invoke Python directly:

```powershell
py -3.15 build_package.py --repo "D:\Work\MyGithub\validation_utils" --markdown no --verify no
```

## Expected SPF repository

```text
validation_utils/
  sas/
    description.sas
    001_macro/
      validate_init.sas
      validate_case.sas
      validate_end.sas
```

The builder uploads source files to remote SAS WORK, runs `%generatePackage`, downloads `<package>.zip` and `<package>.md` into the repository root, and records logs in `build_logs/`. It does not publish GitHub releases.

## Troubleshooting

If SASPy asks for an OMR username, stop the process. Confirm the `authkey` alias exists in `_authinfo` and matches your working CodeVista configuration. This builder's SASPy connection and full remote build must be tested on your computer; syntax checks alone do not validate remote access.
