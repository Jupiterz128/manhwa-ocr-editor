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
.venv\Scripts\python.exe manhwa_ocr.py
if errorlevel 1 pause
