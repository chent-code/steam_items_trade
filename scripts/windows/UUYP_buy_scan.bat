@echo off
rem ============================================================
rem 收购扫描(独立手动运行):完整报告 + CSV,双击或命令行执行
rem 常用参数: --pages 8 扫描页数 | --max-verify 15 | --mode qiugou 求购单
rem ============================================================
cd /d "%~dp0..\..\core"

set "PY=C:\Users\lee\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" auto_buy_test.py --pages 8 --max-verify 15
pause
