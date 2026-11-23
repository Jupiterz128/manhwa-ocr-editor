@echo off
setlocal
cd /d "%~dp0"
py -3.11 --version >nul 2>&1
if errorlevel 1 (
  echo Please install Python 3.11 64-bit with the Python launcher first.
  echo https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements_text_psd.txt
if errorlevel 1 goto fail
echo Basic local OCR is ready. Use START_3_1.bat.
echo For PaddleOCR and comic bubble detection run INSTALL_ADVANCED_WINDOWS.bat.
echo Text uses TH Sarabun New Bold. Editable PSD export requires Photoshop Desktop.
pause
exit /b 0
:fail
echo Installation failed. Copy the error shown above.
pause
exit /b 1
