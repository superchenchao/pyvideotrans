@echo off
setlocal
cd /d "%~dp0"
call "%~dp0FIX_VSR_RTX5080.cmd"
exit /b %ERRORLEVEL%
