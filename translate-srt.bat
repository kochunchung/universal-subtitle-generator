@echo off
rem Subtitle Generator - translate SRT files. Drag one or more .srt files onto this file.
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag one or more .srt files onto this .bat file.
  pause
  exit /b 1
)
echo Target language:  1 = zh-TW   2 = en   3 = ja
set "T=2"
set /p "T=Choose [2]: "
set "TO=en"
if "%T%"=="1" set "TO=zh-TW"
if "%T%"=="3" set "TO=ja"
echo.
echo Engine:  1 = m2m100 (offline)   2 = anthropic (Claude API)   3 = openai (OpenAI compatible)   4 = nllb (offline)
set "E=1"
set /p "E=Choose [1]: "
set "ENG=m2m100"
if "%E%"=="2" set "ENG=anthropic"
if "%E%"=="3" set "ENG=openai"
if "%E%"=="4" set "ENG=nllb"
echo.
set "B=n"
set /p "B=Bilingual subtitles (original + translation)? y/n [n]: "
set "BI="
if /i "%B%"=="y" set "BI=--bilingual"
set "HF_HOME=%~dp0models"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONNOUSERSITE=1"
set "PATH=%~dp0ffmpeg;%PATH%"
"%~dp0python\python.exe" -m app.cli translate --to %TO% --engine %ENG% %BI% --overwrite %*
pause
