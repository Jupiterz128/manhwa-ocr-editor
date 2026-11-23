@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install_windows.bat first, or copy this update into your existing app folder.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements_rapid.txt
if errorlevel 1 (
  echo RapidOCR installation failed. Existing OCR modes are still available.
  pause
  exit /b 1
)
.venv\Scripts\python.exe prepare_rapid_models.py
if errorlevel 1 (
  echo RapidOCR model preparation failed. Check your connection and run this file again.
  echo Existing OCR modes are still available.
  pause
  exit /b 1
)
echo RapidOCR is ready. Start the app and use the RapidOCR crop button, or select OCR=rapid.
echo RapidOCR uses CPU. The existing auto mode stays unchanged.
pause
