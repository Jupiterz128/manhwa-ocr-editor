@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install_windows.bat first.
  pause
  exit /b 1
)
echo Close Manhwa OCR Editor before restoring CPU PyTorch.
.venv\Scripts\python.exe -m pip install --no-cache-dir --no-deps --force-reinstall torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 (
  pause
  exit /b 1
)
echo CPU PyTorch restored. Restart START_3_1.bat.
pause
