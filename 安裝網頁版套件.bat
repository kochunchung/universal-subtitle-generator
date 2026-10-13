@echo off
rem Web version - install the extra Python packages (fastapi, uvicorn, itsdangerous, authlib). Safe to run again.
setlocal
cd /d "%~dp0"
if not exist "%~dp0python\python.exe" (
  echo Python not found. Please run install.bat first.
  pause
  exit /b 1
)
if not exist "%~dp0tools\uv.exe" (
  echo tools\uv.exe not found. Please run install.bat first.
  pause
  exit /b 1
)
set "UV_CACHE_DIR=%~dp0.cache\uv"
set "UV_LINK_MODE=copy"
set "UV_NO_CONFIG=1"
set "NO_COLOR=1"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
"%~dp0tools\uv.exe" pip install --python "%~dp0python\python.exe" --break-system-packages -r "%~dp0requirements.web.in" -c "%~dp0requirements.lock"
if errorlevel 1 (
  echo.
  echo [X] Install failed. If your company network inspects SSL, tell Claude the error text above.
) else (
  echo.
  echo [OK] Web packages installed.
)
pause
