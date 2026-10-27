@echo off
REM Map census: leave it running while you play; it adds every map visit to census.db.
REM Needs the tracker's .venv (run run.bat once first) and Wireshark/Npcap.
setlocal
cd /d "%~dp0"
set NAMES=
if exist "..\maple-map-report\data\osms\maps.json" set NAMES=--names ..\maple-map-report\data\osms\maps.json
.venv\Scripts\python.exe census.py --quiet %NAMES% %*
pause
