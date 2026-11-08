#!/usr/bin/env bash
# Linux 首次部署:建 venv、装依赖、准备 data/
set -e
cd "$(dirname "$0")/../.."

python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt

mkdir -p data/logs data/reports
[ -f data/config.json ] || cp config/config.example.json data/config.json

echo "完成。下一步:"
echo "  1. 编辑 data/config.json 填入真实 token/uid"
echo "  2. cookie 放 data/steam_cookie.txt 与 data/buff_cookie.txt"
echo "  3. 参照 scripts/linux/crontab.example 配置定时任务"
