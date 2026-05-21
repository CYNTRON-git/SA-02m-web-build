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
WEB_PORT="${WEB_PORT:-9999}"
HTTP_PROBE_URL="${HTTP_PROBE_URL:-http://127.0.0.1:9999/login.html}"
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

check_iface() {
    local iface=$1 oper
    if [ ! -e "/sys/class/net/${iface}/operstate" ]; then
        echo "no-iface"
        return 0
    fi
    oper=$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo unknown)
    case "$oper" in
        up)        echo "$oper"; return 0 ;;
        unknown)   echo "$oper"; return 0 ;;  # некоторые драйверы всегда unknown
        *)         echo "$oper"; return 1 ;;
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
        fi

        loop=$(( loop + 1 ))
        [ $(( loop % 30 )) -eq 0 ] && trim_log_if_needed

        sleep "$CHECK_INTERVAL"
    done
}

main "$@"
