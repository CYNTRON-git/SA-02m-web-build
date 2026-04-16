#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# fix-eth.sh  —  Static-IP interface recovery
# Called by: udev (ACTION==add/bind) or net-watchdog.service
#
# Логика: проверяет carrier → IP → gateway ping.
# Если что-то не так — делает ifdown/ifup (читает /etc/network/interfaces.d/).
# Поддерживает eth0 и eth1 (если существует eth1.conf).
# ═══════════════════════════════════════════════════════════════════════════

LOG_FILE="/var/log/fix-eth.log"
LOG_MAX_BYTES=524288          # 512 KB — ротация лога
LOCK_DIR="/run/fix-eth"       # lock-файлы для предотвращения параллельных запусков
RECOVER_COOLDOWN=60           # секунд между восстановлениями одного интерфейса
PING_COUNT=2
PING_TIMEOUT=3

# Подгружаем пользовательские настройки (опционально).
# Там можно задать:
#   WATCHDOG_PING_ETH0=192.168.1.1     — хост для пинга на eth0 (переопределяет шлюз)
#   WATCHDOG_PING_ETH1=10.0.0.2        — хост для пинга на eth1 (когда шлюза нет)
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

# ── Link / IP / gateway detection (без ethtool) ────────────────────────────
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

get_gateway() {
    # Читаем шлюз из interfaces.d (не из routing table — она может быть пустой).
    # Возвращает пустую строку если шлюз не задан — это нормально для LAN-only интерфейсов.
    local iface=$1
    local conf="/etc/network/interfaces.d/${iface}.conf"
    [ -f "$conf" ] && awk '/^[[:space:]]*gateway/{print $2; exit}' "$conf"
}


# check_connectivity iface
# Уровни проверки (применяется первый подходящий):
#   1. WATCHDOG_PING_<IFACE> задан явно → пинг этого хоста
#   2. Шлюз задан в interfaces.d → пинг шлюза
#   3. Ни шлюза, ни custom-пинга → пропускаем проверку (carrier+IP достаточно)
# Возвращает: 0 — OK/пропущено, 1 — хост недоступен
check_connectivity() {
    local iface=$1
    local iface_upper; iface_upper=$(echo "$iface" | tr '[:lower:]' '[:upper:]' | tr '-' '_')

    # Явно заданная цель пинга для интерфейса (переопределяет шлюз)
    # Пример: WATCHDOG_PING_ETH1=10.0.0.2 в /etc/sa02m_network.conf
    local custom_ping_var="WATCHDOG_PING_${iface_upper}"
    local target="${!custom_ping_var:-}"

    if [ -z "$target" ]; then
        target=$(get_gateway "$iface")
    fi

    # "skip" — явный отказ от пинга для этого интерфейса
    [ "$target" = "skip" ] && return 0

    if [ -z "$target" ]; then
        # Нет ни шлюза, ни custom-цели — пропускаем ping.
        # Интерфейс считается здоровым при наличии carrier + IP.
        return 0
    fi

    if ping -c "$PING_COUNT" -W "$PING_TIMEOUT" -q "$target" >/dev/null 2>&1; then
        return 0
    else
        log WARN "$iface: хост ${target} недоступен"
        return 1
    fi
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

    local need_recover=0

    # 1. Нет физического линка — ждать нечего, восстановить нельзя
    if ! carrier_up "$iface"; then
        log INFO "$iface: нет физического линка (carrier=0), пропуск"
        return 0
    fi

    # 2. Нет IP-адреса
    if ! has_ip "$iface"; then
        log WARN "$iface: нет IP-адреса"
        need_recover=1
    fi

    # 3. Проверка связности (шлюз или custom-хост) — только если IP уже есть.
    #    Если шлюз не задан и WATCHDOG_PING_<IFACE> не задан — шаг пропускается.
    if [ "$need_recover" -eq 0 ] && ! check_connectivity "$iface"; then
        need_recover=1
    fi

    [ "$need_recover" -eq 0 ] && return 0

    # ── Восстановление ────────────────────────────────────────────────────
    acquire_lock "$iface" || return 0

    log INFO "$iface: начало процедуры восстановления"

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
        release_lock "$iface"
    else
        log ERROR "$iface: восстановление не удалось"
        # Сохраняем lock — следующий cooldown не пройдёт сразу
    fi
}

# ── Точка входа ────────────────────────────────────────────────────────────
rotate_log
mkdir -p "$LOCK_DIR"

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
