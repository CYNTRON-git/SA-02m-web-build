#!/bin/bash
set -u

CONF_FILE="${CONF_FILE:-/etc/sa02m_failure_monitor.conf}"
STATE_DIR="${STATE_DIR:-/var/lib/sa02m-failure-monitor}"
LOG_FILE="${LOG_FILE:-/var/log/sa02m_failure_monitor.log}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-/run/sa02m_failure_monitor_state.txt}"

CHECK_INTERVAL="${CHECK_INTERVAL:-5}"
HTTP_PROBE_INTERVAL_SEC="${HTTP_PROBE_INTERVAL_SEC:-30}"
HEARTBEAT_INTERVAL_SEC="${HEARTBEAT_INTERVAL_SEC:-300}"
LOG_ROTATE_MAX_BYTES="${LOG_ROTATE_MAX_BYTES:-2097152}"
LOG_ROTATE_KEEP_BYTES="${LOG_ROTATE_KEEP_BYTES:-1048576}"

MONITOR_UNITS="${MONITOR_UNITS:-ssh nginx fcgiwrap mplc4.service}"
PORT_CHECK="${PORT_CHECK:-22}"
# Empty = derived from the panel's real port (resolve_web_port): a board
# installed with `--port N` serves on N, not 9999 (audit A3-04). A value set
# here or in $CONF_FILE still wins.
WEB_PORT="${WEB_PORT:-}"
HTTP_PROBE_URL="${HTTP_PROBE_URL:-}"
STATUS_PROBE_URL="${STATUS_PROBE_URL:-}"
SA02M_WEB_PORT_LIB="${SA02M_WEB_PORT_LIB:-/usr/local/lib/sa02m-web-port-lib.sh}"
# The state snapshot is rewritten when a state changed in the pass, else at
# most this often (H5-01: rewriting it every 5 s cost an ss + ps fork each pass).
SNAPSHOT_MAX_AGE_S="${SNAPSHOT_MAX_AGE_S:-60}"

[ -f "$CONF_FILE" ] && . "$CONF_FILE" 2>/dev/null || true

WEB_PORT_PINNED="$WEB_PORT"
HTTP_PROBE_URL_PINNED="$HTTP_PROBE_URL"
STATUS_PROBE_URL_PINNED="$STATUS_PROBE_URL"
WEB_PORT_SOURCE=""

mkdir -p "$STATE_DIR"

timeout_run() {
    local sec=${1:-2}
    shift || true
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" "$@"
    else
        "$@"
    fi
}

log() {
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    printf '[%s] %s\n' "$ts" "$*" >> "$LOG_FILE" 2>/dev/null || true
    printf '[%s] %s\n' "$ts" "$*"
}

# The panel's port: pinned value, else the installer's port library (sourced in
# a subshell), else 9999. Re-run on every heartbeat; logs only on a change.
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
    STATUS_PROBE_URL=${STATUS_PROBE_URL_PINNED:-http://127.0.0.1:${WEB_PORT}/cgi-bin/status.cgi?part=priority}
}

trim_log_if_needed() {
    local size tmp
    [ -f "$LOG_FILE" ] || return 0
    size=$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)
    [ "$size" -lt "$LOG_ROTATE_MAX_BYTES" ] && return 0
    tmp="${LOG_FILE}.tmp.$$"
    tail -c "$LOG_ROTATE_KEEP_BYTES" "$LOG_FILE" > "$tmp" 2>/dev/null || return 0
    mv "$tmp" "$LOG_FILE" 2>/dev/null || rm -f "$tmp"
    log "log trimmed to ${LOG_ROTATE_KEEP_BYTES} bytes"
}

state_key_path() {
    local raw=${1//[^A-Za-z0-9_.-]/_}
    printf '%s/%s.state\n' "$STATE_DIR" "$raw"
}

read_state() {
    local path
    path=$(state_key_path "$1")
    [ -f "$path" ] && tr -d '\r' < "$path" 2>/dev/null || true
}

write_state() {
    local path
    path=$(state_key_path "$1")
    printf '%s\n' "$2" > "$path"
}

unit_state() {
    case "$1" in
        ssh|ssh.service)
            if command -v ss >/dev/null 2>&1 && ss -H -ltn "sport = :22" 2>/dev/null | awk 'NR==1{found=1} END{exit(found?0:1)}'; then
                echo active
            elif command -v pgrep >/dev/null 2>&1 && pgrep -x sshd >/dev/null 2>&1; then
                echo active
            else
                echo inactive
            fi
            ;;
        nginx|nginx.service)
            if command -v pgrep >/dev/null 2>&1 && pgrep -x nginx >/dev/null 2>&1; then
                echo active
            else
                echo inactive
            fi
            ;;
        fcgiwrap|fcgiwrap.service)
            if command -v pgrep >/dev/null 2>&1 && pgrep -x fcgiwrap >/dev/null 2>&1; then
                echo active
            elif [ -S /run/fcgiwrap/fcgiwrap.socket ] || [ -S /run/fcgiwrap.socket ]; then
                echo active
            else
                echo inactive
            fi
            ;;
        mplc|mplc.service|mplc4|mplc4.service)
            # One pgrep for the three process names (pgrep -x anchors the ERE).
            if command -v pgrep >/dev/null 2>&1 && pgrep -x 'mplc|mplc4|mplc_monitor' >/dev/null 2>&1; then
                echo active
            else
                echo inactive
            fi
            ;;
        *)
            timeout_run 15 systemctl is-active "$1" 2>/dev/null || echo unknown
            ;;
    esac
}

port_state() {
    if ! command -v ss >/dev/null 2>&1; then
        echo unknown
        return 0
    fi
    if ss -H -ltn "sport = :${PORT_CHECK}" 2>/dev/null | awk 'NR==1{found=1} END{exit(found?0:1)}'; then
        echo listening
    else
        echo missing
    fi
}

probe_state() {
    local url=$1
    if ! command -v curl >/dev/null 2>&1; then
        echo no-curl
        return 0
    fi
    if curl -fsS --max-time 4 "$url" >/dev/null 2>&1; then
        echo ok
    else
        echo fail
    fi
}

mem_available_kb() {
    awk '/MemAvailable:/ { print $2; exit }' /proc/meminfo 2>/dev/null || echo 0
}

load_short() {
    awk '{print $1" "$2" "$3}' /proc/loadavg 2>/dev/null || echo "n/a"
}

dump_system_snapshot() {
    local reason=$1
    log "system snapshot reason=${reason} load=$(load_short) mem_avail_kb=$(mem_available_kb)"
    {
        echo "--- ps top cpu ---"
        ps -eo pid,ppid,stat,comm,%cpu,%mem --sort=-%cpu 2>/dev/null | head -n 15 || true
        echo "--- ss listeners ---"
        ss -ltnp 2>/dev/null || true
        echo "--- sshd processes ---"
        ps -eo pid,ppid,stat,comm,%cpu,%mem,args 2>/dev/null | awk 'NR==1 || /[[:space:]]sshd([[:space:]]|$)/' || true
        echo "--- unit states ---"
        for unit in $MONITOR_UNITS; do
            printf '%s=%s\n' "$unit" "$(unit_state "$unit")"
        done
    } >> "$LOG_FILE"
}

# Unit states and the port-22 state read in THIS pass, reused by the snapshot
# instead of a second round of probes (H5-01).
declare -A _pass_unit_state
_pass_port22="unknown"
_pass_changed=0

write_snapshot_file() {
    local tmp
    tmp="${SNAPSHOT_FILE}.tmp.$$"
    {
        echo "generated_at=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo unknown)"
        echo "load=$(load_short)"
        echo "mem_available_kb=$(mem_available_kb)"
        echo "port22=${_pass_port22}"
        printf 'login_http=%s\n' "$_probe_login_http"
        printf 'status_priority=%s\n' "$_probe_status_priority"
        echo "--- units ---"
        for unit in $MONITOR_UNITS; do
            printf '%s=%s\n' "$unit" "${_pass_unit_state[$unit]:-$(unit_state "$unit")}"
        done
        echo "--- ssh listeners ---"
        ss -ltnp 2>/dev/null | awk 'NR==1 || /:22[[:space:]]/' || true
        echo "--- sshd processes ---"
        ps -eo pid,ppid,stat,comm,%cpu,%mem,args 2>/dev/null | awk 'NR==1 || /[[:space:]]sshd([[:space:]]|$)/' || true
        echo "--- monitor tail ---"
        tail -n 25 "$LOG_FILE" 2>/dev/null || true
    } > "$tmp"
    mv "$tmp" "$SNAPSHOT_FILE" 2>/dev/null || rm -f "$tmp"
}

check_unit() {
    local unit=$1 key prev cur
    key="unit:${unit}"
    prev=$(read_state "$key")
    cur=$(unit_state "$unit")
    _pass_unit_state[$unit]=$cur
    [ "$cur" = "$prev" ] && return 0
    _pass_changed=1
    write_state "$key" "$cur"
    log "unit ${unit}: ${prev:-unset} -> ${cur}"
    if [ "$cur" != "active" ]; then
        dump_system_snapshot "unit:${unit}:${cur}"
    fi
}

check_named_probe() {
    local name=$1 cur prev
    shift
    prev=$(read_state "probe:${name}")
    cur=$("$@")
    check_named_probe_cached "$name" "$cur"
}

# Same alert logic as check_named_probe but on an already-computed value — lets
# the caller probe ONCE per cadence and reuse the result (snapshot + alert)
# instead of forking the CGI probe twice per loop (Y8).
check_named_probe_cached() {
    local name=$1 cur=$2 prev
    prev=$(read_state "probe:${name}")
    [ "$cur" = "$prev" ] && return 0
    _pass_changed=1
    write_state "probe:${name}" "$cur"
    log "probe ${name}: ${prev:-unset} -> ${cur}"
    case "$cur" in
        ok|listening) ;;
        *)
            dump_system_snapshot "probe:${name}:${cur}"
            ;;
    esac
}

# Last HTTP-probe results, refreshed on the HTTP_PROBE_INTERVAL_SEC cadence and
# reused by the snapshot writer so it does not re-fork the CGI probe every loop.
_probe_login_http="unknown"
_probe_status_priority="unknown"

heartbeat() {
    local summary unit
    summary=""
    for unit in $MONITOR_UNITS; do
        summary="${summary}${unit}=$(unit_state "$unit") "
    done
    log "heartbeat load=$(load_short) mem_avail_kb=$(mem_available_kb) port22=$(port_state) ${summary}"
}

main() {
    local last_heartbeat=0 last_http_probe=0 last_snapshot=0 now unit loop_count=0
    log "failure monitor started; units=${MONITOR_UNITS}; interval=${CHECK_INTERVAL}s"
    resolve_web_port

    while true; do
        now=$(date +%s)
        _pass_changed=0
        _pass_unit_state=()

        for unit in $MONITOR_UNITS; do
            check_unit "$unit"
        done

        _pass_port22=$(port_state)
        check_named_probe_cached ssh-port "$_pass_port22"
        # HTTP probes fork a Bash CGI (+ its sudo/i2c reads) — far heavier than
        # the systemctl unit-state checks. Run them on a longer cadence
        # (HTTP_PROBE_INTERVAL_SEC, default 30 s) instead of every 5 s loop.
        if [ "$last_http_probe" -eq 0 ] || [ $(( now - last_http_probe )) -ge "$HTTP_PROBE_INTERVAL_SEC" ]; then
            _probe_login_http=$(probe_state "$HTTP_PROBE_URL")
            _probe_status_priority=$(probe_state "$STATUS_PROBE_URL")
            check_named_probe_cached login-http "$_probe_login_http"
            check_named_probe_cached status-priority "$_probe_status_priority"
            last_http_probe=$now
        fi

        if [ "$last_heartbeat" -eq 0 ] || [ $(( now - last_heartbeat )) -ge "$HEARTBEAT_INTERVAL_SEC" ]; then
            heartbeat
            last_heartbeat=$now
            resolve_web_port
        fi

        loop_count=$(( loop_count + 1 ))
        if [ $(( loop_count % 12 )) -eq 0 ]; then
            trim_log_if_needed
        fi

        if [ "$_pass_changed" = 1 ] || [ "$last_snapshot" -eq 0 ] \
           || [ $(( now - last_snapshot )) -ge "$SNAPSHOT_MAX_AGE_S" ]; then
            write_snapshot_file
            last_snapshot=$now
        fi

        sleep "$CHECK_INTERVAL"
    done
}

main "$@"
