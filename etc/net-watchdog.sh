#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# net-watchdog.sh  —  Постоянный мониторинг сетевых интерфейсов
# Запускается как systemd-сервис (Type=simple, Restart=always).
# Каждые CHECK_INTERVAL секунд вызывает fix-eth.sh для каждого
# сконфигурированного eth-интерфейса.
# ═══════════════════════════════════════════════════════════════════════════

CHECK_INTERVAL=30       # секунд между проверками
FIX_SCRIPT="/usr/local/bin/fix-eth.sh"
LOG_FILE="/var/log/fix-eth.log"

log() {
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${ts}] [WDG] $*" >> "$LOG_FILE" 2>/dev/null || true
    echo "[${ts}] [WDG] $*"
}

[ -x "$FIX_SCRIPT" ] || { echo "fix-eth.sh не найден: $FIX_SCRIPT" >&2; exit 1; }

log "Watchdog запущен (интервал ${CHECK_INTERVAL}с)"

while true; do
    for conf in /etc/network/interfaces.d/eth*.conf; do
        [ -f "$conf" ] || continue
        iface=$(basename "$conf" .conf)
        "$FIX_SCRIPT" "$iface"
    done
    sleep "$CHECK_INTERVAL"
done
