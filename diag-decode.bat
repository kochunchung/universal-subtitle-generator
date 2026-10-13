@echo off
rem Subtitle Generator - decoding diagnostic. Drag a video or MP3 onto this file.
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag a video or MP3 file onto this .bat file.
  pause
  exit /b 1
)
set "HF_HOME=%~dp0models"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONNOUSERSITE=1"
set "PATH=%~dp0ffmpeg;%PATH%"
"%~dp0python\python.exe" -m app.tools.diag_decode "%~1"
pause
