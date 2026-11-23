@echo off
setlocal
cd /d "%~dp0"
set "MANHWA_APP_HOME=%CD%"
if not defined MANHWA_RUNNER_HOME set "MANHWA_RUNNER_HOME=D:\GitHubRunner"
if not exist "%MANHWA_RUNNER_HOME%\.runner" (
  echo Run SETUP_GITHUB_RUNNER.ps1 first.
  pause
  exit /b 1
)
start "Manhwa GitHub Runner" /min /d "%MANHWA_RUNNER_HOME%" cmd.exe /c run.cmd
