@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install_windows.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements_text_psd.txt
if errorlevel 1 (
  echo Text setup failed. See the error above.
  pause
  exit /b 1
)
echo Text is ready. Install TH Sarabun New Bold and restart Photoshop if needed.
echo PSD export uses your installed Photoshop Desktop to preserve editable type layers.
pause
