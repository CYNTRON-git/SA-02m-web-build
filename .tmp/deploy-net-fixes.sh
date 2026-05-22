set -e
echo "=== Деплой исправлений сети ==="

# 1. fix-eth.sh (убран ifdown/ifup при gateway unreachable)
echo "  [1] fix-eth.sh"
cat > /usr/local/bin/fix-eth.sh << 'FIXETH_EOF'
#!/bin/bash
LOG_FILE="/var/log/fix-eth.log"
LOG_MAX_BYTES=524288
LOCK_DIR="/run/fix-eth"
STATE_DIR="/run/fix-eth-state"
RECOVER_COOLDOWN=60
PING_COUNT=2
PING_TIMEOUT=3

[ -f /etc/sa02m_network.conf ] && source /etc/sa02m_network.conf

log() {
    local level=$1; shift
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${ts}] [${level}] $*" >> "$LOG_FILE" 2>/dev/null || true
    echo "[${ts}] [${level}] $*"
}

rotate_log() {
    [ -f "$LOG_FILE" ] || return
    local size; size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if (( size > LOG_MAX_BYTES )); then
        tail -n 200 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
    fi
}

carrier_up() {
    [ -f "/sys/class/net/${1}/carrier" ] || return 1
    [ "$(cat "/sys/class/net/${1}/carrier" 2>/dev/null)" = "1" ]
}

has_ip() {
    ip -4 addr show dev "$1" 2>/dev/null | grep -q 'inet '
}

debug_iface_state() {
    local iface=$1 carrier oper addr route
    carrier=$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo "?")
    oper=$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo "unknown")
    addr=$(ip -4 addr show dev "$iface" 2>/dev/null | awk '/inet /{print $2}' | tr '\n' ' ')
    route=$(ip route show dev "$iface" 2>/dev/null | tr '\n' ';')
    log INFO "$iface: debug carrier=${carrier} operstate=${oper} addr='${addr:-none}' routes='${route:-none}'"
}

iface_bootstrap_in_progress() {
    local unit state substate
    for unit in networking.service "ifup@${1}.service"; do
        state=$(systemctl show -p ActiveState --value "$unit" 2>/dev/null)
        substate=$(systemctl show -p SubState --value "$unit" 2>/dev/null)
        case "${state}:${substate}" in
            activating:*|active:running|active:start*|deactivating:*) return 0 ;;
        esac
    done
    return 1
}

get_gateway() {
    local conf="/etc/network/interfaces.d/${1}.conf"
    [ -f "$conf" ] && awk '/^[[:space:]]*gateway/{print $2; exit}' "$conf"
}

state_file_for_iface() { echo "${STATE_DIR}/${1}.last_ok_target"; }

remember_target_success() {
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$2" > "$(state_file_for_iface "$1")"
}

target_was_reachable_before() {
    local sf; sf=$(state_file_for_iface "$1")
    [ -f "$sf" ] && [ "$(cat "$sf" 2>/dev/null)" = "$2" ]
}

gateway_warned_state_file() { echo "${STATE_DIR}/${1}.gw_unreachable_warned"; }

check_connectivity() {
    local iface=$1 target target_source custom_ping_var iface_upper warn_file
    iface_upper=$(echo "$iface" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
    target_source="gateway"
    custom_ping_var="WATCHDOG_PING_${iface_upper}"
    target="${!custom_ping_var:-}"

    if [ -n "$target" ]; then
        target_source="custom"
    else
        target=$(get_gateway "$iface")
    fi

    [ "$target" = "skip" ] && return 0
    [ -z "$target" ] && return 0

    warn_file=$(gateway_warned_state_file "$iface")

    if ping -c "$PING_COUNT" -W "$PING_TIMEOUT" -q "$target" >/dev/null 2>&1; then
        remember_target_success "$iface" "$target"
        if [ -f "$warn_file" ]; then
            log INFO "$iface: связь со шлюзом ${target} восстановлена"
            rm -f "$warn_file"
        fi
        return 0
    fi

    if [ "$target_source" = "gateway" ] && ! target_was_reachable_before "$iface" "$target"; then
        if [ ! -f "$warn_file" ]; then
            log INFO "$iface: шлюз ${target} недоступен с момента старта; интерфейс считаем рабочим по carrier+IP"
            mkdir -p "$STATE_DIR"; : > "$warn_file"
        fi
        return 0
    fi

    if [ ! -f "$warn_file" ]; then
        log WARN "$iface: хост ${target} недоступен"
        mkdir -p "$STATE_DIR"; : > "$warn_file"
    fi
    return 1
}

acquire_lock() {
    mkdir -p "$LOCK_DIR"
    local lock="${LOCK_DIR}/${1}.lock"
    if [ -f "$lock" ]; then
        local last_ts now
        last_ts=$(cat "$lock" 2>/dev/null || echo 0)
        now=$(date +%s)
        if (( now - last_ts < RECOVER_COOLDOWN )); then
            log INFO "$1: cooldown активен ($(( RECOVER_COOLDOWN - (now - last_ts) ))с осталось)"
            return 1
        fi
    fi
    date +%s > "$lock"
}

release_lock() { rm -f "${LOCK_DIR}/${1}.lock"; }

recover_iface() {
    local iface=$1
    local conf="/etc/network/interfaces.d/${iface}.conf"
    [ -f "$conf" ] || return 0
    [ -d "/sys/class/net/${iface}" ] || return 0

    if iface_bootstrap_in_progress "$iface"; then
        log INFO "$iface: ifupdown в процессе, recovery пропущен"
        return 0
    fi

    # 1. Нет физического линка — ждать нечего
    if ! carrier_up "$iface"; then
        log INFO "$iface: нет физического линка (carrier=0), пропуск"
        return 0
    fi

    # 2. IP есть — только проверяем связность (лог), ifdown/ifup НЕ вызываем.
    #    ifdown/ifup при недоступном шлюзе не поможет, но роняет link на 4с → потеря пакетов.
    if has_ip "$iface"; then
        check_connectivity "$iface"
        return 0
    fi

    # 3. IP отсутствует → восстанавливаем
    log WARN "$iface: нет IP-адреса"
    acquire_lock "$iface" || return 0

    log INFO "$iface: начало восстановления"
    debug_iface_state "$iface"

    if command -v ifdown >/dev/null 2>&1; then
        ifdown "$iface" 2>/dev/null || true
        sleep 1
        ifup "$iface" 2>/dev/null
    else
        ip link set "$iface" down 2>/dev/null || true
        sleep 1
        ip link set "$iface" up 2>/dev/null || true
        sleep 2
        local ip nm gw
        ip=$(awk '/^[[:space:]]*address/{print $2; exit}' "$conf")
        nm=$(awk '/^[[:space:]]*netmask/{print $2; exit}' "$conf")
        gw=$(awk '/^[[:space:]]*gateway/{print $2; exit}' "$conf")
        [ -n "$ip" ] && ip addr add "${ip}/${nm:-24}" dev "$iface" 2>/dev/null || true
        [ -n "$gw" ] && ip route add default via "$gw" dev "$iface" 2>/dev/null || true
    fi

    sleep 2

    if has_ip "$iface"; then
        log INFO "$iface: восстановлен, IP=$(ip -4 addr show dev "$iface" | awk '/inet /{print $2}')"
        debug_iface_state "$iface"
    else
        log ERROR "$iface: восстановление не удалось"
        debug_iface_state "$iface"
    fi
    release_lock "$iface"
}

rotate_log
mkdir -p "$LOCK_DIR" "$STATE_DIR"

if [ -n "$1" ]; then
    recover_iface "$1"
else
    for conf in /etc/network/interfaces.d/eth*.conf; do
        [ -f "$conf" ] || continue
        iface=$(basename "$conf" .conf)
        recover_iface "$iface"
    done
fi
FIXETH_EOF
chmod 755 /usr/local/bin/fix-eth.sh

# 2. inet-failover.sh (новый)
echo "  [2] inet-failover.sh"
cat > /usr/local/bin/inet-failover.sh << 'FAILOVER_EOF'
#!/bin/bash
LOG_FILE="/var/log/fix-eth.log"
STATE_DIR="/run/sa02m-inet-failover"
INET_CHECK_TIMEOUT=4
INET_CHECK_URL="http://connectivitycheck.gstatic.com/generate_204"
INET_CHECK_URL2="http://1.1.1.1/"
MODEM_METRIC=100
DEAD_METRIC=300
SWITCH_COOLDOWN=30

[ -f /etc/sa02m_network.conf ] && source /etc/sa02m_network.conf

log() {
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${ts}] [FAILOVER] [$1] $2" >> "$LOG_FILE" 2>/dev/null || true
}

mkdir -p "$STATE_DIR"

get_iface_gateway() {
    ip route show default dev "$1" 2>/dev/null | awk '{print $3; exit}'
}

check_inet() {
    local iface=$1
    ip addr show dev "$iface" 2>/dev/null | grep -q 'inet ' || return 1
    ip link show dev "$iface" 2>/dev/null | grep -q 'state UP' || return 1
    local code
    code=$(curl -s --max-time "$INET_CHECK_TIMEOUT" --interface "$iface" \
           -o /dev/null -w "%{http_code}" "$INET_CHECK_URL" 2>/dev/null)
    [ "$code" = "204" ] && return 0
    code=$(curl -s --max-time "$INET_CHECK_TIMEOUT" --interface "$iface" \
           -o /dev/null -w "%{http_code}" "$INET_CHECK_URL2" 2>/dev/null)
    [ "$code" = "200" ] || [ "$code" = "301" ] || [ "$code" = "302" ]
}

get_current_metric() {
    local m
    m=$(ip route show default dev "$1" 2>/dev/null | awk '/metric/{for(i=1;i<=NF;i++) if($i=="metric"){print $(i+1); exit}}')
    echo "${m:-0}"
}

set_metric() {
    local iface=$1 metric=$2
    local cur; cur=$(get_current_metric "$iface")
    [ "$cur" = "$metric" ] && return 0

    local cooldown_f="${STATE_DIR}/${iface}.cooldown"
    if [ -f "$cooldown_f" ]; then
        local last now
        last=$(cat "$cooldown_f" 2>/dev/null || echo 0)
        now=$(date +%s)
        (( now - last < SWITCH_COOLDOWN )) && return 0
    fi

    local gw; gw=$(get_iface_gateway "$iface")
    [ -z "$gw" ] && return 0

    local onlink=""
    [ "$iface" = "eth0" ] && onlink="onlink"

    ip route del default dev "$iface" 2>/dev/null || true
    if [ "$metric" -eq 0 ]; then
        ip route add default via "$gw" dev "$iface" $onlink 2>/dev/null && \
            log INFO "$iface: metric 0 (primary)" || true
    else
        ip route add default via "$gw" dev "$iface" $onlink metric "$metric" 2>/dev/null && \
            log INFO "$iface: metric $metric" || true
    fi
    date +%s > "$cooldown_f"
}

get_modem_iface() {
    for iface in $(ip -o link show up | awk -F': ' '{print $2}' | grep -E '^(enx|usb[0-9])'); do
        ip addr show dev "$iface" 2>/dev/null | grep -q 'inet ' || continue
        ip route show default dev "$iface" 2>/dev/null | grep -q . && echo "$iface" && return
    done
}

run_failover() {
    local eth0_inet=0 eth1_inet=0
    local modem_iface; modem_iface=$(get_modem_iface)

    [ -d /sys/class/net/eth0 ] && check_inet eth0 && eth0_inet=1
    [ -d /sys/class/net/eth1 ] && check_inet eth1 && eth1_inet=1

    local state_file="${STATE_DIR}/state"
    local prev; prev=$(cat "$state_file" 2>/dev/null || echo "")
    local cur="eth0=${eth0_inet} eth1=${eth1_inet} modem=${modem_iface:-none}"
    if [ "$cur" != "$prev" ]; then
        log INFO "Интернет: $cur"
        echo "$cur" > "$state_file"
    fi

    # eth0: metric 0 при интернете, 300 без
    if [ -d /sys/class/net/eth0 ] && ip route show default dev eth0 2>/dev/null | grep -q .; then
        [ "$eth0_inet" -eq 1 ] && set_metric eth0 0 || set_metric eth0 $DEAD_METRIC
    fi

    # eth1: metric 20 при интернете, 300 без
    if [ -d /sys/class/net/eth1 ] && ip route show default dev eth1 2>/dev/null | grep -q .; then
        [ "$eth1_inet" -eq 1 ] && set_metric eth1 20 || set_metric eth1 $DEAD_METRIC
    fi
    # Модем всегда остаётся на metric 100 — резервный канал
}

run_failover
FAILOVER_EOF
chmod 755 /usr/local/bin/inet-failover.sh

# 3. net-watchdog.sh (добавлен вызов inet-failover.sh)
echo "  [3] net-watchdog.sh"
cat > /usr/local/bin/net-watchdog.sh << 'WDG_EOF'
#!/bin/bash
CHECK_INTERVAL=30
FIX_SCRIPT="/usr/local/bin/fix-eth.sh"
FAILOVER_SCRIPT="/usr/local/bin/inet-failover.sh"
LOG_FILE="/var/log/fix-eth.log"

[ -f /etc/sa02m_network.conf ] && source /etc/sa02m_network.conf
[ -n "${WATCHDOG_INTERVAL:-}" ] && CHECK_INTERVAL="$WATCHDOG_INTERVAL"

log() {
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${ts}] [WDG] $*" >> "$LOG_FILE" 2>/dev/null || true
    echo "[${ts}] [WDG] $*"
}

[ -x "$FIX_SCRIPT" ] || { echo "fix-eth.sh не найден" >&2; exit 1; }
log "Watchdog запущен (интервал ${CHECK_INTERVAL}с)"

while true; do
    for conf in /etc/network/interfaces.d/eth*.conf; do
        [ -f "$conf" ] || continue
        iface=$(basename "$conf" .conf)
        "$FIX_SCRIPT" "$iface"
    done
    [ -x "$FAILOVER_SCRIPT" ] && "$FAILOVER_SCRIPT"
    sleep "$CHECK_INTERVAL"
done
WDG_EOF
chmod 755 /usr/local/bin/net-watchdog.sh

echo "  [4] Перезапуск net-watchdog"
systemctl restart net-watchdog.service 2>/dev/null || true
sleep 3

echo
echo "=== Проверка: watchdog больше не дёргает link ==="
journalctl -u net-watchdog --no-pager --since "1 min ago" 2>/dev/null | tail -8

echo
echo "=== Первый тест failover: curl через eth0 ==="
curl -s --max-time 5 --interface eth0 -o /dev/null -w "eth0: HTTP %{http_code}\n" \
    http://connectivitycheck.gstatic.com/generate_204 2>/dev/null || echo "eth0: no internet"

echo "=== Первый тест failover: curl через модем ==="
IF=$(ip -o link show up | awk -F': ' '{print $2}' | grep -m1 '^enx' || true)
if [ -n "$IF" ]; then
    curl -s --max-time 5 --interface "$IF" -o /dev/null -w "${IF}: HTTP %{http_code}\n" \
        http://connectivitycheck.gstatic.com/generate_204 2>/dev/null || echo "${IF}: no internet"
else
    echo "  (модем не найден)"
fi

echo
echo "=== Маршруты ==="
ip route

echo
echo "=== dmesg: новых link-down событий нет? ==="
dmesg --since "30 sec ago" 2>/dev/null | grep -iE 'eth0.*link|link.*eth0' || echo "  (нет)"
