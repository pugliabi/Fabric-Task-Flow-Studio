@echo off
REM ============================================================
REM  Fabric Task Flows Studio — clone-and-run launcher (Windows)
REM  Creates a virtual environment + installs deps on first run,
REM  then starts the app and opens http://127.0.0.1:8000
REM ============================================================
title Fabric Task Flows Studio
cd /d "%~dp0"

REM Force UTF-8 so diagrams/box glyphs render on Windows consoles
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment ^(.venv^) ...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Could not create the virtual environment.
        echo Make sure Python 3.11+ is installed and on your PATH.
        pause
        exit /b 1
    )
    echo Installing dependencies ...
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r app\requirements-app.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
)

echo.
echo   Starting Fabric Task Flows Studio ...
echo   A browser tab will open at http://127.0.0.1:8000
echo   Close this window to stop the server.
echo.

".venv\Scripts\python.exe" run-app.py %*
if errorlevel 1 (
    echo.
    echo Studio exited with an error.
    pause
    exit /b 1
)
