#!/usr/bin/env bash
# ============================================================
# 收购扫描(独立手动运行,Linux):完整报告 + CSV
# 用法: ./uuyp_buy_scan.sh [--pages 8] [--max-verify 15] [--mode qiugou]
# ============================================================
cd "$(dirname "$0")/../../core"

PY=".venv/bin/python"
[ -x "$PY" ] || PY=python3

exec "$PY" auto_buy_test.py --pages 8 --max-verify 15 "$@"
