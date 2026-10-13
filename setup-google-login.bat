@echo off
rem Web version - save the Google sign-in Client ID / Client Secret into .env (run once per server).
setlocal
cd /d "%~dp0"
if not exist "%~dp0python\python.exe" (
  echo Python not found. Please run install.bat first.
  pause
  exit /b 1
)
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
"%~dp0python\python.exe" -m app.web.setup_google
echo.
pause
