@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem Windows launcher: ASCII only + CRLF (cmd.exe parses with GBK codepage)
pushd "%~dp0..\..\core"
if errorlevel 1 (echo [ERROR] core dir not found & pause & exit /b 1)
set "PY=C://Users//lee//.workbuddy//binaries//python//envs//default//Scripts//python.exe"
if not exist "%PY%" set "PY=python"
echo [run] %CD%
"%PY%" -u watcher_inv.py auto
set RC=%ERRORLEVEL%
echo.
echo [done] exit=%RC% - weekly log in data\logs (monday-named file)
popd
exit /b %RC%
