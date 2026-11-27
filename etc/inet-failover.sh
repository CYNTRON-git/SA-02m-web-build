#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# inet-failover.sh  —  Переключение default-маршрута по наличию интернета
#
# Логика приоритетов:
#   eth0  — приоритет 0   (primary ethernet)
#   eth1  — приоритет 20  (secondary ethernet)
#   modem — приоритет 100 (enx*/usb*/: USB-модем, резерв)
#
# Offline Ethernet priorities are 300 for eth0 and 301 for eth1 by default.
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

show_iface_defaults() {
    ip -4 -o -N route show table main default dev "$1" 2>/dev/null
}

default_route_key() {
    local iface=$1 line=$2 metric=0 protocol=3 prefsrc=- scope=0 onlink=0 start=3 i
    local -a words
    read -r -a words <<< "$line"
    [[ ${#words[@]} -ge 3 && "${words[0]} ${words[1]}" == "default via" \
        && "${words[2]}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    # iproute2 may omit the device from output already filtered by dev.
    if [ "${words[3]:-}" = dev ]; then
        [ "${words[4]:-}" = "$iface" ] || return 1
        start=5
    fi
    for ((i = start; i < ${#words[@]}; i++)); do
        case "${words[i]}" in
            metric) metric=${words[i+1]:-}; i=$((i + 1)) ;;
            proto) protocol=${words[i+1]:-}; i=$((i + 1)) ;;
            src)
                prefsrc=${words[i+1]:-}
                [[ "$prefsrc" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
                i=$((i + 1))
                ;;
            scope) scope=${words[i+1]:-}; i=$((i + 1)) ;;
            onlink) onlink=1 ;;
            linkdown) ;;
            *) return 1 ;;
        esac
    done
    # ip route omits the zero priority and the default boot protocol (3).
    [[ "$metric" =~ ^(0|[1-9][0-9]{0,9})$ && "$protocol" =~ ^[1-9][0-9]{0,2}$ \
        && "$scope" =~ ^(0|[1-9][0-9]{0,2})$ ]] || return 1
    printf '%s %s %s %s %s %s\n' "${words[2]}" "$metric" "$protocol" "$prefsrc" "$scope" "$onlink"
}

# ── Установить metric для default-маршрута интерфейса ────────────────────
set_metric() {
    local iface=$1
    local metric=$2
    local old_line old_key gw cur_metric cur_proto new_proto prefsrc scope onlink observed new_line=""
    local -a rows old_selector new_selector route_attrs=()
    [[ "$iface" == eth[01] && "$metric" =~ ^(0|[1-9][0-9]{0,9})$ ]] || return 1
    if ! old_line=$(show_iface_defaults "$iface"); then
        log WARN "$iface: не удалось прочитать IPv4-маршруты по умолчанию"
        return 1
    fi
    [ -n "$old_line" ] || return 0
    if [[ "$old_line" == *$'\n'* ]] || ! old_key=$(default_route_key "$iface" "$old_line"); then
        log WARN "$iface: неоднозначный IPv4-маршрут по умолчанию, метрика не меняется"
        return 1
    fi
    read -r gw cur_metric cur_proto prefsrc scope onlink <<< "$old_key"

    # Не делать лишних операций если metric уже правильная
    [ "$cur_metric" = "$metric" ] && return 0

    # Cooldown: не менять чаще SWITCH_COOLDOWN секунд
    local cooldown_file="${STATE_DIR}/${iface}.cooldown"
    if [ -f "$cooldown_file" ]; then
        local last_t; last_t=$(cat "$cooldown_file" 2>/dev/null || echo 0)
        local now; now=$(date +%s)
        if [[ "$last_t" =~ ^[0-9]{1,11}$ ]] && (( now - 10#$last_t < SWITCH_COOLDOWN )); then
            return 0
        fi
    fi

    # Linux treats metric 0 as a wildcard on deletion. When either metric is
    # zero, use a distinct protocol to keep both selectors unambiguous.
    new_proto=$cur_proto
    if [[ "$cur_metric" == 0 || "$metric" == 0 ]]; then
        if [ "$cur_proto" = 3 ]; then new_proto=4; else new_proto=3; fi
    fi
    old_selector=(default via "$gw" dev "$iface" table main proto "$cur_proto" metric "$cur_metric")
    new_selector=(default via "$gw" dev "$iface" table main proto "$new_proto" metric "$metric")
    [ "$prefsrc" = - ] || route_attrs+=(src "$prefsrc")
    [ "$scope" = 0 ] || route_attrs+=(scope "$scope")
    [ "$iface" != eth0 ] || onlink=1
    [ "$onlink" = 0 ] || route_attrs+=(onlink)

    if ! ip -4 route add "${new_selector[@]}" "${route_attrs[@]}" 2>/dev/null; then
        log WARN "$iface: не удалось добавить маршрут с метрикой $metric, удаление пропущено"
        return 1
    fi
    if ! observed=$(show_iface_defaults "$iface"); then
        log WARN "$iface: не удалось проверить добавленный маршрут, удаление пропущено"
        return 1
    fi
    mapfile -t rows <<< "$observed"
    if [ "${#rows[@]}" -eq 2 ]; then
        if [ "${rows[0]}" = "$old_line" ]; then
            new_line=${rows[1]}
        elif [ "${rows[1]}" = "$old_line" ]; then
            new_line=${rows[0]}
        fi
    fi
    if [ "$(default_route_key "$iface" "$new_line")" != "$gw $metric $new_proto $prefsrc $scope $onlink" ]; then
        log WARN "$iface: таблица маршрутов изменилась, удаление пропущено"
        return 1
    fi

    if ! ip -4 route del "${old_selector[@]}" 2>/dev/null; then
        log WARN "$iface: не удалось удалить прежний маршрут"
        # Only remove our addition while both observed routes are still present.
        if [ "$(show_iface_defaults "$iface")" = "$observed" ]; then
            ip -4 route del "${new_selector[@]}" 2>/dev/null \
                || log WARN "$iface: не удалось откатить добавление маршрута"
        fi
        return 1
    fi
    if [ "$(show_iface_defaults "$iface")" != "$new_line" ]; then
        log WARN "$iface: не удалось подтвердить итоговый маршрут"
        return 1
    fi

    date +%s > "$cooldown_file" || { log WARN "$iface: не удалось записать время переключения"; return 1; }
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

    local switch_result=0 secondary_dead_metric=$DEAD_METRIC
    # The kernel may reject another default at the same priority. Keep the
    # configured primary priority and use an adjacent valid value for eth1.
    if [[ "$DEAD_METRIC" =~ ^(0|[1-9][0-9]{0,9})$ ]] && (( DEAD_METRIC <= 4294967295 )); then
        secondary_dead_metric=$((DEAD_METRIC == 4294967295 ? DEAD_METRIC - 1 : DEAD_METRIC + 1))
    fi

    # Move eth1 first so a legacy route on DEAD_METRIC vacates that priority.
    if [ -d "/sys/class/net/eth1" ] && ip route show default dev eth1 2>/dev/null | grep -q .; then
        if [ "$eth1_inet" -eq 1 ]; then
            set_metric eth1 "${IFACE_METRIC[eth1]}" || switch_result=1
        else
            set_metric eth1 "$secondary_dead_metric" || switch_result=1
        fi
    fi

    if [ -d "/sys/class/net/eth0" ] && ip route show default dev eth0 2>/dev/null | grep -q .; then
        if [ "$eth0_inet" -eq 1 ]; then
            set_metric eth0 0 || switch_result=1
        else
            set_metric eth0 "$DEAD_METRIC" || switch_result=1
        fi
    fi

    # Модем: всегда оставляем на MODEM_METRIC (100) — не деградируем,
    # так как он единственный резервный канал.
    # Если интернета на модеме нет, но eth0/eth1 тоже нет — оставляем как есть.
    return "$switch_result"
}

# Support both variable names for backward compatibility
# DISABLE_INET_CHECK=1 or INET_FAILOVER_ENABLED=no
_skip_inet=0
[ "${DISABLE_INET_CHECK:-0}" = "1" ] && _skip_inet=1
[ "${INET_FAILOVER_ENABLED:-yes}" = "no" ] && _skip_inet=1
[ "$_skip_inet" -eq 0 ] && run_failover
