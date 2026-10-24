@echo off
setlocal EnableExtensions
title Ultimate Media Downloader v1.0.0
color 0A
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating isolated virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :error
    set "PYTHON=.venv\Scripts\python.exe"
)

echo [2/4] Installing dependencies...
"%PYTHON%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :error

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [3/4] WARNING: ffmpeg was not found. Video and image downloads still work,
    echo       but MP3 extraction is disabled until ffmpeg is installed.
) else (
    echo [3/4] ffmpeg detected. MP3 extraction is available.
)

if "%HOST%"=="" set "HOST=127.0.0.1"
if "%PORT%"=="" set "PORT=5000"

echo [4/4] Starting Ultimate Media Downloader v1.0.0...
echo       Open http://%HOST%:%PORT% in your browser.
echo       For LAN access, run: set HOST=0.0.0.0 ^&^& run.bat
echo ========================================================

"%PYTHON%" app.py
goto :end

:error
echo.
echo Startup failed. Review the errors above.
pause

:end
endlocal
