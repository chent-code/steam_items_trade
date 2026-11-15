@echo off
chcp 65001 >nul
rem Scheduler control wrapper (ASCII + CRLF only)
cd /d "%~dp0"
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSEXE%" set "PSEXE=powershell"
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scheduler.ps1" -Action stop
echo.
pause
