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

# sa02m_pkg_install_tier required|optional|thirdparty <pkg>...
# Install only the still-missing packages, with bounded opts. Offline ⇒ skip and
# WARN (the caller's downstream guards cover the degraded case). A failed
# OPTIONAL package → per-pkg WARN + continue; a failed REQUIRED package → a
# prominent WARN naming the consequence. THIRDPARTY = a third-party stack's
# own packages (docker.io, …): never installed in a refresh run without
# --with-optional (INFO + return 0), otherwise exactly the optional tier.
# Never aborts the install (safe even where the caller runs `set -e`).
sa02m_pkg_install_tier() {
    local tier=$1; shift
    local missing=() p
    for p in "$@"; do
        dpkg -l "$p" 2>/dev/null | grep -q "^ii" || missing+=("$p")
    done
    [ ${#missing[@]} -gt 0 ] || return 0
    if [ "$tier" = thirdparty ]; then
        if [ "${SA02M_INSTALL_MODE:-full}" = refresh ] && [ "${SA02M_WITH_OPTIONAL:-0}" != 1 ]; then
            log INFO "refresh: сторонние пакеты не ставятся: ${missing[*]} (--with-optional — поставить)"
            return 0
        fi
        tier=optional
    fi
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

# Install a sudoers drop-in from repo <src> to <dst>.
# VALIDATE-then-ACTIVATE: a malformed drop-in breaks sudo globally, so never let
# a file visudo rejects reach the live path. Validate a CRLF-stripped copy of the
# source FIRST; only a clean file is installed (as the already-stripped, valid
# copy). Where visudo/mktemp are unavailable, fall back to the prior
# install-then-harden (the aggregate `visudo -c` at the end of install.sh is the
# remaining net). A rejected source leaves the live $dst UNTOUCHED.
sa02m_install_sudoers() {
    local src=$1 dst=$2
    if [ ! -f "$src" ]; then
        log WARN "sudoers-источник $src не найден — пропуск"
        return 0
    fi
    if command -v visudo >/dev/null 2>&1; then
        local _tmp
        _tmp=$(mktemp 2>/dev/null) || _tmp=""
        if [ -n "$_tmp" ]; then
            sed 's/\r$//' "$src" > "$_tmp" 2>/dev/null
            if ! visudo -cf "$_tmp" >/dev/null 2>&1; then
                rm -f "$_tmp"
                log WARN "sudoers $dst НЕ установлен: visudo отклонил источник $src (живой файл не тронут)"
                return 0
            fi
            if install -m 0440 -o root -g root "$_tmp" "$dst" 2>>"$LOG_FILE"; then
                rm -f "$_tmp"
                log OK "sudoers $(basename "$dst") OK"
                return 0
            fi
            rm -f "$_tmp"
            log WARN "не удалось установить sudoers $dst из $src"
            return 0
        fi
    fi
    # Fallback (no visudo / no mktemp): prior install-then-harden behaviour.
    install -m 0440 -o root -g root "$src" "$dst" 2>>"$LOG_FILE" || {
        log WARN "не удалось установить sudoers $dst из $src"
        return 0
    }
    sa02m_harden_sudoers "$dst"
    return 0
}

# Remove known-obsolete www-data sudoers drop-ins superseded by etc/sudoers.d/sa02m-www
# (audit B1 deploy-gap). Allow-list ONLY — never glob /etc/sudoers.d/*.
sa02m_remove_obsolete_www_sudoers() {
    local _name _path
    for _name in www-data sa02m-www.fragment; do
        _path="/etc/sudoers.d/$_name"
        if [ -f "$_path" ]; then
            rm -f "$_path" \
                && log OK "удалён устаревший sudoers: $_path" \
                || log WARN "не удалось удалить устаревший sudoers: $_path"
        fi
    done
    for _name in sa02m-www sa02m-cloud sa02m-flasher sa02m-mqtt; do
        _path="/etc/sudoers.d/$_name"
        if [ -f "$_path" ]; then
            chmod 0440 "$_path" 2>/dev/null || true
        fi
    done
    if command -v visudo >/dev/null 2>&1; then
        visudo -c >/dev/null 2>&1 \
            || log WARN "visudo -c после удаления legacy sudoers — проверьте /etc/sudoers.d"
    fi
}

# OTA map_dst used to strip .sh from the two B1 helpers; remove the twins so
# sudoers (which grants the .sh path) and apply.cgi resolve the real helper.
sa02m_remove_stale_b1_helper_twins() {
    local _p
    for _p in /usr/local/sbin/sa02m-iface-conf-write /usr/local/sbin/sa02m-usb-power; do
        if [ -f "$_p" ] && [ ! -L "$_p" ]; then
            rm -f "$_p" \
                && log OK "удалён helper без .sh (OTA twin): $_p" \
                || log WARN "не удалось удалить helper без .sh: $_p"
        fi
    done
}

sa02m_cleanup_b1_deploy_artifacts() {
    sa02m_remove_obsolete_www_sudoers
    sa02m_remove_stale_b1_helper_twins
}

# ── Stack policy (third-party stacks) ───────────────────────────────────────
# The ID set, /etc/sa02m_stacks.conf, live detection and the verdict table live
# in ONE POSIX file shared with the web ctl; a missing file is a broken tree —
# fail loud, not soft. Contract: docs/contracts/installer-refresh-policy.md.
# shellcheck source=../etc/sa02m-stacks-policy.sh
source "$(dirname "${BASH_SOURCE[0]}")/../etc/sa02m-stacks-policy.sh"

# sa02m_pip_install <import_name> <pip_spec>
# Already importable ⇒ return 0 silently; offline ⇒ WARN + skip (the feature
# degrades); else a bounded `pip3 install`, failure ⇒ WARN. Never aborts.
sa02m_pip_install() {
    local mod=$1 spec=$2 sec=${SA02M_APT_TIMEOUT_SEC:-90}
    local -a opts=(--quiet)
    python3 -c "import $mod" >/dev/null 2>&1 && return 0
    if ! sa02m_online; then
        log WARN "ОФФЛАЙН: python-модуль $mod не установлен (функция деградирует)"
        return 0
    fi
    if ! command -v pip3 >/dev/null 2>&1; then
        log WARN "pip3 не найден — python-модуль $mod не установлен (функция деградирует)"
        return 0
    fi
    # PEP 668 (bookworm+): system pip refuses without the flag; older pip has
    # no such option and rejects it — probe the help text once per call.
    if pip3 install --help 2>/dev/null | grep -q -- '--break-system-packages'; then
        opts+=(--break-system-packages)
    fi
    log INFO "pip3 install $spec"
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" pip3 install "${opts[@]}" "$spec" >> "$LOG_FILE" 2>&1 \
            || log WARN "pip3 install $spec не удался/таймаут — модуль $mod не установлен (функция деградирует)"
    else
        pip3 install "${opts[@]}" "$spec" >> "$LOG_FILE" 2>&1 \
            || log WARN "pip3 install $spec не удался — модуль $mod не установлен (функция деградирует)"
    fi
    return 0
}

# ── Service state: capture BEFORE, apply AFTER ───────────────────────────────
# The ONE home for «what may the installer do to a unit's enable/run state».
# Contract and decision table: docs/contracts/installer-refresh-policy.md.
# Two unit classes:
#   app   — an application unit whose run-state belongs to the operator: the
#           installer NEVER widens it (never enables a disabled one, never
#           starts a stopped one, never unmasks) in ANY mode; a unit that was
#           active is restarted on fresh code (once); a unit that did not exist
#           gets its module's first-install default — unless it belongs to a
#           third-party stack and the run is a refresh without --with-optional.
#   infra — a platform unit the installer owns (nginx, fcgiwrap, networking,
#           watchdogs, chrony, the sa02m system units): asserted unmasked +
#           enabled (+ started / restarted on request) in every mode; historic
#           masks on them are our own imaging bugs the installer must repair.
# Port-lease-safe: orderly restarts only, never the flasher lease
# (sa02m-domain.md ## Subsystems). Every systemctl call acts on the DELTA —
# a state read precedes it — so an unchanged board produces zero calls.
# All helpers are `set -e`-safe (always return 0) and log RU.
declare -gA SA02M_SVC_EN=() SA02M_SVC_ACT=() SA02M_SVC_TS=()
# shellcheck disable=SC2034  # the caller-facing result channel — read by the modules, not here
SA02M_SVC_LAST_RESULT=""

# Canonical unit name: a bare name is a .service.
_sa02m_unit_name() {
    case "$1" in
        *.service|*.timer|*.socket|*.target|*.path|*.mount|*.slice|*.device) printf '%s' "$1" ;;
        *) printf '%s.service' "$1" ;;
    esac
}

# sa02m_unit_exists <unit> → 0 iff systemd knows the unit (fragment or generated).
sa02m_unit_exists() {
    local u; u=$(_sa02m_unit_name "$1")
    if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
        systemctl --root="${SA02M_ROOTFS_ROOT:-/}" cat "$u" >/dev/null 2>&1
        return $?
    fi
    sa02m_systemctl cat "$u" >/dev/null 2>&1
}

# sa02m_sysv_autostart <name> → 0 iff /etc/rc[2-5].d/S??<name> exists — the
# autostart truth of a SysV-only (`generated`) unit; mirror of the ctl's
# mplc4_rc_autostart (6 lines, cross-referenced). SA02M_SYSV_RC_DIRS is a test
# seam (word-split deliberately).
sa02m_sysv_autostart() {
    local d l
    # shellcheck disable=SC2086
    for d in ${SA02M_SYSV_RC_DIRS:-/etc/rc2.d /etc/rc3.d /etc/rc4.d /etc/rc5.d}; do
        [ -d "$d" ] || continue
        for l in "$d"/S??"$1"; do
            [ -e "$l" ] && return 0
        done
    done
    return 1
}

# Internal: one bounded systemctl query. Prints stdout; returns the rc
# (124 = timeout). Never logs.
_sa02m_svc_query() {
    sa02m_systemctl "$@" 2>/dev/null
}

# Internal: 0 iff a unit file with this name exists on disk (no bus needed) —
# the second witness that a silent `is-enabled` answer is NOT "absent".
_sa02m_unit_file_on_disk() {
    local d
    for d in /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system /run/systemd/system /run/systemd/generator /run/systemd/generator.late; do
        [ -e "$d/$1" ] && return 0
    done
    return 1
}

# sa02m_svc_capture <unit>...
# Record each unit's enable state, active state and ActiveEnterTimestampMonotonic
# BEFORE the installer (re)installs its files. Enable states: systemd's own words
# (enabled|enabled-runtime|disabled|masked|masked-runtime|static|indirect|
# generated|alias|linked|transient|…) plus two of ours: `absent` (the unit does
# not exist — the ONLY first-install signal) and `timeout` (systemd did not
# answer — never treated as new, so a wedged D-Bus cannot widen anything).
# `absent` needs THREE witnesses: is-enabled rc=1 with empty stdout, no unit
# file on disk, and is-active answering a real state (the manager is alive).
# Under SA02M_ROOTFS_BUILD everything is `absent` (a chroot has no manager).
sa02m_svc_capture() {
    local u en act ts rc
    for u in "$@"; do
        u=$(_sa02m_unit_name "$u")
        if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
            SA02M_SVC_EN[$u]=absent; SA02M_SVC_ACT[$u]=absent; SA02M_SVC_TS[$u]=""
            continue
        fi
        act=$(_sa02m_svc_query is-active "$u"); rc=$?
        if [ "$rc" -eq 124 ] || [ -z "$act" ]; then act=timeout; fi
        en=$(_sa02m_svc_query is-enabled "$u"); rc=$?
        if [ "$rc" -eq 124 ]; then
            en=timeout
        elif [ -z "$en" ]; then
            if [ "$rc" -eq 1 ] && [ "$act" != timeout ] && ! _sa02m_unit_file_on_disk "$u"; then
                en=absent
            else
                en=timeout
            fi
        fi
        ts=""
        case "$act" in
            active|activating|reloading)
                ts=$(_sa02m_svc_query show -p ActiveEnterTimestampMonotonic --value "$u") || ts="" ;;
        esac
        SA02M_SVC_EN[$u]=$en; SA02M_SVC_ACT[$u]=$act; SA02M_SVC_TS[$u]=$ts
    done
    return 0
}

# Internal: `daemon-reload` once when systemd says the unit needs it — the
# insurance before any enable/start/restart (the module's own daemon-reload
# calls stay).
_sa02m_svc_reload_if_needed() {
    local need
    need=$(_sa02m_svc_query show -p NeedDaemonReload --value "$1") || need=""
    if [ "$need" = yes ]; then
        sa02m_systemctl daemon-reload >> "$LOG_FILE" 2>&1 || true
    fi
    return 0
}

# Internal verbs — each reads state first, acts only on the delta. Print
# nothing; the caller logs. Return 0 on success (or no-op), 1 on failure.
_sa02m_svc_enable() {   # <unit> [--runtime]
    local u=$1 rt=${2:-} now
    now=$(_sa02m_svc_query is-enabled "$u") || true
    if [ -n "$rt" ]; then
        case "$now" in enabled|enabled-runtime) return 0 ;; esac
        _sa02m_svc_reload_if_needed "$u"
        sa02m_systemctl enable --runtime "$u" >> "$LOG_FILE" 2>&1
        return $?
    fi
    [ "$now" = enabled ] && return 0
    _sa02m_svc_reload_if_needed "$u"
    sa02m_systemctl enable "$u" >> "$LOG_FILE" 2>&1
}
_sa02m_svc_start() {
    local u=$1 now
    now=$(_sa02m_svc_query is-active "$u") || true
    case "$now" in active|activating|reloading) return 0 ;; esac
    _sa02m_svc_reload_if_needed "$u"
    sa02m_systemctl start "$u" >> "$LOG_FILE" 2>&1
}
_sa02m_svc_restart() {
    _sa02m_svc_reload_if_needed "$1"
    sa02m_systemctl restart "$1" >> "$LOG_FILE" 2>&1
}

# sa02m_svc_unmask <unit> — unmask ONLY (no enable, no start): the repair half
# of a kernel-aware mask/unmask pair (02-network.sh nftables). Delta-only; one
# log line when something changed.
sa02m_svc_unmask() {
    local u now; u=$(_sa02m_unit_name "$1")
    SA02M_SVC_LAST_RESULT=kept
    [ -n "${SA02M_ROOTFS_BUILD:-}" ] && return 0
    now=$(_sa02m_svc_query is-enabled "$u") || true
    case "$now" in
        masked|masked-runtime)
            if sa02m_systemctl unmask "$u" >> "$LOG_FILE" 2>&1; then
                log INFO "$u: маска снята"
                SA02M_SVC_LAST_RESULT=unmasked
            else
                log WARN "$u: не удалось снять маску"
            fi
            ;;
    esac
    return 0
}

# sa02m_svc_kick <unit> — start a oneshot job NOW (no enable, no state change
# recorded): sa02m-web-update-check.service after its timer is enabled.
sa02m_svc_kick() {
    local u; u=$(_sa02m_unit_name "$1")
    SA02M_SVC_LAST_RESULT=kept
    [ -n "${SA02M_ROOTFS_BUILD:-}" ] && return 0
    _sa02m_svc_reload_if_needed "$u"
    if sa02m_systemctl start "$u" >> "$LOG_FILE" 2>&1; then
        SA02M_SVC_LAST_RESULT=started
    else
        log WARN "$u: разовый запуск не удался — journalctl -u $u"
    fi
    return 0
}

# sa02m_svc_restart_if_active <unit>
# Restart a unit ONLY if it is currently active — to reload a freshly installed
# runtime dependency (e.g. frpc for the cloud agent) WITHOUT starting a unit the
# operator has stopped (never-widen). No enable, no start-if-inactive. This is a
# targeted dependency-reload, distinct from sa02m_svc_apply's code-deploy restart
# (which the TS witness suppresses once code has already been restarted).
# Sets SA02M_SVC_LAST_RESULT ∈ {restarted, left-inactive, kept}. Always returns 0.
sa02m_svc_restart_if_active() {
    local u; u=$(_sa02m_unit_name "$1")
    SA02M_SVC_LAST_RESULT=kept
    [ -n "${SA02M_ROOTFS_BUILD:-}" ] && return 0
    local _act; _act=$(_sa02m_svc_query is-active "$u")
    if [ "$_act" = active ]; then
        if _sa02m_svc_restart "$u"; then
            SA02M_SVC_LAST_RESULT=restarted
            log OK "$u: перезапущен для подхвата свежей зависимости"
        else
            log WARN "$u: перезапуск не удался — journalctl -u $u"
        fi
    else
        SA02M_SVC_LAST_RESULT=left-inactive
        log INFO "$u: не активен — перезапуск для подхвата зависимости не требуется"
    fi
    return 0
}

# sa02m_svc_apply <unit> app <on|enabled|off> [norestart] [--stack=<ID>]
# sa02m_svc_apply <unit> infra [start] [restart]
# Sets SA02M_SVC_LAST_RESULT ∈ {started, restarted, enabled, left-inactive,
# left-masked, kept, skipped-thirdparty, uncaptured, timeout, absent} so a
# caller can branch (a socket wait only on started|restarted, an INFO instead of
# a WARN on left-inactive). Always returns 0.
sa02m_svc_apply() {
    local u kind
    u=$(_sa02m_unit_name "$1"); kind=${2:-}
    shift; [ $# -gt 0 ] && shift
    SA02M_SVC_LAST_RESULT=kept
    case "$kind" in
        app)   _sa02m_svc_apply_app "$u" "$@" ;;
        infra) _sa02m_svc_apply_infra "$u" "$@" ;;
        *)     log WARN "sa02m_svc_apply $u: неизвестный класс юнита '${kind}' (app|infra) — ничего не делаю" ;;
    esac
    return 0
}

_sa02m_svc_apply_app() {
    local u=$1 first=${2:-}
    shift; [ $# -gt 0 ] && shift
    local norestart=0 stack="" a en act ts
    for a in "$@"; do
        case "$a" in
            norestart) norestart=1 ;;
            --stack=*) stack=${a#--stack=} ;;
            *) log WARN "sa02m_svc_apply $u: неизвестный аргумент '$a' — игнорирую" ;;
        esac
    done
    case "$first" in
        on|enabled|off) ;;
        *) log WARN "sa02m_svc_apply $u app: первичное состояние должно быть on|enabled|off (получено '${first}') — считаю 'enabled'"; first=enabled ;;
    esac

    # rootfs build: only the enable symlinks can be baked (via --root); no
    # manager to start anything.
    if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
        case "$first" in
            on|enabled) sa02m_systemctl enable "$u"; SA02M_SVC_LAST_RESULT=enabled ;;
            off)        sa02m_systemctl disable "$u"; SA02M_SVC_LAST_RESULT=left-inactive ;;
        esac
        return 0
    fi

    if [ -z "${SA02M_SVC_EN[$u]+x}" ]; then
        # Caller bug: apply without a prior capture. Fall through as EXISTING
        # (never `first` — a post-install capture cannot see a first install),
        # so the run-state is preserved and nothing widens.
        log WARN "$u: состояние до установки не снято — считаю существующим, автозапуск не расширяю"
        sa02m_svc_capture "$u"
        if [ "${SA02M_SVC_EN[$u]}" = absent ] || [ "${SA02M_SVC_EN[$u]}" = timeout ] \
           || [ "${SA02M_SVC_ACT[$u]}" = timeout ]; then
            SA02M_SVC_LAST_RESULT=uncaptured
            return 0
        fi
        _sa02m_svc_apply_app_existing "$u" "${SA02M_SVC_EN[$u]}" "${SA02M_SVC_ACT[$u]}" "${SA02M_SVC_TS[$u]-}" "$norestart"
        SA02M_SVC_LAST_RESULT=uncaptured
        return 0
    fi
    en=${SA02M_SVC_EN[$u]}; act=${SA02M_SVC_ACT[$u]}; ts=${SA02M_SVC_TS[$u]-}

    if [ "$en" = timeout ] || [ "$act" = timeout ]; then
        log WARN "$u: systemd не ответил до установки — состояние не трогаю"
        SA02M_SVC_LAST_RESULT=timeout
        return 0
    fi

    # ── New unit: the module's first-install default ─────────────────────────
    if [ "$en" = absent ]; then
        if [ -n "$stack" ]; then
            if ! sa02m_stack_id_valid "$stack"; then
                log WARN "$u: неизвестный стек '$stack' в --stack= — считаю сторонним"
            fi
            if { ! sa02m_stack_id_valid "$stack" || sa02m_stack_is_thirdparty "$stack"; } \
               && [ "${SA02M_INSTALL_MODE:-full}" = refresh ] && [ "${SA02M_WITH_OPTIONAL:-0}" != 1 ]; then
                log INFO "refresh: $u (сторонний стек $stack) — автозапуск не включаю"
                SA02M_SVC_LAST_RESULT=skipped-thirdparty
                return 0
            fi
        fi
        case "$first" in
            on)
                if _sa02m_svc_enable "$u" && _sa02m_svc_start "$u"; then
                    log INFO "$u: первая установка — включён и запущен"
                    SA02M_SVC_LAST_RESULT=started
                else
                    log WARN "$u: первая установка — не удалось включить/запустить (journalctl -u $u)"
                fi
                ;;
            enabled)
                if _sa02m_svc_enable "$u"; then
                    log INFO "$u: первая установка — включён (без запуска)"
                    SA02M_SVC_LAST_RESULT=enabled
                else
                    log WARN "$u: первая установка — не удалось включить автозапуск"
                fi
                ;;
            off)
                sa02m_systemctl disable "$u" >> "$LOG_FILE" 2>&1 || true
                sa02m_systemctl stop "$u" >> "$LOG_FILE" 2>&1 || true
                log INFO "$u: первая установка — выключен по умолчанию"
                SA02M_SVC_LAST_RESULT=left-inactive
                ;;
        esac
        return 0
    fi

    _sa02m_svc_apply_app_existing "$u" "$en" "$act" "$ts" "$norestart"
    return 0
}

# Internal: the existing-unit half of the app decision table — the enable state
# is restored exactly (never widened); run state: active ⇒ one restart on fresh
# code (skipped when the TS witness shows it already happened), everything else
# preserved.
_sa02m_svc_apply_app_existing() {
    local u=$1 en=$2 act=$3 ts=$4 norestart=$5 now
    case "$en" in
        enabled)
            _sa02m_svc_enable "$u" || log WARN "$u: не удалось восстановить автозапуск (был enabled)" ;;
        enabled-runtime)
            _sa02m_svc_enable "$u" --runtime || log WARN "$u: не удалось восстановить автозапуск (был enabled-runtime)" ;;
        disabled)
            # Restore-exact: a vendor installer run between capture and apply
            # may have re-enabled the unit (MPLC's own install.sh does) — the
            # operator's disable wins.
            now=$(_sa02m_svc_query is-enabled "$u") || true
            case "$now" in
                enabled|enabled-runtime)
                    sa02m_systemctl disable "$u" >> "$LOG_FILE" 2>&1 || true
                    log INFO "$u: автозапуск снят обратно (до установки был выключен оператором)"
                    ;;
            esac
            ;;
        masked|masked-runtime)
            # The mask symlink cannot have been clobbered (systemd refuses to
            # mask a unit whose fragment lives in /etc, so a masked unit is a
            # /lib one and `install` never touched it) — verify, never touch.
            now=$(_sa02m_svc_query is-enabled "$u") || true
            case "$now" in
                masked|masked-runtime) log INFO "$u: прежнее состояние сохранено (en=$en act=$act)" ;;
                *) log WARN "$u: был замаскирован, сейчас '$now' — маска потеряна при установке; проверьте вручную" ;;
            esac
            SA02M_SVC_LAST_RESULT=left-masked
            return 0
            ;;
        generated)
            # SysV-only unit that may now be a native fragment: the autostart
            # truth is the S-links (the mplc4/codesys generator→native step).
            now=$(_sa02m_svc_query is-enabled "$u") || true
            if [ "$now" != generated ] && sa02m_sysv_autostart "${u%.service}"; then
                _sa02m_svc_enable "$u" || log WARN "$u: не удалось перенести автозапуск SysV → systemd"
            fi
            ;;
        *) : ;;   # disabled / static / indirect / alias / linked / transient — no enable change
    esac

    case "$act" in
        active|activating|reloading)
            if [ "$norestart" = 1 ]; then
                log INFO "$u: активен — перезапуск не требуется (norestart)"
                SA02M_SVC_LAST_RESULT=kept
                return 0
            fi
            now=$(_sa02m_svc_query show -p ActiveEnterTimestampMonotonic --value "$u") || now=""
            if [ -n "$ts" ] && [ -n "$now" ] && [ "$now" != "$ts" ]; then
                log INFO "$u: уже перезапущен после копирования кода — повторный рестарт не нужен"
                SA02M_SVC_LAST_RESULT=kept
                return 0
            fi
            if _sa02m_svc_restart "$u"; then
                log OK "$u: перезапущен на свежем коде (был активен)"
                SA02M_SVC_LAST_RESULT=restarted
            else
                log WARN "$u: перезапуск не удался — journalctl -u $u -n 50"
                SA02M_SVC_LAST_RESULT=kept
            fi
            ;;
        *)
            # Restore-exact: if something (a vendor installer) STARTED the unit
            # between capture and apply, the operator's stop wins — stop back.
            now=$(_sa02m_svc_query is-active "$u") || true
            case "$now" in
                active|activating|reloading)
                    sa02m_systemctl stop "$u" >> "$LOG_FILE" 2>&1 || true
                    log INFO "$u: остановлен обратно (до установки был остановлен оператором)"
                    ;;
                *)
                    if [ "$act" = failed ]; then
                        log INFO "$u: прежнее состояние сохранено (en=$en act=$act) — не запускаю; journalctl -u $u"
                    else
                        log INFO "$u: прежнее состояние сохранено (en=$en act=$act)"
                    fi
                    ;;
            esac
            SA02M_SVC_LAST_RESULT=left-inactive
            ;;
    esac
    return 0
}

_sa02m_svc_apply_infra() {
    local u=$1; shift
    local want_start=0 want_restart=0 a now rc changed=""
    for a in "$@"; do
        case "$a" in
            start)   want_start=1 ;;
            restart) want_restart=1 ;;
            *) log WARN "sa02m_svc_apply $u infra: неизвестный аргумент '$a' — игнорирую" ;;
        esac
    done
    if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
        sa02m_systemctl enable "$u"
        SA02M_SVC_LAST_RESULT=enabled
        return 0
    fi
    now=$(_sa02m_svc_query is-enabled "$u"); rc=$?
    if [ "$rc" -eq 124 ]; then
        log WARN "$u: systemd не ответил — состояние не трогаю"
        SA02M_SVC_LAST_RESULT=timeout
        return 0
    fi
    if [ -z "$now" ] && ! _sa02m_unit_file_on_disk "$u" && ! sa02m_unit_exists "$u"; then
        # The unit genuinely is not there (an optional infra piece not shipped
        # on this variant) — nothing to assert.
        SA02M_SVC_LAST_RESULT=absent
        return 0
    fi
    case "$now" in
        masked|masked-runtime)
            # Historic masks on infra units are our own imaging/old-installer
            # bugs — repaired unconditionally (the operator has no UI for them).
            if sa02m_systemctl unmask "$u" >> "$LOG_FILE" 2>&1; then
                changed="маска снята"
            else
                log WARN "$u: не удалось снять маску"
            fi
            now=$(_sa02m_svc_query is-enabled "$u") || true
            ;;
    esac
    case "$now" in
        enabled|static|indirect|alias|linked|generated|transient) : ;;
        *)
            if _sa02m_svc_enable "$u"; then
                changed="${changed:+$changed, }включён"
                SA02M_SVC_LAST_RESULT=enabled
            else
                log WARN "$u: не удалось включить автозапуск"
            fi
            ;;
    esac
    if [ "$want_start" = 1 ] || [ "$want_restart" = 1 ]; then
        now=$(_sa02m_svc_query is-active "$u") || true
        case "$now" in
            active|activating|reloading)
                if [ "$want_restart" = 1 ]; then
                    if _sa02m_svc_restart "$u"; then
                        changed="${changed:+$changed, }перезапущен"
                        SA02M_SVC_LAST_RESULT=restarted
                    else
                        log WARN "$u: перезапуск не удался — journalctl -u $u -n 50"
                    fi
                fi
                ;;
            *)
                if [ "$want_start" = 1 ]; then
                    if _sa02m_svc_start "$u"; then
                        changed="${changed:+$changed, }запущен"
                        # shellcheck disable=SC2034  # result channel, read by callers
                        SA02M_SVC_LAST_RESULT=started
                    else
                        log WARN "$u: не запустился — journalctl -u $u -n 50"
                    fi
                fi
                ;;
        esac
    fi
    if [ -n "$changed" ]; then
        log OK "$u: $changed"
    fi
    return 0
}
