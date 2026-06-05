#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# net-watchdog.sh  —  Постоянный мониторинг сетевых интерфейсов
# Запускается как systemd-сервис (Type=simple, Restart=always).
# Каждые CHECK_INTERVAL секунд вызывает fix-eth.sh для каждого
# сконфигурированного eth-интерфейса.
# ═══════════════════════════════════════════════════════════════════════════

CHECK_INTERVAL=30       # секунд между проверками
FIX_SCRIPT="/usr/local/bin/fix-eth.sh"
FAILOVER_SCRIPT="/usr/local/bin/inet-failover.sh"
LOG_FILE="/var/log/fix-eth.log"

[ -f /etc/sa02m_network.conf ] && source /etc/sa02m_network.conf

if [ -n "${WATCHDOG_INTERVAL:-}" ]; then
    CHECK_INTERVAL="$WATCHDOG_INTERVAL"
fi

log() {
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${ts}] [WDG] $*" >> "$LOG_FILE" 2>/dev/null || true
    echo "[${ts}] [WDG] $*"
}

[ -x "$FIX_SCRIPT" ] || { echo "fix-eth.sh не найден: $FIX_SCRIPT" >&2; exit 1; }

STARTUP_DELAY="${STARTUP_DELAY:-60}"

log "Watchdog запущен (интервал ${CHECK_INTERVAL}с, начальная задержка ${STARTUP_DELAY}с)"
sleep "$STARTUP_DELAY"

while true; do
    # 1. Восстановление интерфейсов (только при потере IP)
    for conf in /etc/network/interfaces.d/end*.conf; do
        [ -f "$conf" ] || continue
        iface=$(basename "$conf" .conf)
        "$FIX_SCRIPT" "$iface"
    done

    # 2. Переключение маршрутов по наличию интернета (end0/end1 > модем)
    if [ -x "$FAILOVER_SCRIPT" ]; then
        "$FAILOVER_SCRIPT"
    fi

    # 3. eth0_link LED — udev change на carrier=0 ненадёжен, держим в sync здесь.
    if [ -x /usr/local/bin/sa02m-eth0-led.sh ]; then
        /usr/local/bin/sa02m-eth0-led.sh || true
    fi

    sleep "$CHECK_INTERVAL"
done
