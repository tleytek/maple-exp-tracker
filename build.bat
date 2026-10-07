@echo off
REM Builds a single shareable dist\MapleExpTracker.exe (no Python needed to run it).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
)
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1

.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name MapleExpTracker main.py
if errorlevel 1 exit /b 1

echo.
echo Built: %~dp0dist\MapleExpTracker.exe
