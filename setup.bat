@echo off
title AutoClipper setup
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python is not installed.
  echo Get it from https://www.python.org/downloads/  - tick "Add python.exe to PATH" - then run this again.
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(sys.version_info < (3, 11))" || (
  echo Your Python is too old. Install Python 3.12 from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating a private Python environment for AutoClipper...
  %PY% -m venv .venv || (echo Could not create it. & pause & exit /b 1)
)
echo Installing the parts it needs - the first time takes a minute...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q --disable-pip-version-check
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q --disable-pip-version-check || (echo Install failed - check your internet and run setup.bat again. & pause & exit /b 1)
".venv\Scripts\python.exe" -m autoclipper.setup_wizard
pause
