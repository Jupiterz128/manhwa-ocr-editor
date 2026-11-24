@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run INSTALL_WINDOWS.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe check_installation.py
if errorlevel 1 (
  pause
  exit /b 1
)
.venv\Scripts\python.exe web_api.py
if errorlevel 1 pause

