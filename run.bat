@echo off
rem Start the collector in the background without a console window.
setlocal
cd /d "%~dp0"
start "" /b "%~dp0.venv\Scripts\pythonw.exe" -m collector run -c "%~dp0config.json"
endlocal
