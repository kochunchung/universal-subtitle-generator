@echo off
rem Subtitle Generator - offline translation diagnostic. Drag one .srt file onto this file.
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag one .srt file onto this .bat file.
  pause
  exit /b 1
)
echo Target language:  1 = zh-TW   2 = en   3 = ja
set "T=1"
set /p "T=Choose [1]: "
set "TO=zh-TW"
if "%T%"=="2" set "TO=en"
if "%T%"=="3" set "TO=ja"
set "HF_HOME=%~dp0models"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONNOUSERSITE=1"
"%~dp0python\python.exe" -m app.tools.diag_translate "%~1" --to %TO%
pause
