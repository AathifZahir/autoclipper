@echo off
title AutoClipper
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo AutoClipper isn't installed yet. Double-click setup.bat first.
  pause
  exit /b 1
)
echo Opening AutoClipper in your browser...
echo Keep this window open while you use it. The clipper keeps running even if you close it.
".venv\Scripts\python.exe" -m pip install -U yt-dlp -q --disable-pip-version-check >nul 2>nul
".venv\Scripts\python.exe" -m autoclipper.publisher
pause