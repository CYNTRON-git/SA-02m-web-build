#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  Shared functions for install scripts
# ═══════════════════════════════════════════════════════════════════════════

LOG_FILE="${LOG_FILE:-/var/log/sa02m_install.log}"

log() {
    local level=${1:-INFO} msg=$2
    local ts; ts=$(date '+%Y-%m-%d %H:%M:%S')
    local color reset
    case "$level" in
        OK)   color='\033[0;32m' ;;
        WARN) color='\033[0;33m' ;;
        ERR)  color='\033[0;31m' ;;
        *)    color='\033[0;36m' ;;
    esac
    reset='\033[0m'
    echo -e "${color}[${ts}] [${level}] ${msg}${reset}"
    echo    "[${ts}] [${level}] ${msg}" >> "$LOG_FILE" 2>/dev/null || true
}

sa02m_hw_variant() {
    # Priority: env var → config file → default sa02m-1eth (no autodetect — A40i always has 2 MACs)
    case "${SA02M_HW_VARIANT:-}" in
        sa02m-1eth|sa02m-2eth) printf '%s\n' "${SA02M_HW_VARIANT}"; return 0 ;;
    esac
    local conf=/etc/sa02m_hw_variant.conf
    if [ -f "$conf" ]; then
        local v; v=$(awk -F= '/^SA02M_HW_VARIANT=/{gsub(/^[ \t"]+|[ \t"]+$/,"",$2);print $2;exit}' "$conf" 2>/dev/null)
        case "$v" in
            sa02m-1eth|sa02m-2eth) printf '%s\n' "$v"; return 0 ;;
        esac
    fi
    printf '%s\n' "sa02m-1eth"
}

sa02m_default_ip() {
    case "$(sa02m_hw_variant)" in
        sa02m-2eth) printf '%s\n' "192.168.0.136" ;;
        *)           printf '%s\n' "192.168.1.136" ;;
    esac
}

sa02m_default_gw() {
    case "$(sa02m_hw_variant)" in
        sa02m-2eth) printf '%s\n' "192.168.0.1" ;;
        *)           printf '%s\n' "192.168.1.1" ;;
    esac
}

# Return the first of the candidate interface names that actually exists in
# /sys/class/net, else the first candidate (so "absent" semantics still hold).
# Bridges the two interface-naming schemes a SA-02m board may present: the
# classic eth0/eth1 (SA-02m 5.10.35 kernel default) and the systemd-predictable
# end0/end1 (stock Armbian). Prefer whichever actually exists.
# Installer-side one home; the web copy (status.cgi:first_existing_iface) stays
# — different deploy context.
first_existing_iface() {
    local c
    for c in "$@"; do
        [ -d "/sys/class/net/$c" ] && { printf '%s' "$c"; return 0; }
    done
    printf '%s' "$1"
}

# ── Подсеть: проверка «шлюз внутри подсети интерфейса» ─────────────────────
# Порт валидаторов из www/network_config/cgi-bin/lib_web_validate.sh.
# Установщик-сторонний дом; веб-копия остаётся — другой контекст развёртывания
# (тот же приём, что и first_existing_iface выше). Обе стороны закрыты тестами:
# subnet-validate (веб) и iface-gw-repair (установщик).
# Чистая целочисленная арифметика bash (без bc); значения уходят только в
# $(( )) — никогда в shell-слово, путь или запись конфига.

# Print the 32-bit integer for a dotted-quad; non-zero exit on a malformed value
# (that exit is what makes the caller fail CLOSED). 10# forces base 10 so a
# leading-zero octet (010) is read decimally, matching the literal string in the
# config rather than as octal.
# Одно намеренное отличие от веб-копии: здесь октеты проверяются на 0-255 прямо
# внутри. В CGI это делает valid_ipv4 до вызова; у установщика такого вызывающего
# нет — значения приходят из конфига на диске, поэтому проверка встроена.
ipv4_to_int() {
    local ip="$1"
    [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    local o
    for o in "${BASH_REMATCH[@]:1:4}"; do
        [ "$((10#$o))" -le 255 ] || return 1
    done
    printf '%s' "$(( (10#${BASH_REMATCH[1]})*16777216 + (10#${BASH_REMATCH[2]})*65536 + (10#${BASH_REMATCH[3]})*256 + 10#${BASH_REMATCH[4]} ))"
}

# Return 0 iff <mask> is a non-zero contiguous netmask (a run of 1-bits then
# 0-bits). Rejects 0.0.0.0 and non-contiguous masks (e.g. 255.255.0.255).
netmask_is_contiguous() {
    local m inv
    m=$(ipv4_to_int "$1") || return 1
    [ "$m" -ne 0 ] || return 1
    inv=$(( (~m) & 0xFFFFFFFF ))
    (( (inv & (inv + 1)) == 0 ))
}

# Print the dotted-quad netmask for a CIDR prefix length; non-zero exit on an
# out-of-range or malformed prefix. Нужна установщику: современная станса
# ifupdown пишет `address 192.168.1.136/24` вообще без строки netmask, и без
# этого разбора совершенно исправный конфиг выглядел бы «непонятным».
# /0 намеренно даёт 0.0.0.0 — netmask_is_contiguous его отвергает, и станса
# уходит в fail-closed: нулевая маска на интерфейсе смысла не имеет.
sa02m_prefix_to_netmask() {
    local p=$1 m
    [[ "$p" =~ ^[0-9]{1,2}$ ]] || return 1
    p=$((10#$p))
    [ "$p" -le 32 ] || return 1
    if [ "$p" -eq 0 ]; then
        m=0
    else
        m=$(( (0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF ))
    fi
    printf '%d.%d.%d.%d' "$(( (m >> 24) & 255 ))" "$(( (m >> 16) & 255 ))" \
                         "$(( (m >> 8) & 255 ))"  "$(( m & 255 ))"
}

# Return 0 iff <ip> and <gw> share the subnet defined by <mask>. Any malformed
# operand returns non-zero — the caller treats that as "do not write".
same_ipv4_subnet() {
    local ip gw m
    ip=$(ipv4_to_int "$1") || return 1
    gw=$(ipv4_to_int "$2") || return 1
    m=$(ipv4_to_int "$3")  || return 1
    (( (ip & m) == (gw & m) ))
}

# ── resolvconf head (DNS через шлюз) ───────────────────────────────────────
# Единственный дом head-файла: и 01-system.sh (по маршруту, найденному в
# системе), и 02-network.sh (после восстановления маршрута в этом же запуске)
# зовут одну эту функцию.
SA02M_RESOLVCONF_HEAD=/etc/resolvconf/resolv.conf.d/head
SA02M_RESOLVCONF_HEAD_MARK='# SA-02m: DNS через шлюз'

# sa02m_write_resolvconf_head <gateway|empty>
# Непустой аргумент — пишем head с этим шлюзом первым nameserver'ом.
# Пустой — снимаем ТОЛЬКО свой head: запись, называющая несуществующий шлюз,
# хуже отсутствия head'а (это первый nameserver, и каждый запрос платит его
# таймаут). Чужой head не трогаем никогда.
sa02m_write_resolvconf_head() {
    local gw=${1:-}
    command -v resolvconf >/dev/null 2>&1 || [ -d /etc/resolvconf/resolv.conf.d ] || return 0
    mkdir -p "$(dirname "$SA02M_RESOLVCONF_HEAD")" 2>/dev/null || return 0
    if [ -n "$gw" ]; then
        cat > "$SA02M_RESOLVCONF_HEAD" <<EOF
$SA02M_RESOLVCONF_HEAD_MARK (ICS / раздача интернета с ПК)
nameserver ${gw}
EOF
        log OK "DNS через шлюз ${gw} (resolvconf head)"
        return 0
    fi
    if [ -f "$SA02M_RESOLVCONF_HEAD" ] \
       && grep -qF "$SA02M_RESOLVCONF_HEAD_MARK" "$SA02M_RESOLVCONF_HEAD"; then
        rm -f "$SA02M_RESOLVCONF_HEAD"
        log WARN "resolvconf head: маршрута по умолчанию нет — устаревший head со шлюзом удалён (иначе первый nameserver недоступен и каждый DNS-запрос ждёт таймаут)"
    fi
}

sa02m_board_model() {
    tr -d '\0' < /proc/device-tree/model 2>/dev/null \
        || awk -F: '/^Hardware/{gsub(/^[ \t]+/,"",$2);print $2;exit}' /proc/cpuinfo 2>/dev/null \
        || true
}

sa02m_serial_profile_from_file() {
    local conf=${1:-/etc/sa02m_serial_profile.conf}
    local value
    [ -f "$conf" ] || return 1
    value=$(awk -F= '/^SA02M_SERIAL_PROFILE=/{gsub(/^[ \t"]+|[ \t"]+$/,"",$2); print $2; exit}' "$conf" 2>/dev/null)
    case "$value" in
        sa02m-1eth|sa02m-2eth)
            printf '%s\n' "$value"
            return 0
            ;;
    esac
    return 1
}

sa02m_serial_profile() {
    case "${SA02M_SERIAL_PROFILE:-}" in
        sa02m-1eth|sa02m-2eth)
            printf '%s\n' "${SA02M_SERIAL_PROFILE}"
            return 0
            ;;
    esac

    if sa02m_serial_profile_from_file "/etc/sa02m_serial_profile.conf"; then
        return 0
    fi

    sa02m_hw_variant
}

sa02m_serial_targets() {
    local profile=${1:-$(sa02m_serial_profile)}
    case "$profile" in
        sa02m-2eth)
            printf '%s\n' "ttyS3 ttyS4 ttyS5 ttyS7"
            ;;
        *)
            printf '%s\n' "ttyS0 ttyS3 ttyS4 ttyS5 ttyS7"
            ;;
    esac
}

write_sa02m_serial_map_conf() {
    local dst=${1:-/etc/sa02m_serial_map.conf}
    local profile model tmp idx com_idx
    local -a targets

    profile=$(sa02m_serial_profile)
    model=$(sa02m_board_model)
    read -r -a targets <<< "$(sa02m_serial_targets "$profile")"
    tmp="${dst}.tmp.$$"

    {
        echo "# Generated by SA-02m installer"
        echo "SA02M_BOARD_MODEL=\"${model}\""
        echo "SA02M_SERIAL_PROFILE=${profile}"
        echo "SA02M_SERIAL_COUNT=${#targets[@]}"
        idx=0
        for tty in "${targets[@]}"; do
            com_idx=$(( idx + 1 ))
            echo "SA02M_TTY_${idx}=${tty}"
            echo "SA02M_RS485_${idx}=RS-485-${idx}"
            echo "SA02M_COM_${com_idx}=COM${com_idx}"
            idx=$(( idx + 1 ))
        done
    } > "$tmp"

    install -m 644 "$tmp" "$dst"
    rm -f "$tmp"
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log ERR "Запустите скрипт от root: sudo $0"
        exit 1
    fi
}

# systemctl с таймаутом — защита от зависания при сбое D-Bus (Armbian, нагрузка MPLC и т.д.).
SA02M_SYSTEMCTL_TIMEOUT_SEC="${SA02M_SYSTEMCTL_TIMEOUT_SEC:-35}"

sa02m_systemctl() {
    if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
        systemctl --root="${SA02M_ROOTFS_ROOT:-/}" "$@" >> "$LOG_FILE" 2>&1 || true
        return 0
    fi
    local sec=${SA02M_SYSTEMCTL_TIMEOUT_SEC:-35}
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" systemctl "$@"
    else
        systemctl "$@"
    fi
}

pkg_install() {
    local pkgs=("$@")
    local missing=()
    for p in "${pkgs[@]}"; do
        dpkg -l "$p" &>/dev/null || missing+=("$p")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        log INFO "Установка пакетов: ${missing[*]}"
        apt-get install -y "${missing[@]}" >> "$LOG_FILE" 2>&1
    fi
}

svc_enable() {
    sa02m_systemctl enable "$1" >> "$LOG_FILE" 2>&1 || true
    if [ -z "${SA02M_ROOTFS_BUILD:-}" ]; then
        sa02m_systemctl start "$1" >> "$LOG_FILE" 2>&1 || true
    fi
}

svc_restart() {
    sa02m_systemctl restart "$1" >> "$LOG_FILE" 2>&1 || true
}
