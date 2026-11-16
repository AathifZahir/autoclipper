@echo off
cd /d "%~dp0"
echo Makes ONE clip right now, to check that everything works.
set /p "CH=Type the name of a Twitch channel that is LIVE right now: "
if "%CH%"=="" exit /b 0
call "%~dp0start.bat" --clip-now %CH%
