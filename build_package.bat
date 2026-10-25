@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

echo ========================================
echo         SAS PACKAGE BUILDER
echo ========================================
echo.

REM Python 3.15 from system installation; no venv required.
py -3.15 --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.15 is unavailable.
    goto :failed
)
py -3.15 -c "import saspy" >nul 2>&1
if errorlevel 1 (
    echo ERROR: SASPy is unavailable. Run: py -3.15 -m pip install saspy
    goto :failed
)
if not exist "%~dp0build_package.py" (
    echo ERROR: build_package.py not found.
    goto :failed
)

set "REPO=%~1"
if not defined REPO set /p "REPO=Repository path: "
if not defined REPO (
    echo ERROR: Repository path is required.
    goto :failed
)
if not exist "%REPO%\sas\description.sas" (
    echo ERROR: Missing "%REPO%\sas\description.sas"
    goto :failed
)

echo.
echo Repository : %REPO%
echo Package    : From saseschreibung.sas metadata
 echo Markdown   : yes
 echo Verify     : yes
 echo.
py -3.15 "%~dp0build_package.py" --repo "%REPO%" --markdown yes --verify yes
if errorlevel 1 goto :failed

echo.
echo BUILD COMPLETED SUCCESSFULLY
goto :finish
:failed
echo.
echo BUILD FAILED - Review the error above.
:finish
echo.
if "%~1"=="" pause
endlocal
