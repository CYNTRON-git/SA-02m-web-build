#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# inet-failover.sh  —  Переключение default-маршрута по наличию интернета
#
# Логика приоритетов:
#   eth0  — приоритет 10  (primary ethernet)
#   eth1  — приоритет 20  (secondary ethernet)
#   modem — приоритет 100 (enx*/usb*/: USB-модем, резерв)
#
# Если интернет через интерфейс недоступен → metric 300 (фактически отключён).
# Если интернет снова появляется → штатная metric восстанавливается.
#
# Вызывается из net-watchdog.sh каждые CHECK_INTERVAL секунд.
# ═══════════════════════════════════════════════════════════════════════════

LOG_FILE="/var/log/fix-eth.log"
STATE_DIR="/run/sa02m-inet-failover"
# Таймаут curl для проверки интернета (секунды)
INET_CHECK_TIMEOUT=4
# URL для проверки — возвращает HTTP 204 при наличии интернета
INET_CHECK_URL="http://connectivitycheck.gstatic.com/generate_204"
# Fallback URL если первый недоступен
INET_CHECK_URL2="http://1.1.1.1/"
# DISABLE_INET_CHECK=1 — отключить интернет-проверку; метрики не трогать.
# Полезно при LAN-only конфигурации без интернета.
# Задаётся в /etc/sa02m_network.conf

# Метрики
declare -A IFACE_METRIC=( [eth0]=10 [eth1]=20 )
MODEM_METRIC=100
DEAD_METRIC=300

# Cooldown: не переключаться чаще раз в N секунд (предотвращает флаппинг)
SWITCH_COOLDOWN=30

[ -f /etc/sa02m_network.conf ] && source /etc/sa02m_network.conf

log() {
    local level=$1; shift
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${ts}] [FAILOVER] [${level}] $*" >> "$LOG_FILE" 2>/dev/null || true
}

mkdir -p "$STATE_DIR"

# ── Определяем шлюз интерфейса ────────────────────────────────────────────
get_iface_gateway() {
    local iface=$1
    # Читаем из текущей таблицы маршрутов
    ip route show default dev "$iface" 2>/dev/null | awk '{print $3; exit}'
}

# ── Проверка интернета через конкретный интерфейс ─────────────────────────
check_inet() {
    local iface=$1
    # Интерфейс должен быть UP с IP
    ip addr show dev "$iface" 2>/dev/null | grep -q 'inet ' || return 1
    ip link show dev "$iface" 2>/dev/null | grep -q 'state UP' || return 1

    local code
    code=$(curl -s --max-time "$INET_CHECK_TIMEOUT" \
                --interface "$iface" \
                -o /dev/null -w "%{http_code}" \
                "$INET_CHECK_URL" 2>/dev/null)
    [ "$code" = "204" ] && return 0

    # Fallback проверка
    code=$(curl -s --max-time "$INET_CHECK_TIMEOUT" \
                --interface "$iface" \
                -o /dev/null -w "%{http_code}" \
                "$INET_CHECK_URL2" 2>/dev/null)
    [ "$code" = "200" ] || [ "$code" = "301" ] || [ "$code" = "302" ]
}

# ── Получить текущую metric default-маршрута интерфейса ───────────────────
get_current_metric() {
    local iface=$1
    local m
    m=$(ip route show default dev "$iface" 2>/dev/null | awk '/metric/{
        for(i=1;i<=NF;i++) if($i=="metric") {print $(i+1); exit}
    }')
    # Если metric не задана явно — это metric 0
    echo "${m:-0}"
}

# ── Установить metric для default-маршрута интерфейса ────────────────────
set_metric() {
    local iface=$1
    local metric=$2
    local cur_metric
    cur_metric=$(get_current_metric "$iface")

    # Не делать лишних операций если metric уже правильная
    [ "$cur_metric" = "$metric" ] && return 0

    # Cooldown: не менять чаще SWITCH_COOLDOWN секунд
    local cooldown_file="${STATE_DIR}/${iface}.cooldown"
    if [ -f "$cooldown_file" ]; then
        local last_t; last_t=$(cat "$cooldown_file" 2>/dev/null || echo 0)
        local now; now=$(date +%s)
        if (( now - last_t < SWITCH_COOLDOWN )); then
            return 0
        fi
    fi

    local gw; gw=$(get_iface_gateway "$iface")
    [ -z "$gw" ] && return 0

    # Определяем нужен ли onlink (eth0 всегда с onlink из-за особенностей PHY)
    local onlink_flag=""
    [ "$iface" = "eth0" ] && onlink_flag="onlink"

    # Удаляем старый default и добавляем с новой метрикой
    ip route del default dev "$iface" 2>/dev/null || true
    if [ "$metric" -eq 0 ]; then
        ip route add default via "$gw" dev "$iface" $onlink_flag 2>/dev/null || true
    else
        ip route add default via "$gw" dev "$iface" $onlink_flag metric "$metric" 2>/dev/null || true
    fi

    date +%s > "$cooldown_file"
    return 0
}

# ── Определяем имя модемного интерфейса (enx*/usb*/wwan* с IP и шлюзом) ──
# wwan0/wwan1 — интерфейс QMI/MBIM модемов (qmi_wwan / cdc_mbim); IP берётся
# через qmi-network / mbim-network. enx*/usb* — CDC-ECM/RNDIS/NCM (DHCP).
# ppp0 — устаревшие ttyUSB-модемы через pppd (metric ставит /etc/ppp/ip-up.d).
get_modem_iface() {
    for iface in $(ip -o link show up | awk -F': ' '{print $2}' | grep -E '^(enx|usb[0-9]|wwan[0-9])'); do
        ip addr show dev "$iface" 2>/dev/null | grep -q 'inet ' || continue
        ip route show default dev "$iface" 2>/dev/null | grep -q . && echo "$iface" && return
    done
}

# ── Основная логика ────────────────────────────────────────────────────────
run_failover() {
    # INET_FAILOVER_ENABLED=no in /etc/sa02m_network.conf disables metric switching.
    # Use this on LAN-only devices where there is no internet — prevents degrading
    # the default route to metric 300 just because connectivitycheck.gstatic.com
    # is unreachable, which would break outbound routing entirely.
    [ "${INET_FAILOVER_ENABLED:-yes}" = "no" ] && return 0

    local eth0_inet=0 eth1_inet=0 modem_inet=0
    local modem_iface; modem_iface=$(get_modem_iface)

    # Проверяем интернет только для существующих интерфейсов
    if [ -d "/sys/class/net/eth0" ]; then
        check_inet eth0 && eth0_inet=1
    fi
    if [ -d "/sys/class/net/eth1" ]; then
        check_inet eth1 && eth1_inet=1
    fi
    if [ -n "$modem_iface" ]; then
        check_inet "$modem_iface" && modem_inet=1
    fi

    local state_file="${STATE_DIR}/state"
    local prev_state; prev_state=$(cat "$state_file" 2>/dev/null || echo "")
    local cur_state="eth0=${eth0_inet} eth1=${eth1_inet} modem=${modem_inet}(${modem_iface:-none})"

    # Логируем только при изменении состояния
    if [ "$cur_state" != "$prev_state" ]; then
        log INFO "Состояние интернета: eth0=${eth0_inet} eth1=${eth1_inet} modem=${modem_inet}(${modem_iface:-none})"
        echo "$cur_state" > "$state_file"
    fi

    # ── Устанавливаем метрики ──────────────────────────────────────────────
    # eth0
    if [ -d "/sys/class/net/eth0" ] && ip route show default dev eth0 2>/dev/null | grep -q .; then
        if [ "$eth0_inet" -eq 1 ]; then
            set_metric eth0 0
        else
            set_metric eth0 $DEAD_METRIC
        fi
    fi

    # eth1
    if [ -d "/sys/class/net/eth1" ] && ip route show default dev eth1 2>/dev/null | grep -q .; then
        if [ "$eth1_inet" -eq 1 ]; then
            set_metric eth1 ${IFACE_METRIC[eth1]}
        else
            set_metric eth1 $DEAD_METRIC
        fi
    fi

    # Модем: всегда оставляем на MODEM_METRIC (100) — не деградируем,
    # так как он единственный резервный канал.
    # Если интернета на модеме нет, но eth0/eth1 тоже нет — оставляем как есть.
}

# Support both variable names for backward compatibility
# DISABLE_INET_CHECK=1 or INET_FAILOVER_ENABLED=no
_skip_inet=0
[ "${DISABLE_INET_CHECK:-0}" = "1" ] && _skip_inet=1
[ "${INET_FAILOVER_ENABLED:-yes}" = "no" ] && _skip_inet=1
[ "$_skip_inet" -eq 0 ] && run_failover
