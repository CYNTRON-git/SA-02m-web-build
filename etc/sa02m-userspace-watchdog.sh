#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-userspace-watchdog
# ───────────────────────────────────────────────────────────────────────────
# User-space watchdog для SA-02m (Allwinner A40i / Armbian).
# Не владеет /dev/watchdog (им управляет PID1 systemd через
# RuntimeWatchdogSec=) — этот демон проверяет здоровье userspace
# и принудительно перезагружает плату, если что-то стойко сломано.
#
# Поведение:
#   * Проверки выполняются раз в CHECK_INTERVAL секунд.
#   * Каждая проверка возвращает ok/fail.
#   * fail-счётчик каждой проверки независим. Если у любой проверки
#     счётчик достигает FAIL_THRESHOLD подряд — выполняется
#     `sa02m_reboot()` (попытки graceful → force → отпуск /dev/watchdog).
#   * Любой ok сбрасывает свой счётчик.
#
# Логи:
#   /var/log/sa02m_userspace_watchdog.log + journald.
# ═══════════════════════════════════════════════════════════════════════════
set -u

CONF_FILE="${CONF_FILE:-/etc/sa02m_userspace_watchdog.conf}"
LOG_FILE="${LOG_FILE:-/var/log/sa02m_userspace_watchdog.log}"
LOG_ROTATE_MAX_BYTES="${LOG_ROTATE_MAX_BYTES:-2097152}"
LOG_ROTATE_KEEP_BYTES="${LOG_ROTATE_KEEP_BYTES:-1048576}"

# Базовые параметры
CHECK_INTERVAL="${CHECK_INTERVAL:-10}"          # секунд между проверками
FAIL_THRESHOLD="${FAIL_THRESHOLD:-60}"          # ≈ 60 × 10 c = 10 мин подряд → reboot
GRACE_AFTER_BOOT_SEC="${GRACE_AFTER_BOOT_SEC:-180}"  # не реагировать первые N сек после загрузки
IMAGING_LOCK="${IMAGING_LOCK:-/run/sa02m-imaging.lock}"  # make-image: без reboot пока файл есть
HEARTBEAT_INTERVAL_SEC="${HEARTBEAT_INTERVAL_SEC:-300}"

# Что мониторим
SSH_PORT="${SSH_PORT:-22}"
# WEB_PORT / HTTP_PROBE_URL / CGI_PROBE_URL: empty = derived from the panel's
# real port (resolve_web_port) — a board installed with `--port N` serves the
# panel on N, and a hard-coded 9999 rebooted it as «web down» (audit A3-04).
# A value set here or in $CONF_FILE still wins.
WEB_PORT="${WEB_PORT:-}"
HTTP_PROBE_URL="${HTTP_PROBE_URL:-}"
CGI_PROBE_URL="${CGI_PROBE_URL:-}"
SA02M_WEB_PORT_LIB="${SA02M_WEB_PORT_LIB:-/usr/local/lib/sa02m-web-port-lib.sh}"
# Restart a dead fcgiwrap / nginx at this many consecutive failures of their
# check and again at twice that, before FAIL_THRESHOLD reboots the board.
CGI_RESTART_AT="${CGI_RESTART_AT:-6}"
NET_IFACE="${NET_IFACE:-eth0}"
# REQUIRED_PROCS — отсутствие любого из них на FAIL_THRESHOLD подряд → reboot.
# Кладём сюда ТОЛЬКО критичную для управления плитой инфраструктуру.
# mplc4 здесь НЕТ умышленно: на части плат он по дизайну отключён,
# а его отсутствие не должно перезагружать устройство (см. WATCHED_PROCS).
REQUIRED_PROCS="${REQUIRED_PROCS:-nginx sshd}"
# WATCHED_PROCS — мониторим, но не reboot'им. Логируем при изменении состояния.
WATCHED_PROCS="${WATCHED_PROCS:-mplc4 fcgiwrap}"

# Пороги ресурсов
MAX_LOAD1_X100="${MAX_LOAD1_X100:-2400}"        # load average 1m × 100; >24 → fail
MIN_MEM_AVAIL_KB="${MIN_MEM_AVAIL_KB:-16384}"   # минимум 16 MiB available

[ -f "$CONF_FILE" ] && . "$CONF_FILE" 2>/dev/null || true

# What the operator pinned (env or conf); resolve_web_port fills the rest.
WEB_PORT_PINNED="$WEB_PORT"
HTTP_PROBE_URL_PINNED="$HTTP_PROBE_URL"
CGI_PROBE_URL_PINNED="$CGI_PROBE_URL"
WEB_PORT_SOURCE=""

# ───────────────────────────── helpers ────────────────────────────────────
log() {
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo unknown)
    printf '[%s] %s\n' "$ts" "$*" >> "$LOG_FILE" 2>/dev/null || true
    printf '[%s] %s\n' "$ts" "$*"
}

trim_log_if_needed() {
    local size tmp
    [ -f "$LOG_FILE" ] || return 0
    size=$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)
    [ "$size" -lt "$LOG_ROTATE_MAX_BYTES" ] && return 0
    tmp="${LOG_FILE}.tmp.$$"
    tail -c "$LOG_ROTATE_KEEP_BYTES" "$LOG_FILE" > "$tmp" 2>/dev/null || return 0
    mv "$tmp" "$LOG_FILE" 2>/dev/null || rm -f "$tmp"
}

# Загрузка ядра x100 (избегаем bc/python).
load1_x100() {
    awk '{
        split($1, a, ".");
        v = a[1] * 100;
        if (length(a[2]) >= 2) v += substr(a[2],1,2);
        else if (length(a[2]) == 1) v += a[2]*10;
        print v;
    }' /proc/loadavg 2>/dev/null || echo 0
}

mem_available_kb() {
    awk '/^MemAvailable:/ { print $2; exit }' /proc/meminfo 2>/dev/null || echo 0
}

uptime_sec() {
    awk '{ printf "%d\n", $1 }' /proc/uptime 2>/dev/null || echo 0
}

imaging_active() {
    [ -f "$IMAGING_LOCK" ]
}

# The panel's port: pinned value, else the installer's port library (read in a
# subshell — it is sourced code), else 9999. Re-run on every heartbeat: on a
# first install this daemon starts (01-system.sh) before 03-webserver.sh lays
# the library down. Logs only when the port or its source changes.
resolve_web_port() {
    local port="" rc src
    if [ -n "$WEB_PORT_PINNED" ]; then
        port=$WEB_PORT_PINNED; src="conf"
    elif [ -r "$SA02M_WEB_PORT_LIB" ]; then
        port=$(. "$SA02M_WEB_PORT_LIB" >/dev/null 2>&1 && sa02m_web_port 2>/dev/null)
        rc=$?
        case "$rc:$port" in
            0:[0-9]*|3:[0-9]*) src="port library" ;;
            *) port=9999; src="default (port library failed rc=$rc)" ;;
        esac
    else
        port=9999; src="default (no port library at $SA02M_WEB_PORT_LIB)"
    fi
    if [ "$port:$src" != "$WEB_PORT:$WEB_PORT_SOURCE" ]; then
        log "web port ${port} (${src})"
    fi
    WEB_PORT=$port
    WEB_PORT_SOURCE=$src
    HTTP_PROBE_URL=${HTTP_PROBE_URL_PINNED:-http://127.0.0.1:${WEB_PORT}/login.html}
    CGI_PROBE_URL=${CGI_PROBE_URL_PINNED:-http://127.0.0.1:${WEB_PORT}/cgi-bin/auth_check.cgi}
}

# ───────────────────────────── checks ─────────────────────────────────────
# Каждая check_* функция: 0 = ok, 1 = fail. STDOUT — короткий статус.
check_proc() {
    local name=$1
    pgrep -x "$name" >/dev/null 2>&1
}

check_required_procs() {
    local p missing=""
    for p in $REQUIRED_PROCS; do
        if ! check_proc "$p"; then
            missing="$missing $p"
        fi
    done
    if [ -n "$missing" ]; then
        echo "missing:${missing# }"
        return 1
    fi
    echo "ok"
    return 0
}

# Состояние WATCHED_PROCS — храним, чтобы логировать переходы.
declare -A WATCHED_PROC_LAST
log_watched_procs() {
    local p cur
    for p in $WATCHED_PROCS; do
        if check_proc "$p"; then
            cur=running
        else
            cur=missing
        fi
        if [ "${WATCHED_PROC_LAST[$p]:-}" != "$cur" ]; then
            log "watched proc ${p}: ${WATCHED_PROC_LAST[$p]:-unset} -> ${cur}"
            WATCHED_PROC_LAST[$p]=$cur
        fi
    done
}

check_port() {
    local port=$1
    if ! command -v ss >/dev/null 2>&1; then
        echo "no-ss"
        return 0   # не считаем фейлом
    fi
    if ss -H -ltn "sport = :${port}" 2>/dev/null | awk 'NR==1{f=1} END{exit(f?0:1)}'; then
        echo "listening"
        return 0
    fi
    echo "missing"
    return 1
}

check_http() {
    local url=$1
    if ! command -v curl >/dev/null 2>&1; then
        echo "no-curl"
        return 0
    fi
    if curl -fsS --max-time 4 "$url" >/dev/null 2>&1; then
        echo "ok"
        return 0
    fi
    echo "fail"
    return 1
}

# The CGI layer: login.html is static and nginx serves it with fcgiwrap dead,
# so it cannot see the 502 every CGI then answers. auth_check.cgi without a
# session must come back 401 with body `unauthorized` (or 204) — only fcgiwrap
# running the CGI produces that; nginx alone gives 502/504. This probe reads the
# real HTTP status (no -f: a 401 is the healthy answer here) and the body.
check_cgi() {
    local url=$1 out code body
    if ! command -v curl >/dev/null 2>&1; then
        echo "no-curl"
        return 0
    fi
    out=$(curl -sS --max-time 4 -w '\n%{http_code}' "$url" 2>/dev/null)
    code=${out##*$'\n'}
    body=${out%$'\n'*}
    case "$code" in
        204) echo "ok"; return 0 ;;
        401) case "$body" in *unauthorized*) echo "ok"; return 0 ;; esac ;;
    esac
    echo "http=${code:-none}"
    return 1
}

check_iface() {
    local iface=$1 oper carrier
    if [ ! -e "/sys/class/net/${iface}/operstate" ]; then
        echo "no-iface"; return 0
    fi
    # No physical carrier means cable is unplugged — rebooting cannot fix this.
    # Return OK so we don't trigger a pointless reboot loop.
    carrier=$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo 0)
    if [ "$carrier" != "1" ]; then
        echo "no-carrier-skip"; return 0
    fi
    oper=$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo unknown)
    case "$oper" in
        up)      echo "$oper"; return 0 ;;
        unknown) echo "$oper"; return 0 ;;  # some drivers always report unknown
        *)       echo "$oper"; return 1 ;;
    esac
}

check_load() {
    local v
    v=$(load1_x100)
    if [ "$v" -gt "$MAX_LOAD1_X100" ]; then
        echo "load=${v} > ${MAX_LOAD1_X100}"
        return 1
    fi
    echo "load=${v}"
    return 0
}

check_mem() {
    local v
    v=$(mem_available_kb)
    if [ "$v" -lt "$MIN_MEM_AVAIL_KB" ]; then
        echo "memavail=${v}KB < ${MIN_MEM_AVAIL_KB}KB"
        return 1
    fi
    echo "memavail=${v}KB"
    return 0
}

# ───────────────────────────── reboot strategy ────────────────────────────
sa02m_reboot() {
    local reason=$1
    log "FORCED REBOOT: ${reason}"

    # 1. Сохранить логи на диск
    sync 2>/dev/null || true

    # 2. Лучший usability-вариант — systemctl reboot --force.
    #    Если PID1 жив, это завершит сервисы быстро и перезагрузится.
    timeout 15 systemctl reboot --force >/dev/null 2>&1 &
    local pid=$!
    sleep 16
    kill -0 "$pid" 2>/dev/null && wait "$pid" 2>/dev/null

    # 3. Если ещё живы — kernel-level reboot syscall.
    log "FORCED REBOOT: graceful failed, calling reboot -f"
    timeout 5 reboot -f >/dev/null 2>&1 &
    sleep 6

    # 4. Последний бастион — отпустить /dev/watchdog без MAGIC_CLOSE.
    #    PID1 systemd им владеет, но если процесс пишет туда — kernel
    #    не остановит счётчик. Пинаем без MAGIC и не закрываем — ядро
    #    выполнит HW reset через ~16 c.
    log "FORCED REBOOT: triggering HW watchdog reset via /dev/watchdog"
    if [ -c /dev/watchdog ]; then
        # Пишем не-V байт; namespace HW timer уже считает, ничего не пинаем.
        # Если kernel/systemd hang — этот write всё равно может зависнуть,
        # но HW WDT (16 с от последнего PID1 ping) сделает reset сам.
        ( printf 'X' > /dev/watchdog ) 2>/dev/null &
    fi
    # Бесконечный sleep — ждём reset.
    while true; do sleep 60; done
}

# ───────────────────────────── service healing ─────────────────────────────
# A dead fcgiwrap or nginx is a one-service fault: restart that service first
# (at CGI_RESTART_AT and 2*CGI_RESTART_AT consecutive failures); the board is
# rebooted only if FAIL_THRESHOLD is still reached after that.
heal_due() {
    [ "$1" -eq "$CGI_RESTART_AT" ] || [ "$1" -eq $(( 2 * CGI_RESTART_AT )) ]
}

heal_fcgiwrap() {
    log "HEAL: restart fcgiwrap.service (check=cgi count=$1)"
    timeout 30 systemctl reset-failed fcgiwrap.service >/dev/null 2>&1 || true
    timeout 30 systemctl restart fcgiwrap.service >/dev/null 2>&1 || log "HEAL: restart fcgiwrap.service failed"
}

heal_nginx() {
    log "HEAL: restart nginx.service (check=procs count=$1)"
    timeout 30 systemctl reset-failed nginx.service >/dev/null 2>&1 || true
    timeout 30 systemctl restart nginx.service >/dev/null 2>&1 || log "HEAL: restart nginx.service failed"
}

# ───────────────────────────── main loop ──────────────────────────────────
declare -A FAIL_COUNT

run_check() {
    local name=$1 result status
    shift
    result=$("$@")
    status=$?
    if [ "$status" -ne 0 ]; then
        FAIL_COUNT["$name"]=$(( ${FAIL_COUNT["$name"]:-0} + 1 ))
        log "check ${name}: FAIL (${result}) [count=${FAIL_COUNT[$name]}/${FAIL_THRESHOLD}]"
        if heal_due "${FAIL_COUNT[$name]}"; then
            case "$name:$result" in
                cgi:*) heal_fcgiwrap "${FAIL_COUNT[$name]}" ;;
                procs:*nginx*) heal_nginx "${FAIL_COUNT[$name]}" ;;
            esac
        fi
        if [ "${FAIL_COUNT[$name]}" -ge "$FAIL_THRESHOLD" ]; then
            sa02m_reboot "check=${name} reason=${result}"
        fi
    else
        if [ -n "${FAIL_COUNT[$name]:-}" ] && [ "${FAIL_COUNT[$name]}" -gt 0 ]; then
            log "check ${name}: RECOVERED (${result}) [was ${FAIL_COUNT[$name]}]"
        fi
        FAIL_COUNT["$name"]=0
    fi
}

main() {
    local last_heartbeat=0 now loop=0 grace_done=0
    log "userspace watchdog started; interval=${CHECK_INTERVAL}s threshold=${FAIL_THRESHOLD} grace=${GRACE_AFTER_BOOT_SEC}s"
    resolve_web_port

    while true; do
        now=$(uptime_sec)

        if [ "$grace_done" -eq 0 ]; then
            if [ "$now" -lt "$GRACE_AFTER_BOOT_SEC" ]; then
                # Внутри grace-периода ничего не считаем как fail
                if [ $(( loop % 6 )) -eq 0 ]; then
                    log "in grace period (uptime=${now}s < ${GRACE_AFTER_BOOT_SEC}s)"
                fi
            else
                log "grace period passed (uptime=${now}s)"
                grace_done=1
            fi
        fi

        if [ "$grace_done" -eq 1 ]; then
            if imaging_active; then
                if [ $(( loop % 6 )) -eq 0 ]; then
                    log "imaging lock active ($IMAGING_LOCK) — reboot checks paused"
                fi
                log_watched_procs
            else
                run_check "procs"     check_required_procs
                run_check "ssh-port"  check_port "$SSH_PORT"
                run_check "web-port"  check_port "$WEB_PORT"
                run_check "http"      check_http "$HTTP_PROBE_URL"
                run_check "cgi"       check_cgi "$CGI_PROBE_URL"
                run_check "iface"     check_iface "$NET_IFACE"
                run_check "load"      check_load
                run_check "mem"       check_mem
                log_watched_procs
            fi
        fi

        now=$(date +%s)
        if [ "$last_heartbeat" -eq 0 ] || [ $(( now - last_heartbeat )) -ge "$HEARTBEAT_INTERVAL_SEC" ]; then
            log "heartbeat uptime=$(uptime_sec)s load=$(load1_x100)/100 memavail=$(mem_available_kb)KB"
            last_heartbeat=$now
            resolve_web_port
        fi

        loop=$(( loop + 1 ))
        [ $(( loop % 30 )) -eq 0 ] && trim_log_if_needed

        sleep "$CHECK_INTERVAL"
    done
}

# Sourced with SA02M_WD_SOURCE_ONLY=1 the file only defines (test seam,
# scripts/dev/test-userspace-watchdog.sh).
[ "${SA02M_WD_SOURCE_ONLY:-0}" = 1 ] || main "$@"
