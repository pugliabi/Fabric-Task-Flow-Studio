@echo off
REM Launcher for Fabric Task Flows Studio
REM Created: 2026-07-09
REM Starts the local chat web app (FastAPI + uvicorn) and opens the browser.
REM Keep this window open while using the app; close it to stop the server.

title Fabric Task Flows Studio
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo Could not find .venv in %cd%
    echo Create it first:  python -m venv .venv
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo Failed to activate virtual environment
    pause
    exit /b 1
)

echo.
echo   Starting Fabric Task Flows Studio...
echo   A browser tab will open at http://127.0.0.1:8000
echo   Auto-reload is ON — code updates under app\ apply without a restart.
echo   Close this window to stop the server.
echo.

python run-app.py %*
if errorlevel 1 (
    echo.
    echo Studio exited with an error.
    pause
    exit /b 1
)
