@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install_windows.bat first, or copy this update into the existing app folder.
  pause
  exit /b 1
)
.venv\Scripts\python.exe prepare_lama.py %*
if errorlevel 1 (
  echo LaMa setup failed. Auto can only clean validated flat or gradient backgrounds.
  echo Complex backgrounds need working LaMa. Telea is manual only and can leave blotches.
  echo For a corrupt checkpoint, run this file with --redownload.
  pause
  exit /b 1
)
echo LaMa is ready. No existing OCR packages were changed.
pause
