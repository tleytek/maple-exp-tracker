@echo off
REM Builds shareable dist\MapleExpTracker.exe and dist\MaplePacketTracker.exe (no Python needed).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
)
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1

.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --icon meso.ico --add-data "meso.ico;." ^
    --name MapleExpTracker main.py
if errorlevel 1 exit /b 1

REM Second exe: same window fed from network packets (needs Wireshark/Npcap).
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --icon meso.ico --add-data "meso.ico;." ^
    --name MaplePacketTracker packetmain.py
if errorlevel 1 exit /b 1

echo.
echo Built: %~dp0dist\MapleExpTracker.exe
echo Built: %~dp0dist\MaplePacketTracker.exe
