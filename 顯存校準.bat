@echo off
rem Subtitle Generator - measure VRAM usage per batch item (run once per model)
setlocal
cd /d "%~dp0"
set "HF_HOME=%~dp0models"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONNOUSERSITE=1"
set "PATH=%~dp0ffmpeg;%PATH%"
"%~dp0python\python.exe" -m app.cli calibrate
pause
