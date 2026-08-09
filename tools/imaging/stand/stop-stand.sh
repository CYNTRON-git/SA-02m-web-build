#!/bin/bash
# SA-02m — stop flash stand services
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGING_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_DIR="$IMAGING_DIR/stand-data/run"

stop_pidfile() {
    local f="$1"
    [ -f "$f" ] || return 0
    local pid
    pid="$(cat "$f" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        echo "stop pid $pid ($f)"
        kill "$pid" 2>/dev/null || true
        sleep 0.3
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$f"
}

for f in "$PID_DIR"/*.pid; do
    [ -e "$f" ] || continue
    # dnsmasq may be root-owned
    if [[ "$(basename "$f")" == "dnsmasq.pid" ]]; then
        pid="$(cat "$f" 2>/dev/null || true)"
        if [ -n "${pid:-}" ]; then
            kill "$pid" 2>/dev/null || sudo kill "$pid" 2>/dev/null || true
            sleep 0.2
            kill -9 "$pid" 2>/dev/null || sudo kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$f"
        continue
    fi
    stop_pidfile "$f"
done

# Fallback by name
pkill -f "tools/imaging/stand/status-server.py" 2>/dev/null || true
pkill -f "tools/imaging/stand/fel-agent.py" 2>/dev/null || true
pkill -f "tools/imaging/stand/postflash-monitor.py" 2>/dev/null || true

echo "stand stopped"
