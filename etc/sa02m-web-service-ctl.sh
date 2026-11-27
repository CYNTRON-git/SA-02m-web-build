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
# Stack-policy lib (installed by scripts/03-webserver.sh; one home for the
# /etc/sa02m_stacks.conf format — etc/sa02m-stacks-policy.sh). SOFT: an older
# board without it simply records no policy (`command -v` guards at the two
# write sites in cmd_install/cmd_uninstall).
SA02M_STACKS_LIB=/usr/local/lib/sa02m-stacks-policy.sh
if [ -f "$SA02M_STACKS_LIB" ]; then
    # shellcheck disable=SC1090
    . "$SA02M_STACKS_LIB"
fi
RESULT_DIR=/var/run/sa02m-svcctl
TIMEOUT_SEC=8
# SysV-synced units (mplc4, codesyscontrol) need 12–20s for enable/disable.
TIMEOUT_SLOW_SEC=45

# Canonical home: www/network_config/cgi-bin/lib_web_json.sh. This script
# deploys to /etc under /bin/sh (no bash pattern substitution), so it keeps a
# sed copy that MUST give the same bytes as the lib's bash escaper
# (scripts/dev/test-json-escape-equiv.sh compares them). Lines are joined
# BEFORE substituting so `\`, `"` and CR are escaped on every line, not only
# the first; `$!{N;ba}` keeps a single-line value from exiting on N.
json_escape() {
    printf '%s' "$1" | sed -e ':a' -e '$!{' -e 'N' -e 'ba' -e '}' \
        -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\r//g' -e 's/\n/ /g'
}

sc_run() {
    if [ -n "${MPLC_DEADLINE:-}" ]; then
        mplc_bounded "$TIMEOUT_SEC" "$SC" "$@" 2>/dev/null
        return $?
    fi
    if command -v timeout >/dev/null 2>&1; then
        timeout "$TIMEOUT_SEC" "$SC" "$@" 2>/dev/null
    else
        "$SC" "$@" 2>/dev/null
    fi
}

sc_run_slow() {
    if [ -n "${MPLC_DEADLINE:-}" ]; then
        mplc_bounded "$TIMEOUT_SLOW_SEC" "$SC" "$@" 2>/dev/null
        return $?
    fi
    if command -v timeout >/dev/null 2>&1; then
        timeout "$TIMEOUT_SLOW_SEC" "$SC" "$@" 2>/dev/null
    else
        "$SC" "$@" 2>/dev/null
    fi
}

# Every /etc/init.d/{codemeter,codesyscontrol,mplc4} start|stop goes through
# here (A4-10): a wedged vendor script must not hold the service lock and the
# panel's 90 s operation budget forever. 45 + 5 s stays inside that budget.
# timeout signals its whole process group, so the script's own children die
# with it; a daemon that setsid()'d away survives, which is the normal state
# of a started runtime. The bound sits at EVERY init.d call, not only around
# the helper: killing the helper alone would not release the coordinator's
# guard fd while an init.d child still holds it.
INITD_TIMEOUT_S=45
case "${SA02M_INITD_TIMEOUT_S:-}" in ''|*[!0-9]*) ;; *) INITD_TIMEOUT_S=$SA02M_INITD_TIMEOUT_S ;; esac
initd_run() {
    case "$1" in
        /etc/init.d/mplc4|/etc/init.d/codesyscontrol|/etc/init.d/codemeter)
            [ -d /run/systemd/system ] || { echo 'service control: systemd_required' >&2; return 1; } ;;
    esac
    if [ -d /run/systemd/system ]; then
        case "$1" in
            /etc/init.d/mplc4) _initd_unit=mplc4.service ;;
            /etc/init.d/codesyscontrol) _initd_unit=codesyscontrol.service ;;
            /etc/init.d/codemeter) _initd_unit=codemeter.service ;;
            *) _initd_unit= ;;
        esac
        if [ -n "$_initd_unit" ]; then
            # PID1's runtime inherits no caller lease descriptors. A failed
            # manager request never grants permission to start a second owner.
            unit_exists "$_initd_unit" || return 1
            sc_run_slow "$2" "$_initd_unit"
            return $?
        fi
    fi
    command -v timeout >/dev/null 2>&1 || { "$@"; return $?; }
    timeout -k 5 "$INITD_TIMEOUT_S" "$@"
    _initd_rc=$?
    if [ "$_initd_rc" -eq 124 ] || [ "$_initd_rc" -eq 137 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: init.d ${1##*/} ${2:-} timed out after ${INITD_TIMEOUT_S}s" >>"$LOG" 2>&1
    fi
    return "$_initd_rc"
}

# Mandatory private lifecycle module: no unsafe cache/mutation fallback.
SVC_CACHE_LIB=/usr/local/lib/sa02m-service-cache.sh
if [ ! -f "$SVC_CACHE_LIB" ]; then
    if [ "${1:-}" != list ]; then
        printf '%s\n' '{"ok":false,"error":"cache_module_missing"}'
        exit 1
    fi
    svc_list_publish() { cmd_list; }
else
    # shellcheck disable=SC1090
    . "$SVC_CACHE_LIB" || exit 1
fi

emit_result() {
    _json=$1
    if [ -n "${SA02M_SVC_OPERATION_ID:-}" ]; then
        _json=$(printf '%s\n' "$_json" | python3 -I -c 'import json,sys
d=json.load(sys.stdin)
d.update(id=sys.argv[1], action=sys.argv[2], op_id=sys.argv[3])
print(json.dumps(d))' "$ID" "$ACTION" "$SA02M_SVC_OPERATION_ID") || return 1
    fi
    if [ "${SVC_DEFER_RESULT:-0}" = 1 ]; then
        case "$_json" in
            *'"pending":true'*|*'"pending": true'*) ;;
            *) SVC_FINAL_JSON=$_json; return 0 ;;
        esac
    fi
    printf '%s\n' "$_json"
    if [ -n "${SVC_RESULT_FILE:-}" ]; then
        publish_result "$_json"
    fi
}

publish_result() {
    mkdir -p "$RESULT_DIR" || return 1
    _result_tmp=$(mktemp "${SVC_RESULT_FILE}.XXXXXX") || return 1
    if ! { printf '%s\n' "$1" >"$_result_tmp" && chmod 644 "$_result_tmp" && mv -f "$_result_tmp" "$SVC_RESULT_FILE"; }; then
        rm -f "$_result_tmp"
        return 1
    fi
}

begin_result() {
    SVC_RESULT_FILE="${RESULT_DIR}/$1.json"
    emit_result "$(printf '{"ok":true,"pending":true,"id":"%s","action":"%s"}' "$1" "$2")" >/dev/null
}

# atomic_install_unit SRC DST [MODE=0644] — land a file on a LIVE path as
# tmp beside the target → fdatasync → rename-over → dir fsync, so any instant
# of a hard reset leaves DST OLD or NEW, never 0 bytes. A bare `install -m`
# truncates the live path first and fills it later, and systemd reads an empty
# unit as MASKED (bench 1.136 booted with the flasher unit masked that way,
# 2026-09-08 — web-code-rigor.md ## System scripts). This script runs
# standalone on the board as /bin/sh and cannot source scripts/lib.sh, so it
# carries the update runner's atomic_install_file() shape as its own copy; the
# list of etc/ scripts that carry one, and why each does, has one home:
# scripts/dev/codemod-install-atomic.py. `sync -d FILE` / `sync DIR` need
# coreutils >= 8.24 (every board runs 8.32 or 9.4). Every step is checked and
# a failed step removes the tmp: a refused fdatasync must stop the rename (the
# tmp may not be on disk). Gate: scripts/dev/test-nodered-ctl.sh.
atomic_install_unit() {
    _ai_src=$1
    _ai_dst=$2
    _ai_mode=${3:-0644}
    _ai_dir=$(dirname "$_ai_dst")
    _ai_tmp="$_ai_dst.tmp.$$"
    mkdir -p "$_ai_dir" || return 1
    install -m "$_ai_mode" "$_ai_src" "$_ai_tmp" || { rm -f "$_ai_tmp"; return 1; }
    sync -d -- "$_ai_tmp" || { rm -f "$_ai_tmp"; return 1; }
    mv -f "$_ai_tmp" "$_ai_dst" || { rm -f "$_ai_tmp"; return 1; }
    sync -- "$_ai_dir" || return 1
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

# UI-grade refusal for the COM pollers: a fast, clear `flasher_busy` while a
# flash/scan holds the line. Defense in depth only — the broker's mutation
# guard (the `ACTION:ID` dispatch below) is the atomic authority; this runs in
# front of it so the panel gets the honest reason instead of a generic
# `port_busy`, and once more inside cmd_start on the guarded re-exec.
start_blocked_by_flasher() {
    case "$1" in
        mplc4|mqtt-bridge) flasher_blocks_com_pollers ;;
        vplc) vplc_uses_rtu && flasher_blocks_com_pollers ;;
        *) return 1 ;;
    esac
}

# vPLC держит COM только в режиме RTU. Признак пишет интегратор, когда в
# проекте есть modbus_devices mode=rtu. В режиме MQTT пуск не блокируется
# прошивкой: шину занимает мост, не vPLC.
vplc_uses_rtu() {
    [ -s /etc/vplc.d/rtu-ports ]
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
            # Installed ⇔ the runtime payload is present (or its process is
            # live). The init.d script and mplc4.service are wrappers that only
            # exec start_mplc4.sh — a leftover wrapper without the payload (the
            # pre-1.0.6.24 uninstall left them behind) made the panel show a
            # dead «Пуск» instead of «Установить» (bench 1.135, 2026-08-29).
            # MPLC4_RUNTIME is a test seam: sudoers runs this script under
            # env_reset, so no web request can point it elsewhere (same
            # argument as FLASHER_SOCK / SA02M_NODERED_DIR).
            [ -x "${MPLC4_RUNTIME:-/opt/mplc4/start_mplc4.sh}" ] && return 0
            mplc4_process_active && return 0
            return 1
            ;;
        mosquitto)
            command -v mosquitto >/dev/null 2>&1 && return 0
            dpkg -s mosquitto >/dev/null 2>&1 && return 0
            ;;
        mqtt-bridge)
            [ -x /opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py ] && return 0
            ;;
        vplc)
            # Пакет, не OTA-оверлей: без бинарника строка не нужна (иначе мёртвый Пуск).
            [ -x /usr/bin/vplc ] && return 0
            dpkg -s vplc >/dev/null 2>&1 && return 0
            return 1
            ;;
        mqtt-telemetry)
            [ -x /opt/sa02m-modbus-mqtt/sa02m_telemetry.py ] && return 0
            ;;
        homekit)
            # Installed ⇔ the install.sh footprint (scripts/06c-homekit.sh): the
            # venv interpreter and the system user. OTA delivers the unit file
            # to every board, and a unit whose User= does not exist can never
            # start — without this the list would offer a dead «Пуск».
            [ -x /opt/sa02m-homekit-venv/bin/python ] || return 1
            id -u sa02m-homekit >/dev/null 2>&1 || return 1
            ;;
        homeconnect)
            # Installed ⇔ the install.sh footprint (scripts/06d-homeconnect.sh):
            # the package and the system user. OTA delivers the unit file and
            # the package to every board; a unit whose User= does not exist can
            # never start — without this the list would offer a dead «Пуск».
            [ -d /opt/sa02m-homeconnect/sa02m_homeconnect ] || return 1
            id -u sa02m-homeconnect >/dev/null 2>&1 || return 1
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
        initd_run /etc/init.d/codemeter start >>"$LOG" 2>&1 || true
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

mplc4_process_active() { mplc_policy_load && sa02m_mplc_runtime_active; }

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

# alice: the client unit exits 0 (standby) while client_enabled=false in
# /etc/sa02m-alice/sa02m-alice-client.conf, so a bare `systemctl start` can
# never pass the runtime-active check. Пуск/Стоп therefore also sync that
# flag through its one home (sa02m_alice config_store — same code path as the
# Alice card's enable/disable). Bounded; a failed write is logged and start
# proceeds (it will then fail honestly on the runtime-active check).
# Both syncs run as root (sudo from services_ctrl.cgi keeps the CGI's cwd,
# which www-data owns): `python3 -I` never puts the cwd, PYTHONPATH or the user
# site on sys.path, and the package root is inserted explicitly — a module
# planted in the cwd under the imported name is never run as root. Pinned by
# scripts/dev/test-service-ctl-policy.sh section 3.
alice_sync_client_enabled() {
    _val=$1  # true|false
    timeout 15 python3 -I -c "
import sys; sys.path.insert(0, '/opt/sa02m-alice')
from sa02m_alice.common.config_store import set_client_enabled
set_client_enabled($([ "$_val" = true ] && echo True || echo False))
" >>"$LOG" 2>&1 || echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: alice client_enabled=${_val} write FAILED" >>"$LOG" 2>&1
}

# homekit: same shape as alice — the daemon exits 0 (standby) while
# enabled=false in /etc/sa02m-homekit/sa02m-homekit.conf, so Пуск/Стоп sync the
# flag through its one home (sa02m_homekit.config — the code path the HomeKit
# card's enable/disable uses). Bounded; a failed write is logged and the start
# then fails honestly on the runtime-active check.
homekit_sync_enabled() {
    _val=$1  # true|false
    timeout 15 python3 -I -c "
import sys; sys.path.insert(0, '/opt/sa02m-homekit')
from sa02m_homekit import config
c = config.load()
c.enabled = $([ "$_val" = true ] && echo True || echo False)
config.save(c)
" >>"$LOG" 2>&1 || echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: homekit enabled=${_val} write FAILED" >>"$LOG" 2>&1
}

# homeconnect: same shape — the client exits 0 (standby) while enabled=false in
# /etc/sa02m-homeconnect/sa02m-homeconnect.conf; Пуск/Стоп sync the flag
# through its one home (sa02m_homeconnect.config, the card's code path).
homeconnect_sync_enabled() {
    _val=$1  # true|false
    timeout 15 python3 -I -c "
import sys; sys.path.insert(0, '/opt/sa02m-homeconnect')
from sa02m_homeconnect import config
c = config.load()
c.enabled = $([ "$_val" = true ] && echo True || echo False)
config.save(c)
" >>"$LOG" 2>&1 || echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: homeconnect enabled=${_val} write FAILED" >>"$LOG" 2>&1
}

# id | UI label | candidate units (first existing wins)
# alice: plain Type=simple systemd unit (no init.d/SysV shim, no COM lease) —
# the generic present/resolve/list path handles it; not svc_is_installable, so
# it shows Пуск/Стоп like mosquitto/mqtt-bridge (install-time-only overlay).
SERVICE_DEFS=$(cat <<'SVC_DEFS'
alice|Яндекс Алиса|sa02m-alice-client.service
homekit|Apple HomeKit|sa02m-homekit.service
homeconnect|Home Connect|sa02m-homeconnect.service
docker|Docker|docker.service
codesys|CODESYS|codesyscontrol.service,codesys.service,CODESYSControl.service,CODESYSControlRuntime.service
mplc4|MPLC4|mplc4.service
vplc|vPLC|vplc.service
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

# Only complete, uniquely keyed records enter this request-local map. Aliases
# returned under another Id must use the original per-unit probe.
list_unit_snapshot() {
    local wanted=$1 name active load
    while IFS='|' read -r name active load; do
        if [ "$name" = "$wanted" ]; then
            printf '%s|%s' "$active" "$load"
            return 0
        fi
    done <<EOF
$_batch_props
EOF
    return 1
}

# Catalog strings and normal systemd states need no JSON substitutions. Keep
# the existing escaping for any row containing quotes, backslashes or controls.
list_append_row() {
    local enabled=false masked=false admin_off=false
    case "$1$2$3$4" in
        *'"'*|*'\'*|*[[:cntrl:]]*)
            set -- "$(json_escape "$1")" "$(json_escape "$2")" \
                "$(json_escape "$3")" "$(json_escape "$4")" "$5" "$6" "$7" "$8"
            ;;
    esac
    [ "$5" = enabled ] && enabled=true
    [ "$6" = 1 ] && masked=true
    [ "$7" = 1 ] && admin_off=true
    parts="${parts}${sep}{\"id\":\"$1\",\"label\":\"$2\",\"unit\":\"$3\",\"active\":\"$4\",\"enabled\":$enabled,\"masked\":$masked,\"user_disabled\":$admin_off,\"installed\":$8}"
    sep=,
}

# Resolve candidates and reuse their state from one keyed show snapshot. The
# second call is the original is-enabled batch, preserving its policy meaning.
cmd_list() {
    _present_rows=""
    _candidate_units=""
    while IFS='|' read -r _id _label _cands; do
        [ -z "$_id" ] && continue
        if ! service_present "$_id" "$_cands"; then
            if svc_is_installable "$_id"; then
                _present_rows="${_present_rows}${_id}|${_label}|__absent__
"
            fi
            continue
        fi
        _present_rows="${_present_rows}${_id}|${_label}|${_cands}
"
        _old_ifs=$IFS
        IFS=,
        for _u in $_cands; do
            IFS=$_old_ifs
            case "$_u" in *.service) ;; *) _u="${_u}.service" ;; esac
            _candidate_units="${_candidate_units} ${_u}"
        done
        IFS=$_old_ifs
    done <<EOF
$SERVICE_DEFS
EOF

    _batch_props=""
    if [ -n "$_candidate_units" ] && _show=$(sc_run show --property=Id,ActiveState,LoadState $_candidate_units 2>/dev/null); then
        _batch_props=$(printf '%s\n' "$_show" | awk '
            function record() {
                if (id != "") {
                    if (ni == 1 && na == 1 && nl == 1 && act != "" && load != "" && !bad)
                        value[id] = id "|" act "|" load
                    else invalid[id] = 1
                }
                id=""; act=""; load=""; ni=0; na=0; nl=0; bad=0
            }
            { sub(/\r$/, "") }
            /^$/ { record(); next }
            /^Id=/ { id=substr($0,4); seen[id]++; ni++; next }
            /^ActiveState=/ { act=substr($0,13); na++; next }
            /^LoadState=/ { load=substr($0,11); nl++; next }
            { bad=1 }
            END { record(); for (id in seen) if (seen[id] == 1 && !invalid[id]) print value[id] }
        ')
    fi

    _rows=""
    _batch_units=""
    while IFS='|' read -r _id _label _cands; do
        [ -z "$_id" ] && continue
        if [ "$_cands" = __absent__ ]; then
            _rows="${_rows}${_id}|${_label}|__absent__
"
            continue
        fi
        _unit=""
        _old_ifs=$IFS
        IFS=,
        for _u in $_cands; do
            IFS=$_old_ifs
            case "$_u" in *.service) ;; *) _u="${_u}.service" ;; esac
            if _al=$(list_unit_snapshot "$_u"); then
                case "${_al#*|}" in not-found|"") continue ;; esac
                _unit=$_u
                break
            elif unit_exists "$_u"; then
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
$_present_rows
EOF

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
            list_append_row "$_id" "$_label" "" inactive disabled 0 0 false
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
            _al=$(list_unit_snapshot "$_unit") || _al=""
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
        list_append_row "$_id" "$_label" "$_unit" "$_active" "$_enabled" "$_masked" "$_admin_off" true
    done <<EOF
$_rows
EOF
    _flasher_busy=false
    if flasher_poll_lock_held; then
        _flasher_busy=true
    fi
    _migration=''
    if [ ! -s "$RESULT_DIR/owned-v1" ] || [ "$(cat "$RESULT_DIR/owned-v1")" != "$(cat /proc/sys/kernel/random/boot_id)" ]; then
        _migration=',"activation":"pending"'
        if [ -x /usr/local/libexec/sa02m-runtime-activate ]; then
            _phase=$(/usr/local/libexec/sa02m-runtime-activate status 2>/dev/null | /usr/bin/python3 -ISB -c 'import json,sys
d=json.load(sys.stdin)
print("failed" if d.get("phase") == "failed" else "reboot" if d.get("reason") == "ordinary_reboot_required" else "pending")' 2>/dev/null) || _phase=pending
            _migration=',"activation":"'"$_phase"'"'
        fi
    fi
    printf '{"ok":true,"flasher_busy":%s,"services":[%s]%s}\n' "$_flasher_busy" "$parts" "$_migration"
}

validate_id() {
    [ "${#1}" -le 64 ] || return 1
    case "$1" in
        ''|*[!a-zA-Z0-9_-]*) return 1 ;;
    esac
    return 0
}

service_id_known() {
    while IFS='|' read -r _known_id _known_label _known_units; do
        [ "$_known_id" = "$1" ] && return 0
    done <<SVC_IDS
$SERVICE_DEFS
SVC_IDS
    return 1
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

mplc_clock() { sa02m_mplc_clock; }
mplc_budget_begin() { mplc_policy_load && sa02m_mplc_budget_begin; }
mplc_bounded() { sa02m_mplc_bounded "$@"; }

mplc_policy_load() {
    MPLC_POLICY_LIB=/usr/local/lib/sa02m-mplc-policy.sh
    [ -f "$MPLC_POLICY_LIB" ] || return 1
    # shellcheck disable=SC1090
    . "$MPLC_POLICY_LIB"
}
sa02m_mplc_atomic() { atomic_install_unit "$@"; }
sa02m_mplc_sc() { sc_run "$@"; }

mplc_observe() {
    mplc_policy_load || return 1
    _mplc_observation=$(sa02m_mplc_policy_observe) || return $?
    _mplc_status=${_mplc_observation%|*}
    _mplc_result=${_mplc_observation##*|}
}

mplc_wait_terminal() { sa02m_mplc_wait_terminal "$@"; }

mplc_failed_start_cleanup() {
    # Native Restart=always is preserved. Cancel the same unit after a failed
    # attempt so automatic retries cannot outlive this bounded operation.
    if sa02m_mplc_cancel_start >>"$LOG" 2>&1; then
        case "$_mplc_status" in
            terminal\|inactive\|*|terminal\|failed\|*)
                if ! mplc4_process_active; then
                    emit_result '{"ok":false,"id":"mplc4","error":"start_failed"}'
                    return 1
                fi ;;
        esac
    fi
    emit_result '{"ok":false,"id":"mplc4","error":"mplc_outcome_unknown"}'
    return 1
}

mplc_managed_action() {
    _mplc_action=$1
    _mplc_quiet=${2:-}
    [ -n "${MPLC_DEADLINE:-}" ] || mplc_budget_begin || return 1
    # A board with systemd but without the canonical wrapper needs fullrefresh,
    # not a retry through an unrelated SysV process tree.
    if ! unit_exists mplc4.service; then
        emit_result '{"ok":false,"id":"mplc4","error":"mplc_unit_missing"}'
        return 1
    fi
    if ! sc_run_slow unmask mplc4.service >>"$LOG" 2>&1 || ! sc_run daemon-reload >>"$LOG" 2>&1 || ! mplc_observe; then
        emit_result '{"ok":false,"id":"mplc4","error":"mplc_timeout_policy"}'
        return 1
    fi
    if [ "$_mplc_action" = start ]; then
        case "$_mplc_status" in
            changing\|*)
                emit_result '{"ok":false,"id":"mplc4","error":"mplc_job_pending"}'
                return 1 ;;
        esac
        if ! sc_run_slow enable mplc4.service >>"$LOG" 2>&1; then
            emit_result '{"ok":false,"id":"mplc4","error":"enable_failed"}'
            return 1
        fi
        if [ -x /etc/init.d/mplc4 ] && ! mplc_bounded 15 update-rc.d mplc4 defaults >>"$LOG" 2>&1; then
            emit_result '{"ok":false,"id":"mplc4","error":"enable_failed"}'
            return 1
        fi
    fi
    # The client can time out before PID1 finishes its 45s + 15s cleanup.
    # Observe that same job; never issue a direct init.d retry or reset-failed.
    _mplc_client_rc=0
    sc_run_slow "$_mplc_action" mplc4.service >>"$LOG" 2>&1 || _mplc_client_rc=$?
    if [ "$_mplc_action" = start ]; then
        case "$_mplc_client_rc" in
            0|124|137) ;;
            *) mplc_failed_start_cleanup; return 1 ;;
        esac
    fi
    if ! mplc_wait_terminal "$_mplc_action"; then
        if [ "$_mplc_action" = start ]; then
            mplc_failed_start_cleanup
            return 1
        fi
        emit_result '{"ok":false,"id":"mplc4","error":"mplc_outcome_unknown"}'
        return 1
    fi
    if [ "$_mplc_action" = start ]; then
        case "$_mplc_status" in
            terminal\|active\|enabled|terminal\|active\|enabled-runtime) ;;
            *) mplc_failed_start_cleanup; return 1 ;;
        esac
        if ! mplc4_process_active; then
            mplc_failed_start_cleanup
            return 1
        fi
    else
        case "$_mplc_status" in
            terminal\|inactive\|*|terminal\|failed\|*) ;;
            *) emit_result '{"ok":false,"id":"mplc4","error":"still_running"}'; return 1 ;;
        esac
        if mplc4_process_active; then
            emit_result '{"ok":false,"id":"mplc4","error":"still_running"}'
            return 1
        fi
        if ! sc_run_slow disable mplc4.service >>"$LOG" 2>&1; then
            emit_result '{"ok":false,"id":"mplc4","error":"disable_failed"}'
            return 1
        fi
        if [ -x /etc/init.d/mplc4 ] && ! mplc_bounded 15 update-rc.d mplc4 disable >>"$LOG" 2>&1; then
            emit_result '{"ok":false,"id":"mplc4","error":"disable_failed"}'
            return 1
        fi
        if unit_can_mask mplc4.service; then
            sc_run mask mplc4.service >>"$LOG" 2>&1 || { emit_result '{"ok":false,"error":"mask_failed","id":"mplc4"}'; return 1; }
        fi
        if ! mplc_observe; then
            emit_result '{"ok":false,"id":"mplc4","error":"mplc_outcome_unknown"}'
            return 1
        fi
        case "$_mplc_status" in
            terminal\|inactive\|disabled|terminal\|failed\|disabled|terminal\|inactive\|masked|terminal\|failed\|masked) ;;
            *) emit_result '{"ok":false,"id":"mplc4","error":"disable_failed"}'; return 1 ;;
        esac
    fi
    if [ "$_mplc_quiet" != quiet ]; then
        emit_result "{\"ok\":true,\"id\":\"mplc4\",\"action\":\"$_mplc_action\"}"
    fi
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
    begin_result "$_id" stop || return 1
    if [ "$_id" = mplc4 ]; then
        mplc_managed_action stop
        return $?
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: stop ${_id} (${_u})" >>"$LOG" 2>&1
    if [ "$_id" = "codesys" ] && [ -x /etc/init.d/codesyscontrol ]; then
        codesys_rc_disable
    fi
    if [ "$_id" = "alice" ]; then
        alice_sync_client_enabled false
    fi
    if [ "$_id" = "homekit" ]; then
        homekit_sync_enabled false
    fi
    if [ "$_id" = "homeconnect" ]; then
        homeconnect_sync_enabled false
    fi
    if [ "$_id" = "vplc" ]; then
        curl -fsS --max-time 5 -X POST http://127.0.0.1:1234/api/stop >>"$LOG" 2>&1 || true
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
        vplc)
            if vplc_uses_rtu && flasher_blocks_com_pollers; then
                emit_result '{"ok":false,"error":"flasher_busy"}'
                return 1
            fi
            ;;
    esac
    _u=$_found_unit
    begin_result "$_id" start || return 1
    if [ "$_id" = mplc4 ]; then
        mplc_managed_action start
        return $?
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: start ${_id} (${_u:-init.d})" >>"$LOG" 2>&1
    # codesys: start = RUNTIME ONLY (kernel-conditional service policy) — no
    # codesys_rc_enable, and no `systemctl enable` on the SysV-generated unit
    # (it shims to `update-rc.d enable`, re-arming the disabled autostart).
    if [ "$_id" = "codesys" ]; then
        codemeter_runtime_start
    fi
    if [ "$_id" = "alice" ]; then
        alice_sync_client_enabled true
    fi
    if [ "$_id" = "homekit" ]; then
        homekit_sync_enabled true
    fi
    if [ "$_id" = "homeconnect" ]; then
        homeconnect_sync_enabled true
    fi
    if [ -n "$_u" ]; then
        sc_run_slow unmask "$_u" >>"$LOG" 2>&1 || true
        if [ "$_id" != "codesys" ]; then
            sc_run_slow enable "$_u" >>"$LOG" 2>&1 || true
        fi
        sc_run_slow start "$_u" >>"$LOG" 2>&1 || true
        sc_run daemon-reload >>"$LOG" 2>&1 || true
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
codesys_vendor_handoff() {
    [ "${SVC_WORKER_UNIT:-}" = sa02m-svcctl-install-codesys.service ] || return 1
    svc_worker_verify "$SVC_WORKER_UNIT" "${SA02M_SVC_WORKER_PID:-}" || return 1
    python3 -I - "$SVC_WORKER_UNIT" <<'PY_CODESYS_HANDOFF'
import os
from pathlib import Path
import stat
import subprocess
import sys

group = '/system.slice/' + sys.argv[1]

def runtime_processes():
    result = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            argv = (path / 'cmdline').read_bytes().split(b'\0')
            if not any(os.path.basename(arg) == b'codesyscontrol.bin' for arg in argv):
                continue
            ticks = (path / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            groups = {line.split(':', 2)[2] for line in (path / 'cgroup').read_text().splitlines()}
            result[int(path.name)] = (ticks, groups)
        except FileNotFoundError:
            continue
    return result

try:
    before = runtime_processes()
    owned = {pid: value for pid, value in before.items() if group in value[1]}
    if not owned:
        sys.exit(0)
    if len(owned) != 1 or before != owned:
        raise ValueError('ambiguous or foreign CODESYS runtime; refusing vendor stop')
    pidfile = Path('/var/run/codesyscontrol.pid')
    info = pidfile.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_nlink != 1 or info.st_size > 32:
        raise ValueError('unsafe vendor pidfile')
    pid = int(pidfile.read_text().strip())
    if pid not in owned or runtime_processes() != before:
        raise ValueError('vendor process identity changed before handoff')
    # This single stop is for the proven postinst process in this worker,
    # before its first PID1 start. It is never a retry of a manager request.
    subprocess.run(['timeout', '-k', '5', '45', '/etc/init.d/codesyscontrol', 'stop'],
                   close_fds=False, check=True, timeout=52)
    if runtime_processes():
        raise ValueError('vendor stop did not prove runtime termination')
    old = Path('/proc') / str(pid) / 'stat'
    if old.exists() and old.read_text().rsplit(')', 1)[1].split()[19] == owned[pid][0]:
        raise ValueError('original vendor PID still exists after stop')
except (OSError, ValueError, subprocess.SubprocessError) as exc:
    print('CODESYS handoff: ' + str(exc), file=sys.stderr)
    sys.exit(1)
PY_CODESYS_HANDOFF
}

svc_runtime_cgroup_proof() {
    python3 -I - "$1" "$SC" <<'PY_RUNTIME_CGROUP'
import os
from pathlib import Path
import re
import subprocess
import sys
identity, manager = sys.argv[1:]
units = {'codesys': 'codesyscontrol.service', 'mplc4': 'mplc4.service', 'node-red': 'nodered.service'}
try:
    unit = units[identity]
    if identity == 'node-red':
        unit = 'node-red.service'
        if subprocess.run([manager, 'show', '-p', 'LoadState', '--value', unit], capture_output=True, text=True, timeout=8).stdout.strip() == 'not-found':
            unit = 'nodered.service'
    group = subprocess.run([manager, 'show', '--property=ControlGroup', '--value', unit],
                           capture_output=True, text=True, timeout=8, check=True).stdout.strip()
    if not group.startswith('/system.slice/'):
        raise ValueError('missing canonical runtime cgroup')
    found = 0
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            argv = (process / 'cmdline').read_bytes().split(b'\0')
            comm = (process / 'comm').read_text().strip()
            matched = (identity == 'codesys' and any(os.path.basename(a) == b'codesyscontrol.bin' for a in argv) or
                       identity == 'mplc4' and (comm in {'mplc', 'mplc4', 'mplc_monitor'} or
                           any(re.fullmatch(rb'mplc[^/]*\.bin|MasterPLC|masterplc', os.path.basename(a)) for a in argv)) or
                       identity == 'node-red' and (comm == 'node-red' or any(a.endswith(b'/red.js') for a in argv)))
            if not matched:
                continue
            groups = {line.split(':', 2)[2] for line in (process / 'cgroup').read_text().splitlines()}
            if group not in groups:
                raise ValueError('runtime outside its canonical PID1 cgroup')
            found += 1
        except FileNotFoundError:
            continue
    if not found:
        raise ValueError('runtime process not found')
except (OSError, ValueError, subprocess.SubprocessError) as exc:
    print('runtime ownership: ' + str(exc), file=sys.stderr)
    sys.exit(1)
PY_RUNTIME_CGROUP
}

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
    if ! DEBIAN_FRONTEND=noninteractive dpkg -i --force-depends "$_deb" >>"$LOG" 2>&1; then
        emit_result '{"ok":false,"error":"install_failed","id":"codesys"}'
        return 1
    fi
    if ! codesys_vendor_handoff >>"$LOG" 2>&1; then
        emit_result '{"ok":false,"error":"runtime_cgroup_unknown","id":"codesys"}'
        return 1
    fi
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
        initd_run /etc/init.d/codesyscontrol start >>"$LOG" 2>&1 || true
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
mplc_install_failure() {
    _mplc_install_error=$1
    MPLC_DEADLINE=
    SA02M_MPLC_DEADLINE=
    if mplc_budget_begin && sa02m_mplc_cancel_start && ! mplc4_process_active; then
        emit_result "{\"ok\":false,\"error\":\"$_mplc_install_error\",\"id\":\"mplc4\"}"
    else
        emit_result '{"ok":false,"error":"mplc_outcome_unknown","id":"mplc4"}'
    fi
    return 1
}

mplc4_install() {
    _src=/opt/vendor-installers/mplc4
    _unit_src=/opt/sa02m-mplc/systemd
    if [ ! -d "$_src" ] || [ ! -f "$_src/install.sh" ] \
       || [ ! -f "$_src/mplc4.tar.gz" ] || [ ! -f "$_src/nginx.tar.gz" ]; then
        emit_result '{"ok":false,"error":"staging_missing","id":"mplc4"}'
        return 1
    fi
    if ! mplc_policy_load || ! sa02m_mplc_policy_install "$_unit_src/mplc4.service" ||
            ! sa02m_mplc_wrapper_install "$_unit_src/mplc4.service" "$_unit_src/mplc4-native.service" ||
            ! sc_run daemon-reload >>"$LOG" 2>&1 || ! mplc_observe; then
        emit_result '{"ok":false,"error":"mplc_timeout_policy","id":"mplc4"}'
        return 1
    fi
    if ! (cd "$_src" && unset SYSTEMCTL_SKIP_REDIRECT &&
            bash ./install.sh --use-systemd --http-port=8082 --enable-log) >>"$LOG" 2>&1; then
        mplc_install_failure install_failed
        return $?
    fi
    if ! sa02m_mplc_wrapper_install "$_unit_src/mplc4.service" "$_unit_src/mplc4-native.service" ||
            ! sc_run daemon-reload >>"$LOG" 2>&1 || ! mplc_observe; then
        mplc_install_failure mplc_timeout_policy
        return $?
    fi
    if [ -f "$_src/mplc_cyntron.so" ] && [ -d /opt/mplc4 ]; then
        install -m 0755 "$_src/mplc_cyntron.so" /opt/mplc4/mplc_cyntron.so >>"$LOG" 2>&1 || true
    fi
    if [ -f "$_src/mplc_protocol_fast_modbus.so" ] && [ -d /opt/mplc4 ]; then
        install -m 0755 "$_src/mplc_protocol_fast_modbus.so" /opt/mplc4/mplc_protocol_fast_modbus.so >>"$LOG" 2>&1 || true
    fi
    MPLC_DEADLINE=
    SA02M_MPLC_DEADLINE=
    mplc_managed_action stop quiet || return 1
    MPLC_DEADLINE=
    SA02M_MPLC_DEADLINE=
    mplc_managed_action start quiet || return 1
    MPLC_DEADLINE=
    SA02M_MPLC_DEADLINE=
    if service_runtime_active mplc4 mplc4.service; then
        # Payload-version stamp — the same format scripts/09-mplc.sh writes
        # (first dotted version, one line): an equal payload then never re-runs
        # the vendor installer on the next full install.
        _mv=$(grep -m1 -oE '[0-9]+(\.[0-9]+){1,3}' "$_src/version.txt" 2>/dev/null)
        if [ -n "$_mv" ] && [ -d /opt/mplc4 ]; then
            printf '%s\n' "$_mv" > /opt/mplc4/.sa02m-payload-version 2>>"$LOG" || true
        fi
        emit_result '{"ok":true,"id":"mplc4","action":"install"}'
        return 0
    fi
    emit_result '{"ok":false,"error":"install_failed","id":"mplc4"}'
    return 1
}

# ── Node-RED: staged payload through the pinned vendor installer ───────────
# Archive validation, compatibility, activation and rollback have one owner.
nodered_install() {
    if [ ! -f /usr/local/lib/sa02m-vendor-install.py ]; then
        emit_result '{"ok":false,"error":"payload_unavailable","id":"node-red"}'
        return 1
    fi
    _nr_rc=0
    _nr_out=$(/usr/bin/python3 -I /usr/local/lib/sa02m-vendor-install.py install node-red --activate 2>>"$LOG") || _nr_rc=$?
    _nr_result=$(printf '%s' "$_nr_out" | /usr/bin/python3 -I -c '
import json,re,sys
try:
    raw=sys.stdin.read(16385)
    if len(raw)>16384: raise ValueError("output limit")
    data=json.loads(raw)
    if not isinstance(data,dict) or data.get("id")!="node-red": raise ValueError("identity")
    if sys.argv[1]=="0" and data.get("ok") is True:
        print(json.dumps(data,separators=(",",":")))
    elif sys.argv[1]!="0" and data.get("ok") is False and isinstance(data.get("error"),str) and re.fullmatch("[a-z_]{1,80}",data["error"]):
        print(json.dumps(data,separators=(",",":")))
    else: raise ValueError("result")
except (ValueError,TypeError):
    sys.exit(1)
' "$_nr_rc") || {
        emit_result '{"ok":false,"error":"install_failed","id":"node-red"}'
        return 1
    }
    emit_result "$_nr_result"
    [ "$_nr_rc" -eq 0 ]
}

# ── CODESYS uninstall (ordering load-bearing; heals the apt poison) ─────────
codesys_uninstall() {
    apt-mark unhold codesyscontrol >>"$LOG" 2>&1 || true
    if [ -x /etc/init.d/codesyscontrol ]; then
        initd_run /etc/init.d/codesyscontrol stop >>"$LOG" 2>&1 || true
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
        MPLC_DEADLINE=
        SA02M_MPLC_DEADLINE=
        mplc_managed_action stop quiet || return 1
        MPLC_DEADLINE=
        SA02M_MPLC_DEADLINE=
    elif mplc4_process_active; then
        emit_result '{"ok":false,"error":"mplc_outcome_unknown","id":"mplc4"}'
        return 1
    fi
    if command -v update-rc.d >/dev/null 2>&1; then
        update-rc.d mplc4 remove >>"$LOG" 2>&1 || true
    fi
    # Tarball install (not dpkg): update-rc.d only drops the rc symlinks, not the
    # SysV script itself. Remove it (and its late generator unit) so no dead
    # wrapper lingers after uninstall (service_present no longer reads the
    # wrappers — the payload probe decides — but a leftover would still confuse
    # a human and old deployed builds). All rm -f — idempotent on a clean
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

# Fixed service-id → stack-policy-ID map (never derived from the request:
# validate_id + the closed case below are the injection floor).
stack_key_for_id() {
    case "$1" in
        codesys) printf 'CODESYS' ;;
        mplc4) printf 'MPLC' ;;
        node-red) printf 'NODERED' ;;
    esac
}

# Persist the operator's decision after a SUCCESSFUL install/uninstall:
# install ⇒ present, uninstall ⇒ disabled (never auto-installed again —
# docs/contracts/installer-refresh-policy.md). No-op when the policy lib is
# absent (older board) or the action failed.
stack_policy_record() {
    _spr_key=$(stack_key_for_id "$1")
    [ -n "$_spr_key" ] || return 0
    command -v sa02m_stack_policy_set >/dev/null 2>&1 || return 0
    sa02m_stack_policy_set "$_spr_key" "$2" 2>>"$LOG" || true
    return 0
}

cmd_install() {
    _id=$1
    validate_id "$_id" || { emit_result '{"ok":false,"error":"invalid_id"}'; return 1; }
    if ! svc_is_installable "$_id"; then
        emit_result '{"ok":false,"error":"not_installable","id":"'"$_id"'"}'
        return 1
    fi
    begin_result "$_id" install || return 1
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: install ${_id}" >>"$LOG" 2>&1
    case "$_id" in
        codesys) codesys_install ;;
        mplc4) mplc4_install ;;
        node-red) nodered_install ;;
    esac
    _rc=$?
    if [ "$_rc" -eq 0 ] && ! svc_runtime_cgroup_proof "$_id" >>"$LOG" 2>&1; then
        emit_result '{"ok":false,"error":"runtime_cgroup_unknown"}'
        return 1
    fi
    [ "$_rc" -eq 0 ] && stack_policy_record "$_id" present
    return $_rc
}

cmd_uninstall() {
    _id=$1
    validate_id "$_id" || { emit_result '{"ok":false,"error":"invalid_id"}'; return 1; }
    if ! svc_is_installable "$_id"; then
        emit_result '{"ok":false,"error":"not_installable","id":"'"$_id"'"}'
        return 1
    fi
    begin_result "$_id" uninstall || return 1
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-service-ctl: uninstall ${_id}" >>"$LOG" 2>&1
    case "$_id" in
        codesys) codesys_uninstall ;;
        mplc4) mplc4_uninstall ;;
        node-red) nodered_uninstall ;;
    esac
    _rc=$?
    [ "$_rc" -eq 0 ] && stack_policy_record "$_id" disabled
    return $_rc
}

# Every candidate comes from the fixed catalogue, never request-supplied unit
# text. Include companion units before any install/start can dispatch their job.
svc_cache_units() {
    while IFS='|' read -r _cu_id _cu_label _cu_units; do
        [ "$_cu_id" = "$1" ] || continue
        printf '%s\n' "$_cu_units" | tr ',' ' '
        case "$1" in
            codesys) printf '%s\n' codemeter.service ;;
            mplc4) printf '%s\n' nginx.service ;;
        esac
        [ -z "${SVC_WORKER_UNIT:-}" ] || printf '%s\n' "$SVC_WORKER_UNIT"
        return 0
    done <<CACHE_UNITS
$SERVICE_DEFS
CACHE_UNITS
    return 1
}

svc_cache_exit() {
    _cache_exit_rc=$?
    trap - EXIT
    if ! svc_cache_finish; then
        echo 'sa02m-web-service-ctl: cache completion unresolved; operation record retained' >>"$LOG" 2>&1
        if [ "$_cache_exit_rc" -eq 0 ]; then
            _cache_exit_rc=1
            SVC_FINAL_JSON='{"ok":false,"error":"cache_completion_unknown"}'
        fi
    fi
    SVC_DEFER_RESULT=0
    [ -z "${SVC_FINAL_JSON:-}" ] || emit_result "$SVC_FINAL_JSON"
    exit "$_cache_exit_rc"
}

svc_worker_verify() {
    case "$1" in
        sa02m-svcctl-install-codesys.service|sa02m-svcctl-uninstall-codesys.service|sa02m-svcctl-install-mplc4.service|sa02m-svcctl-uninstall-mplc4.service|sa02m-svcctl-install-node-red.service|sa02m-svcctl-uninstall-node-red.service) ;;
        *) return 1 ;;
    esac
    [ "$(id -u)" = 0 ] || return 1
    _worker_main=$(sc_run show --property=MainPID --value "$1") || return 1
    [ "$_worker_main" = "$2" ] || return 1
    awk -F: -v u="/system.slice/$1" '$3 == u {found=1} END {exit !found}' "/proc/$$/cgroup"
}

svc_vendor_admit() {
    command -v systemd-run >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1 || {
        emit_result '{"ok":false,"error":"systemd_required"}'; return 1;
    }
    _worker_unit="sa02m-svcctl-${ACTION}-${ID}.service"
    _worker_reply=$(timeout -k 5 600 systemd-run --quiet --wait --pipe --collect \
        --unit="$_worker_unit" --service-type=exec --uid=root --gid=root \
        --property=RuntimeMaxSec=570s --property=TimeoutStopSec=15s \
        --property=KillMode=control-group /bin/sh /usr/local/sbin/sa02m-web-service-ctl.sh \
        --vendor-worker "$ACTION" "$ID" "$SA02M_SVC_OPERATION_ID" 2>>"$LOG")
    _worker_rc=$?
    if [ "$_worker_rc" -ne 0 ]; then
        # A collected failed unit's disappearance cannot turn its client exit
        # into success. A precise worker JSON error is preserved when present.
        case "$_worker_reply" in
            *'"ok":false'*|*'"ok": false'*) emit_result "$_worker_reply" ;;
            *) emit_result '{"ok":false,"error":"vendor_worker_failed"}' ;;
        esac
        return 1
    fi
    case "$_worker_reply" in
        '{'*'}') emit_result "$_worker_reply" ;;
        *) emit_result '{"ok":false,"error":"vendor_worker_failed"}'; return 1 ;;
    esac
}

SVC_WORKER_UNIT=
if [ "${1:-}" = --vendor-worker ]; then
    [ "$#" = 4 ] || { printf '%s\n' '{"ok":false,"error":"invalid_worker"}'; exit 1; }
    _worker_expected="sa02m-svcctl-${2}-${3}.service"
    svc_worker_verify "$_worker_expected" "$$" || { printf '%s\n' '{"ok":false,"error":"invalid_worker"}'; exit 1; }
    SVC_WORKER_UNIT=$_worker_expected
    SA02M_SVC_WORKER_UNIT=$SVC_WORKER_UNIT
    SA02M_SVC_WORKER_PID=$$
    export SA02M_SVC_WORKER_UNIT SA02M_SVC_WORKER_PID
    unset SYSTEMCTL_SKIP_REDIRECT
    set -- "$2" "$3" "$4"
elif [ -n "${SA02M_SVC_WORKER_UNIT:-}" ]; then
    # The coordinator child must still be in the same PID1-owned cgroup and
    # carry the real cache OFD. Environment text alone grants no internal mode.
    svc_worker_verify "$SA02M_SVC_WORKER_UNIT" "${SA02M_SVC_WORKER_PID:-}" &&
        svc_cache_state verify >/dev/null || { printf '%s\n' '{"ok":false,"error":"invalid_worker"}'; exit 1; }
    SVC_WORKER_UNIT=$SA02M_SVC_WORKER_UNIT
fi

ACTION=${1:-}
ID=${2:-}
SA02M_SVC_OPERATION_ID=${3:-${SA02M_SVC_OPERATION_ID:-}}
case "$SA02M_SVC_OPERATION_ID" in
    *[!a-f0-9]*) printf '{"ok":false,"error":"invalid_operation"}\n'; exit 1 ;;
esac
if [ -n "$SA02M_SVC_OPERATION_ID" ] && [ "${#SA02M_SVC_OPERATION_ID}" -ne 32 ]; then
    printf '{"ok":false,"error":"invalid_operation"}\n'; exit 1
fi
export SA02M_SVC_OPERATION_ID
case "$ACTION" in
    start|stop|install|uninstall)
        validate_id "$ID" || { printf '{"ok":false,"error":"invalid_id"}\n'; exit 1; }
        service_id_known "$ID" || { emit_result '{"ok":false,"error":"unknown_service"}'; exit 1; }
        if [ ! -f "$RESULT_DIR/owned-v1" ] || [ "$(cat "$RESULT_DIR/owned-v1")" != "$(cat /proc/sys/kernel/random/boot_id)" ]; then
            SVC_RESULT_FILE="${RESULT_DIR}/${ID}.json"
            emit_result '{"ok":false,"error":"migration_required"}'
            exit 1
        fi
        case "$ACTION" in
            install|uninstall)
                svc_is_installable "$ID" || { emit_result '{"ok":false,"error":"not_installable"}'; exit 1; }
                if [ -z "$SVC_WORKER_UNIT" ]; then
                    svc_vendor_admit
                    exit $?
                fi ;;
        esac
        case "$ID" in
            mplc4|codesys)
                [ -d /run/systemd/system ] || { emit_result '{"ok":false,"error":"systemd_required"}'; exit 1; } ;;
        esac
        case "$ACTION:$ID" in
            start:mplc4|stop:mplc4)
                mplc_budget_begin || { emit_result '{"ok":false,"error":"mplc_budget_unavailable"}'; exit 1; } ;;
        esac
        begin_result "$ID" "$ACTION" || exit 1
        _cache_units=$(svc_cache_units "$ID") || exit 1
        if ! svc_cache_begin "$_cache_units"; then
            emit_result '{"ok":false,"error":"cache_registration_failed"}'
            exit 1
        fi
        SVC_DEFER_RESULT=1
        trap 'svc_cache_exit' EXIT
        ;;
esac

# The root broker serializes the entire helper operation against UART leases.
# The inherited fd is checked by inode; a UI flag or snapshot is not authority.
case "$ACTION:$ID" in
    start:mplc4|stop:mplc4|install:mplc4|uninstall:mplc4|    start:codesys|stop:codesys|install:codesys|uninstall:codesys|    start:vplc|stop:vplc|start:mqtt-bridge|stop:mqtt-bridge)
        case "$ID" in
            mplc4) _guard_unit=mplc4.service ;;
            codesys) _guard_unit=codesyscontrol.service ;;
            vplc) _guard_unit=vplc.service ;;
            mqtt-bridge) _guard_unit=sa02m-modbus-mqtt.service ;;
        esac
        if resolve_unit_for_id "$ID" && [ -n "$_found_unit" ]; then
            _guard_unit=$_found_unit
        fi
        _guard_code='import sys; sys.path.insert(0, "/opt/sa02m-port-coordinator"); from sa02m_ports.service_guard import main; sys.exit(main())'
        SVC_RESULT_FILE="${RESULT_DIR}/${ID}.json"
        if [ -n "${SA02M_SERVICE_GUARD_FD:-}" ]; then
            if ! _guard_reply=$(/usr/bin/python3 -I -c "$_guard_code" --verify "$_guard_unit"); then
                emit_result "${_guard_reply:-{\"ok\":false,\"error\":\"coordinator_unavailable\",\"detail\":\"service guard did not answer\"\}}"
                exit 1
            fi
        else
            if [ "$ACTION" = start ] && start_blocked_by_flasher "$ID"; then
                emit_result '{"ok":false,"error":"flasher_busy"}'
                exit 1
            fi
            _guard_reply=$(/usr/bin/python3 -I -c "$_guard_code" --run "$0" "$ACTION" "$ID" "$_guard_unit")
            _guard_rc=$?
            # The guard prints the codes the panel maps (`coordinator_unavailable`
            # = outage to repair, `port_busy` = a real lease/maintenance conflict);
            # an interpreter that printed nothing is the outage case.
            emit_result "${_guard_reply:-{\"ok\":false,\"error\":\"coordinator_unavailable\",\"detail\":\"service guard did not answer\"\}}"
            exit "$_guard_rc"
        fi
        ;;
esac

case "$ACTION" in
    list)
        svc_list_publish
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
