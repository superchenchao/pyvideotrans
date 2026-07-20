@echo off
setlocal
cd /d "%~dp0"
if /I "%~1"=="--self-test" (
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_rtx5080_deploy.ps1" -LauncherSelfTest
  exit /b %ERRORLEVEL%
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_rtx5080_deploy.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" (
  echo.
  echo Installation failed. Send the newest file in deploy-logs to Codex.
) else (
  echo.
  echo Installation complete. Run START_pyVideoTrans.cmd.
)
pause
exit /b %EXIT_CODE%
