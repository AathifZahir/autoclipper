@echo off
cd /d "%~dp0"
echo TEST MODE: shows the moments it would clip, but doesn't create any clips.
call "%~dp0start.bat" --test-mode
