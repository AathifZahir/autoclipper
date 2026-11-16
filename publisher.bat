@echo off
title AutoClipper Publisher
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo AutoClipper isn't installed yet. Double-click setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m autoclipper.publisher
pause