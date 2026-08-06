#!/bin/bash
set -euo pipefail

CONF_FILE="${CONF_FILE:-/etc/sa02m_hw.conf}"
STATE_DIR="${STATE_DIR:-/var/lib/sa02m-hw-backend-guard}"
CACHE_DIR="${CACHE_DIR:-/tmp/sa02m_status_cache}"
LOG_FILE="${LOG_FILE:-/var/log/sa02m_hw_backend_guard.log}"
DEFAULT_TIMEOUT_SEC="${DEFAULT_TIMEOUT_SEC:-120}"
STATUS_CURL_TIMEOUT_SEC="${STATUS_CURL_TIMEOUT_SEC:-6}"
STATUS_URL_BASE="${STATUS_URL_BASE:-http://127.0.0.1:9999/cgi-bin/status.cgi}"
# Пусто по умолчанию — и это осознанно. Раньше здесь стоял статический токен
# `session_token=cyntron_session`, выведенный из обращения 2026-07-12: он давно
# ничего не открывал, а откатному сторожу учётные данные и не нужны — он
# проверяет «отвечает ли стек», а не «пускают ли меня внутрь». Кто хочет более
# глубокой проверки под сессией — экспортирует живой токен в SESSION_COOKIE.
SESSION_COOKIE="${SESSION_COOKIE:-}"
ARMED_FILE="${STATE_DIR}/armed"
PID_FILE="${STATE_DIR}/armed.pid"

mkdir -p "$STATE_DIR"

log() {
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    printf '[%s] %s\n' "$ts" "$*" | tee -a "$LOG_FILE" >/dev/null
}

require_root() {
    if [ "${EUID:-0}" -ne 0 ]; then
        echo "Run as root." >&2
        exit 1
    fi
}

current_backend() {
    awk -F= '/^SA02M_HW_BACKEND=/{print $2; exit}' "$CONF_FILE" 2>/dev/null || true
}

write_backend() {
    local backend=$1 tmp
    tmp="${CONF_FILE}.tmp.$$"

    if [ -f "$CONF_FILE" ]; then
        awk -v backend="$backend" '
            BEGIN { done = 0 }
            /^SA02M_HW_BACKEND=/ {
                print "SA02M_HW_BACKEND=" backend
                done = 1
                next
            }
            { print }
            END {
                if (!done) {
                    print "SA02M_HW_BACKEND=" backend
                }
            }
        ' "$CONF_FILE" > "$tmp"
    else
        printf 'SA02M_HW_BACKEND=%s\n' "$backend" > "$tmp"
    fi

    install -m 644 "$tmp" "$CONF_FILE"
    rm -f "$tmp"
}

clear_status_cache() {
    rm -f "$CACHE_DIR"/*.json "$CACHE_DIR"/*.lock 2>/dev/null || true
}

# В репозитории два HTTP-слоя с противоположными идиомами ошибок. Bash-CGI
# отвечает HTTP 200 даже на отказ (`{"error":"unauthorized"}`), поэтому здесь
# `curl -f` не поймает НИЧЕГО — единственное, что может дать сбой, это
# проверка ТЕЛА. Общий дом правила: docs/agent-rules/web-code-rigor.md
# ## Bash CGI floors. До этого правки проба не умела падать вовсе.
probe_status_part() {
    local url=$1 body
    if [ -n "$SESSION_COOKIE" ]; then
        body=$(curl -fsS --max-time "$STATUS_CURL_TIMEOUT_SEC" -H "Cookie: ${SESSION_COOKIE}" "$url") || return 1
    else
        body=$(curl -fsS --max-time "$STATUS_CURL_TIMEOUT_SEC" "$url") || return 1
    fi
    # Ответ обязан быть JSON-объектом. `{"error":"unauthorized"}` засчитывается:
    # сторож проверяет живость стека, а не свои права. Пустой ответ, HTML-страница
    # ошибки nginx или обрезанный вывод — не засчитываются.
    case "$body" in
        '{'*'}') return 0 ;;
    esac
    return 1
}

probe_dashboard() {
    # /login.html — статика, здесь слой отдаёт настоящие коды и `-f` уместен.
    curl -fsS --max-time "$STATUS_CURL_TIMEOUT_SEC" "http://127.0.0.1:9999/login.html" >/dev/null
    probe_status_part "${STATUS_URL_BASE}?part=priority"
    probe_status_part "${STATUS_URL_BASE}?part=main"
    probe_status_part "${STATUS_URL_BASE}?part=hardware"
}

probe_with_retries() {
    local attempt
    for attempt in 1 2 3; do
        if probe_dashboard; then
            return 0
        fi
        sleep 3
    done
    return 1
}

write_armed_state() {
    local token=$1 rollback_backend=$2 target_backend=$3 timeout_sec=$4
    cat > "$ARMED_FILE" <<EOF
TOKEN=$token
ROLLBACK_BACKEND=$rollback_backend
TARGET_BACKEND=$target_backend
TIMEOUT_SEC=$timeout_sec
EOF
}

load_armed_var() {
    local key=$1
    awk -F= -v key="$key" '$1 == key { print substr($0, length($1) + 2); exit }' "$ARMED_FILE" 2>/dev/null || true
}

disarm_guard() {
    rm -f "$ARMED_FILE"
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
        case "$pid" in
            ''|*[!0-9]*) ;;
            *) kill "$pid" 2>/dev/null || true ;;
        esac
        rm -f "$PID_FILE"
    fi
}

rollback_now() {
    local backend=$1
    write_backend "$backend"
    clear_status_cache
    log "Rollback applied, backend=${backend}"
}

background_worker() {
    local token=$1 timeout_sec=$2 rollback_backend=$3
    sleep "$timeout_sec"

    [ -f "$ARMED_FILE" ] || exit 0
    if [ "$(load_armed_var TOKEN)" != "$token" ]; then
        exit 0
    fi

    rollback_now "$rollback_backend"
    disarm_guard
}

cmd_activate() {
    require_root

    local target_backend=${1:-auto}
    local timeout_sec=${2:-$DEFAULT_TIMEOUT_SEC}
    local rollback_backend=${3:-disabled}
    local token backup_file worker_pid

    token="$(date +%s).$$"
    backup_file="${STATE_DIR}/sa02m_hw.conf.$(date +%Y%m%d_%H%M%S).bak"
    [ -f "$CONF_FILE" ] && cp -f "$CONF_FILE" "$backup_file"

    write_backend "$target_backend"
    clear_status_cache
    write_armed_state "$token" "$rollback_backend" "$target_backend" "$timeout_sec"

    nohup "$0" --rollback-worker "$token" "$timeout_sec" "$rollback_backend" >/dev/null 2>&1 &
    worker_pid=$!
    printf '%s\n' "$worker_pid" > "$PID_FILE"

    log "Guard armed: target=${target_backend}, rollback=${rollback_backend}, timeout=${timeout_sec}s, backup=${backup_file}"

    if probe_with_retries; then
        log "Activation probe succeeded, backend=${target_backend}. Confirm with: $0 confirm"
        echo "Activation succeeded. Auto-rollback to '${rollback_backend}' is armed for ${timeout_sec}s."
        exit 0
    fi

    log "Activation probe failed, immediate rollback to ${rollback_backend}"
    rollback_now "$rollback_backend"
    disarm_guard
    echo "Activation probe failed. Rolled back to '${rollback_backend}'." >&2
    exit 1
}

cmd_confirm() {
    require_root
    if [ ! -f "$ARMED_FILE" ]; then
        echo "No armed rollback."
        exit 0
    fi

    local target_backend
    target_backend=$(load_armed_var TARGET_BACKEND)
    disarm_guard
    log "Guard confirmed, backend kept=${target_backend:-$(current_backend)}"
    echo "Rollback disarmed."
}

cmd_rollback() {
    require_root
    local backend=${1:-disabled}
    rollback_now "$backend"
    disarm_guard
    echo "Rolled back to '${backend}'."
}

cmd_status() {
    local backend armed target rollback timeout
    backend=$(current_backend)
    echo "current_backend=${backend:-unknown}"

    if [ -f "$ARMED_FILE" ]; then
        armed=yes
        target=$(load_armed_var TARGET_BACKEND)
        rollback=$(load_armed_var ROLLBACK_BACKEND)
        timeout=$(load_armed_var TIMEOUT_SEC)
    else
        armed=no
        target=
        rollback=
        timeout=
    fi

    echo "rollback_armed=${armed}"
    [ -n "$target" ] && echo "target_backend=${target}"
    [ -n "$rollback" ] && echo "rollback_backend=${rollback}"
    [ -n "$timeout" ] && echo "timeout_sec=${timeout}"
}

case "${1:-}" in
    activate)
        shift
        cmd_activate "$@"
        ;;
    confirm)
        shift
        cmd_confirm "$@"
        ;;
    rollback)
        shift
        cmd_rollback "$@"
        ;;
    status)
        shift
        cmd_status "$@"
        ;;
    --rollback-worker)
        shift
        background_worker "$@"
        ;;
    *)
        cat <<'EOF'
Usage:
  sa02m-hw-backend-guard activate [target_backend] [timeout_sec] [rollback_backend]
  sa02m-hw-backend-guard confirm
  sa02m-hw-backend-guard rollback [backend]
  sa02m-hw-backend-guard status

Examples:
  sa02m-hw-backend-guard activate auto 120 disabled
  sa02m-hw-backend-guard confirm
  sa02m-hw-backend-guard rollback disabled
EOF
        exit 1
        ;;
esac
