@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
".venv\Scripts\python.exe" -X utf8 gdrive_sync.py
if errorlevel 1 (
  echo Google Drive setup failed.
  pause
  exit /b 1
)
echo Google Drive authorization saved.
pause
