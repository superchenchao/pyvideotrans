@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_rtx5080_deploy.ps1"
if errorlevel 1 (
  echo.
  echo 安装失败，请把 deploy-logs 目录中最新日志发给 Codex。
) else (
  echo.
  echo 安装完成。现在可以双击“启动pyVideoTrans.cmd”。
)
pause

