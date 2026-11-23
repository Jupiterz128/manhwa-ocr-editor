@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
if exist "%ProgramFiles%\Git\cmd\git.exe" set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
.venv\Scripts\python.exe github_backup.py --backup
if errorlevel 1 (
  echo BACKUP_TO_GITHUB failed. Check the message above.
  pause
  exit /b 1
)
echo BACKUP_TO_GITHUB completed.
pause
