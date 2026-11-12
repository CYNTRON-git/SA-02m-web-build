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
#     ИСКЛЮЧЕНИЕ (решение Оператора 2026-11-11): веб-проверки — `cgi` (слой CGI),
#     `web-proc` (нет процесса nginx), `web-port` (панель не слушает порт) и
#     `http` (страница входа не отдаётся) — только ЛЕЧАТ: перезапуск fcgiwrap /
#     nginx, но НИКОГДА не перезагрузка платы. Такая поломка (потерянный
#     exec-бит или CRLF в auth_check.cgi, права на сокет fcgiwrap, nginx не
#     стартует) перезапуском не всегда лечится, а перезагрузка прерывала бы
#     CODESYS/MPLC4 каждые ~13 мин; плата живёт без панели. Перезагрузку
#     по-прежнему вызывают отказы всей платы: нет sshd (и любого другого
#     процесса из REQUIRED_PROCS, кроме nginx), закрыт порт SSH, интерфейс,
#     load, память.
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
# check and again at twice that, then every FAIL_THRESHOLD. The web checks
# (cgi, web-proc, web-port, http) are heal-only and never reboot the board.
CGI_RESTART_AT="${CGI_RESTART_AT:-6}"
# A probe that TIMES OUT (curl rc 28) is a busy fcgiwrap pool, not a dead layer:
# a restart would kill an in-flight offline-update upload (600 s) or backup
# stream (300 s). It is healed only after this many consecutive timeouts
# (90 x (4 s probe + 10 s interval) = about 21 min, longer than any single
# bounded request).
CGI_BUSY_RESTART_AT="${CGI_BUSY_RESTART_AT:-90}"
NET_IFACE="${NET_IFACE:-eth0}"
# REQUIRED_PROCS — отсутствие любого из них на FAIL_THRESHOLD подряд → reboot.
# nginx отсюда исключён кодом (WEB_PROCS): его отсутствие только перезапускает
# nginx (проверка web-proc), плату не перезагружает, даже если он указан в conf.
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

# Web-only processes: their absence is a panel fault, not a board fault, so it is
# judged by `check_web_procs` (heal-only) and NEVER by `check_required_procs`
# (reboot). Fixed on purpose, not configurable: a conf must not be able to put
# nginx back on the reboot path.
WEB_PROCS="nginx"

is_web_proc() {
    case " $WEB_PROCS " in *" $1 "*) return 0 ;; esac
    return 1
}

check_required_procs() {
    local p missing=""
    for p in $REQUIRED_PROCS; do
        is_web_proc "$p" && continue
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

check_web_procs() {
    local p missing=""
    for p in $WEB_PROCS; do
        check_proc "$p" || missing="$missing $p"
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
# A probe TIMEOUT (curl rc 28) answers "busy" and is not a failure of this
# check: the pool may only be saturated by long requests (CGI_BUSY_RESTART_AT).
# Seam: auth_check.cgi + the nginx /cgi-bin/ route must keep answering 401
# `unauthorized` without a session; the shipped pair is executed by
# scripts/dev/test-userspace-watchdog.sh case A6.
check_cgi() {
    local url=$1 out code body rc
    if ! command -v curl >/dev/null 2>&1; then
        echo "no-curl"
        return 0
    fi
    out=$(curl -sS --max-time 4 -w '\n%{http_code}' "$url" 2>/dev/null)
    rc=$?
    [ "$rc" -eq 28 ] && { echo "busy"; return 0; }
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
# Web-only faults are HEAL-ONLY (Operator 2026-11-11): the checks `cgi`,
# `web-proc` (nginx process missing), `web-port` (the panel port not listening)
# and `http` (login page not served) restart the web service and NEVER reach
# the board reboot — a reboot cannot cure them and interrupts CODESYS/MPLC4
# about every 13 minutes. Whole-board faults (`procs` for non-web processes,
# `ssh-port`, `iface`, `load`, `mem`) still reboot at FAIL_THRESHOLD.
# A heal-only check restarts at CGI_RESTART_AT and 2*CGI_RESTART_AT consecutive
# failures, then again every FAIL_THRESHOLD failures.
is_heal_only() {
    case "$1" in cgi|web-proc|web-port|http) return 0 ;; esac
    return 1
}

heal_due() {
    [ "$1" -eq "$CGI_RESTART_AT" ] || [ "$1" -eq $(( 2 * CGI_RESTART_AT )) ]
}

heal_only_due() {
    heal_due "$1" || { [ "$1" -gt $(( 2 * CGI_RESTART_AT )) ] && [ $(( $1 % FAIL_THRESHOLD )) -eq 0 ]; }
}

heal_fcgiwrap() {
    log "HEAL: restart fcgiwrap.service (check=cgi count=$1)"
    timeout 30 systemctl reset-failed fcgiwrap.service >/dev/null 2>&1 || true
    timeout 30 systemctl restart fcgiwrap.service >/dev/null 2>&1 || log "HEAL: restart fcgiwrap.service failed"
}

# $1 = count, $2 = check name. One nginx restart per pass however many of the
# web checks fail together (a dead nginx fails web-proc, web-port and http).
NGINX_HEALED_PASS=""
heal_nginx() {
    [ "$NGINX_HEALED_PASS" = "$WD_PASS" ] && return 0
    NGINX_HEALED_PASS=$WD_PASS
    log "HEAL: restart nginx.service (check=${2:-web} count=$1)"
    timeout 30 systemctl reset-failed nginx.service >/dev/null 2>&1 || true
    timeout 30 systemctl restart nginx.service >/dev/null 2>&1 || log "HEAL: restart nginx.service failed"
}

# What a heal-only check restarts: the CGI layer -> fcgiwrap, the rest -> nginx.
heal_web() {
    case "$1" in
        cgi) heal_fcgiwrap "$2" ;;
        *)   heal_nginx "$2" "$1" ;;
    esac
}

# ───────────────────────────── main loop ──────────────────────────────────
declare -A FAIL_COUNT
CGI_BUSY_COUNT=0
WD_PASS=0

run_check() {
    local name=$1 result status
    shift
    result=$("$@")
    status=$?
    if [ "$name" = cgi ]; then
        if [ "$result" = busy ]; then
            CGI_BUSY_COUNT=$(( CGI_BUSY_COUNT + 1 ))
            [ "$CGI_BUSY_COUNT" -eq 1 ] && log "check cgi: probe timed out - pool busy, not restarting yet [busy=1/${CGI_BUSY_RESTART_AT}]"
            if [ "$CGI_BUSY_COUNT" -eq "$CGI_BUSY_RESTART_AT" ]; then
                heal_fcgiwrap "busy=${CGI_BUSY_COUNT}"
                CGI_BUSY_COUNT=0
            fi
        else
            CGI_BUSY_COUNT=0
        fi
    fi
    if [ "$status" -ne 0 ]; then
        FAIL_COUNT["$name"]=$(( ${FAIL_COUNT["$name"]:-0} + 1 ))
        # Heal-only (Operator 2026-11-11): a web-only failure never reaches the
        # reboot below. One line on purpose — the comment-out proof pins it.
        is_heal_only "$name" && { log "check ${name}: FAIL (${result}) [count=${FAIL_COUNT[$name]}, heal-only: never reboots]"; heal_only_due "${FAIL_COUNT[$name]}" && heal_web "$name" "${FAIL_COUNT[$name]}"; return 0; }
        log "check ${name}: FAIL (${result}) [count=${FAIL_COUNT[$name]}/${FAIL_THRESHOLD}]"
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
        WD_PASS=$loop
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
                run_check "web-proc"  check_web_procs
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
