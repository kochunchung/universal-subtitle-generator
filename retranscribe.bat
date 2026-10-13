@echo off
rem Subtitle Generator - transcribe again, overwrite existing SRT/MP3, save word timings for checking
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag one or more video or audio files onto this .bat file.
  pause
  exit /b 1
)
set "HF_HOME=%~dp0models"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONNOUSERSITE=1"
set "PATH=%~dp0ffmpeg;%PATH%"
"%~dp0python\python.exe" -m app.cli transcribe --overwrite --debug %*
pause
