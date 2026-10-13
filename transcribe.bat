@echo off
rem Subtitle Generator - command line transcription (stage 1 test)
rem Usage: drag one or more video/audio files onto this file.
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
"%~dp0python\python.exe" -m app.cli transcribe %*
pause
