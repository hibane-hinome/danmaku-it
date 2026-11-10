@echo off
rem Launch the floating danmaku machine from the local virtualenv (no console window).
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" -m collector gui -c "%~dp0config.json"
