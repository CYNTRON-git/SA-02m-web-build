#!/bin/bash
set -euo pipefail

CONF_FILE="${CONF_FILE:-/etc/sa02m_status_blocks.conf}"
STATE_DIR="${STATE_DIR:-/var/lib/sa02m-status-blocks-guard}"
LOG_FILE="${LOG_FILE:-/var/log/sa02m_status_blocks_guard.log}"
DEFAULT_TIMEOUT_SEC="${DEFAULT_TIMEOUT_SEC:-120}"
STATUS_CURL_TIMEOUT_SEC="${STATUS_CURL_TIMEOUT_SEC:-6}"
BASE_URL="${BASE_URL:-http://127.0.0.1:9999/cgi-bin/status.cgi}"
# Пусто по умолчанию — и это осознанно. Раньше здесь стоял статический токен
# `session_token=cyntron_session`, выведенный из обращения 2026-07-12: он давно
# ничего не открывал, а откатному сторожу учётные данные и не нужны — он
# проверяет «отвечает ли стек», а не «пускают ли меня внутрь». Кто хочет более
# глубокой проверки под сессией — экспортирует живой токен в COOKIE_HDR.
COOKIE_HDR="${COOKIE_HDR:-}"
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

clear_status_cache() {
    rm -f /tmp/sa02m_status_cache/*.json /tmp/sa02m_status_cache/*.lock 2>/dev/null || true
}

# В репозитории два HTTP-слоя с противоположными идиомами ошибок. Bash-CGI
# отвечает HTTP 200 даже на отказ (`{"error":"unauthorized"}`), поэтому здесь
# `curl -f` не поймает НИЧЕГО — единственное, что может дать сбой, это
# проверка ТЕЛА. Общий дом правила: docs/agent-rules/web-code-rigor.md
# ## Bash CGI floors. До этой правки проба не умела падать вовсе.
probe_status_part() {
    local url=$1 body
    if [ -n "$COOKIE_HDR" ]; then
        body=$(curl -fsS --max-time "$STATUS_CURL_TIMEOUT_SEC" -H "Cookie: ${COOKIE_HDR}" "$url") || return 1
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
    curl -fsS --max-time "$STATUS_CURL_TIMEOUT_SEC" http://127.0.0.1:9999/login.html >/dev/null
    probe_status_part "${BASE_URL}?part=priority"
    probe_status_part "${BASE_URL}?part=main"
    probe_status_part "${BASE_URL}?part=services"
    probe_status_part "${BASE_URL}?part=hardware"
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

write_defaults_if_missing() {
    [ -f "$CONF_FILE" ] && return 0
    cat > "$CONF_FILE" <<'EOF'
SA02M_STATUS_ENABLE_STORAGE=1
SA02M_STATUS_ENABLE_TIME=1
SA02M_STATUS_ENABLE_UPTIME=1
SA02M_STATUS_ENABLE_NETWORK=1
SA02M_STATUS_ENABLE_LOAD=1
SA02M_STATUS_ENABLE_SYSTEM=1
SA02M_STATUS_ENABLE_SERVICES=1
SA02M_STATUS_ENABLE_HARDWARE=1
SA02M_STATUS_ENABLE_RS485=1
EOF
    chmod 644 "$CONF_FILE"
}

set_key_value() {
    local key=$1 value=$2 tmp
    tmp="${CONF_FILE}.tmp.$$"
    awk -F= -v key="$key" -v value="$value" '
        BEGIN { done = 0 }
        $1 == key {
            print key "=" value
            done = 1
            next
        }
        { print }
        END {
            if (!done) {
                print key "=" value
            }
        }
    ' "$CONF_FILE" > "$tmp"
    install -m 644 "$tmp" "$CONF_FILE"
    rm -f "$tmp"
}

write_profile() {
    local profile=$1
    write_defaults_if_missing
    case "$profile" in
        safe)
            set_key_value SA02M_STATUS_ENABLE_STORAGE 0
            set_key_value SA02M_STATUS_ENABLE_TIME 0
            set_key_value SA02M_STATUS_ENABLE_UPTIME 1
            set_key_value SA02M_STATUS_ENABLE_NETWORK 1
            set_key_value SA02M_STATUS_ENABLE_LOAD 1
            set_key_value SA02M_STATUS_ENABLE_SYSTEM 1
            set_key_value SA02M_STATUS_ENABLE_SERVICES 0
            set_key_value SA02M_STATUS_ENABLE_HARDWARE 0
            set_key_value SA02M_STATUS_ENABLE_RS485 0
            ;;
        full)
            set_key_value SA02M_STATUS_ENABLE_STORAGE 1
            set_key_value SA02M_STATUS_ENABLE_TIME 1
            set_key_value SA02M_STATUS_ENABLE_UPTIME 1
            set_key_value SA02M_STATUS_ENABLE_NETWORK 1
            set_key_value SA02M_STATUS_ENABLE_LOAD 1
            set_key_value SA02M_STATUS_ENABLE_SYSTEM 1
            set_key_value SA02M_STATUS_ENABLE_SERVICES 1
            set_key_value SA02M_STATUS_ENABLE_HARDWARE 1
            set_key_value SA02M_STATUS_ENABLE_RS485 1
            ;;
        *)
            echo "Unknown profile: $profile" >&2
            return 1
            ;;
    esac
}

write_armed_state() {
    local token=$1 backup_file=$2 timeout_sec=$3 mode=$4
    cat > "$ARMED_FILE" <<EOF
TOKEN=$token
BACKUP_FILE=$backup_file
TIMEOUT_SEC=$timeout_sec
MODE=$mode
EOF
}

armed_var() {
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

restore_backup() {
    local backup_file=$1
    [ -f "$backup_file" ] || return 1
    install -m 644 "$backup_file" "$CONF_FILE"
    clear_status_cache
    log "Rollback applied from backup=${backup_file}"
}

background_worker() {
    local token=$1 timeout_sec=$2 backup_file=$3
    sleep "$timeout_sec"
    [ -f "$ARMED_FILE" ] || exit 0
    if [ "$(armed_var TOKEN)" != "$token" ]; then
        exit 0
    fi
    restore_backup "$backup_file" || true
    disarm_guard
}

arm_guard() {
    local backup_file=$1 timeout_sec=$2 mode=$3 token worker_pid
    token="$(date +%s).$$"
    write_armed_state "$token" "$backup_file" "$timeout_sec" "$mode"
    nohup "$0" --rollback-worker "$token" "$timeout_sec" "$backup_file" >/dev/null 2>&1 &
    worker_pid=$!
    printf '%s\n' "$worker_pid" > "$PID_FILE"
}

cmd_profile() {
    require_root
    local profile=${1:-safe}
    local timeout_sec=${2:-$DEFAULT_TIMEOUT_SEC}
    local backup_file="${STATE_DIR}/sa02m_status_blocks.$(date +%Y%m%d_%H%M%S).bak"
    write_defaults_if_missing
    cp -f "$CONF_FILE" "$backup_file"
    write_profile "$profile"
    clear_status_cache
    arm_guard "$backup_file" "$timeout_sec" "profile:${profile}"
    log "Guard armed for profile=${profile}, timeout=${timeout_sec}s, backup=${backup_file}"
    if probe_with_retries; then
        log "Profile ${profile} probe succeeded"
        echo "Profile '${profile}' applied. Auto-rollback armed for ${timeout_sec}s."
        exit 0
    fi
    restore_backup "$backup_file" || true
    disarm_guard
    echo "Profile '${profile}' probe failed, rolled back." >&2
    exit 1
}

cmd_set() {
    require_root
    local block=$1 value=$2
    local timeout_sec=${3:-$DEFAULT_TIMEOUT_SEC}
    local key="SA02M_STATUS_ENABLE_${block^^}"
    local backup_file="${STATE_DIR}/sa02m_status_blocks.$(date +%Y%m%d_%H%M%S).bak"
    case "$value" in 0|1) ;; *) echo "Value must be 0 or 1" >&2; exit 1 ;; esac
    write_defaults_if_missing
    cp -f "$CONF_FILE" "$backup_file"
    set_key_value "$key" "$value"
    clear_status_cache
    arm_guard "$backup_file" "$timeout_sec" "set:${block}=${value}"
    log "Guard armed for ${block}=${value}, timeout=${timeout_sec}s, backup=${backup_file}"
    if probe_with_retries; then
        log "Block ${block}=${value} probe succeeded"
        echo "Block '${block}' set to '${value}'. Auto-rollback armed for ${timeout_sec}s."
        exit 0
    fi
    restore_backup "$backup_file" || true
    disarm_guard
    echo "Block '${block}' change failed, rolled back." >&2
    exit 1
}

cmd_confirm() {
    require_root
    [ -f "$ARMED_FILE" ] || { echo "No armed rollback."; exit 0; }
    disarm_guard
    log "Guard confirmed"
    echo "Rollback disarmed."
}

cmd_rollback() {
    require_root
    local backup_file
    backup_file=$(armed_var BACKUP_FILE)
    if [ -n "$backup_file" ] && [ -f "$backup_file" ]; then
        restore_backup "$backup_file" || true
    fi
    disarm_guard
    echo "Rollback applied."
}

cmd_status() {
    write_defaults_if_missing
    echo "config_file=$CONF_FILE"
    sed -n '1,40p' "$CONF_FILE"
    if [ -f "$ARMED_FILE" ]; then
        echo "---"
        cat "$ARMED_FILE"
    fi
}

case "${1:-}" in
    profile)
        shift
        cmd_profile "$@"
        ;;
    set)
        shift
        cmd_set "$@"
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
  sa02m-status-blocks-guard profile [safe|full] [timeout_sec]
  sa02m-status-blocks-guard set <block> <0|1> [timeout_sec]
  sa02m-status-blocks-guard confirm
  sa02m-status-blocks-guard rollback
  sa02m-status-blocks-guard status

Blocks:
  storage time uptime network load system services hardware rs485
EOF
        exit 1
        ;;
esac
