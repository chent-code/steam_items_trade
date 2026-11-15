#!/usr/bin/env bash
# Scheduler control for cron mode. Usage: ./scheduler.sh status|stop|start|restart
# stop  = comment out the marked cron line + kill any running round
# start = uncomment it (does NOT force an immediate run; next 10-min tick will fire)
set -u

ACTION="${1:-status}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CRONTAB_MARK="# steam_items_trade:scheduler"
CRON_CMD="${REPO}/scripts/linux/uuyp_auto.sh"
CONSOLE_LOG="${REPO}/data/logs/auto_console.log"

running_pids() {
    pgrep -f "watcher_inv.py auto" 2>/dev/null | tr '\n' ' '
}

show_status() {
    echo "REPO: ${REPO}"
    if command -v crontab >/dev/null 2>&1; then
        line="$(crontab -l 2>/dev/null | grep -F 'uuyp_auto.sh' || true)"
        if [ -z "$line" ]; then
            echo "CRON: no uuyp_auto.sh line installed"
        elif echo "$line" | grep -q '^[[:space:]]*#'; then
            echo "CRON: DISABLED (commented out)"
            echo "  ${line}"
        else
            echo "CRON: ENABLED"
            echo "  ${line}"
        fi
    else
        echo "CRON: crontab command not available"
    fi
    echo "RUNNING ROUNDS: $(running_pids)"
    if [ -f "$CONSOLE_LOG" ]; then
        echo "--- last 6 lines of auto_console.log ---"
        tail -6 "$CONSOLE_LOG"
    fi
}

stop_scheduler() {
    if command -v crontab >/dev/null 2>&1; then
        tmp="$(mktemp)"
        crontab -l 2>/dev/null | sed -E "s|^([^#[:space:]].*uuyp_auto\.sh.*)$|${CRONTAB_MARK} \1|" > "$tmp"
        crontab "$tmp"
        rm -f "$tmp"
        echo "[OK] cron line disabled (commented, reversible with start)"
    fi
    for pid in $(running_pids); do
        kill -9 "$pid" 2>/dev/null && echo "[OK] killed running round PID=${pid}"
    done
    echo "--- status after stop ---"
    show_status
}

start_scheduler() {
    if command -v crontab >/dev/null 2>&1; then
        tmp="$(mktemp)"
        crontab -l 2>/dev/null | sed -E "s|^${CRONTAB_MARK} (.*uuyp_auto\.sh.*)$|\1|" > "$tmp"
        crontab "$tmp"
        rm -f "$tmp"
        echo "[OK] cron line enabled (next 10-min tick will run)"
    fi
    echo "--- status after start ---"
    show_status
}

case "$ACTION" in
    status)  show_status ;;
    stop)    stop_scheduler ;;
    start)   start_scheduler ;;
    restart) stop_scheduler; start_scheduler ;;
    *) echo "Usage: $0 status|stop|start|restart"; exit 1 ;;
esac
