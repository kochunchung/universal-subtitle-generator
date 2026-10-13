@echo off
rem Web version stage 1 acceptance: run the same videos the desktop way and the web way, then compare the SRT files.
rem Drag 1-3 video files onto this file. Optional: set TO=en (or zh-TW / ja) below to also translate.
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag one to three video files onto this .bat file.
  pause
  exit /b 1
)
if not exist "%~dp0python\python.exe" (
  echo Python not found. Please run install.bat first.
  pause
  exit /b 1
)
echo Translate to:  0 = none (transcribe only)   1 = zh-TW   2 = en   3 = ja
set "T=0"
set /p "T=Choose [0]: "
set "TO="
if "%T%"=="1" set "TO=--to zh-TW"
if "%T%"=="2" set "TO=--to en"
if "%T%"=="3" set "TO=--to ja"
set "HF_HOME=%~dp0models"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "PATH=%~dp0ffmpeg;%PATH%"
"%~dp0python\python.exe" -m app.web.acceptance %TO% %*
echo.
pause
