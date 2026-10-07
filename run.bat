@echo off
REM Maple EXP Tracker launcher.
REM First run creates a private Python environment (.venv) and installs the
REM dependencies; after that it just opens the tracker window.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" goto deps

echo First run: setting things up (one time, about a minute)...
where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Python was not found.
    echo Install Python 3 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run this again.
    echo.
    pause
    exit /b 1
)

:deps
REM (Re)install packages whenever requirements.txt changes.
fc /b requirements.txt .venv\requirements.installed >nul 2>nul
if errorlevel 1 (
    echo Installing packages...
    .venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Package install failed - see the messages above.
        pause
        exit /b 1
    )
    copy /y requirements.txt .venv\requirements.installed >nul
)

start "" ".venv\Scripts\pythonw.exe" main.py
