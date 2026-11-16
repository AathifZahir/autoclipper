@echo off
cd /d "%~dp0"
if not exist final mkdir final
start "" explorer "%~dp0final"
