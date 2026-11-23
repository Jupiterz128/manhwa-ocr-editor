@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run INSTALL_WINDOWS.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements_advanced.txt
if errorlevel 1 (
  echo Advanced installation failed. The basic EasyOCR installation may still be used.
  echo Choose easy and disable the comic detector if needed.
  pause
  exit /b 1
)
.venv\Scripts\python.exe download_paddle_models.py
if errorlevel 1 (
  echo Paddle weights download failed. EasyOCR is still available.
  echo Run this installer again when the connection is available.
  pause
  exit /b 1
)
echo Advanced models are ready. Comic detector weights download on first use.
pause
