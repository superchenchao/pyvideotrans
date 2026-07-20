@echo off
setlocal
cd /d "%~dp0"
set "PYVIDEOTRANS_SUBTITLE_REMOVER_HOME=%~dp0video-subtitle-remover"
set "PYVIDEOTRANS_SUBTITLE_REMOVER_PYTHON=%~dp0vsr-venv310\Scripts\python.exe"
set "PYVIDEOTRANS_SUBTITLE_INPAINT=auto"
set "PATH=%~dp0ffmpeg;%PATH%"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Runtime is missing. Run INSTALL_RTX5080.cmd first.
  pause
  exit /b 1
)
start "pyVideoTrans RTX5080" /D "%~dp0" "%~dp0.venv\Scripts\python.exe" "%~dp0sp.py"
