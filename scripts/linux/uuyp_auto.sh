#!/usr/bin/env bash
# ============================================================
# 10 分钟调度入口(Linux):库存调度 + 待发货检测 + 收购扫描推送
# 由 cron/systemd timer 每 10 分钟调用,见 crontab.example
# ============================================================
cd "$(dirname "$0")/../../core"

# 优先用仓库 venv,否则用系统 python3
PY=".venv/bin/python"
[ -x "$PY" ] || PY=python3

exec "$PY" watcher_inv.py auto
