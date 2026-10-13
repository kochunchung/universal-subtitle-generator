@echo off
rem Web version (development). Starts the website AND the GPU job runner on this computer.
rem Dev login is ON: you can log in with any name (no Google account needed). Do NOT expose this to the internet.
setlocal
cd /d "%~dp0"
if not exist "%~dp0python\python.exe" (
  echo Python not found. Please run install.bat first.
  pause
  exit /b 1
)
"%~dp0python\python.exe" -c "import fastapi, uvicorn, itsdangerous, authlib, cryptography" 2>nul
if errorlevel 1 (
  echo Web packages are missing or outdated. Please run the web-packages installer first.
  pause
  exit /b 1
)
set "HF_HOME=%~dp0models"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "PATH=%~dp0ffmpeg;%PATH%"
set "WEB_DEV_LOGIN=1"
rem The port comes from WEB_PORT in .env (default 8780). The browser opens by itself when the site is ready.
echo Starting...  Close this window to stop.
"%~dp0python\python.exe" -m app.web --open
pause
