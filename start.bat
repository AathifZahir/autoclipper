@echo off
title AutoClipper
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo AutoClipper isn't installed yet. Double-click setup.bat first.
  pause
  exit /b 1
)
echo Checking for downloader updates...
".venv\Scripts\python.exe" -m pip install -U yt-dlp -q --disable-pip-version-check >nul 2>nul
:run
".venv\Scripts\python.exe" -m autoclipper %*
if "%errorlevel%"=="2" (
  echo.
  echo Something went wrong - restarting automatically in 15 seconds...
  timeout /t 15 /nobreak >nul
  goto run
)
pause
