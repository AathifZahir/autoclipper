@echo off
cd /d "%~dp0"
if not exist clips mkdir clips
start "" explorer "%~dp0clips"
