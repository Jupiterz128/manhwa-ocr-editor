@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
if exist "%ProgramFiles%\Git\cmd\git.exe" set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
git pull --rebase
if errorlevel 1 (
  echo SYNC_FROM_GITHUB failed. Check the message above.
  pause
  exit /b 1
)
echo SYNC_FROM_GITHUB completed.
pause
