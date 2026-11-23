@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install_windows.bat first.
  pause
  exit /b 1
)
echo Close Manhwa OCR Editor before installing. Requires an NVIDIA CUDA GPU.
echo Installing the same pinned PyTorch versions with CUDA 12.4 support.
.venv\Scripts\python.exe -m pip install --no-cache-dir --no-deps --force-reinstall torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
if errorlevel 1 goto failed
.venv\Scripts\python.exe -c "import torch; assert torch.cuda.is_available(), 'CUDA unavailable: check NVIDIA driver'; print('GPU:',torch.cuda.get_device_name(0))"
if errorlevel 1 goto failed
.venv\Scripts\python.exe prepare_lama.py --check
if errorlevel 1 goto failed
echo GPU LaMa is ready. Restart START_3_1.bat. OCR settings and quality checks stay unchanged.
pause
exit /b 0
:failed
echo GPU setup or LaMa check did not pass. See the error above.
echo To restore CPU packages, run restore_cpu_windows.bat.
pause
exit /b 1
