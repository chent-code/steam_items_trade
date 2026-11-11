@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem Scheduled-task entry: NO console output (avoids QuickEdit freeze), all output to file
pushd "%~dp0..\..\core"
if errorlevel 1 exit /b 1
if not exist "%~dp0..\..\data\logs" mkdir "%~dp0..\..\data\logs"
set "LOG=%~dp0..\..\data\logs\auto_console.log"
set "PY=C:\Users\lee\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo [run] %CD% > "%LOG%" 2>&1
"%PY%" -u watcher_inv.py auto >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo [done] exit=%RC% >> "%LOG%" 2>&1
popd
exit /b %RC%
