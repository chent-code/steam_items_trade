@echo off
rem ============================================================
rem 10 分钟调度入口:库存调度 + 待发货检测 + 收购扫描推送
rem 用法: 由 Windows 计划任务每 10 分钟调用本脚本
rem   schtasks /create /tn "UUYP_auto" /tr "C:\path\to\steam_id_trade\scripts\windows\UUYP_auto.bat" /sc minute /mo 10
rem ============================================================
cd /d "%~dp0..\..\core"

rem 优先使用本机已装好依赖的解释器,没有则回退 PATH 中的 python
set "PY=C:\Users\lee\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" watcher_inv.py auto
