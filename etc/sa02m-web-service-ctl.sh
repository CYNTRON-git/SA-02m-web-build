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

# ── RS-485 port-lease probe (флашер держит линию?) ─────────────────────────
# Читает poll_locked с НЕавторизованного GET /health демона sa02m-flasher —
# союз «flock на диске ∪ активные задачи», который демон и так вычисляет.
# Контракт ответа: docs/contracts/flasher-health.md.
#
# История, чтобы это не повторилось: до 2026-07-12 проба ходила на /status со
# статическим токеном `session_token=cyntron_session`. Токен вывели из
# обращения, демон стал отвечать 401 — а `curl` БЕЗ `-f` выходит с кодом 0 на
# 401, поэтому `|| return 1` не срабатывал, подстроки `"busy":true` в
# `{"error":"unauthorized"}` не было, и гейт три недели молча отвечал «не
# занято». Отсюда два правила ниже.
#
# ПРАВИЛО 1 — слой решает форму проверки. В репозитории два HTTP-слоя с
# ПРОТИВОПОЛОЖНЫМИ идиомами ошибок: демон отдаёт настоящие коды (здесь `-f`
# необходим и достаточен), а Bash-CGI отвечает HTTP 200 с `{"error":...}` в
# теле (там `-f` бесполезен, спасает только проверка ТЕЛА). Общий дом правила:
# docs/agent-rules/web-code-rigor.md ## Bash CGI floors.
#
# ПРАВИЛО 2 — FAIL CLOSED. Это предохранительный гейт: пока демон запущен
# (сокет на месте), но ответ непригоден — нет curl, таймаут, ошибка
# транспорта, мусор в теле, нет поля poll_locked — проба отвечает «занято».
# Молчаливый отказ и есть то, что скрывало поломку; шумный отказ («Пуск не
# нажимается») позовёт человека. Причина пишется в $LOG.
#
# Отсутствие сокета — НЕ «неизвестно»: демона нет ⇒ аренды порта нет ⇒ не
# занято (единственное состояние, читаемое без запроса).
FLASHER_SOCK="${FLASHER_SOCK:-/run/sa02m-flasher/flasher.sock}"
# `cmd_list` — горячий путь опроса вкладки «Службы», поэтому причина отказа
# пишется не чаще раза в FLASHER_PROBE_LOG_EVERY секунд: залипший демон иначе
# растит /var/log/sa02m_install.log на каждом поллинге.
FLASHER_PROBE_LOG_EVERY="${FLASHER_PROBE_LOG_EVERY:-300}"
FLASHER_PROBE_STAMP="${FLASHER_PROBE_STAMP:-${RESULT_DIR}/flasher-probe-warn.stamp}"

flasher_probe_unknown() {
    _now=$(date +%s 2>/dev/null || echo 0)
    _last=$(cat "$FLASHER_PROBE_STAMP" 2>/dev/null || echo 0)
    case "$_last" in ''|*[!0-9]*) _last=0 ;; esac
    if [ "$_now" -eq 0 ] || [ $((_now - _last)) -ge "$FLASHER_PROBE_LOG_EVERY" ]; then
        mkdir -p "$(dirname "$FLASHER_PROBE_STAMP")" 2>/dev/null || true
        printf '%s\n' "$_now" >"$FLASHER_PROBE_STAMP" 2>/dev/null || true
        echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: port-lease probe: $1 — считаю флашер занятым (fail closed)" >>"$LOG" 2>&1
    fi
    return 0
}

flasher_poll_lock_held() {
    _sock=$FLASHER_SOCK
    [ -S "$_sock" ] || return 1
    if ! command -v curl >/dev/null 2>&1; then
        flasher_probe_unknown "демон запущен, но curl недоступен"
        return 0
    fi
    # -f обязателен: без него 401/500 приходят с кодом выхода 0 (см. историю).
    if ! _resp=$(curl -fsS --max-time 2 --unix-socket "$_sock" \
        http://localhost/health 2>/dev/null); then
        flasher_probe_unknown "GET /health не ответил (таймаут/HTTP-ошибка)"
        return 0
    fi
    # Сравнение с ЛИТЕРАЛЬНОЙ подстрокой: ничего из ответа не попадает в
    # shell-слово, путь или имя unit'а (web-code-rigor.md).
    case "$_resp" in
        *'"poll_locked":true'*|*'"poll_locked": true'*) return 0 ;;
        *'"poll_locked":false'*|*'"poll_locked": false'*) return 1 ;;
    esac
    flasher_probe_unknown "в ответе /health нет поля poll_locked"
    return 0
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
    if [ -f "$_src/mplc_protocol_fast_modbus.so" ] && [ -d /opt/mplc4 ]; then
        install -m 0755 "$_src/mplc_protocol_fast_modbus.so" /opt/mplc4/mplc_protocol_fast_modbus.so >>"$LOG" 2>&1 || true
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

# ── Node-RED: staged payload (preferred) or online ─────────────────────────
# Pinned release. This is ONE of the pin's homes; the others are
# scripts/07-nodered.sh, scripts/dev/build-nodered-payload.sh,
# vendor-src/nodered/package.json and docs/vendor-integrations.md. The quality
# row `nodered-pin-consistency` fails the build when they disagree.
NODERED_PIN_VERSION=4.1.13
NODERED_PIN_MAJOR=4
# The Node floor. These two constants MIRROR engines.node of the pinned
# node-red in vendor-src/nodered/package-lock.json (">=18.5" today) and are
# pinned to it mechanically by the `nodered-pin-consistency` quality row — a
# lockfile bump that raises the floor (node-red 5 wants >=22.9) fails the build
# instead of silently leaving a board installing a runtime that cannot boot.
# They are duplicated here rather than parsed at run time because the lockfile
# is a build-host artifact: it is never delivered to the device, and this
# script is POSIX sh with no JSON parser. Minor is carried too — ">=18.5"
# truncated to major 18 would admit Node 18.0-18.4, which the engine rejects.
# Below the floor the runtime refuses to boot, so it is a hard stop, not a
# warning. Node 22 is what the payload ships, not the floor.
NODERED_NODE_MIN_MAJOR=18
NODERED_NODE_MIN_MINOR=5

nodered_internet_reachable() {
    command -v curl >/dev/null 2>&1 || return 1
    curl -fsS --max-time 15 -I https://registry.npmjs.org/node-red >/dev/null 2>&1
}

# Staging home. $SA02M_NODERED_DIR is an install-time override for
# scripts/07-nodered.sh, which resolves the payload from the repo tree
# ($REPO/vendor/nodered) before /opt/vendor-installers exists. It is NOT
# reachable from the web: sudoers runs this script under the default
# env_reset, so no request can set it (threat-model §3 / plan S1).
nodered_staging_dir() {
    case "${SA02M_NODERED_DIR:-}" in
        /*) if [ -d "$SA02M_NODERED_DIR" ]; then printf '%s' "$SA02M_NODERED_DIR"; return 0; fi ;;
    esac
    printf '%s' /opt/vendor-installers/nodered
}

# Every global module root that could hold an installed node-red. The board can
# carry two: apt's Node owns /usr/lib/node_modules, and a staged Node 22 in
# /usr/local (the coexistence the device runbook creates) makes `npm root -g`
# answer /usr/local/lib/node_modules. Checking only one of them is how a
# cross-major install hides from the guard.
nodered_global_roots() {
    _r=$(npm root -g 2>/dev/null)
    case "$_r" in /*) printf '%s\n' "$_r" ;; esac
    printf '%s\n' /usr/lib/node_modules /usr/local/lib/node_modules
}

# Every installed global node-red version, one per line. Exit status is the
# point here:
#   0 + version(s) on stdout — installed and readable
#   1                        — not installed anywhere (clean board)
#   2                        — a package.json is present but unreadable
# 2 must never be folded into 1: "I cannot tell what is installed" is not
# "nothing is installed", and the difference decides whether an irreversible
# cross-major overwrite proceeds. A pre-release ("5.0.0-beta.1") or a truncated
# file lands in 2 by construction. Reads package.json and never boots node-red —
# a runtime whose Node is too old crashes on start, so `node-red --version` is
# not a safe probe (that parse mis-fire is what left a crash-looping v5 on the
# bench).
nodered_installed_version() {
    _any=1
    for _root in $(nodered_global_roots); do
        _pkg="$_root/node-red/package.json"
        [ -f "$_pkg" ] || continue
        _v=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' "$_pkg" 2>/dev/null | head -n1)
        if [ -z "$_v" ]; then
            printf 'nodered: %s exists but no version could be read from it\n' "$_pkg" >>"$LOG" 2>&1
            return 2
        fi
        printf '%s\n' "$_v"
        _any=0
    done
    return "$_any"
}

# First readable installed version, for reporting. Callers that must DECIDE use
# nodered_guard_major_upgrade, which weighs every root.
nodered_installed_version_first() {
    nodered_installed_version 2>/dev/null | head -n1
}

# The install REFUSES to overwrite an installed Node-RED across a major.
# A cross-major upgrade migrates flows and re-encrypts credentials in place and
# is irreversible without a backup — that is a documented device procedure
# (docs/deployment.md, "Доставка vendor-payload Node-RED и обновление
# стека"), not an unattended button.
# Nothing installed => clean install, exactly as before. Unreadable => REFUSE:
# this guard protects a field board with no backup, so it fails CLOSED.
nodered_guard_major_upgrade() {
    _all=$(nodered_installed_version)
    case "$?" in
        1) return 0 ;;   # nothing installed anywhere — clean install, as before
        2)
            printf 'nodered: REFUSING to install — a node-red is present but its version cannot be read, so a cross-major overwrite cannot be ruled out. Inspect package.json under %s, or remove the install deliberately ("Удалить" WIPES the flows) before retrying.\n' \
                "$(nodered_global_roots | tr '\n' ' ')" >>"$LOG" 2>&1
            emit_result '{"ok":false,"error":"major_upgrade_refused","id":"node-red","installed":"unreadable","target":"'"$NODERED_PIN_VERSION"'"}'
            return 1
            ;;
    esac
    # EVERY root is weighed, not just the first: a board can carry two global
    # module roots (apt's /usr/lib and a staged Node's /usr/local/lib), and a
    # 3.x hiding in the one we did not look at is exactly the install this
    # guard exists to refuse.
    _cur=""
    for _v in $_all; do
        if [ "${_v%%.*}" != "$NODERED_PIN_MAJOR" ]; then
            _cur=$_v
            break
        fi
    done
    [ -n "$_cur" ] || return 0
    printf 'nodered: REFUSING in-place upgrade %s -> %s (crosses a major).\n' \
        "$_cur" "$NODERED_PIN_VERSION" >>"$LOG" 2>&1
    printf 'nodered: what to do instead — back up /home/nodered/.node-red (flows, credentials AND the .config.* dotfiles) off the device, then either re-image the board or run the staged upgrade in docs/deployment.md. "Удалить" in the panel also clears the way, but it WIPES the flows.\n' >>"$LOG" 2>&1
    emit_result '{"ok":false,"error":"major_upgrade_refused","id":"node-red","installed":"'"$_cur"'","target":"'"$NODERED_PIN_VERSION"'"}'
    return 1
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

# Flow-level health since $2 (a journalctl --since stamp taken before the
# restart). Port 1880 being open is a FALSE GREEN: a runtime that came up with
# every flow stopped for missing node types listens exactly the same, and the
# panel would show green while the operator's automation is dead.
#
# The verdict rests on ONE positive marker, and that is deliberate. In
# node-red 4.1.13 (@node-red/runtime/lib/flows/index.js) the start path has
# four early returns — missing node types, unloadable external modules, safe
# mode, and a saved runtimeFlowState of "stop" — and every one of them returns
# BEFORE the "started flows" message is logged. So that message is emitted if
# and only if the flows really started, which makes its ABSENCE the general
# failure signal and saves us a marker per failure mode.
#
# LANGUAGE. The runtime localises this log. Our own unit pins the locale
# (etc/systemd/system/nodered.service) so a board we installed logs English,
# but the ONLINE path leaves the official installer's unit in place and a
# ru-locale board then logs «Запущены потоки» — observed on the bench. Both
# spellings are therefore matched, taken verbatim from the shipped catalogues
# (@node-red/runtime/locales/{en-US,ru}/runtime.json, keys
# nodes.flows.started-flows and nodes.flows.missing-types). HONEST LIMIT: en
# and ru are the two catalogues this fleet runs; node-red ships eight more
# (de, es-ES, fr, ja, ko, pt-BR, zh-CN, zh-TW). A HEALTHY board in one of those
# IS judged wrongly — its start marker matches nothing, so it lands in the
# "logging but never said started" branch and is reported as a FAILURE. The
# error is one-directional: never a false success, only a false failure. That
# is the safe direction, not an absence of error. Pinned by the de/ja fixture
# in scripts/dev/test-nodered-ctl.sh.
#
#   0 = flows started        1 = a named stop condition was logged
#   2 = no evidence at all (no journalctl, or the unit logged nothing)
#   3 = the unit is logging but has not said it started — keep polling; at the
#       end of the poll window this is a FAILURE, not "no evidence"
nodered_flows_healthy() {
    _fu=$1
    _fsince=$2
    command -v journalctl >/dev/null 2>&1 || return 2
    _jl=$(journalctl -u "$_fu" --since "$_fsince" --no-pager -n 500 2>/dev/null)
    [ -n "$_jl" ] || return 2
    if printf '%s\n' "$_jl" | grep -q 'Started flows\|Запущены потоки'; then
        return 0
    fi
    # Named stop conditions — a precise log line beats waiting out the poll.
    if printf '%s\n' "$_jl" | grep -q \
        'Waiting for missing types to be registered\|Ожидание регистрации отсутствующих типов\|Failed to load external modules\|Flows stopped in safe mode\|Потоки остановлены в безопасном режиме\|Stopped flows\|Остановлены потоки\|Error loading credentials\|Ошибка при загрузке учетных данных\|Unsupported version of\|Неподдерживаемая версия'; then
        return 1
    fi
    return 3
}

# $1 (optional) — extra JSON fields for the success result, each starting with
# a comma (e.g. ',"node":"v22.23.0"').
nodered_enable_start() {
    _extra=${1:-}
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
    _since=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
    sc_run daemon-reload >>"$LOG" 2>&1 || true
    sc_run_slow enable "$_unit" >>"$LOG" 2>&1 || true
    sc_run_slow restart "$_unit" >>"$LOG" 2>&1 || true
    # Node-RED cold start on this SoC is seconds, not milliseconds — poll for
    # the verdict instead of guessing with a fixed sleep. Bounded at ~45 s; the
    # panel's poll deadline for install/uninstall is 600 s (app/services.js
    # `pollMax`; 90 s is the start/stop one), so this sits well inside it.
    _i=0
    _verdict=2
    while [ "$_i" -lt 15 ]; do
        _i=$((_i + 1))
        sleep 3
        nodered_flows_healthy "$_unit" "$_since"
        _verdict=$?
        # 0 = started, 1 = a named stop condition: both are final.
        [ "$_verdict" -le 1 ] && break
        # 2 = nothing in the journal at all. If there is no journalctl on this
        # system nothing will ever arrive, so stop waiting once the runtime is
        # up; otherwise keep polling — the unit may not have logged yet.
        if [ "$_verdict" -eq 2 ] \
           && ! command -v journalctl >/dev/null 2>&1 \
           && port_listening 1880; then
            break
        fi
    done
    if [ "$_verdict" -eq 0 ]; then
        emit_result '{"ok":true,"id":"node-red","action":"install","verified":"flows"'"$_extra"'}'
        return 0
    fi
    if [ "$_verdict" -eq 1 ] || [ "$_verdict" -eq 3 ]; then
        # Verdict 3 = the unit logged for the whole poll window and never said
        # it started. That is a failure, NOT missing evidence: we could read
        # the journal, and it did not say the thing that only a real start
        # says. Reporting it as success is the false green this exists to kill.
        printf 'nodered: the runtime did not report started flows within the poll window (missing node types / unloadable modules / safe mode / credential error / unsupported Node) — journalctl -u %s\n' "$_unit" >>"$LOG" 2>&1
        emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
        return 1
    fi
    if port_listening 1880 || service_runtime_active node-red "$_unit"; then
        printf 'nodered: no journal for %s — reporting the runtime state only, NOT that the flows run\n' "$_unit" >>"$LOG" 2>&1
        emit_result '{"ok":true,"id":"node-red","action":"install","verified":"runtime_only"'"$_extra"'}'
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
    nodered_guard_major_upgrade || return 1
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
    # The official installer parses --node22 and --nodered-version=<x> (both
    # verified in the canonical script), so the pin is requested directly and
    # the old install-latest-then-downgrade dance is gone. Honesty: that URL is
    # an UNPINNED `master` script executed as root, and --node22 on armhf goes
    # through NodeSource, whose armhf index may serve a different 22.x than the
    # offline payload — the online path is best-effort, the payload is the
    # deterministic one (docs/vendor-integrations.md).
    bash "$_tmp" --confirm-root --confirm-install --skip-pi --no-init --node22 \
        "--nodered-version=$NODERED_PIN_VERSION" \
        --restart --nodered-user=nodered >>"$LOG" 2>&1 || true
    rm -f "$_tmp"
    # Defensive: the installer swallows its own failures behind `|| true`, and a
    # flag that silently stops being parsed would otherwise leave whatever npm
    # served. Read the installed package.json — never boot node-red to ask.
    _got=$(nodered_installed_version_first)
    if [ -z "$_got" ] || [ "${_got%%.*}" != "$NODERED_PIN_MAJOR" ]; then
        printf 'nodered: online install did not land the %s pin (installed=%s)\n' \
            "$NODERED_PIN_VERSION" "${_got:-none}" >>"$LOG" 2>&1
        emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
        return 1
    fi
    _nr_home=$(getent passwd nodered 2>/dev/null | cut -d: -f6)
    [ -n "$_nr_home" ] || _nr_home=/home/nodered
    nodered_fix_settings "$_nr_home"
    chown -R nodered:nodered "$_nr_home" 2>/dev/null || true
    nodered_enable_start ',"version":"'"$_got"'","source":"online"'
}

# Resolves the staged payload into _nr_src / _nr_tar / _nr_node_tar / _nr_unit.
# Returns 0 only when BOTH mandatory pieces are present (the node-red tree and
# the unit) — a half-staged directory is not a payload, and treating it as one
# is what lets an install "succeed" having done nothing.
# The payload shape has one home: docs/vendor-integrations.md.
nodered_payload_resolve() {
    _nr_src=$(nodered_staging_dir)
    _nr_tar=""
    _nr_node_tar=""
    _nr_unit=""
    [ -d "$_nr_src" ] || return 1
    # The Node.js tarball. `node-*.tar.*` ALSO matches node-red-4.1.13.tar.gz —
    # with only the Node-RED tarball staged on a Node-less board that glob used
    # to extract the Node-RED tree into /usr/local (--strip-components=1) and
    # then still fail, having polluted the filesystem. Skip node-red-* here.
    for _f in "$_nr_src"/node-*-linux-armv7l.tar.* "$_nr_src"/node-*-linux-arm*.tar.* "$_nr_src"/node-*.tar.*; do
        [ -f "$_f" ] || continue
        case "${_f##*/}" in node-red-*) continue ;; esac
        _nr_node_tar=$_f
        break
    done
    # Exactly one supported shape: a single top-level `node-red/` directory with
    # its dependencies NESTED inside it (node-red/node_modules/…), so nothing is
    # splattered across the global module dir. The extraction asserts it below.
    for _f in "$_nr_src"/node-red-*.tar.*; do
        [ -f "$_f" ] || continue
        _nr_tar=$_f
        break
    done
    for _f in "$_nr_src/nodered.service" "$_nr_src/node-red.service"; do
        [ -f "$_f" ] || continue
        _nr_unit=$_f
        break
    done
    [ -n "$_nr_tar" ] && [ -n "$_nr_unit" ]
}

# True when the archive holds exactly one top-level entry, `node-red/`.
# This runs BEFORE a root `tar -x` into /usr/lib, so it is a security check,
# not a tidiness one: it rejects `../` traversal, absolute-path members, and a
# mis-built archive that would scatter sibling packages across the global
# module dir (S4). Because `tar -t` reads the whole index first, a malformed
# payload costs the board nothing — not even a half-extracted /usr/local.
# The empty-string mapping is load-bearing: an absolute member (`/etc/passwd`)
# reduces to the EMPTY first component, and simply dropping empties is how such
# a member would ride along unnoticed beside a valid `node-red/`.
nodered_payload_top_level_ok() {
    _tar=$1
    _tops=$(tar -tf "$_tar" 2>/dev/null | sed 's#^\./##; s#/.*##; s#^$#<absolute-or-empty>#' | sort -u)
    [ "$_tops" = "node-red" ] && return 0
    printf 'nodered: payload %s must hold exactly one top-level entry `node-red/` (found: %s)\n' \
        "${_tar##*/}" "$(printf '%s' "$_tops" | tr '\n' ' ')" >>"$LOG" 2>&1
    return 1
}

# Offline install from the pre-built payload. No npm registry, no compilation.
nodered_install_offline() {
    if ! nodered_payload_resolve; then
        emit_result '{"ok":false,"error":"staging_missing","id":"node-red"}'
        return 1
    fi
    nodered_guard_major_upgrade || return 1
    if ! nodered_payload_top_level_ok "$_nr_tar"; then
        emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
        return 1
    fi
    nodered_ensure_user
    # Node.js. The staged tarball is extracted when the board has NO node, or
    # one below node-red's engines floor — otherwise Node-RED would install and
    # then refuse to boot. A usable-but-older Node (apt's 20 beside the staged
    # 22) is KEPT and coexists, and the skip is LOGGED and reported: the old
    # `command -v node` gate reported plain success while installing nothing,
    # which would make a Node-upgrade step a no-op that looked done.
    _node_ver=$(node --version 2>/dev/null | sed 's/^v//')
    _node_major=${_node_ver%%.*}
    case "$_node_major" in ''|*[!0-9]*) _node_major=0 ;; esac
    _node_minor=${_node_ver#*.}
    _node_minor=${_node_minor%%.*}
    case "$_node_minor" in ''|*[!0-9]*) _node_minor=0 ;; esac
    # Below the floor = older major, OR the same major with a smaller minor
    # (the engines range is ">=18.5", so 18.0-18.4 is below it too).
    _node_too_old=0
    if [ "$_node_major" -lt "$NODERED_NODE_MIN_MAJOR" ]; then
        _node_too_old=1
    elif [ "$_node_major" -eq "$NODERED_NODE_MIN_MAJOR" ] && [ "$_node_minor" -lt "$NODERED_NODE_MIN_MINOR" ]; then
        _node_too_old=1
    fi
    _node_src=kept
    if [ "$_node_too_old" -eq 1 ]; then
        if [ -z "$_nr_node_tar" ]; then
            printf 'nodered: node %s is below the node-red %s floor (engines: >=%s.%s) and no Node tarball is staged in %s\n' \
                "${_node_ver:-absent}" "$NODERED_PIN_VERSION" "$NODERED_NODE_MIN_MAJOR" "$NODERED_NODE_MIN_MINOR" "$_nr_src" >>"$LOG" 2>&1
            emit_result '{"ok":false,"error":"staging_missing","id":"node-red"}'
            return 1
        fi
        # --no-same-owner: the archive's uids are the build host's, and this
        # runs as root — file ownership comes from us, not from the tarball.
        if ! tar -C /usr/local --strip-components=1 --no-same-owner -xf "$_nr_node_tar" >>"$LOG" 2>&1; then
            emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
            return 1
        fi
        _node_src=extracted
        _node_ver=$(/usr/local/bin/node --version 2>/dev/null | sed 's/^v//')
    elif [ -n "$_nr_node_tar" ]; then
        printf 'nodered: keeping node v%s (at or above the node-red %s floor); staged %s NOT extracted — a Node major upgrade is a separate documented device step (docs/deployment.md)\n' \
            "$_node_ver" "$NODERED_PIN_VERSION" "${_nr_node_tar##*/}" >>"$LOG" 2>&1
    fi
    # tar MERGES into an existing directory — it never deletes what the new
    # tree dropped. An in-place extraction over a live install therefore left a
    # franken-tree behind (3.x leftovers under a 4.x install) that failed later
    # as an unattributable runtime error. Stop the unit, then replace wholesale.
    for _u in nodered.service node-red.service; do
        if unit_exists "$_u"; then
            sc_run_slow stop "$_u" >>"$LOG" 2>&1 || true
        fi
    done
    mkdir -p /usr/lib/node_modules 2>/dev/null || true
    rm -rf /usr/lib/node_modules/node-red 2>>"$LOG" || true
    if ! tar -C /usr/lib/node_modules --no-same-owner -xf "$_nr_tar" >>"$LOG" 2>&1; then
        emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
        return 1
    fi
    if [ ! -f /usr/lib/node_modules/node-red/red.js ]; then
        printf 'nodered: payload %s extracted without node-red/red.js\n' "${_nr_tar##*/}" >>"$LOG" 2>&1
        emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
        return 1
    fi
    ln -sf ../lib/node_modules/node-red/red.js /usr/bin/node-red 2>>"$LOG" || true
    chmod +x /usr/bin/node-red 2>/dev/null || true
    _nr_home=$(getent passwd nodered 2>/dev/null | cut -d: -f6)
    [ -n "$_nr_home" ] || _nr_home=/home/nodered
    if [ -f "$_nr_src/settings.js" ] && [ ! -f "$_nr_home/.node-red/settings.js" ]; then
        cp "$_nr_src/settings.js" "$_nr_home/.node-red/settings.js" 2>>"$LOG" || true
    fi
    nodered_fix_settings "$_nr_home"
    chown -R nodered:nodered "$_nr_home" 2>/dev/null || true
    # The unit is mandatory (nodered_payload_resolve already proved it exists),
    # so a failed copy is a failed install, not something to swallow.
    if ! install -m 0644 "$_nr_unit" /etc/systemd/system/nodered.service >>"$LOG" 2>&1; then
        emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
        return 1
    fi
    _got=$(nodered_installed_version_first)
    nodered_enable_start ',"version":"'"${_got:-unknown}"'","source":"offline","node":"'"${_node_ver:-unknown}"'","node_action":"'"$_node_src"'"'
}

# Payload FIRST: a staged, pre-built payload is a pin, and a pin that loses to
# whatever the registry serves that day is not a pin. With no payload staged
# this is byte-for-byte the previous behaviour — online, then the errors below.
nodered_install() {
    if nodered_payload_resolve; then
        nodered_install_offline
        return $?
    fi
    if nodered_internet_reachable; then
        nodered_install_online
        return $?
    fi
    if [ -d "$(nodered_staging_dir)" ]; then
        # The directory exists but is not a usable payload — say so precisely
        # rather than blaming the network.
        emit_result '{"ok":false,"error":"staging_missing","id":"node-red"}'
        return 1
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
