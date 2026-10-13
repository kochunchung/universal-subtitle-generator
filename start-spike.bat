@echo off
rem Web version - stage 0 spike server (browser audio extraction + chunked upload test).
rem Runs only on this computer (127.0.0.1). Close this window to stop it.
setlocal
cd /d "%~dp0"
if not exist "%~dp0python\python.exe" (
  echo Python not found. Please run install.bat first.
  pause
  exit /b 1
)
set "HF_HOME=%~dp0models"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "PATH=%~dp0ffmpeg;%PATH%"
echo Starting stage-0 test server...
echo Open Chrome and go to  http://127.0.0.1:8765/
start "" "http://127.0.0.1:8765/"
"%~dp0python\python.exe" -m app.web.spike.server
pause
