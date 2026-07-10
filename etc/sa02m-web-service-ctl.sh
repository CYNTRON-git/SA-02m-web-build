#!/bin/sh
# Управление прикладными службами SA-02m из веб-интерфейса (www-data → sudo).
# stop: stop + disable + mask — не стартует после перезагрузки до ручного включения.
# start: unmask + enable + start
# Usage: sa02m-web-service-ctl.sh list | stop <id> | start <id>

SC=/usr/bin/systemctl
[ -x "$SC" ] || SC=/bin/systemctl
LOG=/var/log/sa02m_install.log
RESULT_DIR=/var/run/sa02m-svcctl
TIMEOUT_SEC=8
# SysV-synced units (mplc4, codesyscontrol) need 12–20s for enable/disable.
TIMEOUT_SLOW_SEC=45

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g; :a;N;$!ba;s/\n/ /g'
}

sc_run() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "$TIMEOUT_SEC" "$SC" "$@" 2>/dev/null
    else
        "$SC" "$@" 2>/dev/null
    fi
}

sc_run_slow() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "$TIMEOUT_SLOW_SEC" "$SC" "$@" 2>/dev/null
    else
        "$SC" "$@" 2>/dev/null
    fi
}

emit_result() {
    _json=$1
    printf '%s\n' "$_json"
    if [ -n "${SVC_RESULT_FILE:-}" ]; then
        mkdir -p "$RESULT_DIR" 2>/dev/null || true
        printf '%s\n' "$_json" >"$SVC_RESULT_FILE" 2>/dev/null || true
        chmod 644 "$SVC_RESULT_FILE" 2>/dev/null || true
    fi
}

# True, если sa02m-flasher выполняет scan/flash (GET /status → busy).
flasher_poll_lock_held() {
    _sock=/run/sa02m-flasher/flasher.sock
    [ -S "$_sock" ] || return 1
    if ! command -v curl >/dev/null 2>&1; then
        return 1
    fi
    _it=$(tr -d '[:space:]' < /etc/sa02m-web-internal-token 2>/dev/null || true)
    _resp=$(curl -sS --max-time 2 --unix-socket "$_sock" \
        -H "X-SA02M-Auth: ${_it}" \
        http://localhost/status 2>/dev/null) || return 1
    case "$_resp" in
        *'"busy":true'*|*'"busy": true'*) return 0 ;;
    esac
    return 1
}

flasher_blocks_com_pollers() {
    flasher_poll_lock_held
}

unit_can_mask() {
    _u=$1
    _path=$(sc_run show -p FragmentPath --value "$_u" 2>/dev/null | head -n1 | tr -d '\r')
    case "$_path" in
        /etc/systemd/system/*)
            [ -f "$_path" ] && [ ! -L "$_path" ] && return 1
            ;;
    esac
    return 0
}

unit_load_state() {
    sc_run show -p LoadState --value "$1" | head -n1 | tr -d '\r'
}

unit_exists() {
    local st
    st=$(unit_load_state "$1")
    case "$st" in not-found|"") return 1 ;; esac
    return 0
}

# Unit-файл на диске (пакет установлен), а не только LoadState в systemd.
unit_file_installed() {
    local bn=$1
    case "$bn" in
        *.service|*.socket) ;;
        *) bn="${bn}.service" ;;
    esac
    local d
    for d in /lib/systemd/system /usr/lib/systemd/system /etc/systemd/system; do
        [ -f "$d/$bn" ] && return 0
    done
    return 1
}

# Служба считается установленной: unit-файл, init.d, бинарник или dpkg.
service_present() {
    _sid=$1
    _cands=$2
    case "$_sid" in
        codesys)
            [ -x /etc/init.d/codesyscontrol ] && return 0
            ;;
        docker)
            command -v docker >/dev/null 2>&1 && return 0
            ;;
        mplc4)
            unit_file_installed mplc4.service && return 0
            unit_file_installed mplc.service && return 0
            return 1
            ;;
        mosquitto)
            command -v mosquitto >/dev/null 2>&1 && return 0
            dpkg -s mosquitto >/dev/null 2>&1 && return 0
            ;;
        mqtt-bridge)
            [ -x /opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py ] && return 0
            ;;
        mqtt-telemetry)
            [ -x /opt/sa02m-modbus-mqtt/sa02m_telemetry.py ] && return 0
            ;;
    esac
    _old_ifs=$IFS
    IFS=,
    for _u in $_cands; do
        IFS=$_old_ifs
        case "$_u" in *.service|*.socket) ;; *) _u="${_u}.service" ;; esac
        unit_file_installed "$_u" && return 0
    done
    IFS=$_old_ifs
    return 1
}

codesys_process_active() {
    pgrep -f '[c]odesyscontrol\.bin' >/dev/null 2>&1
}

codesys_rc_disable() {
    command -v update-rc.d >/dev/null 2>&1 || return 0
    update-rc.d codesyscontrol disable >>"$LOG" 2>&1 || true
}

codesys_rc_enable() {
    command -v update-rc.d >/dev/null 2>&1 || return 0
    update-rc.d codesyscontrol defaults >>"$LOG" 2>&1 || true
}

codesys_rc_autostart() {
    local d link
    for d in /etc/rc2.d /etc/rc3.d /etc/rc4.d /etc/rc5.d; do
        [ -d "$d" ] || continue
        for link in "$d"/S*codesyscontrol; do
            [ -e "$link" ] || continue
            return 0
        done
    done
    return 1
}

mplc4_rc_disable() {
    command -v update-rc.d >/dev/null 2>&1 || return 0
    update-rc.d mplc4 disable >>"$LOG" 2>&1 || true
}

mplc4_rc_enable() {
    command -v update-rc.d >/dev/null 2>&1 || return 0
    update-rc.d mplc4 defaults >>"$LOG" 2>&1 || true
}

mplc4_process_active() {
    pgrep -f '[m]plc.*\.bin\|[M]asterPLC\|[m]asterplc' >/dev/null 2>&1
}

service_runtime_active() {
    _sid=$1
    _u=$2
    case "$_sid" in
        codesys) codesys_process_active ;;
        mplc4)
            if [ -n "$_u" ]; then
                _a=$(sc_run is-active "$_u" 2>/dev/null | head -n1 | tr -d '\r')
                case "$_a" in active|activating) return 0 ;; esac
            fi
            mplc4_process_active
            ;;
        *)
            [ -n "$_u" ] || return 1
            _a=$(sc_run is-active "$_u" 2>/dev/null | head -n1 | tr -d '\r')
            case "$_a" in active|activating) return 0 ;; esac
            return 1
            ;;
    esac
}

service_admin_off() {
    _sid=$1
    _u=$2
    if [ -n "$_u" ] && unit_admin_disabled "$_u"; then
        return 0
    fi
    case "$_sid" in
        codesys) codesys_rc_autostart || return 0; return 1 ;;
        mplc4)
            if [ -n "$_u" ] && unit_admin_disabled "$_u"; then
                return 0
            fi
            return 1
            ;;
    esac
    return 1
}

service_admin_on() {
    _sid=$1
    _u=$2
    if [ -n "$_u" ] && unit_admin_enabled "$_u"; then
        return 0
    fi
    case "$_sid" in
        codesys) codesys_rc_autostart ;;
        mplc4) [ -n "$_u" ] && unit_admin_enabled "$_u" ;;
        *) return 1 ;;
    esac
}

unit_admin_disabled() {
    _u=$1
    [ -n "$_u" ] || return 0
    _en=$(sc_run is-enabled "$_u" 2>/dev/null | head -n1 | tr -d '\r')
    case "$_en" in
        disabled|masked) return 0 ;;
    esac
    return 1
}

unit_admin_enabled() {
    _u=$1
    [ -n "$_u" ] || return 0
    _en=$(sc_run is-enabled "$_u" 2>/dev/null | head -n1 | tr -d '\r')
    case "$_en" in
        enabled|enabled-runtime) return 0 ;;
    esac
    return 1
}

# id | UI label | candidate units (first existing wins)
SERVICE_DEFS=$(cat <<'SVC_DEFS'
docker|Docker|docker.service
codesys|CODESYS|codesyscontrol.service,codesys.service,CODESYSControl.service,CODESYSControlRuntime.service
mplc4|MPLC4|mplc4.service
mosquitto|Mosquitto|mosquitto.service
mqtt-bridge|MQTT мост|sa02m-modbus-mqtt.service
mqtt-telemetry|MQTT телеметрия|sa02m-telemetry.service
node-red|Node-RED|node-red.service,nodered.service
klogic|KLogic|klogicd.service,klogic.service
SVC_DEFS
)

resolve_unit_for_id() {
    _want_id=$1
    _found_unit=""
    while IFS='|' read -r _id _label _cands; do
        [ -z "$_id" ] && continue
        [ "$_id" != "$_want_id" ] && continue
        if ! service_present "$_id" "$_cands"; then
            return 1
        fi
        _old_ifs=$IFS
        IFS=,
        for _u in $_cands; do
            IFS=$_old_ifs
            case "$_u" in *.service) ;; *) _u="${_u}.service" ;; esac
            if unit_exists "$_u"; then
                _found_unit=$_u
                return 0
            fi
        done
        IFS=$_old_ifs
        IFS=,
        for _u in $_cands; do
            IFS=$_old_ifs
            case "$_u" in *.service) ;; *) _u="${_u}.service" ;; esac
            if unit_file_installed "$_u"; then
                _found_unit=$_u
                return 0
            fi
        done
        IFS=$_old_ifs
        if [ "$_id" = "codesys" ] && [ -x /etc/init.d/codesyscontrol ]; then
            if [ -z "$_found_unit" ] && unit_file_installed codesyscontrol.service; then
                _found_unit=codesyscontrol.service
            fi
            return 0
        fi
        return 1
    done <<EOF
$SERVICE_DEFS
EOF
    return 1
}

label_for_id() {
    _want_id=$1
    while IFS='|' read -r _id _label _cands; do
        [ "$_id" = "$_want_id" ] && printf '%s' "$_label" && return 0
    done <<EOF
$SERVICE_DEFS
EOF
    return 1
}

unit_props() {
    _u=$1
    _active=$(sc_run show -p ActiveState --value "$_u" | head -n1 | tr -d '\r')
    _en_raw=$(sc_run is-enabled "$_u" 2>/dev/null | head -n1 | tr -d '\r')
    _load=$(unit_load_state "$_u")
    _is_masked=0
    case "$_en_raw" in masked) _is_masked=1 ;; esac
    case "$_load" in masked) _is_masked=1 ;; esac
    [ -z "$_active" ] && _active=inactive
    _enabled=disabled
    case "$_en_raw" in enabled|enabled-runtime) _enabled=enabled ;; esac
    _admin_off=0
    if [ "$_is_masked" = 1 ]; then
        _admin_off=1
    elif [ "$_enabled" != enabled ]; then
        _admin_off=1
    fi
    printf '%s|%s|%s|%s' "$_active" "$_enabled" "$_is_masked" "$_admin_off"
}

cmd_list() {
    parts="" sep=""
    while IFS='|' read -r _id _label _cands; do
        [ -z "$_id" ] && continue
        if ! service_present "$_id" "$_cands"; then
            continue
        fi
        _unit=""
        _old_ifs=$IFS
        IFS=,
        for _u in $_cands; do
            IFS=$_old_ifs
            case "$_u" in *.service) ;; *) _u="${_u}.service" ;; esac
            if unit_exists "$_u"; then
                _unit=$_u
                break
            fi
        done
        if [ -z "$_unit" ]; then
            IFS=,
            for _u in $_cands; do
                IFS=$_old_ifs
                case "$_u" in *.service) ;; *) _u="${_u}.service" ;; esac
                if unit_file_installed "$_u"; then
                    _unit=$_u
                    break
                fi
            done
        fi
        IFS=$_old_ifs
        if [ -z "$_unit" ] && [ "$_id" = "codesys" ] && [ -x /etc/init.d/codesyscontrol ]; then
            _unit="init.d"
            _active=inactive
            _enabled=disabled
            _masked=0
            _admin_off=1
            if codesys_process_active; then
                _active=active
            fi
            if codesys_rc_autostart; then
                _admin_off=0
                _enabled=enabled
            fi
        fi
        [ -z "$_unit" ] && continue
        if [ "$_unit" != "init.d" ]; then
        IFS='|' read -r _active _enabled _masked _admin_off <<EOF
$(unit_props "$_unit")
EOF
        fi
        if [ "$_id" = "codesys" ]; then
            if codesys_process_active; then
                _active=active
                _admin_off=0
            elif [ "$_admin_off" = 1 ]; then
                _active=inactive
            fi
        fi
        _id_e=$(json_escape "$_id")
        _label_e=$(json_escape "$_label")
        _unit_e=$(json_escape "$_unit")
        _active_e=$(json_escape "$_active")
        parts="${parts}${sep}{\"id\":\"${_id_e}\",\"label\":\"${_label_e}\",\"unit\":\"${_unit_e}\",\"active\":\"${_active_e}\",\"enabled\":$([ "$_enabled" = enabled ] && echo true || echo false),\"masked\":$([ "$_masked" = 1 ] && echo true || echo false),\"user_disabled\":$([ "$_admin_off" = 1 ] && echo true || echo false),\"installed\":true}"
        sep=,
    done <<EOF
$SERVICE_DEFS
EOF
    _flasher_busy=false
    if flasher_poll_lock_held; then
        _flasher_busy=true
    fi
    printf '{"ok":true,"flasher_busy":%s,"services":[%s]}\n' "$([ "$_flasher_busy" = true ] && echo true || echo false)" "$parts"
}

validate_id() {
    case "$1" in
        ''|*[!a-zA-Z0-9_-]*) return 1 ;;
    esac
    return 0
}

cmd_stop() {
    _id=$1
    validate_id "$_id" || { emit_result '{"ok":false,"error":"invalid_id"}'; return 1; }
    if ! resolve_unit_for_id "$_id"; then
        emit_result '{"ok":false,"error":"unknown_service"}'
        return 1
    fi
    _u=$_found_unit
    SVC_RESULT_FILE="${RESULT_DIR}/${_id}.json"
    mkdir -p "$RESULT_DIR" 2>/dev/null || true
    printf '{"ok":true,"pending":true,"id":"%s","action":"stop"}\n' "$_id" >"$SVC_RESULT_FILE" 2>/dev/null || true
    chmod 644 "$SVC_RESULT_FILE" 2>/dev/null || true
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: stop ${_id} (${_u})" >>"$LOG" 2>&1
    if [ "$_id" = "codesys" ] && [ -x /etc/init.d/codesyscontrol ]; then
        /etc/init.d/codesyscontrol stop >>"$LOG" 2>&1 || true
        codesys_rc_disable
    fi
    if [ "$_id" = "mplc4" ] && [ -x /etc/init.d/mplc4 ]; then
        /etc/init.d/mplc4 stop >>"$LOG" 2>&1 || true
        mplc4_rc_disable
    fi
    if [ -n "$_u" ]; then
        sc_run_slow stop "$_u" >>"$LOG" 2>&1 || true
        sc_run_slow disable "$_u" >>"$LOG" 2>&1 || true
        if unit_can_mask "$_u"; then
            sc_run mask "$_u" >>"$LOG" 2>&1 || true
        else
            echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: skip mask ${_u} (static unit file)" >>"$LOG" 2>&1
        fi
        sc_run daemon-reload >>"$LOG" 2>&1 || true
    fi
    if [ "$_id" = "codesys" ] && codesys_process_active; then
        pkill -f '[c]odesyscontrol\.bin' >>"$LOG" 2>&1 || true
        sleep 1
    fi
    if service_runtime_active "$_id" "$_u"; then
        emit_result "$(printf '{"ok":false,"error":"still_running","id":"%s"}' "$_id")"
        return 1
    fi
    if ! service_admin_off "$_id" "$_u"; then
        if [ -n "$_u" ]; then
            sc_run_slow disable "$_u" >>"$LOG" 2>&1 || true
            sc_run daemon-reload >>"$LOG" 2>&1 || true
        fi
        if [ "$_id" = "codesys" ]; then
            codesys_rc_disable
        fi
        if [ "$_id" = "mplc4" ]; then
            mplc4_rc_disable
        fi
    fi
    if ! service_admin_off "$_id" "$_u"; then
        emit_result "$(printf '{"ok":false,"error":"disable_failed","id":"%s"}' "$_id")"
        return 1
    fi
    emit_result "$(printf '{"ok":true,"id":"%s","action":"stop"}' "$_id")"
}

cmd_start() {
    _id=$1
    validate_id "$_id" || { emit_result '{"ok":false,"error":"invalid_id"}'; return 1; }
    if ! resolve_unit_for_id "$_id"; then
        emit_result '{"ok":false,"error":"unknown_service"}'
        return 1
    fi
    case "$_id" in
        mplc4|mqtt-bridge)
            if flasher_blocks_com_pollers; then
                emit_result '{"ok":false,"error":"flasher_busy"}'
                return 1
            fi
            ;;
    esac
    _u=$_found_unit
    SVC_RESULT_FILE="${RESULT_DIR}/${_id}.json"
    mkdir -p "$RESULT_DIR" 2>/dev/null || true
    printf '{"ok":true,"pending":true,"id":"%s","action":"start"}\n' "$_id" >"$SVC_RESULT_FILE" 2>/dev/null || true
    chmod 644 "$SVC_RESULT_FILE" 2>/dev/null || true
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: start ${_id} (${_u:-init.d})" >>"$LOG" 2>&1
    if [ "$_id" = "codesys" ]; then
        codesys_rc_enable
    fi
    if [ "$_id" = "mplc4" ]; then
        mplc4_rc_enable
    fi
    if [ -n "$_u" ]; then
        sc_run_slow unmask "$_u" >>"$LOG" 2>&1 || true
        sc_run_slow enable "$_u" >>"$LOG" 2>&1 || true
        sc_run_slow start "$_u" >>"$LOG" 2>&1 || true
        sc_run daemon-reload >>"$LOG" 2>&1 || true
    fi
    if [ "$_id" = "codesys" ] && [ -x /etc/init.d/codesyscontrol ]; then
        if ! codesys_process_active; then
            /etc/init.d/codesyscontrol start >>"$LOG" 2>&1 || true
        fi
    fi
    if [ "$_id" = "mplc4" ] && [ -x /etc/init.d/mplc4 ]; then
        if ! service_runtime_active mplc4 "$_u"; then
            /etc/init.d/mplc4 start >>"$LOG" 2>&1 || true
        fi
    fi
    if ! service_runtime_active "$_id" "$_u"; then
        emit_result "$(printf '{"ok":false,"error":"start_failed","id":"%s"}' "$_id")"
        return 1
    fi
    if ! service_admin_on "$_id" "$_u"; then
        if [ -n "$_u" ]; then
            sc_run_slow enable "$_u" >>"$LOG" 2>&1 || true
            sc_run daemon-reload >>"$LOG" 2>&1 || true
        fi
        if [ "$_id" = "codesys" ]; then
            codesys_rc_enable
        fi
        if [ "$_id" = "mplc4" ]; then
            mplc4_rc_enable
        fi
    fi
    if ! service_admin_on "$_id" "$_u"; then
        emit_result "$(printf '{"ok":false,"error":"enable_failed","id":"%s"}' "$_id")"
        return 1
    fi
    emit_result "$(printf '{"ok":true,"id":"%s","action":"start"}' "$_id")"
}

ACTION=${1:-}
ID=${2:-}

case "$ACTION" in
    list)
        cmd_list
        ;;
    stop)
        [ -n "$ID" ] || { printf '{"ok":false,"error":"missing_id"}\n'; exit 1; }
        cmd_stop "$ID"
        ;;
    start)
        [ -n "$ID" ] || { printf '{"ok":false,"error":"missing_id"}\n'; exit 1; }
        cmd_start "$ID"
        ;;
    *)
        printf '{"ok":false,"error":"bad_action"}\n'
        exit 1
        ;;
esac
