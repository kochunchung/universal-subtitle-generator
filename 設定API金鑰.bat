@echo off
rem Subtitle Generator - save a translation API key into Windows Credential Manager
setlocal
cd /d "%~dp0"
echo Which API?  1 = anthropic (Claude)   2 = openai (OpenAI / Azure / company server)
set "P=1"
set /p "P=Choose [1]: "
set "PROV=anthropic"
if "%P%"=="2" set "PROV=openai"
set "PYTHONNOUSERSITE=1"
"%~dp0python\python.exe" -m app.cli set-key %PROV%
pause
