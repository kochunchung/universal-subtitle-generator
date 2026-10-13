@echo off
rem ============================================================
rem  Subtitle Generator - one-click installer (stage 0)
rem  No administrator rights needed. Safe to run again.
rem  The real work is done by scripts\install.ps1
rem ============================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set "ERR=%ERRORLEVEL%"
echo.
if not "%ERR%"=="0" (
  echo [X] Install failed. Details: logs\install.log
) else (
  echo [OK] Install finished.
)
pause
exit /b %ERR%
