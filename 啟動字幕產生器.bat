@echo off
rem Subtitle Generator - launcher (GUI). You can also drag videos onto this file.
setlocal
cd /d "%~dp0"
if not exist "%~dp0python\python.exe" (
  echo Python not found. Please run install.bat first.
  pause
  exit /b 1
)
set "HF_HOME=%~dp0models"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "PATH=%~dp0ffmpeg;%PATH%"
rem pythonw = no black console window. Errors are written to logs\app.log and logs\worker.log
if exist "%~dp0python\pythonw.exe" (
  start "" "%~dp0python\pythonw.exe" -m app.main %*
) else (
  "%~dp0python\python.exe" -m app.main %*
)
