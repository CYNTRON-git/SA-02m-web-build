#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# fix-eth.sh  —  Static-IP interface recovery
# Called by: udev (ACTION==add/bind) or net-watchdog.service
#
# Логика: проверяет carrier → IP → ping-target.
# Если что-то не так — делает ifdown/ifup (читает /etc/network/interfaces.d/).
# Поддерживает eth0 и eth1 (если существует eth1.conf).
# ═══════════════════════════════════════════════════════════════════════════

LOG_FILE="/var/log/fix-eth.log"
LOG_MAX_BYTES=524288          # 512 KB — ротация лога
LOCK_DIR="/run/fix-eth"       # lock-файлы для предотвращения параллельных запусков
STATE_DIR="/run/fix-eth-state"
RECOVER_COOLDOWN=60           # секунд между восстановлениями одного интерфейса
PING_COUNT=2
PING_TIMEOUT=3

# Подгружаем пользовательские настройки (опционально).
# Там можно задать:
#   WATCHDOG_PING_ETH0=192.168.1.1     — хост для пинга на eth0 (переопределяет шлюз)
#   WATCHDOG_PING_ETH1=10.0.0.2        — хост для пинга на eth1 (когда шлюза нет)
#   WATCHDOG_PING_ETH0=skip            — не пинговать, считать рабочим по carrier+IP
#   RECOVER_COOLDOWN=90                — изменить cooldown
[ -f /etc/sa02m_network.conf ] && source /etc/sa02m_network.conf

# ── Logging ────────────────────────────────────────────────────────────────
log() {
    local level=$1; shift
    local msg="$*"
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${ts}] [${level}] ${msg}" >> "$LOG_FILE" 2>/dev/null || true
    # stdout → journald когда вызван из сервиса
    echo "[${ts}] [${level}] ${msg}"
}

rotate_log() {
    [ -f "$LOG_FILE" ] || return
    local size; size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if (( size > LOG_MAX_BYTES )); then
        tail -n 200 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
        log INFO "Лог ротирован (>${LOG_MAX_BYTES} байт)"
    fi
}

# ── Link / IP / ping-target detection (без ethtool) ───────────────────────
carrier_up() {
    # /sys/class/net/ethX/carrier = 1 если физический линк есть
    local iface=$1
    [ -f "/sys/class/net/${iface}/carrier" ] || return 1
    [ "$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null)" = "1" ]
}

has_ip() {
    local iface=$1
    ip -4 addr show dev "$iface" 2>/dev/null | grep -q 'inet '
}

debug_iface_state() {
    local iface=$1 addr route carrier oper
    carrier=$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo "?")
    oper=$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo "unknown")
    addr=$(ip -4 addr show dev "$iface" 2>/dev/null | awk '/inet /{print $2}' | tr '\n' ' ')
    route=$(ip route show dev "$iface" 2>/dev/null | tr '\n' ';')
    log INFO "$iface: debug carrier=${carrier} operstate=${oper} addr='${addr:-none}' routes='${route:-none}'"
}

iface_bootstrap_in_progress() {
    local iface=$1
    local unit state substate

    # Во время boot/restart networking.service и ifup@ethX могут поднимать
    # интерфейс параллельно. В этот момент нельзя запускать своё ifdown/ifup:
    # это приводит к гонке за ifupdown lock и откладывает восстановление.
    for unit in networking.service "ifup@${iface}.service"; do
        state=$(systemctl show -p ActiveState --value "$unit" 2>/dev/null)
        substate=$(systemctl show -p SubState --value "$unit" 2>/dev/null)
        case "${state}:${substate}" in
            activating:*|active:running|active:start*|deactivating:*)
                return 0
                ;;
        esac
    done

    return 1
}

get_gateway() {
    # Читаем шлюз из interfaces.d (не из routing table — она может быть пустой).
    # Возвращает пустую строку если шлюз не задан — это нормально для LAN-only интерфейсов.
    local iface=$1
    local conf="/etc/network/interfaces.d/${iface}.conf"
    [ -f "$conf" ] && awk '/^[[:space:]]*gateway/{print $2; exit}' "$conf"
}

state_file_for_iface() {
    local iface=$1
    echo "${STATE_DIR}/${iface}.last_ok_target"
}

remember_target_success() {
    local iface=$1
    local target=$2
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$target" > "$(state_file_for_iface "$iface")"
}

target_was_reachable_before() {
    local iface=$1
    local target=$2
    local state_file

    state_file=$(state_file_for_iface "$iface")
    [ -f "$state_file" ] || return 1
    [ "$(cat "$state_file" 2>/dev/null)" = "$target" ]
}

# Состояние «шлюз заведомо недоступен, но интерфейс считаем рабочим»
# пишем в файл, чтобы не повторять один и тот же INFO каждые 30 секунд.
# Сообщение появится только при изменении состояния (ok→fail или новый boot).
gateway_warned_state_file() {
    local iface=$1
    echo "${STATE_DIR}/${iface}.gw_unreachable_warned"
}

# check_connectivity iface
# Уровни проверки (применяется первый подходящий):
#   1. WATCHDOG_PING_<IFACE> задан явно → пинг этого хоста
#   2. Шлюз задан в interfaces.d → пинг шлюза
#   3. Ни шлюза, ни custom-пинга → пропускаем проверку (carrier+IP достаточно)
# Возвращает: 0 — OK/пропущено, 1 — хост недоступен
check_connectivity() {
    local iface=$1
    local iface_upper target_source custom_ping_var target warn_file
    iface_upper=$(echo "$iface" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
    target_source="gateway"

    # Явно заданная цель пинга для интерфейса (переопределяет шлюз)
    # Пример: WATCHDOG_PING_ETH1=10.0.0.2 в /etc/sa02m_network.conf
    custom_ping_var="WATCHDOG_PING_${iface_upper}"
    target="${!custom_ping_var:-}"

    if [ -n "$target" ]; then
        target_source="custom"
    else
        target=$(get_gateway "$iface")
    fi

    # "skip" — явный отказ от пинга для этого интерфейса
    [ "$target" = "skip" ] && return 0

    if [ -z "$target" ]; then
        # Нет ни шлюза, ни custom-цели — пропускаем ping.
        # Интерфейс считается здоровым при наличии carrier + IP.
        return 0
    fi

    warn_file=$(gateway_warned_state_file "$iface")

    # Если шлюз уже помечен как «никогда не отвечал» — используем короткий ping
    # (1 пакет, 1 сек) чтобы проверить, не появился ли он, без 6-секундной задержки.
    local _pc="$PING_COUNT" _pt="$PING_TIMEOUT"
    if [ "$target_source" = "gateway" ] \
       && ! target_was_reachable_before "$iface" "$target" \
       && [ -f "$warn_file" ]; then
        _pc=1; _pt=1
    fi

    if ping -c "$_pc" -W "$_pt" -q "$target" >/dev/null 2>&1; then
        remember_target_success "$iface" "$target"
        # Восстановились — лог только если ранее писали WARN.
        if [ -f "$warn_file" ]; then
            log INFO "$iface: связь со шлюзом ${target} восстановлена"
            rm -f "$warn_file"
        fi
        return 0
    fi

    # Если используем fallback на gateway и он ни разу не отвечал после запуска,
    # не считаем это аварией: на isolated-LAN шлюз может быть указан формально.
    # Лог пишем ОДИН раз, потом молчим.
    if [ "$target_source" = "gateway" ] && ! target_was_reachable_before "$iface" "$target"; then
        if [ ! -f "$warn_file" ]; then
            log INFO "$iface: шлюз ${target} недоступен с момента старта; интерфейс считаем рабочим по carrier+IP"
            mkdir -p "$STATE_DIR"
            : > "$warn_file"
        fi
        return 0
    fi

    # Шлюз ранее был доступен — пишем WARN один раз.
    if [ ! -f "$warn_file" ]; then
        log WARN "$iface: хост ${target} недоступен"
        mkdir -p "$STATE_DIR"
        : > "$warn_file"
    fi
    return 1
}

# ── Lock: предотвращает параллельный запуск для одного интерфейса ──────────
acquire_lock() {
    local iface=$1
    mkdir -p "$LOCK_DIR"
    local lock="${LOCK_DIR}/${iface}.lock"
    # Проверяем cooldown: если последнее восстановление было < RECOVER_COOLDOWN сек назад
    if [ -f "$lock" ]; then
        local last_ts; last_ts=$(cat "$lock" 2>/dev/null || echo 0)
        local now; now=$(date +%s)
        if (( now - last_ts < RECOVER_COOLDOWN )); then
            log INFO "$iface: cooldown активен, пропуск ($(( RECOVER_COOLDOWN - (now - last_ts) ))с осталось)"
            return 1
        fi
    fi
    date +%s > "$lock"
    return 0
}

release_lock() {
    local iface=$1
    rm -f "${LOCK_DIR}/${iface}.lock"
}

# ── Основное восстановление интерфейса ────────────────────────────────────
recover_iface() {
    local iface=$1
    local conf="/etc/network/interfaces.d/${iface}.conf"

    # Интерфейс не сконфигурирован — пропускаем
    [ -f "$conf" ] || return 0
    # Нет физического устройства
    [ -d "/sys/class/net/${iface}" ] || return 0

    # 1. Нет физического линка — попытаться согласовать PHY один раз,
    #    потом пропустить (кабель не воткнут — ifdown/ifup не поможет).
    if ! carrier_up "$iface"; then
        local oper; oper=$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo unknown)
        if [ "$oper" = "down" ] && ! [ -f "${LOCK_DIR}/${iface}.link_cycled" ]; then
            log INFO "$iface: carrier=0 operstate=down, forcing link cycle to renegotiate PHY"
            mkdir -p "$LOCK_DIR"
            touch "${LOCK_DIR}/${iface}.link_cycled"
            ip link set "$iface" down 2>/dev/null || true
            sleep 0.5
            ip link set "$iface" up   2>/dev/null || true
            sleep 0.5
            ethtool -r "$iface" 2>/dev/null || true
            sleep 2
        else
            log INFO "$iface: нет физического линка (carrier=0), пропуск"
            debug_iface_state "$iface"
        fi
        return 0
    fi

    # 2. Ранний link bounce при ПЕРВОМ появлении carrier — до bootstrap check!
    #    Выполняется ПРЕЖДЕ чем networking.service назначит IP, поэтому peer
    #    (коммутатор/Windows) видит carrier loss и сбрасывает ARP-кэш.
    #    Когда затем arp_notify=1 пошлёт grat-ARP при RTM_NEWADDR, peer его примет.
    local bounce_marker="${STATE_DIR}/${iface}.bounce_done"
    if [ ! -f "$bounce_marker" ]; then
        touch "$bounce_marker"
        ip link set "$iface" down 2>/dev/null || true
        sleep 0.1
        ip link set "$iface" up   2>/dev/null || true
        sleep 0.2
        # Восстанавливаем LED: PHY restart при down/up сбрасывает trigger в [none]
        local _led=/sys/class/leds/eth0_link
        if [ -d "$_led" ]; then
            local _phy
            _phy=$(tr ' ' '\n' < "$_led/trigger" 2>/dev/null \
                   | sed 's/^\[//; s/\]$//' \
                   | grep -E 'mdio.*:link$' | head -1)
            if [ -n "$_phy" ]; then
                echo "$_phy" > "$_led/trigger" 2>/dev/null || true
            else
                echo "netdev"  > "$_led/trigger"     2>/dev/null || true
                echo "$iface"  > "$_led/device_name" 2>/dev/null || true
                echo "1"       > "$_led/link"        2>/dev/null || true
            fi
        fi
    fi

    # 3. Если networking.service ещё назначает IP — не мешаем, bounce уже выполнен.
    if iface_bootstrap_in_progress "$iface"; then
        log INFO "$iface: ifupdown в процессе, IP-recovery пропущен (bounce выполнен)"
        return 0
    fi

    # 4. Нет IP-адреса — единственная причина для ifdown/ifup.
    #    Недоступность шлюза/интернета НЕ является поводом для сброса интерфейса.
    if ! has_ip "$iface"; then
        log WARN "$iface: нет IP-адреса"
    else
        # IP есть — только логируем состояние шлюза, восстановление не нужно.
        check_connectivity "$iface"
        # Gratuitous ARP burst: каждую секунду 15с — перекрывает окно когда
        # Windows выходит из ARP FAILED (~15-20с от device offline).
        # Один раз за загрузку (маркер в /run/).
        local grat_marker="${STATE_DIR}/${iface}.grat_arp_done"
        if [ ! -f "$grat_marker" ] && command -v python3 >/dev/null 2>&1 \
           && [ -f /usr/local/bin/sa02m-grat-arp.py ]; then
            touch "$grat_marker"
            python3 /usr/local/bin/sa02m-grat-arp.py "$iface" 2>/dev/null || true
            (
                for _rep in 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
                    sleep 1
                    python3 /usr/local/bin/sa02m-grat-arp.py "$iface" 2>/dev/null || true
                done
            ) &
            disown 2>/dev/null || true
        fi
        return 0
    fi

    # ── Восстановление IP ─────────────────────────────────────────────────
    acquire_lock "$iface" || return 0

    log INFO "$iface: начало процедуры восстановления"
    debug_iface_state "$iface"

    # ifdown/ifup — применяет статику из interfaces.d правильно
    if command -v ifdown >/dev/null 2>&1; then
        ifdown "$iface" 2>/dev/null || true
        sleep 1
        ifup  "$iface" 2>/dev/null
    else
        # fallback: ip link + ручная установка адреса из конфига
        ip link set "$iface" down 2>/dev/null || true
        sleep 1
        ip link set "$iface" up   2>/dev/null || true
        sleep 2
        local ip nm gw2
        ip=$(awk '/^[[:space:]]*address/{print $2; exit}' "$conf")
        nm=$(awk '/^[[:space:]]*netmask/{print $2; exit}' "$conf")
        gw2=$(awk '/^[[:space:]]*gateway/{print $2; exit}' "$conf")
        [ -n "$ip" ] && ip addr add "${ip}/${nm:-24}" dev "$iface" 2>/dev/null || true
        [ -n "$gw2" ] && ip route add default via "$gw2" dev "$iface" 2>/dev/null || true
    fi

    sleep 2

    if has_ip "$iface"; then
        log INFO "$iface: восстановлен, IP=$(ip -4 addr show dev "$iface" | awk '/inet /{print $2}')"
        debug_iface_state "$iface"
        release_lock "$iface"
        # Grat-ARP burst после восстановления IP (тот же механизм что и при has_ip)
        local grat_marker="${STATE_DIR}/${iface}.grat_arp_done"
        if [ ! -f "$grat_marker" ] && command -v python3 >/dev/null 2>&1 \
           && [ -f /usr/local/bin/sa02m-grat-arp.py ]; then
            touch "$grat_marker"
            python3 /usr/local/bin/sa02m-grat-arp.py "$iface" 2>/dev/null || true
            (
                for _rep in 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
                    sleep 1
                    python3 /usr/local/bin/sa02m-grat-arp.py "$iface" 2>/dev/null || true
                done
            ) &
            disown 2>/dev/null || true
        fi
    else
        log ERROR "$iface: восстановление не удалось"
        debug_iface_state "$iface"
        release_lock "$iface"
    fi
}

# ── Точка входа ────────────────────────────────────────────────────────────
rotate_log
mkdir -p "$LOCK_DIR"
mkdir -p "$STATE_DIR"

# Если передан конкретный интерфейс (из udev: KERNEL==ethX) — работаем только с ним
# Иначе — проверяем все сконфигурированные интерфейсы
if [ -n "$1" ]; then
    recover_iface "$1"
else
    for conf in /etc/network/interfaces.d/eth*.conf; do
        [ -f "$conf" ] || continue
        iface=$(basename "$conf" .conf)
        recover_iface "$iface"
    done
fi
