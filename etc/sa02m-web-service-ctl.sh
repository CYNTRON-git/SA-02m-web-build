#!/bin/sh
# Управление прикладными службами SA-02m из веб-интерфейса (www-data → sudo).
# stop: stop + disable + mask — не стартует после перезагрузки до ручного включения.
# start: unmask + enable + start
# install/uninstall (codesys|mplc4|node-red only): full runtime install / full
#   clean removal (purge package + wipe data), preserving /opt/vendor-installers.
# Usage: sa02m-web-service-ctl.sh list | stop <id> | start <id>
#        | install <id> | uninstall <id>

SC=/usr/bin/systemctl
[ -x "$SC" ] || SC=/bin/systemctl
LOG=/var/log/sa02m_install.log
RESULT_DIR=/var/run/sa02m-svcctl
TIMEOUT_SEC=8
# SysV-synced units (mplc4, codesyscontrol) need 12–20s for enable/disable.
TIMEOUT_SLOW_SEC=45

# Canonical home: www/network_config/cgi-bin/lib_web_json.sh. This script
# deploys to /etc (a different directory from the cgi-bin libs) so it keeps a
# byte-identical local copy rather than a cross-dir source; the two MUST stay
# in sync (web-code-rigor.md ## Bash CGI floors).
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
    _resp=$(curl -sS --max-time 2 --unix-socket "$_sock" \
        -H 'Cookie: session_token=cyntron_session' \
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
            [ -x /etc/init.d/mplc4 ] && return 0
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

# Kernel-conditional service policy (docs/contracts/kernel-conditional-
# services.md): start = runtime only — codesys_rc_enable is deliberately NOT
# called from the start/install paths any more (it re-armed the SysV autostart
# the policy disables). Kept for a future explicit autostart toggle; the
# kernel-policy-contract quality gate pins the call sites.
codesys_rc_enable() {
    command -v update-rc.d >/dev/null 2>&1 || return 0
    update-rc.d codesyscontrol defaults >>"$LOG" 2>&1 || true
}

# Licensing companion for a manual CODESYS start: the policy keeps CodeMeter
# autostart off, and without the daemon CODESYS silently falls back to demo
# mode. Runtime-only start, no enable.
codemeter_runtime_start() {
    [ -x /etc/init.d/codemeter ] || return 0
    if ! pgrep -f '[C]odeMeter' >/dev/null 2>&1; then
        /etc/init.d/codemeter start >>"$LOG" 2>&1 || true
    fi
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

mplc4_rc_autostart() {
    local d link
    for d in /etc/rc2.d /etc/rc3.d /etc/rc4.d /etc/rc5.d; do
        [ -d "$d" ] || continue
        for link in "$d"/S*mplc4; do
            [ -e "$link" ] || continue
            return 0
        done
    done
    return 1
}

mplc4_process_active() {
    # Align with status.cgi fast_service_state: comm names first, then vendor
    # binary patterns. Never trust systemctl ActiveState alone — mplc4.service
    # is Type=oneshot RemainAfterExit=yes and stays "active (exited)" with no
    # runtime process (dashboard then shows «<1м»).
    pgrep -x mplc >/dev/null 2>&1 \
        || pgrep -x mplc4 >/dev/null 2>&1 \
        || pgrep -x mplc_monitor >/dev/null 2>&1 \
        || pgrep -f '[m]plc.*\.bin\|[M]asterPLC\|[m]asterplc' >/dev/null 2>&1
}

service_runtime_active() {
    _sid=$1
    _u=$2
    case "$_sid" in
        codesys) codesys_process_active ;;
        mplc4) mplc4_process_active ;;
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
            [ -z "$_u" ] && { mplc4_rc_autostart || return 0; return 1; }
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
        mplc4)
            if [ -n "$_u" ]; then unit_admin_enabled "$_u"; else mplc4_rc_autostart; fi
            ;;
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
        if [ "$_id" = "mplc4" ] && [ -x /etc/init.d/mplc4 ]; then
            if [ -z "$_found_unit" ] && unit_file_installed mplc4.service; then
                _found_unit=mplc4.service
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

# Derive the emitted status tuple from three raw systemd values. Split out of
# unit_props so cmd_list can feed values from ONE batched `systemctl show`
# (instead of forking `show` per unit) while keeping byte-identical output.
# $1=ActiveState, $2=is-enabled state, $3=LoadState → active|enabled|masked|admin_off
derive_props() {
    _active=$1
    _en_raw=$2
    _load=$3
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

unit_props() {
    _u=$1
    _active=$(sc_run show -p ActiveState --value "$_u" | head -n1 | tr -d '\r')
    _en_raw=$(sc_run is-enabled "$_u" 2>/dev/null | head -n1 | tr -d '\r')
    _load=$(unit_load_state "$_u")
    derive_props "$_active" "$_en_raw" "$_load"
}

# Fetch every resolved unit's status in TWO batched systemctl calls instead of
# ~3 forks per service (was ~4 s on the ARM target). Pass 1 resolves units with
# the exact prior logic (unchanged — same unit selected). Then, over the full
# resolved-unit list: ONE `systemctl show` for ActiveState+LoadState (keyed by
# unit Id) and ONE `systemctl is-enabled` for every unit (it accepts many units
# and prints one status line per unit IN INPUT ORDER — this IS is-enabled, exact
# enabled/masked/static/… semantics, just batched, unlike swapping in
# UnitFileState). Pass 2 derives each service's status from the two lookups. Any
# unit missing from either batch (older systemd, short/garbled output, a batch
# error) falls back to the original per-unit path for that unit — so the emitted
# JSON stays byte-identical to the per-unit implementation for every state.
cmd_list() {
    # ── Pass 1: resolve the unit for every present service (prior logic) ──
    _rows=""
    _batch_units=""
    while IFS='|' read -r _id _label _cands; do
        [ -z "$_id" ] && continue
        if ! service_present "$_id" "$_cands"; then
            # Absent but installable (codesys|mplc4|node-red) → emit a ghost row
            # with the __absent__ sentinel unit. It is NOT added to the batched
            # systemctl probes (there is nothing to probe); Pass 2 emits a fixed
            # installed:false tuple for it. Non-installable absent services keep
            # the original `continue` (never shown).
            if svc_is_installable "$_id"; then
                _rows="${_rows}${_id}|${_label}|__absent__
"
            fi
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
        fi
        if [ -z "$_unit" ] && [ "$_id" = "mplc4" ] && [ -x /etc/init.d/mplc4 ]; then
            _unit="init.d"
        fi
        [ -z "$_unit" ] && continue
        _rows="${_rows}${_id}|${_label}|${_unit}
"
        [ "$_unit" != "init.d" ] && _batch_units="${_batch_units} ${_unit}"
    done <<EOF
$SERVICE_DEFS
EOF

    # ── Batch: one `systemctl show` for every resolved real unit ──
    # Records are blank-line-separated, one per unit; awk (no paragraph-mode
    # RS, so busybox/mawk/gawk-safe) flattens each to `Id|ActiveState|LoadState`.
    _batch_props=""
    if [ -n "$_batch_units" ]; then
        _batch_props=$(sc_run show --property=Id,ActiveState,LoadState $_batch_units 2>/dev/null | awk '
            { sub(/\r$/, "") }
            /^$/ { if (id != "") print id "|" act "|" load; id=""; act=""; load=""; next }
            /^Id=/ { id = substr($0, 4) }
            /^ActiveState=/ { act = substr($0, 13) }
            /^LoadState=/ { load = substr($0, 11) }
            END { if (id != "") print id "|" act "|" load }
        ')
    fi

    # ── Batch: one `systemctl is-enabled` for every resolved real unit ──
    # is-enabled accepts multiple units and prints one status line per unit in
    # the order given; awk pairs those lines positionally with $_batch_units
    # into a unit→state lookup. is-enabled's exit code is non-zero for
    # disabled/static units even on success, so stdout is captured regardless
    # (2>/dev/null, no exit-code check — mirroring the per-unit call). If the
    # line count does not match the unit count (short/garbled batch), the map is
    # left empty and pass 2 falls back to per-unit is-enabled for every unit.
    _enabled_map=""
    if [ -n "$_batch_units" ]; then
        _enabled_map=$(sc_run is-enabled $_batch_units 2>/dev/null | awk -v units="$_batch_units" '
            BEGIN { n = split(units, u, " ") }
            { gsub(/\r/, ""); line[NR] = $0 }
            END { if (NR == n) for (i = 1; i <= n; i++) print u[i] "|" line[i] }
        ')
    fi

    # ── Pass 2: emit JSON, reading batched props (per-unit fallback) ──
    parts="" sep=""
    while IFS='|' read -r _id _label _unit; do
        [ -z "$_id" ] && continue
        if [ "$_unit" = "__absent__" ]; then
            # Ghost row for an installable-but-absent service. No systemctl
            # probe (the unit does not exist); fixed installed:false tuple so
            # the frontend renders the [Установить] button.
            _id_e=$(json_escape "$_id")
            _label_e=$(json_escape "$_label")
            parts="${parts}${sep}{\"id\":\"${_id_e}\",\"label\":\"${_label_e}\",\"unit\":\"\",\"active\":\"inactive\",\"enabled\":false,\"masked\":false,\"user_disabled\":false,\"installed\":false}"
            sep=,
            continue
        fi
        if [ "$_unit" = "init.d" ]; then
            _active=inactive
            _enabled=disabled
            _masked=0
            _admin_off=1
            if [ "$_id" = "mplc4" ]; then
                if mplc4_process_active; then
                    _active=active
                fi
                if mplc4_rc_autostart; then
                    _admin_off=0
                    _enabled=enabled
                fi
            else
                if codesys_process_active; then
                    _active=active
                fi
                if codesys_rc_autostart; then
                    _admin_off=0
                    _enabled=enabled
                fi
            fi
        else
            _al=$(printf '%s\n' "$_batch_props" | awk -F'|' -v u="$_unit" '$1==u{print $2"|"$3; exit}')
            if [ -n "$_al" ]; then
                _en_raw=$(printf '%s\n' "$_enabled_map" | awk -F'|' -v u="$_unit" '$1==u{print $2; exit}')
                if [ -z "$_en_raw" ]; then
                    _en_raw=$(sc_run is-enabled "$_unit" 2>/dev/null | head -n1 | tr -d '\r')
                fi
                IFS='|' read -r _active _enabled _masked _admin_off <<EOF2
$(derive_props "${_al%%|*}" "$_en_raw" "${_al#*|}")
EOF2
            else
                IFS='|' read -r _active _enabled _masked _admin_off <<EOF2
$(unit_props "$_unit")
EOF2
            fi
        fi
        # SysV/oneshot wrappers (codesyscontrol RemainAfterExit, mplc4 oneshot)
        # keep ActiveState=active after the start script exits. Emit runtime
        # truth from the process probe so list/dashboard never show a false
        # Active + «<1м» with uptime_s=0.
        if [ "$_id" = "codesys" ]; then
            if codesys_process_active; then
                _active=active
                _admin_off=0
            else
                _active=inactive
            fi
        fi
        if [ "$_id" = "mplc4" ]; then
            if mplc4_process_active; then
                _active=active
            else
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
$_rows
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

# Only these three runtimes carry install/uninstall (local .deb / vendor
# payload / online installer). Everything else is install-time-only.
svc_is_installable() {
    case "$1" in
        codesys|mplc4|node-red) return 0 ;;
    esac
    return 1
}

# True if a TCP port is in LISTEN state (post-install / post-uninstall verify).
port_listening() {
    command -v ss >/dev/null 2>&1 || return 1
    ss -H -ltn "sport = :$1" 2>/dev/null | grep -q ":$1"
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
    # codesys: start = RUNTIME ONLY (kernel-conditional service policy) — no
    # codesys_rc_enable, and no `systemctl enable` on the SysV-generated unit
    # (it shims to `update-rc.d enable`, re-arming the disabled autostart).
    if [ "$_id" = "codesys" ]; then
        codemeter_runtime_start
    fi
    if [ "$_id" = "mplc4" ]; then
        mplc4_rc_enable
    fi
    if [ -n "$_u" ]; then
        sc_run_slow unmask "$_u" >>"$LOG" 2>&1 || true
        if [ "$_id" != "codesys" ]; then
            sc_run_slow enable "$_u" >>"$LOG" 2>&1 || true
        fi
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
    # codesys is exempt from the autostart (admin_on) enforcement below:
    # start = runtime only, autostart stays as the policy left it — with the
    # rc links disabled the check could never pass and would re-enable them.
    if [ "$_id" != "codesys" ] && ! service_admin_on "$_id" "$_u"; then
        if [ -n "$_u" ]; then
            sc_run_slow enable "$_u" >>"$LOG" 2>&1 || true
            sc_run daemon-reload >>"$LOG" 2>&1 || true
        fi
        if [ "$_id" = "mplc4" ]; then
            mplc4_rc_enable
        fi
    fi
    if [ "$_id" != "codesys" ] && ! service_admin_on "$_id" "$_u"; then
        emit_result "$(printf '{"ok":false,"error":"enable_failed","id":"%s"}' "$_id")"
        return 1
    fi
    emit_result "$(printf '{"ok":true,"id":"%s","action":"start"}' "$_id")"
}

# ═══════════════════════════════════════════════════════════════════════════
# INSTALL / UNINSTALL
#
# Runtime distillations of scripts/08-codesys.sh, 09-mplc.sh, 07-nodered.sh —
# NOT a shell-out to those scripts (absent on the device, and they source
# scripts/lib.sh + REPO paths). Keep 08/09/07 as the canonical install-time
# path; any shared step edited here is mirrored there (see plan §Architecture).
# All three PRESERVE their /opt/vendor-installers/<c>/ staging.
# ═══════════════════════════════════════════════════════════════════════════

# ── CODESYS install (local .deb only) ──────────────────────────────────────
codesys_install() {
    _deb=""
    for _c in /opt/vendor-installers/codesys/codesyscontrol_linuxarm_*_armhf.deb \
              /opt/vendor-installers/codesys/codesyscontrol_*_armhf.deb; do
        for _f in $_c; do
            [ -f "$_f" ] && _deb="$_f" && break 2
        done
    done
    if [ -z "$_deb" ] || [ ! -f "$_deb" ]; then
        emit_result '{"ok":false,"error":"staging_missing","id":"codesys"}'
        return 1
    fi
    # codemeter-lite is absent in Debian main → --force-depends; demo mode until
    # the license .wbc lands. Hold so `apt-get -f install` never removes it.
    DEBIAN_FRONTEND=noninteractive dpkg -i --force-depends "$_deb" >>"$LOG" 2>&1 || true
    apt-mark hold codesyscontrol >>"$LOG" 2>&1 || true
    _dropin_src=/opt/vendor-installers/codesys/sa02m.conf
    _dropin_dir=/etc/systemd/system/codesyscontrol.service.d
    if [ -f "$_dropin_src" ]; then
        mkdir -p "$_dropin_dir" 2>/dev/null || true
        cp "$_dropin_src" "$_dropin_dir/sa02m.conf" >>"$LOG" 2>&1 || true
        chmod 644 "$_dropin_dir/sa02m.conf" 2>/dev/null || true
    fi
    # A stale pidfile from a previous instance makes init.d think it is alive.
    if [ -f /var/run/codesyscontrol.pid ] && ! codesys_process_active; then
        rm -f /var/run/codesyscontrol.pid
    fi
    # Mirror of 08-codesys.sh (see the section header note): the .deb postinst
    # re-arms the SysV rc links — apply-policy disables them again.
    if [ -x /usr/local/sbin/sa02m-kernel-service-guard.sh ]; then
        /usr/local/sbin/sa02m-kernel-service-guard.sh apply-policy >>"$LOG" 2>&1 || true
    fi
    sc_run daemon-reload >>"$LOG" 2>&1 || true
    # Install = runtime only too (kernel-conditional service policy): no
    # codesys_rc_enable — the verification below needs a running process, so
    # start it (a manual act the policy allows), with CodeMeter for licensing.
    codemeter_runtime_start
    if [ -x /etc/init.d/codesyscontrol ] && ! codesys_process_active; then
        /etc/init.d/codesyscontrol start >>"$LOG" 2>&1 || true
        sleep 2
    fi
    if codesys_process_active || port_listening 11740; then
        emit_result '{"ok":true,"id":"codesys","action":"install"}'
        return 0
    fi
    emit_result '{"ok":false,"error":"install_failed","id":"codesys"}'
    return 1
}

# ── MPLC4 install (vendor payload only) ─────────────────────────────────────
mplc4_install() {
    _src=/opt/vendor-installers/mplc4
    if [ ! -d "$_src" ] || [ ! -f "$_src/install.sh" ] \
       || [ ! -f "$_src/mplc4.tar.gz" ] || [ ! -f "$_src/nginx.tar.gz" ]; then
        emit_result '{"ok":false,"error":"staging_missing","id":"mplc4"}'
        return 1
    fi
    (
        cd "$_src" || exit 1
        chmod +x ./install.sh 2>/dev/null || true
        bash ./install.sh --use-systemd --http-port=8082 --enable-log
    ) >>"$LOG" 2>&1 || true
    if [ -f "$_src/mplc_cyntron.so" ] && [ -d /opt/mplc4 ]; then
        install -m 0755 "$_src/mplc_cyntron.so" /opt/mplc4/mplc_cyntron.so >>"$LOG" 2>&1 || true
    fi
    sc_run daemon-reload >>"$LOG" 2>&1 || true
    sc_run_slow enable mplc4 >>"$LOG" 2>&1 || true
    sc_run_slow restart mplc4 >>"$LOG" 2>&1 || true
    sleep 3
    if port_listening 8082 || service_runtime_active mplc4 mplc4.service; then
        emit_result '{"ok":true,"id":"mplc4","action":"install"}'
        return 0
    fi
    emit_result '{"ok":false,"error":"install_failed","id":"mplc4"}'
    return 1
}

# ── Node-RED: online (preferred) or offline from staged payload ─────────────
nodered_internet_reachable() {
    command -v curl >/dev/null 2>&1 || return 1
    curl -fsS --max-time 15 -I https://registry.npmjs.org/node-red >/dev/null 2>&1
}

# uiHost 0.0.0.0 so the panel is reachable by device IP (mirrors 07:114-124).
nodered_fix_settings() {
    _h=$1
    _s="$_h/.node-red/settings.js"
    _def=/usr/lib/node_modules/node-red/settings.js
    if [ ! -f "$_s" ] && [ -f "$_def" ]; then
        cp "$_def" "$_s" 2>>"$LOG" || true
    fi
    [ -f "$_s" ] || return 0
    if grep -qE '^[[:space:]]*uiHost:' "$_s" 2>/dev/null; then
        sed -i 's/^\([[:space:]]*uiHost:\)[[:space:]]*.*/\1 "0.0.0.0",/' "$_s" 2>>"$LOG" || true
    elif grep -qE '^[[:space:]]*//[[:space:]]*uiHost:' "$_s" 2>/dev/null; then
        sed -i 's/^\([[:space:]]*\)\/\/[[:space:]]*uiHost:.*/\1uiHost: "0.0.0.0",/' "$_s" 2>>"$LOG" || true
    else
        sed -i '/uiPort:/a\    uiHost: "0.0.0.0",' "$_s" 2>>"$LOG" || true
    fi
}

nodered_enable_start() {
    _unit=""
    for _u in nodered.service node-red.service; do
        if unit_exists "$_u" || unit_file_installed "$_u"; then
            _unit=$_u
            break
        fi
    done
    if [ -z "$_unit" ]; then
        emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
        return 1
    fi
    sc_run daemon-reload >>"$LOG" 2>&1 || true
    sc_run_slow enable "$_unit" >>"$LOG" 2>&1 || true
    sc_run_slow restart "$_unit" >>"$LOG" 2>&1 || true
    sleep 2
    if port_listening 1880 || service_runtime_active node-red "$_unit"; then
        emit_result '{"ok":true,"id":"node-red","action":"install"}'
        return 0
    fi
    emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
    return 1
}

nodered_ensure_user() {
    if ! id nodered >/dev/null 2>&1; then
        useradd -r -m -d /home/nodered -s /usr/sbin/nologin nodered >>"$LOG" 2>&1 || true
    fi
    _nr_home=$(getent passwd nodered 2>/dev/null | cut -d: -f6)
    [ -n "$_nr_home" ] || _nr_home=/home/nodered
    mkdir -p "$_nr_home/.node-red" 2>/dev/null || true
}

nodered_install_online() {
    _url="${NODERED_INSTALLER_URL:-https://raw.githubusercontent.com/node-red/linux-installers/master/deb/update-nodejs-and-nodered}"
    if ! curl -fsS --max-time 15 -I "$_url" >/dev/null 2>&1; then
        emit_result '{"ok":false,"error":"no_internet","id":"node-red"}'
        return 1
    fi
    nodered_ensure_user
    # Download then run with bash — no process substitution (this script is
    # POSIX sh / dash on the device; `bash <(curl…)` is not portable).
    _tmp=/tmp/sa02m-nodered-install.$$
    if ! curl -fsSL "$_url" -o "$_tmp" 2>>"$LOG"; then
        emit_result '{"ok":false,"error":"no_internet","id":"node-red"}'
        return 1
    fi
    bash "$_tmp" --confirm-root --confirm-install --skip-pi --no-init --node20 \
        --restart --nodered-user=nodered >>"$LOG" 2>&1 || true
    rm -f "$_tmp"
    # armhf/armv7l on Node < 22 cannot run Node-RED v4/v5 (they require Node
    # v22.9+). The official installer pulls the latest node-red (v5 today), so
    # on this arch ALWAYS pin node-red@3 — do NOT gate on parsing the installed
    # version: that parse mis-fired on-stand and left a crash-looping v5 (the
    # `|| true` swallowed the failed downgrade). Uninstall, re-pin @3, verify.
    _arch=$(dpkg --print-architecture 2>/dev/null || uname -m)
    if [ "$_arch" = "armhf" ] || [ "$_arch" = "armv7l" ]; then
        _nodemajor=$(node --version 2>/dev/null | awk -F. '{print substr($1,2)+0}')
        if [ -n "$_nodemajor" ] && [ "$_nodemajor" -lt 22 ] 2>/dev/null; then
            npm uninstall -g node-red >>"$LOG" 2>&1 || true
            if ! npm install -g --no-audit --no-fund --unsafe-perm node-red@3 >>"$LOG" 2>&1; then
                # --force overrides a leftover v5 engines/peer refusal.
                npm install -g --no-audit --no-fund --unsafe-perm --force node-red@3 >>"$LOG" 2>&1 || true
            fi
            # Verify the pin took by reading the installed package.json (do NOT
            # boot node-red — a stray v5 crashes on Node 20). CLI is a fallback.
            _nr_pkg="$(npm root -g 2>/dev/null)/node-red/package.json"
            _nrmajor=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9][0-9]*\).*/\1/p' "$_nr_pkg" 2>/dev/null | head -n1)
            [ -n "$_nrmajor" ] || _nrmajor=$(node-red --version 2>/dev/null | awk -F. '{print $1+0}')
            if [ -z "$_nrmajor" ] || [ "$_nrmajor" -ge 4 ] 2>/dev/null; then
                printf 'nodered: v3 pin failed, installed major=%s\n' "${_nrmajor:-?}" >>"$LOG" 2>&1
                emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
                return 1
            fi
        fi
    fi
    _nr_home=$(getent passwd nodered 2>/dev/null | cut -d: -f6)
    [ -n "$_nr_home" ] || _nr_home=/home/nodered
    nodered_fix_settings "$_nr_home"
    chown -R nodered:nodered "$_nr_home" 2>/dev/null || true
    nodered_enable_start
}

# Offline install from a pre-built payload (staged separately — code defensively
# against its absence). Extracts a staged Node tarball (only if Node.js is
# absent — shared Node is kept) + a pre-built node-red global tree, recreates
# the /usr/bin/node-red symlink, drops the unit, enables + starts. No npm registry.
nodered_install_offline() {
    _src=/opt/vendor-installers/nodered
    if [ ! -d "$_src" ]; then
        emit_result '{"ok":false,"error":"staging_missing","id":"node-red"}'
        return 1
    fi
    _node_tar=""
    for _c in "$_src"/node-*-linux-armv7l.tar.* "$_src"/node-*-linux-arm*.tar.* "$_src"/node-*.tar.*; do
        for _f in $_c; do [ -f "$_f" ] && _node_tar="$_f" && break 2; done
    done
    _nr_tar=""
    for _c in "$_src"/node-red-*.tar.* "$_src"/node_modules*.tar.*; do
        for _f in $_c; do [ -f "$_f" ] && _nr_tar="$_f" && break 2; done
    done
    _unit_src=""
    for _c in "$_src/nodered.service" "$_src/node-red.service"; do
        [ -f "$_c" ] && _unit_src="$_c" && break
    done
    if [ -z "$_nr_tar" ] || [ -z "$_unit_src" ]; then
        emit_result '{"ok":false,"error":"staging_missing","id":"node-red"}'
        return 1
    fi
    nodered_ensure_user
    if ! command -v node >/dev/null 2>&1; then
        if [ -z "$_node_tar" ]; then
            emit_result '{"ok":false,"error":"staging_missing","id":"node-red"}'
            return 1
        fi
        if ! tar -C /usr/local --strip-components=1 -xf "$_node_tar" >>"$LOG" 2>&1; then
            emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
            return 1
        fi
    fi
    mkdir -p /usr/lib/node_modules 2>/dev/null || true
    if ! tar -C /usr/lib/node_modules -xf "$_nr_tar" >>"$LOG" 2>&1; then
        emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
        return 1
    fi
    if [ -f /usr/lib/node_modules/node-red/red.js ]; then
        ln -sf ../lib/node_modules/node-red/red.js /usr/bin/node-red 2>>"$LOG" || true
        chmod +x /usr/bin/node-red 2>/dev/null || true
    fi
    _nr_home=$(getent passwd nodered 2>/dev/null | cut -d: -f6)
    [ -n "$_nr_home" ] || _nr_home=/home/nodered
    if [ -f "$_src/settings.js" ] && [ ! -f "$_nr_home/.node-red/settings.js" ]; then
        cp "$_src/settings.js" "$_nr_home/.node-red/settings.js" 2>>"$LOG" || true
    fi
    nodered_fix_settings "$_nr_home"
    chown -R nodered:nodered "$_nr_home" 2>/dev/null || true
    cp "$_unit_src" /etc/systemd/system/nodered.service 2>>"$LOG" || true
    nodered_enable_start
}

nodered_install() {
    if nodered_internet_reachable; then
        nodered_install_online
        return $?
    fi
    if [ -d /opt/vendor-installers/nodered ]; then
        nodered_install_offline
        return $?
    fi
    emit_result '{"ok":false,"error":"no_internet","id":"node-red"}'
    return 1
}

# ── CODESYS uninstall (ordering load-bearing; heals the apt poison) ─────────
codesys_uninstall() {
    apt-mark unhold codesyscontrol >>"$LOG" 2>&1 || true
    if [ -x /etc/init.d/codesyscontrol ]; then
        /etc/init.d/codesyscontrol stop >>"$LOG" 2>&1 || true
    fi
    if command -v update-rc.d >/dev/null 2>&1; then
        update-rc.d codesyscontrol disable >>"$LOG" 2>&1 || true
        update-rc.d codesyscontrol remove >>"$LOG" 2>&1 || true
    fi
    if unit_exists codesyscontrol.service; then
        sc_run_slow stop codesyscontrol.service >>"$LOG" 2>&1 || true
        sc_run_slow disable codesyscontrol.service >>"$LOG" 2>&1 || true
    fi
    if codesys_process_active; then
        pkill -f '[c]odesyscontrol\.bin' >>"$LOG" 2>&1 || true
        sleep 1
    fi
    # dpkg NOT apt: apt would try to satisfy the missing codemeter dep and can
    # wedge. NEVER run apt-get -f install / autoremove here.
    if dpkg -s codesyscontrol >/dev/null 2>&1; then
        if ! dpkg --purge --force-depends codesyscontrol >>"$LOG" 2>&1; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: codesys --purge refused, fallback dpkg --remove --force-all" >>"$LOG" 2>&1
            dpkg --remove --force-all codesyscontrol >>"$LOG" 2>&1 || true
        fi
    fi
    rm -rf /etc/systemd/system/codesyscontrol.service.d 2>>"$LOG" || true
    rm -f /var/run/codesyscontrol.pid 2>>"$LOG" || true
    rm -f /etc/3S.dat 2>>"$LOG" || true
    rm -rf /var/opt/codesys 2>>"$LOG" || true
    sc_run daemon-reload >>"$LOG" 2>&1 || true
    if codesys_process_active || port_listening 11740 || port_listening 4840; then
        emit_result '{"ok":false,"error":"uninstall_failed","id":"codesys"}'
        return 1
    fi
    if dpkg -s codesyscontrol >/dev/null 2>&1; then
        emit_result '{"ok":false,"error":"purge_blocked","id":"codesys"}'
        return 1
    fi
    emit_result '{"ok":true,"id":"codesys","action":"uninstall"}'
}

# ── MPLC4 uninstall (full clean; preserve staging) ──────────────────────────
mplc4_uninstall() {
    if unit_exists mplc4.service; then
        sc_run_slow stop mplc4.service >>"$LOG" 2>&1 || true
        sc_run_slow disable mplc4.service >>"$LOG" 2>&1 || true
    fi
    if [ -x /etc/init.d/mplc4 ]; then
        /etc/init.d/mplc4 stop >>"$LOG" 2>&1 || true
    fi
    if command -v update-rc.d >/dev/null 2>&1; then
        update-rc.d mplc4 remove >>"$LOG" 2>&1 || true
    fi
    # Tarball install (not dpkg): update-rc.d only drops the rc symlinks, not the
    # SysV script itself. Remove it (and its late generator unit) so a leftover
    # executable /etc/init.d/mplc4 does not keep installed:true after uninstall
    # (service_present now recognises it). All rm -f — idempotent on a clean
    # device; the generator is regenerated on daemon-reload below.
    rm -f /etc/init.d/mplc4 2>>"$LOG" || true
    rm -f /run/systemd/generator.late/mplc4.service 2>>"$LOG" || true
    if mplc4_process_active; then
        pkill -f '[m]plc.*\.bin\|[M]asterPLC\|[m]asterplc' >>"$LOG" 2>&1 || true
        sleep 1
    fi
    if dpkg -s mplc4 >/dev/null 2>&1; then
        dpkg --purge mplc4 >>"$LOG" 2>&1 || dpkg --remove --force-all mplc4 >>"$LOG" 2>&1 || true
    fi
    rm -f /etc/systemd/system/mplc4.service /lib/systemd/system/mplc4.service \
          /usr/lib/systemd/system/mplc4.service 2>>"$LOG" || true
    rm -rf /opt/mplc4 2>>"$LOG" || true
    sc_run daemon-reload >>"$LOG" 2>&1 || true
    if port_listening 8082 || mplc4_process_active; then
        emit_result '{"ok":false,"error":"uninstall_failed","id":"mplc4"}'
        return 1
    fi
    emit_result '{"ok":true,"id":"mplc4","action":"uninstall"}'
}

# ── Node-RED uninstall (full clean incl. user+home; KEEP Node.js) ───────────
nodered_uninstall() {
    for _u in nodered.service node-red.service; do
        if unit_exists "$_u"; then
            sc_run_slow stop "$_u" >>"$LOG" 2>&1 || true
            sc_run_slow disable "$_u" >>"$LOG" 2>&1 || true
        fi
    done
    if command -v npm >/dev/null 2>&1; then
        npm uninstall -g node-red >>"$LOG" 2>&1 || true
    fi
    rm -rf /usr/lib/node_modules/node-red 2>>"$LOG" || true
    rm -f /usr/bin/node-red 2>>"$LOG" || true
    rm -f /etc/systemd/system/nodered.service /etc/systemd/system/node-red.service \
          /lib/systemd/system/nodered.service /lib/systemd/system/node-red.service \
          /usr/lib/systemd/system/nodered.service /usr/lib/systemd/system/node-red.service 2>>"$LOG" || true
    rm -rf /home/nodered/.node-red 2>>"$LOG" || true
    if id nodered >/dev/null 2>&1; then
        userdel -r nodered >>"$LOG" 2>&1 || true
    fi
    sc_run daemon-reload >>"$LOG" 2>&1 || true
    if port_listening 1880; then
        emit_result '{"ok":false,"error":"uninstall_failed","id":"node-red"}'
        return 1
    fi
    emit_result '{"ok":true,"id":"node-red","action":"uninstall"}'
}

cmd_install() {
    _id=$1
    validate_id "$_id" || { emit_result '{"ok":false,"error":"invalid_id"}'; return 1; }
    if ! svc_is_installable "$_id"; then
        emit_result '{"ok":false,"error":"not_installable","id":"'"$_id"'"}'
        return 1
    fi
    SVC_RESULT_FILE="${RESULT_DIR}/${_id}.json"
    mkdir -p "$RESULT_DIR" 2>/dev/null || true
    printf '{"ok":true,"pending":true,"id":"%s","action":"install"}\n' "$_id" >"$SVC_RESULT_FILE" 2>/dev/null || true
    chmod 644 "$SVC_RESULT_FILE" 2>/dev/null || true
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: install ${_id}" >>"$LOG" 2>&1
    case "$_id" in
        codesys) codesys_install ;;
        mplc4) mplc4_install ;;
        node-red) nodered_install ;;
    esac
}

cmd_uninstall() {
    _id=$1
    validate_id "$_id" || { emit_result '{"ok":false,"error":"invalid_id"}'; return 1; }
    if ! svc_is_installable "$_id"; then
        emit_result '{"ok":false,"error":"not_installable","id":"'"$_id"'"}'
        return 1
    fi
    SVC_RESULT_FILE="${RESULT_DIR}/${_id}.json"
    mkdir -p "$RESULT_DIR" 2>/dev/null || true
    printf '{"ok":true,"pending":true,"id":"%s","action":"uninstall"}\n' "$_id" >"$SVC_RESULT_FILE" 2>/dev/null || true
    chmod 644 "$SVC_RESULT_FILE" 2>/dev/null || true
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: uninstall ${_id}" >>"$LOG" 2>&1
    case "$_id" in
        codesys) codesys_uninstall ;;
        mplc4) mplc4_uninstall ;;
        node-red) nodered_uninstall ;;
    esac
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
    install)
        [ -n "$ID" ] || { printf '{"ok":false,"error":"missing_id"}\n'; exit 1; }
        cmd_install "$ID"
        ;;
    uninstall)
        [ -n "$ID" ] || { printf '{"ok":false,"error":"missing_id"}\n'; exit 1; }
        cmd_uninstall "$ID"
        ;;
    *)
        printf '{"ok":false,"error":"bad_action"}\n'
        exit 1
        ;;
esac
