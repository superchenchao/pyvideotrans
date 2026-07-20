@echo off
setlocal
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_rtx5080_vsr_fix.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" (
  echo VSR runtime repair failed. Send this window to Codex.
) else (
  echo VSR runtime repair completed. Run INSTALL_RTX5080.cmd again.
)
pause
exit /b %EXIT_CODE%
