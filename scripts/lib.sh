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

# ── Bounded apt + offline fast-path ────────────────────────────────────────
# The installer must fail FAST when offline, not grind for minutes against
# unreachable mirrors (a full run once spent ~6 min on `apt-get update` against
# 6 dead mirrors). Two single-home pieces used by every module:
#   1) bounded apt options — short per-mirror timeout, no retry storm, IPv4
#      (an A40i on a dead-IPv6 LAN otherwise waits out the v6 timeout per
#      request), all wrapped in `timeout SA02M_APT_TIMEOUT_SEC`;
#   2) a ONE-SHOT online probe whose verdict is cached for the whole run — when
#      offline, every apt op is skipped and the caller falls through to its
#      [ -f ]/pip/vendored guards.
SA02M_APT_TIMEOUT_SEC="${SA02M_APT_TIMEOUT_SEC:-90}"
SA02M_ONLINE_CACHE=""

# Bounded apt options as a word-split string (intentionally unquoted at the call
# site). 6 s per-mirror connect/read timeout; a single retry, not the default
# storm; force IPv4.
sa02m_apt_opts() {
    printf '%s' "-o Acquire::Retries=1 -o Acquire::http::Timeout=6 -o Acquire::https::Timeout=6 -o Acquire::ForceIPv4=true"
}

# Return 0 iff the box looks online. Probes once (<=3 s) and caches the verdict.
# Fast DNS+resolve of the Ubuntu mirror host; falls back to a TCP:53 probe of
# the default gateway when DNS itself is the thing that is down. No `timeout`
# binary → assume online and rely on the bounded apt opts to cap the cost.
sa02m_online() {
    case "$SA02M_ONLINE_CACHE" in
        yes) return 0 ;;
        no)  return 1 ;;
    esac
    if ! command -v timeout >/dev/null 2>&1; then
        SA02M_ONLINE_CACHE=yes; return 0
    fi
    if timeout 3 getent hosts ports.ubuntu.com >/dev/null 2>&1; then
        SA02M_ONLINE_CACHE=yes; return 0
    fi
    local gw
    gw=$(ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="via"){print $(i+1); exit}}') || true
    if [ -n "${gw:-}" ] && timeout 3 bash -c "exec 3<>/dev/tcp/${gw}/53" 2>/dev/null; then
        SA02M_ONLINE_CACHE=yes; return 0
    fi
    SA02M_ONLINE_CACHE=no
    return 1
}

# `apt-get update` with bounded opts; skipped entirely when offline. Never
# aborts the install — a failed update just means the caller works from the
# already-cached package index.
sa02m_apt_update() {
    if ! sa02m_online; then
        log WARN "Нет сети — пропускаю apt-get update (offline fast-path)"
        return 0
    fi
    local sec=${SA02M_APT_TIMEOUT_SEC:-90}
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" apt-get $(sa02m_apt_opts) update -qq >> "$LOG_FILE" 2>&1 \
            || log WARN "apt-get update: ошибка/таймаут — продолжаю (offline/медленные зеркала)"
    else
        apt-get $(sa02m_apt_opts) update -qq >> "$LOG_FILE" 2>&1 \
            || log WARN "apt-get update: ошибка — продолжаю"
    fi
    return 0
}

# sa02m_pkg_install_tier required|optional <pkg>...
# Install only the still-missing packages, with bounded opts. Offline ⇒ skip and
# WARN (the caller's downstream guards cover the degraded case). A failed
# OPTIONAL package → per-pkg WARN + continue; a failed REQUIRED package → a
# prominent WARN naming the consequence. Never aborts the install (safe even
# where the caller runs `set -e`).
sa02m_pkg_install_tier() {
    local tier=$1; shift
    local missing=() p
    for p in "$@"; do
        dpkg -l "$p" 2>/dev/null | grep -q "^ii" || missing+=("$p")
    done
    [ ${#missing[@]} -gt 0 ] || return 0
    if ! sa02m_online; then
        if [ "$tier" = required ]; then
            log WARN "ОФФЛАЙН: обязательные пакеты не установлены: ${missing[*]} — веб-интерфейс может не подняться (проявит 03-webserver)"
        else
            log WARN "ОФФЛАЙН: опциональные пакеты пропущены: ${missing[*]} (функции деградируют; продолжаю)"
        fi
        return 0
    fi
    log INFO "Установка пакетов ($tier): ${missing[*]}"
    local sec=${SA02M_APT_TIMEOUT_SEC:-90}
    if command -v timeout >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive timeout "$sec" apt-get $(sa02m_apt_opts) install -y "${missing[@]}" >> "$LOG_FILE" 2>&1 \
            && { log OK "Установлены: ${missing[*]}"; return 0; }
    else
        DEBIAN_FRONTEND=noninteractive apt-get $(sa02m_apt_opts) install -y "${missing[@]}" >> "$LOG_FILE" 2>&1 \
            && { log OK "Установлены: ${missing[*]}"; return 0; }
    fi
    local still=() q
    for q in "${missing[@]}"; do
        dpkg -l "$q" 2>/dev/null | grep -q "^ii" || still+=("$q")
    done
    if [ ${#still[@]} -gt 0 ]; then
        if [ "$tier" = required ]; then
            log WARN "ОБЯЗАТЕЛЬНЫЕ пакеты не установились: ${still[*]} — веб-интерфейс может не подняться"
        else
            for q in "${still[@]}"; do
                log WARN "опциональный пакет не установлен: $q (функция деградирует)"
            done
        fi
    fi
    return 0
}

# Back-compat thin wrapper: bounded + offline-aware, non-aborting. Existing
# callers pass a flat package list (mosquitto, openssl, …); they degrade
# gracefully like the optional tier.
pkg_install() {
    sa02m_pkg_install_tier optional "$@"
}

# ── sudoers drop-in install: single home ────────────────────────────────────
# One place for the "install a /etc/sudoers.d drop-in safely" recipe that was
# copy-pasted (unevenly — some sites skipped the CRLF strip or the visudo check)
# across the 03/04/05/06 installers: enforce 0440 root:root, strip CRLF (a
# Windows-checkout \r is a visudo syntax error that breaks sudo globally —
# BUGLOG sa02m-flasher), validate with `visudo -cf`. A rejected file is
# WARN-and-kept — never auto-rm (could widen a different failure); the aggregate
# `visudo -c` at the end of install.sh is the fail-closed catch. Never aborts.

# Harden a sudoers drop-in already written to <dst> (for the installers that
# generate their drop-in content inline via a heredoc).
sa02m_harden_sudoers() {
    local dst=$1
    chown root:root "$dst" 2>/dev/null || true
    chmod 0440 "$dst" 2>/dev/null || true
    sed -i 's/\r$//' "$dst" 2>/dev/null || true
    if visudo -cf "$dst" >/dev/null 2>&1; then
        log OK "sudoers $(basename "$dst") OK"
    else
        log WARN "visudo отклонил $dst — файл оставлен как есть (см. итоговый visudo -c)"
    fi
    return 0
}

# Install a sudoers drop-in from repo <src> to <dst>, then harden it.
sa02m_install_sudoers() {
    local src=$1 dst=$2
    if [ ! -f "$src" ]; then
        log WARN "sudoers-источник $src не найден — пропуск"
        return 0
    fi
    install -m 0440 -o root -g root "$src" "$dst" 2>>"$LOG_FILE" || {
        log WARN "не удалось установить sudoers $dst из $src"
        return 0
    }
    sa02m_harden_sudoers "$dst"
    return 0
}

# ── Service state capture / restore ─────────────────────────────────────────
# Record a service's enabled/active state BEFORE the installer touches it, then
# restore EXACTLY that state after the new code lands — so a full re-install of
# a configured device comes back with the SAME services running it had before,
# on fresh code. Port-lease-safe: the installer does orderly restarts only, it
# never grabs the flasher lease (sa02m-domain.md ## Subsystems). Direction
# guarantees: never ENABLE a service that was disabled (no autostart widening),
# never leave a service the operator was RUNNING stopped (the MQTT-bridge
# stale-code failure this exists to prevent).

# sa02m_capture_svc_state <service>  → prints "<enabled> <active>" tokens (each
# "unknown" when the unit is absent / the query fails). Capture BEFORE the unit
# file is (re)installed: a freshly-installed unit already reads "disabled", so
# only a pre-install probe distinguishes a first install from an upgrade.
sa02m_capture_svc_state() {
    local svc=$1 en act
    en=$(sa02m_systemctl is-enabled "$svc" 2>/dev/null) || true
    act=$(sa02m_systemctl is-active "$svc" 2>/dev/null) || true
    printf '%s %s\n' "${en:-unknown}" "${act:-unknown}"
}

# sa02m_restore_svc_state <service> <prev_enabled> <prev_active> [refresh|start]
# Pure restore — never widens state:
#   * re-enable ONLY if it was enabled (never newly enable a disabled unit);
#   * if it WAS active: `refresh` → restart (load new code); `start` (default) →
#     start only if not already running; if it was NOT active, leave it stopped.
# The caller keeps its own FIRST-INSTALL default (its existing enable/start
# lines); this layer re-asserts a real prior runtime state on the upgrade path.
sa02m_restore_svc_state() {
    local svc=$1 prev_en=$2 prev_act=$3 mode=${4:-start}
    [ -n "${SA02M_ROOTFS_BUILD:-}" ] && return 0
    if [ "$prev_en" = enabled ]; then
        sa02m_systemctl enable "$svc" >> "$LOG_FILE" 2>&1 || true
    fi
    if [ "$prev_act" = active ]; then
        if [ "$mode" = refresh ]; then
            sa02m_systemctl restart "$svc" >> "$LOG_FILE" 2>&1 || true
        else
            sa02m_systemctl is-active --quiet "$svc" 2>/dev/null \
                || sa02m_systemctl start "$svc" >> "$LOG_FILE" 2>&1 || true
        fi
    fi
    return 0
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
