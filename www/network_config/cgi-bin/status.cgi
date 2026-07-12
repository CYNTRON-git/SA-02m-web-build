#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-cache"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STATUS_PART=full
case "${QUERY_STRING:-}" in
    *part=cpu*)      STATUS_PART=cpu ;;
    *part=temp*)     STATUS_PART=temp ;;
    *part=ram*)      STATUS_PART=ram ;;
    *part=disk*)     STATUS_PART=disk ;;
    *part=storage*)  STATUS_PART=storage ;;
    *part=time*)     STATUS_PART=time ;;
    *part=uptime*)   STATUS_PART=uptime ;;
    *part=network*)  STATUS_PART=network ;;
    *part=load*)     STATUS_PART=load ;;
    *part=system*)   STATUS_PART=system ;;
    *part=services*) STATUS_PART=services ;;
    *part=hardware*) STATUS_PART=hardware ;;
    *part=priority*) STATUS_PART=priority ;;
    *part=main*)     STATUS_PART=main ;;
    *part=rs485*)    STATUS_PART=rs485 ;;
    *part=blocks*)   STATUS_PART=blocks ;;
    *part=core*)     STATUS_PART=core ;;
esac

allow_public_part() {
    case "$1" in
        cpu|temp|ram|disk) return 0 ;;
        *) return 1 ;;
    esac
}

check_auth() {
    web_session_check_cookie && return 0
    return 1
}

if ! allow_public_part "$STATUS_PART" && ! check_auth; then
    echo '{"error":"unauthorized"}'
    exit 0
fi

HW_CONF="/etc/sa02m_hw.conf"
. "$SCRIPT_DIR/lib_hw.sh"
HW_VARIANT=""
_hw_variant_conf="/etc/sa02m_hw_variant.conf"
if [ -f "$_hw_variant_conf" ]; then
    _v=$(grep -E '^SA02M_HW_VARIANT=' "$_hw_variant_conf" 2>/dev/null | head -n1 | cut -d= -f2 | tr -d '[:space:]')
    case "$_v" in
        sa02m-1eth|sa02m-2eth) HW_VARIANT=$_v ;;
    esac
fi
STATUS_BLOCKS_CONF="${STATUS_BLOCKS_CONF:-/etc/sa02m_status_blocks.conf}"
[ -f "$STATUS_BLOCKS_CONF" ] && . "$STATUS_BLOCKS_CONF" 2>/dev/null || true

CACHE_DIR="/tmp/sa02m_status_cache"
mkdir -p "$CACHE_DIR" 2>/dev/null || true
# Свежие метрики железа после POST hw_set.cgi (иначе 3 с main.json затирает UI).
if [[ "${QUERY_STRING:-}" == *part=main* ]] && [[ "${QUERY_STRING:-}" == *no_cache=1* ]]; then
    rm -f "${CACHE_DIR}/main.json" "${CACHE_DIR}/main.json.lock" 2>/dev/null || true
fi
if [[ "${QUERY_STRING:-}" == *part=rs485* ]] && [[ "${QUERY_STRING:-}" == *no_cache=1* ]]; then
    rm -f "${CACHE_DIR}/rs485.json" "${CACHE_DIR}/rs485.json.lock" 2>/dev/null || true
fi
OPTIONAL_SVCS_JSON="[]"
SVC_CODESYS_UPTIME_S=0
SVC_CODESYS_UNIT=""
SVC_FCGIWRAP_UPTIME_S=0
SVC_MOSQUITTO="unknown"
SVC_BRIDGE="unknown"
SVC_MOSQUITTO_UPTIME_S=0
SVC_BRIDGE_UPTIME_S=0
SVC_CODESYS_INSTALLED=0
SVC_FCGIWRAP_INSTALLED=0
SVC_MOSQUITTO_INSTALLED=0
SVC_BRIDGE_INSTALLED=0
MPLC_INSTALLED=0
STATUS_CACHE_LOCK_WAIT_SEC="${STATUS_CACHE_LOCK_WAIT_SEC:-0.25}"

# ── Helpers ───────────────────────────────────────────────────────────────────
json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g; :a;N;$!ba;s/\n/ /g'
}

cache_is_fresh() {
    local file=$1 ttl=$2 now mtime
    [ -f "$file" ] || return 1
    now=$(date +%s 2>/dev/null || echo 0)
    mtime=$(stat -c %Y "$file" 2>/dev/null || echo 0)
    (( mtime > now )) && return 1
    (( now - mtime < ttl ))
}

cache_print_or_build() {
    local file=$1 ttl=$2 builder=$3 tmp lock_file rc have_lock=0
    if cache_is_fresh "$file" "$ttl"; then
        cat "$file"
        return 0
    fi

    lock_file="${file}.lock"
    if command -v flock >/dev/null 2>&1; then
        exec 8>"$lock_file" 2>/dev/null || true
        if flock -w "$STATUS_CACHE_LOCK_WAIT_SEC" 8 >/dev/null 2>&1; then
            have_lock=1
            if cache_is_fresh "$file" "$ttl"; then
                cat "$file"
                flock -u 8 >/dev/null 2>&1 || true
                exec 8>&-
                return 0
            fi
        else
            exec 8>&-
            # Кэш уже протух — не отдаём stale при таймауте flock; пересобираем без блокировки.
            tmp="${file}.$$"
            if "$builder" > "$tmp"; then
                mv "$tmp" "$file" 2>/dev/null || cp "$tmp" "$file" 2>/dev/null
                cat "$file"
                rm -f "$tmp"
                return 0
            fi
            rm -f "$tmp"
            if [ -f "$file" ]; then
                cat "$file"
                return 0
            fi
        fi
    fi

    tmp="${file}.$$"
    if "$builder" > "$tmp"; then
        mv "$tmp" "$file" 2>/dev/null || cp "$tmp" "$file" 2>/dev/null
        # После mv временный путь исчез — читаем уже атомарно записанный кэш.
        cat "$file"
        rm -f "$tmp"
        if (( have_lock )); then
            flock -u 8 >/dev/null 2>&1 || true
            exec 8>&-
        fi
        return 0
    fi
    rc=$?
    rm -f "$tmp"
    if (( have_lock )); then
        flock -u 8 >/dev/null 2>&1 || true
        exec 8>&-
    fi
    if [ -f "$file" ]; then
        cat "$file"
        return 0
    fi
    return "$rc"
}

status_timeout_run() {
    local sec=${STATUS_CMD_TIMEOUT_SEC:-2}
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" "$@"
    else
        "$@"
    fi
}

load_serial_map_conf() {
    local conf=/etc/sa02m_serial_map.conf
    SA02M_SERIAL_COUNT=${SA02M_SERIAL_COUNT:-5}
    [ -f "$conf" ] || return 0
    # shellcheck disable=SC1090
    . "$conf" 2>/dev/null || return 0
    case "${SA02M_SERIAL_COUNT:-}" in
        ''|*[!0-9]*) SA02M_SERIAL_COUNT=5 ;;
    esac
}

load_serial_map_conf

# shellcheck source=lib_rtc.sh
. "$SCRIPT_DIR/lib_rtc.sh"

status_block_enabled() {
    case "$1" in
        storage)  [ "${SA02M_STATUS_ENABLE_STORAGE:-1}" = "1" ] ;;
        time)     [ "${SA02M_STATUS_ENABLE_TIME:-1}" = "1" ] ;;
        uptime)   [ "${SA02M_STATUS_ENABLE_UPTIME:-1}" = "1" ] ;;
        network)  [ "${SA02M_STATUS_ENABLE_NETWORK:-1}" = "1" ] ;;
        load)     [ "${SA02M_STATUS_ENABLE_LOAD:-1}" = "1" ] ;;
        system)   [ "${SA02M_STATUS_ENABLE_SYSTEM:-1}" = "1" ] ;;
        services) [ "${SA02M_STATUS_ENABLE_SERVICES:-1}" = "1" ] ;;
        hardware) [ "${SA02M_STATUS_ENABLE_HARDWARE:-1}" = "1" ] ;;
        rs485)    [ "${SA02M_STATUS_ENABLE_RS485:-1}" = "1" ] ;;
        *) return 0 ;;
    esac
}

gpio_state() {
    local n=$1 v
    if [ -z "$n" ] || ! [[ "$n" =~ ^[0-9]+$ ]]; then
        echo -1
        return
    fi
    if [ -r "/sys/class/gpio/gpio${n}/value" ]; then
        IFS= read -r v < "/sys/class/gpio/gpio${n}/value"
        printf '%s\n' "${v:-0}"
    else
        echo -1
    fi
}

svc_is_active() {
    local svc=${1%.service}
    svc=${svc%.socket}
    if pgrep -x "$svc" >/dev/null 2>&1; then
        echo active
    else
        echo inactive
    fi
}

proc_is_running() {
    local name=$1
    command -v pgrep >/dev/null 2>&1 || return 1
    pgrep -x "$name" >/dev/null 2>&1
}

port_is_listening() {
    local port=$1
    command -v ss >/dev/null 2>&1 || return 1
    ss -H -ltn "sport = :${port}" 2>/dev/null | awk 'NR==1{found=1} END{exit(found?0:1)}'
}

# Аптайн по процессам, подобранным pgrep -f (для Node-RED и др.).
proc_uptime_seconds_by_pgrep_f() {
    local pattern=$1 pid best=0 boot_j clock_hz proc_start up
    command -v pgrep >/dev/null 2>&1 || { echo 0; return; }
    case "$pattern" in *\'*|*\"*) echo 0; return ;; esac
    while IFS= read -r pid; do
        case "$pid" in ''|*[!0-9]*) continue ;; esac
        [ -r "/proc/${pid}/stat" ] || continue
        boot_j=$(awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || echo 0)
        clock_hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
        proc_start=$(( boot_j / clock_hz ))
        up=$(( UPTIME_SEC - proc_start ))
        (( up < 0 )) && up=0
        (( up > best )) && best=$up
    done < <(pgrep -f "$pattern" 2>/dev/null || true)
    echo "$best"
}

proc_uptime_seconds_by_name() {
    local name=$1 pid best=0 boot_j clock_hz proc_start up
    command -v pgrep >/dev/null 2>&1 || { echo 0; return; }
    while IFS= read -r pid; do
        case "$pid" in ''|*[!0-9]*) continue ;; esac
        [ -r "/proc/${pid}/stat" ] || continue
        boot_j=$(awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || echo 0)
        clock_hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
        proc_start=$(( boot_j / clock_hz ))
        up=$(( UPTIME_SEC - proc_start ))
        (( up < 0 )) && up=0
        (( up > best )) && best=$up
    done < <(pgrep -x "$name" 2>/dev/null || true)
    echo "$best"
}

fast_service_state() {
    case "$1" in
        ssh|ssh.service)
            if port_is_listening 22 || proc_is_running sshd; then
                echo active
            else
                echo inactive
            fi
            ;;
        nginx|nginx.service)
            if proc_is_running nginx || port_is_listening 80 || port_is_listening 9999; then
                echo active
            else
                echo inactive
            fi
            ;;
        fcgiwrap|fcgiwrap.service)
            if proc_is_running fcgiwrap || [ -S /run/fcgiwrap/fcgiwrap.socket ] || [ -S /run/fcgiwrap.socket ]; then
                echo active
            else
                echo inactive
            fi
            ;;
        mplc|mplc.service|mplc4|mplc4.service)
            if proc_is_running mplc || proc_is_running mplc4 || proc_is_running mplc_monitor; then
                echo active
            else
                echo inactive
            fi
            ;;
        klogic|klogic.service|klogicd|klogicd.service)
            if proc_is_running klogic || proc_is_running klogicd; then
                echo active
            else
                echo inactive
            fi
            ;;
        node-red|node-red.service|nodered|nodered.service)
            if port_is_listening 1880 || pgrep -f '[n]ode-red' >/dev/null 2>&1 || pgrep -f 'node_red' >/dev/null 2>&1; then
                echo active
            else
                echo inactive
            fi
            ;;
        sa02m-modbus-mqtt|sa02m-modbus-mqtt.service)
            if pgrep -f '[m]odbus_mqtt_bridge' >/dev/null 2>&1 \
                || pgrep -f '[s]a02m-modbus-mqtt' >/dev/null 2>&1; then
                echo active
            else
                echo inactive
            fi
            ;;
        docker|docker.service)
            if proc_is_running dockerd || [ -S /run/docker.sock ]; then
                echo active
            else
                echo inactive
            fi
            ;;
        codesyscontrol|codesyscontrol.service|codesys.service|codesys3.service|CODESYSControl.service|CODESYSControlRuntime.service)
            if pgrep -f '[c]odesyscontrol\.bin' >/dev/null 2>&1 \
                || pgrep -f '[C]ODESYSControl' >/dev/null 2>&1 \
                || pgrep -f '[c]odesyscontrol' >/dev/null 2>&1 \
                || { [ -r /var/run/codesyscontrol.pid ] && kill -0 "$(cat /var/run/codesyscontrol.pid 2>/dev/null)" 2>/dev/null; }; then
                echo active
            else
                echo inactive
            fi
            ;;
        *)
            svc_is_active "$1"
            ;;
    esac
}

fast_service_uptime() {
    case "$1" in
        ssh|ssh.service) proc_uptime_seconds_by_name sshd ;;
        nginx|nginx.service) proc_uptime_seconds_by_name nginx ;;
        fcgiwrap|fcgiwrap.service) proc_uptime_seconds_by_name fcgiwrap ;;
        mplc|mplc.service) proc_uptime_seconds_by_name mplc ;;
        mplc4|mplc4.service)
            local up
            up=$(proc_uptime_seconds_by_name mplc4)
            (( up == 0 )) && up=$(proc_uptime_seconds_by_name mplc_monitor)
            echo "$up"
            ;;
        klogic|klogic.service|klogicd|klogicd.service)
            local up up2
            up=$(proc_uptime_seconds_by_name klogicd)
            up2=$(proc_uptime_seconds_by_name klogic)
            (( up2 > up )) && up=$up2
            echo "$up"
            ;;
        node-red|node-red.service|nodered|nodered.service)
            local up=0 u2
            up=$(proc_uptime_seconds_by_pgrep_f '[n]ode-red')
            u2=$(proc_uptime_seconds_by_pgrep_f 'node_red')
            (( u2 > up )) && up=$u2
            echo "$up"
            ;;
        sa02m-modbus-mqtt|sa02m-modbus-mqtt.service)
            local up=0 u2
            up=$(proc_uptime_seconds_by_pgrep_f '[m]odbus_mqtt_bridge')
            u2=$(proc_uptime_seconds_by_pgrep_f '[s]a02m-modbus-mqtt')
            (( u2 > up )) && up=$u2
            echo "$up"
            ;;
        docker|docker.service) proc_uptime_seconds_by_name dockerd ;;
        codesyscontrol|codesyscontrol.service|codesys.service|codesys3.service|CODESYSControl.service|CODESYSControlRuntime.service)
            local up=0 u2 u3 u4
            up=$(proc_uptime_seconds_by_pgrep_f '[c]odesyscontrol\.bin')
            u2=$(proc_uptime_seconds_by_pgrep_f '[C]ODESYSControl')
            u3=$(proc_uptime_seconds_by_pgrep_f '[c]odesyscontrol')
            u4=$(proc_uptime_seconds_by_name codesyscontrol)
            (( u2 > up )) && up=$u2
            (( u3 > up )) && up=$u3
            (( u4 > up )) && up=$u4
            (( up == 0 )) && up=$(uptime_from_cgroup_slice "$1")
            echo "$up"
            ;;
        *) unit_uptime_seconds "$1" ;;
    esac
}

# Короткое имя для UI: mplc4 вместо mplc4.service (аналогично .socket).
unit_display_id() {
    local s=${1:-}
    case "$s" in
        *.service) s=${s%.service} ;;
        *.socket)  s=${s%.socket} ;;
    esac
    printf '%s' "$s"
}

# Аптайн unit в секундах (по MainPID и /proc/<pid>/stat); 0 если не активен или не вычислилось.
# Нужен заранее вызванный gather_uptime_metrics → UPTIME_SEC.
unit_uptime_seconds() {
    local unit=$1 svc
    svc=${unit%.service}
    svc=${svc%.socket}
    proc_uptime_seconds_by_name "$svc"
}

# Аптайн по slice (MainPID=0 у mplc4 и др.): берём максимум среди PID в cgroup.procs.
uptime_from_cgroup_slice() {
    local slice=$1 f pid boot_j clock_hz proc_start up best=0
    case "$slice" in *.service) ;; *) slice="${slice}.service" ;; esac
    for f in \
        "/sys/fs/cgroup/system.slice/${slice}/cgroup.procs" \
        "/sys/fs/cgroup/unified/system.slice/${slice}/cgroup.procs"; do
        [ -r "$f" ] || continue
        while IFS= read -r pid; do
            case "$pid" in ''|*[!0-9]*) continue ;; esac
            [ -r "/proc/${pid}/stat" ] || continue
            boot_j=$(awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || echo 0)
            clock_hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
            proc_start=$(( boot_j / clock_hz ))
            up=$(( UPTIME_SEC - proc_start ))
            (( up < 0 )) && up=0
            (( up > best )) && best=$up
        done < "$f"
        (( best > 0 )) && echo "$best" && return 0
    done
    echo 0
}

# Доп. платформенные unit’ы (если установлены — показываем в «Службы»).
# Порядок: сначала типичные имена; дубликаты по Id после unit_display_id отбрасываем.
gather_optional_platform_services() {
    OPTIONAL_SVCS_JSON="[]"
    command -v systemctl >/dev/null 2>&1 || return 0
    local parts="" sep="" u load id_raw id_disp st_raw st_esc id_esc seen up_sec
    seen=" "
    for u in \
        node-red.service \
        nodered.service \
        codesys.service \
        codesys3.service \
        codesyscontrol.service \
        klogic.service \
        klogicd.service \
        CODESYSControl.service \
        CODESYSControlRuntime.service; do
        load=$(status_timeout_run systemctl show -p LoadState --value "$u" 2>/dev/null | head -n1 | tr -d '\r')
        case "$load" in not-found|'') continue ;; esac
        id_raw=$(status_timeout_run systemctl show -p Id --value "$u" 2>/dev/null | head -n1 | tr -d '\r')
        [ -z "$id_raw" ] && id_raw=$u
        id_disp=$(unit_display_id "$id_raw")
        [ -z "$id_disp" ] && continue
        case "$seen" in *" ${id_disp} "*) continue ;; esac
        seen="${seen}${id_disp} "
        # Не использовать «is-active || echo inactive» — при failed/activating попадёт два слова.
        st_raw=$(status_timeout_run systemctl show -p ActiveState --value "$id_raw" 2>/dev/null | head -n1 | tr -d '\r')
        [ -z "$st_raw" ] && st_raw="inactive"
        # В списке только реально работающие службы (без failed/activating и «пустых» unit).
        [ "$st_raw" != "active" ] && continue
        up_sec=$(unit_uptime_seconds "$id_raw")
        (( up_sec == 0 )) && up_sec=$(uptime_from_cgroup_slice "$id_raw")
        id_esc=$(json_escape "$id_disp")
        st_esc=$(json_escape "$st_raw")
        parts="${parts}${sep}{\"id\":\"${id_esc}\",\"status\":\"${st_esc}\",\"uptime_s\":${up_sec}}"
        sep=,
    done
    [ -n "$parts" ] && OPTIONAL_SVCS_JSON="[${parts}]" || OPTIONAL_SVCS_JSON="[]"
}

# Первый unit CODESYS в «Службы» (замена nginx в UI).
resolve_codesys_unit() {
    local u
    for u in \
        codesyscontrol.service \
        codesys.service \
        codesys3.service \
        CODESYSControl.service \
        CODESYSControlRuntime.service; do
        if systemd_unit_file_installed "$u"; then
            printf '%s' "$u"
            return 0
        fi
    done
    if [ -x /etc/init.d/codesyscontrol ]; then
        printf '%s' "codesyscontrol.service"
        return 0
    fi
    return 1
}

# MPLC4 в UI (mplc_status / mplc_unit). Сюда — только службы,
# которые не дублируют её: KLogic и Node-RED при наличии unit-файла (в т.ч. неактивны).
systemd_unit_file_installed() {
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

# Единый источник статуса управляемых служб — sa02m-web-service-ctl.sh list
# (active / inactive / disabled с учётом user_disabled+mask, как «Управление → Службы»).
declare -A SVC_CTL_STATE=()

load_svc_ctl_states() {
    SVC_CTL_STATE=()
    local CTL=/usr/local/sbin/sa02m-web-service-ctl.sh
    [ -x "$CTL" ] || return 0
    command -v python3 >/dev/null 2>&1 || return 0
    local tmp
    tmp=$(mktemp /tmp/sa02m-svcctl.XXXXXX)
    if ! sudo -n "$CTL" list >"$tmp" 2>/dev/null; then
        rm -f "$tmp"
        return 0
    fi
    while IFS='|' read -r _sid _st; do
        [ -n "$_sid" ] && SVC_CTL_STATE[$_sid]=$_st
    done < <(python3 - "$tmp" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
for s in d.get("services", []):
    sid = s.get("id") or ""
    if not sid:
        continue
    if s.get("user_disabled") or s.get("masked"):
        st = "disabled"
    elif s.get("active") == "active":
        st = "active"
    else:
        st = "inactive"
    print(f"{sid}|{st}")
PY
    )
    rm -f "$tmp"
}

svc_ctl_override() {
    local id=$1
    local -n _out=$2
    local st="${SVC_CTL_STATE[$id]:-}"
    [ -n "$st" ] && _out=$st
}

gather_important_optional_services_json() {
    OPTIONAL_SVCS_JSON="[]"
    local parts="" sep="" u st_raw up_sec id_disp id_esc st_esc label_esc label_raw ctl_id

    if systemd_unit_file_installed docker.service; then
        u="docker.service"
        st_raw=$(fast_service_state "$u")
        svc_ctl_override docker st_raw
        up_sec=$(fast_service_uptime "$u")
        [ "$st_raw" = "disabled" ] && up_sec=0
        (( up_sec == 0 )) && [ "$st_raw" = "active" ] && up_sec=$(uptime_from_cgroup_slice "$u")
        id_disp=$(unit_display_id "$u")
        id_esc=$(json_escape "$id_disp")
        st_esc=$(json_escape "$st_raw")
        label_raw="Docker"
        label_esc=$(json_escape "$label_raw")
        parts="${parts}${sep}{\"id\":\"${id_esc}\",\"label\":\"${label_esc}\",\"status\":\"${st_esc}\",\"uptime_s\":${up_sec},\"installed\":true}"
        sep=,
    fi

    u=""
    if systemd_unit_file_installed klogicd.service; then
        u="klogicd.service"
    elif systemd_unit_file_installed klogic.service; then
        u="klogic.service"
    fi
    if [ -n "$u" ]; then
        st_raw=$(fast_service_state "$u")
        svc_ctl_override klogic st_raw
        up_sec=$(fast_service_uptime "$u")
        [ "$st_raw" = "disabled" ] && up_sec=0
        (( up_sec == 0 )) && [ "$st_raw" = "active" ] && up_sec=$(uptime_from_cgroup_slice "$u")
        id_disp=$(unit_display_id "$u")
        id_esc=$(json_escape "$id_disp")
        st_esc=$(json_escape "$st_raw")
        label_raw="KLogic"
        label_esc=$(json_escape "$label_raw")
        parts="${parts}${sep}{\"id\":\"${id_esc}\",\"label\":\"${label_esc}\",\"status\":\"${st_esc}\",\"uptime_s\":${up_sec},\"installed\":true}"
        sep=,
    fi

    u=""
    if systemd_unit_file_installed node-red.service; then
        u="node-red.service"
    elif systemd_unit_file_installed nodered.service; then
        u="nodered.service"
    fi
    if [ -n "$u" ]; then
        st_raw=$(fast_service_state "$u")
        svc_ctl_override node-red st_raw
        up_sec=$(fast_service_uptime "$u")
        [ "$st_raw" = "disabled" ] && up_sec=0
        (( up_sec == 0 )) && [ "$st_raw" = "active" ] && up_sec=$(uptime_from_cgroup_slice "$u")
        id_disp=$(unit_display_id "$u")
        id_esc=$(json_escape "$id_disp")
        st_esc=$(json_escape "$st_raw")
        label_raw="Node-RED"
        label_esc=$(json_escape "$label_raw")
        parts="${parts}${sep}{\"id\":\"${id_esc}\",\"label\":\"${label_esc}\",\"status\":\"${st_esc}\",\"uptime_s\":${up_sec},\"installed\":true}"
        sep=,
    fi

    [ -n "$parts" ] && OPTIONAL_SVCS_JSON="[${parts}]" || OPTIONAL_SVCS_JSON="[]"
}

net_iface_stats() {
    local iface=$1 rx=0 tx=0
    if [ -r "/sys/class/net/${iface}/statistics/rx_bytes" ]; then
        IFS= read -r rx < "/sys/class/net/${iface}/statistics/rx_bytes"
        IFS= read -r tx < "/sys/class/net/${iface}/statistics/tx_bytes"
    fi
    echo "${rx:-0} ${tx:-0}"
}

iface_ipv4_addr() {
    local iface=$1 ip=""
    if command -v ip >/dev/null 2>&1; then
        ip=$(status_timeout_run ip -o -4 addr show dev "$iface" 2>/dev/null | awk '{print $4}' | head -n1 | cut -d/ -f1 | tr -d '\r')
    fi
    printf '%s' "${ip:-}"
}

iface_mode() {
    local iface=$1
    local conf="/etc/network/interfaces.d/${iface}.conf"
    [ -f "$conf" ] || { echo "unknown"; return; }
    if grep -qiE "iface[[:space:]]+${iface}[[:space:]]+inet[[:space:]]+dhcp" "$conf" 2>/dev/null; then
        echo "dhcp"; return
    fi
    if grep -qiE "^[[:space:]]*address[[:space:]]+" "$conf" 2>/dev/null; then
        echo "static"; return
    fi
    echo "unknown"
}

cpu_usage_from_stat_lines() {
    local c1=$1 c2=$2
    local a1=($c1) a2=($c2) total1=0 total2=0
    local idle1=${a1[4]:-0} idle2=${a2[4]:-0}
    for v in "${a1[@]:1}"; do (( total1 += v )); done
    for v in "${a2[@]:1}"; do (( total2 += v )); done
    local dt=$(( total2 - total1 )) di=$(( idle2 - idle1 ))
    (( dt > 0 )) && echo $(( (dt - di) * 100 / dt )) || echo 0
}

# Option C: /tmp sample cache — delta без sleep при свежем baseline (<2 s); иначе короткий sleep 50 ms.
cpu_usage() {
    local cache_file="${CACHE_DIR}/cpu_sample.state"
    local now c1 c2 cached_ts cached_line pct
    now=$(date +%s 2>/dev/null || echo 0)
    read -r c2 < /proc/stat
    cached_ts=0
    cached_line=""
    if [ -f "$cache_file" ]; then
        IFS= read -r cached_ts cached_line < "$cache_file" || true
        cached_ts=${cached_ts:-0}
    fi
    if [ -n "$cached_line" ] && (( now - cached_ts < 2 )); then
        pct=$(cpu_usage_from_stat_lines "$cached_line" "$c2")
        printf '%s %s\n' "$now" "$c2" > "$cache_file" 2>/dev/null || true
        echo "$pct"
        return 0
    fi
    c1=$c2
    read -r c2 < /proc/stat
    pct=$(cpu_usage_from_stat_lines "$c1" "$c2")
    if [ "$pct" = "0" ]; then
        sleep 0.05
        read -r c2 < /proc/stat
        pct=$(cpu_usage_from_stat_lines "$c1" "$c2")
    fi
    printf '%s %s\n' "$now" "$c2" > "$cache_file" 2>/dev/null || true
    echo "$pct"
}

ram_stats() {
    awk '
        /^MemTotal:/     { total=$2 }
        /^MemAvailable:/ { avail=$2 }
        /^SwapTotal:/    { swap_total=$2 }
        /^SwapFree:/     { swap_free=$2 }
        END {
            used = total - avail
            swap_used = swap_total - swap_free
            printf "%d %d %d %d %d\n", total, used, avail, swap_total, swap_used
        }
    ' /proc/meminfo 2>/dev/null
}

cpu_temp() {
    local hottest=0 raw
    for z in /sys/class/thermal/thermal_zone*/temp; do
        [ -r "$z" ] || continue
        IFS= read -r raw < "$z"
        raw=${raw:-0}
        (( raw > hottest )) && hottest=$raw
    done
    echo $(( hottest / 1000 ))
}

root_disk_usage_kb() {
    df / 2>/dev/null | awk 'NR==2{print $2,$3,$4}'
}

root_disk_device() {
    local root_src dev resolved
    root_src=$(findmnt -n -o SOURCE / 2>/dev/null | head -1)
    if [ -z "$root_src" ]; then
        root_src=$(df / 2>/dev/null | awk 'NR==2{print $1}')
    fi
    [ -z "$root_src" ] && return 1

    resolved=$(readlink -f "$root_src" 2>/dev/null) || resolved="$root_src"
    dev=$(basename "$resolved")

    case "$dev" in
        mmcblk*|nvme*) dev=${dev%%p*} ;;
        sd[a-z]*|vd[a-z]*|hd[a-z]*|xvd[a-z]*) dev=${dev%%[0-9]*} ;;
    esac

    printf '%s\n' "$dev"
}

removable_mounted() {
    local mp=$1
    if command -v findmnt >/dev/null 2>&1; then
        status_timeout_run findmnt -n "$mp" >/dev/null 2>&1 && return 0
    fi
    status_timeout_run mount 2>/dev/null | grep -qF " ${mp} " && return 0
    return 1
}

removable_df_kb() {
    local mp=$1
    df "$mp" 2>/dev/null | awk 'NR==2{print $2,$3,$4}'
}

# microSD: /media/sdcard от storage-mount; иначе любой смонтированный mmcblk1/3, кроме носителя с корнем /.
sdcard_mountpoint() {
    local root_src dev mnt rb db
    root_src=$(findmnt -n -o SOURCE / 2>/dev/null | head -1)
    case "$root_src" in
        /dev/mmcblk*) rb=${root_src#/dev/mmcblk}; rb=${rb%%p*} ;;
        *) rb="" ;;
    esac
    if removable_mounted /media/sdcard; then
        printf '%s\n' /media/sdcard
        return 0
    fi
    while read -r dev mnt _; do
        [ -n "$dev" ] && [ -n "$mnt" ] || continue
        [ "$mnt" = / ] && continue
        case "$dev" in
            /dev/mmcblk1*|/dev/mmcblk3*)
                db=${dev#/dev/mmcblk}; db=${db%%p*}
                if [ -n "$rb" ] && [ "$db" = "$rb" ]; then
                    continue
                fi
                printf '%s\n' "$mnt"
                return 0
                ;;
        esac
    done < /proc/mounts
    return 1
}

i2c_expander_absent() {
    local cache_file="${CACHE_DIR}/i2c_expander_absent" state=0 ig
    if cache_is_fresh "$cache_file" 60; then
        tr -d '\n' < "$cache_file"
        return
    fi
    if [ -d /sys/bus/i2c/devices/2-0041 ]; then
        state=0
    elif [ -c /dev/i2c-2 ] && command -v i2cget >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
        ig=$(command -v i2cget)
        if timeout 1 "$ig" -y 2 0x41 0 >/dev/null 2>&1 || timeout 1 sudo -n "$ig" -y 2 0x41 0 >/dev/null 2>&1; then
            state=0
        else
            state=1
        fi
    fi
    printf '%s' "$state" > "$cache_file" 2>/dev/null || true
    printf '%s' "$state"
}

# ── RS-485 serial statistics ──────────────────────────────────────────────────
RS485_STATS_HELPER="${RS485_STATS_HELPER:-/usr/local/sbin/sa02m-rs485-stats.sh}"
# www-data не может читать каталог /proc/tty/driver — glob даёт пустой список.
SERIAL_DRIVER_FILES=(/proc/tty/driver/serial)
for _f in /proc/tty/driver/*; do
    [ -f "$_f" ] || continue
    case " ${_f} " in
        *" $_f "*) ;;
        *) SERIAL_DRIVER_FILES+=("$_f") ;;
    esac
done

rs485_driver_serial_text() {
    local f=$1 text=""
    [ -n "$f" ] || return 1
    # -r врёт для www-data (каталог /proc/tty/driver недоступен) — пробуем cat, затем sudo.
    text=$(cat "$f" 2>/dev/null) || text=""
    [ -n "$text" ] && printf '%s' "$text" && return 0
    if [ -x "$RS485_STATS_HELPER" ]; then
        text=$(/usr/bin/sudo -n "$RS485_STATS_HELPER" driver 2>/dev/null) || text=""
        [ -n "$text" ] && printf '%s' "$text" && return 0
    fi
    return 1
}

RS485_DRIVER_TEXT=""
declare -A RS485_INUSE

rs485_load_driver_text() {
    local _f text=""
    [ -n "$RS485_DRIVER_TEXT" ] && return 0
    for _f in "${SERIAL_DRIVER_FILES[@]}"; do
        text=$(rs485_driver_serial_text "$_f" 2>/dev/null) || text=""
        [ -n "$text" ] && break
    done
    if [ -z "$text" ] && [ -x "$RS485_STATS_HELPER" ]; then
        text=$(/usr/bin/sudo -n "$RS485_STATS_HELPER" driver 2>/dev/null) || text=""
    fi
    RS485_DRIVER_TEXT="$text"
}

rs485_inuse_batch() {
    local -a devs=("$@") dev flag
    local line
    [ "${#devs[@]}" -gt 0 ] || return 0
    [ -x "$RS485_STATS_HELPER" ] || return 1
    while IFS= read -r line; do
        dev=${line%%:*}
        flag=${line#*:}
        [ -n "$dev" ] || continue
        RS485_INUSE[$dev]=${flag:-0}
    done < <(/usr/bin/sudo -n "$RS485_STATS_HELPER" inuse-batch "${devs[@]}" 2>/dev/null)
}

rs485_tty_in_use() {
    local dev=$1
    [ -z "$dev" ] && return 1
    if [ -n "${RS485_INUSE[$dev]+x}" ]; then
        [ "${RS485_INUSE[$dev]}" = "1" ] && return 0
        return 1
    fi
    if [ -x "$RS485_STATS_HELPER" ]; then
        /usr/bin/sudo -n "$RS485_STATS_HELPER" inuse "$dev" >/dev/null 2>&1 && return 0
    fi
    if command -v timeout >/dev/null 2>&1; then
        if command -v fuser >/dev/null 2>&1; then
            timeout 0.25 fuser "$dev" >/dev/null 2>&1 && return 0
        fi
        if command -v lsof >/dev/null 2>&1; then
            timeout 0.25 lsof "$dev" >/dev/null 2>&1 && return 0
        fi
    else
        if command -v fuser >/dev/null 2>&1; then
            fuser "$dev" >/dev/null 2>&1 && return 0
        fi
        if command -v lsof >/dev/null 2>&1; then
            lsof "$dev" >/dev/null 2>&1 && return 0
        fi
    fi
    return 1
}

# Ядро накапливает fe/pe/oe с загрузки — для UI считаем прирост между опросами.
RS485_ERR_PREV="${CACHE_DIR}/rs485_err_prev.state"
declare -A RS485_ERR_PREV_FE RS485_ERR_PREV_PE RS485_ERR_PREV_OE
RS485_ERR_PREV_LOADED=0

rs485_err_prev_load() {
    local idx fe pe oe
    [ "$RS485_ERR_PREV_LOADED" = "1" ] && return 0
    RS485_ERR_PREV_LOADED=1
    [ -f "$RS485_ERR_PREV" ] || return 0
    while IFS=: read -r idx fe pe oe _; do
        case "$idx" in
            ''|*[!0-9]*) continue ;;
        esac
        RS485_ERR_PREV_FE[$idx]=${fe:-0}
        RS485_ERR_PREV_PE[$idx]=${pe:-0}
        RS485_ERR_PREV_OE[$idx]=${oe:-0}
    done < "$RS485_ERR_PREV"
}

rs485_err_prev_save() {
    local tmp="${RS485_ERR_PREV}.$$" idx
    : > "$tmp"
    for idx in "${!RS485_ERR_PREV_FE[@]}"; do
        printf '%s:%s:%s:%s\n' "$idx" \
            "${RS485_ERR_PREV_FE[$idx]:-0}" \
            "${RS485_ERR_PREV_PE[$idx]:-0}" \
            "${RS485_ERR_PREV_OE[$idx]:-0}" >> "$tmp"
    done
    mv "$tmp" "$RS485_ERR_PREV" 2>/dev/null || cp "$tmp" "$RS485_ERR_PREV" 2>/dev/null
    rm -f "$tmp"
}

rs485_err_deltas() {
    local idx=$1 fe=$2 pe=$3 oe=$4
    local pfe ppe poe dfe dpe doe
    rs485_err_prev_load
    if [ -z "${RS485_ERR_PREV_FE[$idx]+x}" ]; then
        RS485_ERR_PREV_FE[$idx]=$fe
        RS485_ERR_PREV_PE[$idx]=$pe
        RS485_ERR_PREV_OE[$idx]=$oe
        printf '0 0 0'
        return 0
    fi
    pfe=${RS485_ERR_PREV_FE[$idx]:-0}
    ppe=${RS485_ERR_PREV_PE[$idx]:-0}
    poe=${RS485_ERR_PREV_OE[$idx]:-0}
    dfe=$(( fe - pfe )); (( dfe < 0 )) && dfe=$fe
    dpe=$(( pe - ppe )); (( dpe < 0 )) && dpe=$pe
    doe=$(( oe - poe )); (( doe < 0 )) && doe=$oe
    RS485_ERR_PREV_FE[$idx]=$fe
    RS485_ERR_PREV_PE[$idx]=$pe
    RS485_ERR_PREV_OE[$idx]=$oe
    printf '%s %s %s' "$dfe" "$dpe" "$doe"
}

rs485_port_json() {
    local num=$1 dev real ttyname portidx line tx rx fe pe oe fe_d pe_d oe_d deltas inuse driver_text
    dev="/dev/RS-485-${num}"

    if [ ! -e "$dev" ]; then
        printf '{"n":%d,"dev":"","st":"absent","open":0,"tx":0,"rx":0,"fe":0,"pe":0,"oe":0,"fe_d":0,"pe_d":0,"oe_d":0}' "$num"
        return
    fi

    real=$(readlink -f "$dev" 2>/dev/null)
    [ -n "$real" ] || real="$dev"
    ttyname=$(basename "$real")
    portidx=$(printf '%s' "$ttyname" | tr -dc '0-9')
    tx=0
    rx=0
    fe=0
    pe=0
    oe=0

    if [ -n "$portidx" ]; then
        driver_text="$RS485_DRIVER_TEXT"
        if [ -n "$driver_text" ]; then
            line=$(printf '%s\n' "$driver_text" | awk -v idx="$portidx" '$1 ~ ("^" idx ":") { print; exit }')
        else
            line=""
        fi
        if [ -n "$line" ]; then
            tx=$(printf '%s\n' "$line" | grep -o 'tx:[0-9]*' | cut -d: -f2); tx=${tx:-0}
            rx=$(printf '%s\n' "$line" | grep -o 'rx:[0-9]*' | cut -d: -f2); rx=${rx:-0}
            fe=$(printf '%s\n' "$line" | grep -o 'fe:[0-9]*' | cut -d: -f2); fe=${fe:-0}
            pe=$(printf '%s\n' "$line" | grep -o 'pe:[0-9]*' | cut -d: -f2); pe=${pe:-0}
            oe=$(printf '%s\n' "$line" | grep -o 'oe:[0-9]*' | cut -d: -f2); oe=${oe:-0}
        fi
    fi

    inuse=0
    rs485_tty_in_use "$real" && inuse=1

    fe_d=0
    pe_d=0
    oe_d=0
    if [ -n "$portidx" ]; then
        deltas=$(rs485_err_deltas "$portidx" "$fe" "$pe" "$oe")
        fe_d=$(printf '%s' "$deltas" | awk '{print $1}')
        pe_d=$(printf '%s' "$deltas" | awk '{print $2}')
        oe_d=$(printf '%s' "$deltas" | awk '{print $3}')
    fi

    printf '{"n":%d,"dev":"%s","st":"present","open":%d,"tx":%s,"rx":%s,"fe":%s,"pe":%s,"oe":%s,"fe_d":%s,"pe_d":%s,"oe_d":%s}' \
        "$num" "$(json_escape "$ttyname")" "$inuse" "$tx" "$rx" "$fe" "$pe" "$oe" "$fe_d" "$pe_d" "$oe_d"
}

build_rs485_array() {
    local json="" i port_count
    port_count=${SA02M_SERIAL_COUNT:-5}

    if ! status_block_enabled rs485; then
        for (( i=0; i<port_count; i++ )); do
            [ -n "$json" ] && json="${json},"
            json+='{"n":'"$i"',"dev":"","st":"disabled","open":0,"tx":0,"rx":0,"fe":0,"pe":0,"oe":0,"fe_d":0,"pe_d":0,"oe_d":0}'
        done
        printf '%s' "$json"
        return 0
    fi

    rs485_err_prev_load
    RS485_DRIVER_TEXT=""
    RS485_INUSE=()
    rs485_load_driver_text
    local -a rs485_devs=()
    local i real dev
    for (( i=0; i<port_count; i++ )); do
        dev="/dev/RS-485-${i}"
        [ -e "$dev" ] || continue
        real=$(readlink -f "$dev" 2>/dev/null)
        [ -n "$real" ] || real="$dev"
        rs485_devs+=("$real")
    done
    rs485_inuse_batch "${rs485_devs[@]}"
    for (( i=0; i<port_count; i++ )); do
        [ -n "$json" ] && json="${json},"
        json="${json}$(rs485_port_json "$i")"
    done
    rs485_err_prev_save
    printf '%s' "$json"
}

build_rs485_json() {
    printf '{"rs485":[%s]}\n' "$(build_rs485_array)"
}

build_blocks_json() {
    printf '{"storage":%s,"time":%s,"uptime":%s,"network":%s,"load":%s,"system":%s,"services":%s,"hardware":%s,"rs485":%s}\n' \
        "$(status_block_enabled storage && echo 1 || echo 0)" \
        "$(status_block_enabled time && echo 1 || echo 0)" \
        "$(status_block_enabled uptime && echo 1 || echo 0)" \
        "$(status_block_enabled network && echo 1 || echo 0)" \
        "$(status_block_enabled load && echo 1 || echo 0)" \
        "$(status_block_enabled system && echo 1 || echo 0)" \
        "$(status_block_enabled services && echo 1 || echo 0)" \
        "$(status_block_enabled hardware && echo 1 || echo 0)" \
        "$(status_block_enabled rs485 && echo 1 || echo 0)"
}

# ── Metric collection ─────────────────────────────────────────────────────────
gather_priority_metrics() {
    local ram_data disk_data

    CPU_USAGE=$(cpu_usage)

    ram_data=($(ram_stats))
    RAM_TOTAL=${ram_data[0]:-0}
    RAM_USED=${ram_data[1]:-0}
    RAM_AVAIL=${ram_data[2]:-0}
    SWAP_TOTAL=${ram_data[3]:-0}
    SWAP_USED=${ram_data[4]:-0}
    RAM_PCT=0
    SWAP_PCT=0
    (( RAM_TOTAL > 0 )) && RAM_PCT=$(( RAM_USED * 100 / RAM_TOTAL ))
    (( SWAP_TOTAL > 0 )) && SWAP_PCT=$(( SWAP_USED * 100 / SWAP_TOTAL ))

    TEMP=$(cpu_temp)

    disk_data=($(root_disk_usage_kb))
    DISK_TOTAL=${disk_data[0]:-0}
    DISK_USED=${disk_data[1]:-0}
    DISK_FREE=${disk_data[2]:-0}
    DISK_PCT=0
    (( DISK_TOTAL > 0 )) && DISK_PCT=$(( DISK_USED * 100 / DISK_TOTAL ))
}

gather_storage_metrics() {
    if ! status_block_enabled storage; then
        USB_M=0
        USB_TOTAL=0
        USB_USED=0
        USB_FREE=0
        USB_PCT=0
        SD_M=0
        SD_TOTAL=0
        SD_USED=0
        SD_FREE=0
        SD_PCT=0
        DISK_IO_READ=0
        DISK_IO_WRITE=0
        USB_MODEM_PRESENT=0
        USB_MODEM_VENDOR=""
        USB_MODEM_MODEL=""
        USB_MODEM_IFACE=""
        USB_MODEM_IP=""
        USB_MODEM_STATE="unknown"
        USB_MODEM_RX=0
        USB_MODEM_TX=0
        return 0
    fi

    local usb_data sd_data root_dev stat_line

    USB_M=0
    USB_TOTAL=0
    USB_USED=0
    USB_FREE=0
    USB_PCT=0
    if removable_mounted /media/usb; then
        USB_M=1
        usb_data=($(removable_df_kb /media/usb))
        USB_TOTAL=${usb_data[0]:-0}
        USB_USED=${usb_data[1]:-0}
        USB_FREE=${usb_data[2]:-0}
        (( USB_TOTAL > 0 )) && USB_PCT=$(( USB_USED * 100 / USB_TOTAL ))
    fi

    SD_M=0
    SD_TOTAL=0
    SD_USED=0
    SD_FREE=0
    SD_PCT=0
    sd_mp=""
    sd_mp=$(sdcard_mountpoint 2>/dev/null) || sd_mp=""
    if [ -n "$sd_mp" ]; then
        SD_M=1
        sd_data=($(removable_df_kb "$sd_mp"))
        SD_TOTAL=${sd_data[0]:-0}
        SD_USED=${sd_data[1]:-0}
        SD_FREE=${sd_data[2]:-0}
        (( SD_TOTAL > 0 )) && SD_PCT=$(( SD_USED * 100 / SD_TOTAL ))
    fi

    root_dev=$(root_disk_device)
    DISK_IO_READ=0
    DISK_IO_WRITE=0
    if [ -n "$root_dev" ] && [ -r "/sys/block/${root_dev}/stat" ]; then
        stat_line=($(cat "/sys/block/${root_dev}/stat" 2>/dev/null))
        DISK_IO_READ=$(( ${stat_line[2]:-0} * 512 ))
        DISK_IO_WRITE=$(( ${stat_line[6]:-0} * 512 ))
    fi

    gather_usb_modem_metrics
}

# ── USB-модем (CDC-ECM / RNDIS / NCM интерфейс) ──────────────────────────────
# Детектится в двух режимах:
#  1) через /sys/class/net/*   — когда модем уже создал сетевой интерфейс
#     (rndis0/usb0/wwan0/...), тогда есть IP/traffic/operstate.
#  2) через /sys/bus/usb/devices/* — когда модем виден по VID:PID, но ещё
#     не поднял сеть (mass-storage до usb-modeswitch, AT-only, инициализация).
#     В этом случае set USB_MODEM_PRESENT=1, state="init", iface/ip пустые.
gather_usb_modem_metrics() {
    USB_MODEM_PRESENT=0
    USB_MODEM_VENDOR=""
    USB_MODEM_MODEL=""
    USB_MODEM_IFACE=""
    USB_MODEM_IP=""
    USB_MODEM_STATE="unknown"
    USB_MODEM_RX=0
    USB_MODEM_TX=0

    # Известные USB ID модемных вендоров.
    # 19d2=ZTE, 12d1=Huawei, 2c7c=Quectel, 1199=Sierra, 0bdb=Ericsson,
    # 413c=Dell WWAN, 1c9e=Longcheer (ZTE-based), 0af0=Option NV, 2cb7=Fibocom,
    # 05c6=Qualcomm CDMA (SIM7600 в QMI-режиме), 1e0e=SimCom / некоторые ZTE,
    # 1546=u-blox, 1782=Longsung / Meig / некоторые SIM7100, 1bbb=Alcatel/T&A,
    # 2020=Meig / некоторые Fibocom.
    local modem_vendors="19d2 12d1 2c7c 1199 0bdb 413c 1c9e 0af0 2cb7 05c6 1e0e 1546 1782 1bbb 2020"
    local iface vendor product manufacturer usb_dir d _ip

    # 1) Ищем модем через сетевые интерфейсы (наиболее полная информация)
    for iface in $(ls /sys/class/net/ 2>/dev/null); do
        d=$(readlink -f "/sys/class/net/${iface}/device" 2>/dev/null) || continue
        printf '%s' "$d" | grep -q '/usb' || continue

        usb_dir="$d"
        vendor=""
        while [ -n "$usb_dir" ] && [ "$usb_dir" != "/" ]; do
            if [ -f "${usb_dir}/idVendor" ]; then
                vendor=$(tr -d '[:space:]' < "${usb_dir}/idVendor" 2>/dev/null)
                break
            fi
            usb_dir=$(dirname "$usb_dir")
        done
        [ -n "$vendor" ] || continue

        printf ' %s ' "$modem_vendors" | grep -qF " ${vendor} " || continue

        product=$(cat "${usb_dir}/product" 2>/dev/null || echo "")
        manufacturer=$(cat "${usb_dir}/manufacturer" 2>/dev/null || echo "")

        USB_MODEM_PRESENT=1
        USB_MODEM_VENDOR=$(json_escape "${manufacturer}")
        USB_MODEM_MODEL=$(json_escape "${product}")
        USB_MODEM_IFACE=$(json_escape "$iface")
        USB_MODEM_STATE=$(json_escape "$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || echo "unknown")")
        USB_MODEM_RX=$(cat "/sys/class/net/${iface}/statistics/rx_bytes" 2>/dev/null || echo 0)
        USB_MODEM_TX=$(cat "/sys/class/net/${iface}/statistics/tx_bytes" 2>/dev/null || echo 0)
        _ip=$(ip -4 addr show "$iface" 2>/dev/null | awk '/inet /{gsub("/.*","",$2); print $2; exit}')
        USB_MODEM_IP=$(json_escape "${_ip}")
        return 0
    done

    # 2) Fallback: модем есть на USB-шине по VID, но сети ещё нет
    #    (mass-storage до usb-modeswitch, AT-only, инициализация ModemManager).
    for d in /sys/bus/usb/devices/*/idVendor; do
        [ -r "$d" ] || continue
        vendor=$(tr -d '[:space:]' < "$d" 2>/dev/null)
        [ -n "$vendor" ] || continue
        printf ' %s ' "$modem_vendors" | grep -qF " ${vendor} " || continue

        usb_dir=$(dirname "$d")
        product=$(cat "${usb_dir}/product" 2>/dev/null || echo "")
        manufacturer=$(cat "${usb_dir}/manufacturer" 2>/dev/null || echo "")

        USB_MODEM_PRESENT=1
        USB_MODEM_VENDOR=$(json_escape "${manufacturer}")
        USB_MODEM_MODEL=$(json_escape "${product}")
        USB_MODEM_IFACE=""
        USB_MODEM_STATE=$(json_escape "init")
        USB_MODEM_IP=""
        USB_MODEM_RX=0
        USB_MODEM_TX=0
        return 0
    done
}

gather_time_metrics() {
    if ! status_block_enabled time; then
        DATETIME_SYS=""
        DATETIME_SYS_JSON=""
        RTC_DT=""
        RTC_JSON=""
        return 0
    fi

    RTC_DT=$(read_rtc_datetime 2>/dev/null || true)
    DATETIME_SYS=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
    DATETIME_SYS_JSON=$(json_escape "$DATETIME_SYS")
    RTC_JSON=$(json_escape "$RTC_DT")
}

gather_uptime_metrics() {
    if ! status_block_enabled uptime; then
        UPTIME_SEC=0
        UPTIME_D=0
        UPTIME_H=0
        UPTIME_M=0
        return 0
    fi

    UPTIME_SEC=$(awk '{printf "%d",$1}' /proc/uptime 2>/dev/null)
    UPTIME_D=$(( UPTIME_SEC / 86400 ))
    UPTIME_H=$(( (UPTIME_SEC % 86400) / 3600 ))
    UPTIME_M=$(( (UPTIME_SEC % 3600) / 60 ))
}

gather_network_metrics() {
    if ! status_block_enabled network; then
        ETH0_ST="unknown"
        ETH1_ST="unknown"
        NET0_RX=0
        NET0_TX=0
        NET1_RX=0
        NET1_TX=0
        ETH0_IP=""
        ETH1_IP=""
        ETH0_MODE="unknown"
        ETH1_MODE="unknown"
        IP=""
        return 0
    fi

    local net0 net1
    ETH0_ST="absent"
    if [ -d /sys/class/net/eth0 ]; then
        IFS= read -r ETH0_ST < /sys/class/net/eth0/operstate
        ETH0_ST=${ETH0_ST:-unknown}
    fi

    net0=($(net_iface_stats eth0))
    NET0_RX=${net0[0]:-0}
    NET0_TX=${net0[1]:-0}

    ETH1_ST="absent"
    NET1_RX=0
    NET1_TX=0
    if [ -d /sys/class/net/eth1 ]; then
        IFS= read -r ETH1_ST < /sys/class/net/eth1/operstate
        ETH1_ST=${ETH1_ST:-absent}
        net1=($(net_iface_stats eth1))
        NET1_RX=${net1[0]:-0}
        NET1_TX=${net1[1]:-0}
    fi

    ETH0_IP=$(iface_ipv4_addr eth0)
    ETH1_IP=$(iface_ipv4_addr eth1)
    ETH0_MODE=$(iface_mode eth0)
    ETH1_MODE=$(iface_mode eth1)

    # Для верхней панели оставляем единый IP (первый доступный).
    IP="${ETH0_IP:-}"
    [ -n "$IP" ] || IP="${ETH1_IP:-}"
    [ -n "$IP" ] || IP=$(status_timeout_run hostname -I 2>/dev/null | awk '{print $1}')

    ETH0_ST=$(json_escape "$ETH0_ST")
    ETH1_ST=$(json_escape "$ETH1_ST")
    ETH0_IP=$(json_escape "$ETH0_IP")
    ETH1_IP=$(json_escape "$ETH1_IP")
    ETH0_MODE=$(json_escape "$ETH0_MODE")
    ETH1_MODE=$(json_escape "$ETH1_MODE")
    IP=$(json_escape "$IP")
}

gather_load_metrics() {
    if ! status_block_enabled load; then
        LOAD_1=0
        LOAD_5=0
        LOAD_15=0
        PROC_RUN=0
        PROC_TOT=0
        CPU_FREQ_KHZ=0
        CPU_MAX_KHZ=0
        CPU_FREQ_MHZ=0
        CPU_THROTTLE=0
        return 0
    fi

    local load_raw
    load_raw=($(cat /proc/loadavg 2>/dev/null))
    LOAD_1=${load_raw[0]:-0}
    LOAD_5=${load_raw[1]:-0}
    LOAD_15=${load_raw[2]:-0}
    PROC_STAT=${load_raw[3]:-0/0}
    PROC_RUN=${PROC_STAT%%/*}
    PROC_TOT=${PROC_STAT##*/}

    CPU_FREQ_KHZ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo 0)
    CPU_MAX_KHZ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || echo 0)
    CPU_FREQ_MHZ=$(( CPU_FREQ_KHZ / 1000 ))
    CPU_THROTTLE=0
    (( CPU_MAX_KHZ > 0 )) && CPU_THROTTLE=$(( CPU_FREQ_KHZ * 100 / CPU_MAX_KHZ ))
}

is_rt_kernel_status() {
    local kr
    kr=$(uname -r 2>/dev/null || true)
    case "$kr" in
        *-rt*|*rt[0-9]*) return 0 ;;
    esac
    grep -q 'PREEMPT_RT' /proc/version 2>/dev/null
}

gather_system_metrics() {
    if ! status_block_enabled system; then
        BOARD=""
        CPU_MODEL=""
        KERNEL_VER=""
        ARMBIAN_VER=""
        STORAGE_AUTO_FORMAT_UI=0
        STORAGE_MOUNT_INSTALLED=0
        KERNEL_IS_RT=0
        CPU_PROFILE=""
        CPU_GOVERNOR=""
        CPU_PROFILE_UI_AVAILABLE=0
        CPU_MIN_MHZ=0
        CPU_MAX_MHZ_CAP=0
        CPU_FREQ_MHZ=0
        return 0
    fi

    # Название устройства — фиксированный кириллический бренд «ЦИНТРОН СА-02м»
    # с суффиксом «-2» для варианта с двумя Ethernet (выбирается в разделе «Управление»).
    case "${HW_VARIANT:-}" in
        sa02m-2eth) BOARD_RAW="ЦИНТРОН СА-02м-2" ;;
        *)          BOARD_RAW="ЦИНТРОН СА-02м" ;;
    esac
    BOARD=$(json_escape "$BOARD_RAW")

    # Процессор: фиксированное SoC-имя (Allwinner A40i, Cortex-A7)
    # и HW-максимум частоты из cpuinfo_max_freq (kHz → MHz).
    # Формат без префикса числа ядер: «Allwinner A40i Cortex-A7 1200МГц».
    _cpu_hw_max_khz=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || echo 0)
    case "$_cpu_hw_max_khz" in ''|*[!0-9]*) _cpu_hw_max_khz=0 ;; esac
    _cpu_hw_max_mhz=$(( _cpu_hw_max_khz / 1000 ))
    (( _cpu_hw_max_mhz <= 0 )) && _cpu_hw_max_mhz=1200
    CPU_MODEL_RAW="Allwinner A40i Cortex-A7 ${_cpu_hw_max_mhz}МГц"
    CPU_MODEL=$(json_escape "$CPU_MODEL_RAW")

    # Ядро: только X.Y.Z, без суффикса -sa02m+ и т.п.
    _kernel_raw=$(uname -r 2>/dev/null)
    _kernel_short=$(printf '%s' "$_kernel_raw" | sed -nE 's/^([0-9]+\.[0-9]+\.[0-9]+).*/\1/p')
    [ -z "$_kernel_short" ] && _kernel_short="$_kernel_raw"
    KERNEL_VER=$(json_escape "$_kernel_short")

    # ОС: короткое «Debian <point-release>» — из /etc/debian_version (11.11).
    ARMBIAN_VER=""
    if [ -r /etc/debian_version ]; then
        _debian_ver=$(head -n1 /etc/debian_version 2>/dev/null | tr -d '\r\n[:space:]')
        [ -n "$_debian_ver" ] && ARMBIAN_VER="Debian ${_debian_ver}"
    fi
    if [ -z "$ARMBIAN_VER" ] && [ -r /etc/os-release ]; then
        _os_id=$(awk -F= '/^ID=/{v=$2; gsub(/^"|"$/, "", v); print v; exit}' /etc/os-release 2>/dev/null)
        _os_ver=$(awk -F= '/^VERSION_ID=/{v=$2; gsub(/^"|"$/, "", v); print v; exit}' /etc/os-release 2>/dev/null)
        if [ -n "$_os_id" ]; then
            _os_id_cap=$(printf '%s' "$_os_id" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')
            if [ -n "$_os_ver" ]; then
                ARMBIAN_VER="${_os_id_cap} ${_os_ver}"
            else
                ARMBIAN_VER="${_os_id_cap}"
            fi
        fi
    fi
    ARMBIAN_VER=$(json_escape "${ARMBIAN_VER:-}")
    STORAGE_AUTO_FORMAT_UI=1
    STORAGE_MOUNT_INSTALLED=0
    if [ -x /usr/local/bin/storage-mount.sh ] && [ -x /usr/local/sbin/sa02m-set-storage-auto-format ]; then
        STORAGE_MOUNT_INSTALLED=1
    fi
    if [ -f /etc/sa02m_storage.conf ]; then
        # shellcheck source=/dev/null
        . /etc/sa02m_storage.conf 2>/dev/null || true
    fi
    case "${STORAGE_AUTO_FORMAT:-1}" in
        1|yes|true|on|ON|Y) STORAGE_AUTO_FORMAT_UI=1 ;;
        *) STORAGE_AUTO_FORMAT_UI=0 ;;
    esac

    KERNEL_IS_RT=0
    is_rt_kernel_status && KERNEL_IS_RT=1
    SA02M_CPU_PROFILE="adaptive"
    if [ -f /etc/sa02m_cpu.conf ]; then
        # shellcheck source=/dev/null
        . /etc/sa02m_cpu.conf 2>/dev/null || true
    fi
    CPU_PROFILE=$(json_escape "${SA02M_CPU_PROFILE:-adaptive}")
    CPU_GOVERNOR=$(json_escape "$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "")")
    CPU_PROFILE_UI_AVAILABLE=0
    if [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ] && [ "$KERNEL_IS_RT" = "0" ]; then
        CPU_PROFILE_UI_AVAILABLE=1
    fi
    _cpu_min_khz=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq 2>/dev/null || echo 0)
    _cpu_max_cap_khz=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq 2>/dev/null || echo 0)
    CPU_MIN_MHZ=$(( _cpu_min_khz / 1000 ))
    CPU_MAX_MHZ_CAP=$(( _cpu_max_cap_khz / 1000 ))
    _cpu_freq_khz=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo 0)
    CPU_FREQ_MHZ=$(( _cpu_freq_khz / 1000 ))
}

gather_services_metrics() {
    if ! status_block_enabled services; then
        OPTIONAL_SVCS_JSON="[]"
        MPLC_STATUS="unknown"
        MPLC_UPTIME_S=0
        MPLC_UNIT_RAW=""
        SVC_CODESYS="unknown"
        SVC_FCGI="unknown"
        SVC_CODESYS_UPTIME_S=0
        SVC_FCGIWRAP_UPTIME_S=0
        SVC_MOSQUITTO="unknown"
        SVC_BRIDGE="unknown"
        SVC_MOSQUITTO_UPTIME_S=0
        SVC_BRIDGE_UPTIME_S=0
        MPLC_UNIT=""
        SVC_CODESYS_INSTALLED=0
        SVC_FCGIWRAP_INSTALLED=0
        SVC_MOSQUITTO_INSTALLED=0
        SVC_BRIDGE_INSTALLED=0
        MPLC_INSTALLED=0
        return 0
    fi

    # Статус опроса RS-485: на части образов активен mplc4.service / процесс mplc4,
    # а mplc.service — только алиас или отсутствует. Раньше учитывался только pgrep -x mplc,
    # из-за чего дашборд показывал «Неактивен», хотя порт удерживал опрос.
    # MPLC_UNIT — короткое имя для UI (например mplc4), из systemctl Id / cgroup.
    local proc_pids active_unit mpl_pid cg_unit mpl_slice try up_try
    OPTIONAL_SVCS_JSON="[]"
    mpl_slice=""
    SVC_CODESYS_UNIT=$(resolve_codesys_unit 2>/dev/null || true)
    if [ -n "$SVC_CODESYS_UNIT" ]; then
        SVC_CODESYS=$(fast_service_state "$SVC_CODESYS_UNIT")
    else
        SVC_CODESYS=$(fast_service_state codesyscontrol)
    fi
    SVC_FCGI=$(fast_service_state fcgiwrap)
    SVC_MOSQUITTO=$(fast_service_state mosquitto)
    SVC_BRIDGE=$(fast_service_state sa02m-modbus-mqtt)

    load_svc_ctl_states
    svc_ctl_override codesys SVC_CODESYS
    svc_ctl_override mosquitto SVC_MOSQUITTO
    svc_ctl_override mqtt-bridge SVC_BRIDGE

    MPLC_STATUS="inactive"
    MPLC_UPTIME_S=0
    MPLC_UNIT_RAW=""
    SVC_CODESYS_UPTIME_S=0
    SVC_FCGIWRAP_UPTIME_S=0
    gather_uptime_metrics

    if [ -n "$SVC_CODESYS_UNIT" ]; then
        SVC_CODESYS_UPTIME_S=$(fast_service_uptime "$SVC_CODESYS_UNIT")
    fi
    (( SVC_CODESYS_UPTIME_S == 0 )) && SVC_CODESYS_UPTIME_S=$(fast_service_uptime codesyscontrol)
    (( SVC_CODESYS_UPTIME_S == 0 )) && SVC_CODESYS_UPTIME_S=$(fast_service_uptime codesyscontrol.service)
    SVC_FCGIWRAP_UPTIME_S=$(fast_service_uptime fcgiwrap.service)
    (( SVC_FCGIWRAP_UPTIME_S == 0 )) && SVC_FCGIWRAP_UPTIME_S=$(fast_service_uptime fcgiwrap)
    SVC_MOSQUITTO_UPTIME_S=$(fast_service_uptime mosquitto.service)
    (( SVC_MOSQUITTO_UPTIME_S == 0 )) && SVC_MOSQUITTO_UPTIME_S=$(fast_service_uptime mosquitto)
    SVC_BRIDGE_UPTIME_S=$(fast_service_uptime sa02m-modbus-mqtt.service)
    (( SVC_BRIDGE_UPTIME_S == 0 )) && SVC_BRIDGE_UPTIME_S=$(fast_service_uptime sa02m-modbus-mqtt)

    mpl_pid=""
    active_unit=""
    for u in mplc.service mplc mplc4.service mplc4; do
        if [ "$(fast_service_state "$u")" = "active" ]; then
            active_unit=$u
            break
        fi
    done

    if [ -n "$active_unit" ]; then
        MPLC_STATUS="active"
        MPLC_UNIT_RAW=$active_unit
        mpl_slice=$active_unit
    fi

    if [ -z "$mpl_pid" ]; then
        proc_pids=$(pgrep -x mplc 2>/dev/null || true)
        [ -z "$proc_pids" ] && proc_pids=$(pgrep -x mplc4 2>/dev/null || true)
        [ -z "$proc_pids" ] && proc_pids=$(pgrep -x mplc_monitor 2>/dev/null || true)
        if [ -n "$proc_pids" ]; then
            MPLC_STATUS="active"
            mpl_pid=${proc_pids%%$'\n'*}
        fi
    fi

    if [ -n "$mpl_pid" ] && [ -r "/proc/${mpl_pid}/stat" ]; then
        BOOT_JIFFIES=$(awk '{print $22}' "/proc/${mpl_pid}/stat" 2>/dev/null || echo 0)
        CLOCK_HZ=$(getconf CLK_TCK 2>/dev/null || echo 100)
        PROC_START_S=$(( BOOT_JIFFIES / CLOCK_HZ ))
        MPLC_UPTIME_S=$(( UPTIME_SEC - PROC_START_S ))
        (( MPLC_UPTIME_S < 0 )) && MPLC_UPTIME_S=0
    fi

    if [ -z "$MPLC_UNIT_RAW" ] && [ -n "$mpl_pid" ] && [ -r "/proc/${mpl_pid}/cgroup" ]; then
        cg_unit=$(grep -oE 'mplc[a-zA-Z0-9._-]*\.service' "/proc/${mpl_pid}/cgroup" 2>/dev/null | head -n1)
        [ -n "$cg_unit" ] && MPLC_UNIT_RAW=$cg_unit
        [ -n "$cg_unit" ] && mpl_slice=$cg_unit
    fi

    if [ -z "$MPLC_UNIT_RAW" ] && [ -n "$mpl_pid" ]; then
        if proc_is_running mplc4 || proc_is_running mplc_monitor; then
            MPLC_UNIT_RAW="mplc4"
            mpl_slice="mplc4.service"
        elif proc_is_running mplc; then
            MPLC_UNIT_RAW="mplc"
            mpl_slice="mplc.service"
        fi
    fi

    if [ "$MPLC_STATUS" = "active" ] && (( MPLC_UPTIME_S == 0 )); then
        case "$mpl_slice" in
            '') [ -n "$MPLC_UNIT_RAW" ] && mpl_slice=$MPLC_UNIT_RAW ;;
        esac
        case "$mpl_slice" in
            *.service) ;;
            *) [ -n "$mpl_slice" ] && mpl_slice="${mpl_slice}.service" ;;
        esac
        [ -n "$mpl_slice" ] && MPLC_UPTIME_S=$(uptime_from_cgroup_slice "$mpl_slice")
        if (( MPLC_UPTIME_S == 0 )); then
            for try in mplc4.service mplc.service; do
                up_try=$(uptime_from_cgroup_slice "$try")
                (( up_try > 0 )) && MPLC_UPTIME_S=$up_try && break
            done
        fi
    fi

    # Опрос остановлен, но unit в образе есть — иначе виджет «Службы» скрывает строку (нет mplc_unit и не active).
    if [ -z "$MPLC_UNIT_RAW" ] && [ "$MPLC_STATUS" != "active" ]; then
        local tryfile
        for tryfile in \
            /lib/systemd/system/mplc4.service \
            /usr/lib/systemd/system/mplc4.service \
            /etc/systemd/system/mplc4.service; do
            if [ -f "$tryfile" ]; then
                MPLC_UNIT_RAW="mplc4.service"
                break
            fi
        done
    fi
    if [ -z "$MPLC_UNIT_RAW" ] && [ "$MPLC_STATUS" != "active" ]; then
        for tryfile in \
            /lib/systemd/system/mplc.service \
            /usr/lib/systemd/system/mplc.service \
            /etc/systemd/system/mplc.service; do
            if [ -f "$tryfile" ]; then
                MPLC_UNIT_RAW="mplc.service"
                break
            fi
        done
    fi

    if [ "$MPLC_STATUS" = "disabled" ]; then
        MPLC_UPTIME_S=0
    fi
    if [ "$SVC_CODESYS" = "disabled" ]; then
        SVC_CODESYS_UPTIME_S=0
    fi
    if [ "$SVC_MOSQUITTO" = "disabled" ]; then
        SVC_MOSQUITTO_UPTIME_S=0
    fi
    if [ "$SVC_BRIDGE" = "disabled" ]; then
        SVC_BRIDGE_UPTIME_S=0
    fi

    svc_ctl_override mplc4 MPLC_STATUS
    [ "$MPLC_STATUS" = "disabled" ] && MPLC_UPTIME_S=0

    MPLC_UNIT_RAW=$(unit_display_id "${MPLC_UNIT_RAW:-}")
    # KLogic / Node-RED в optional_services только при установленном unit-файле;
    # активность — fast_service_state (без systemctl show, чтобы не зависать на dbus).
    gather_important_optional_services_json

    SVC_CODESYS_INSTALLED=0
    if resolve_codesys_unit >/dev/null 2>&1 || [ -x /etc/init.d/codesyscontrol ]; then
        SVC_CODESYS_INSTALLED=1
    fi
    SVC_FCGIWRAP_INSTALLED=0
    if systemd_unit_file_installed fcgiwrap.service \
        || systemd_unit_file_installed fcgiwrap.socket \
        || [ -S /run/fcgiwrap/fcgiwrap.socket ] \
        || [ -S /run/fcgiwrap.socket ]; then
        SVC_FCGIWRAP_INSTALLED=1
    fi
    SVC_MOSQUITTO_INSTALLED=0
    systemd_unit_file_installed mosquitto.service && SVC_MOSQUITTO_INSTALLED=1
    SVC_BRIDGE_INSTALLED=0
    systemd_unit_file_installed sa02m-modbus-mqtt.service && SVC_BRIDGE_INSTALLED=1
    MPLC_INSTALLED=0
    if systemd_unit_file_installed mplc4.service || systemd_unit_file_installed mplc.service; then
        MPLC_INSTALLED=1
    fi

    SVC_CODESYS=$(json_escape "$SVC_CODESYS")
    SVC_FCGI=$(json_escape "$SVC_FCGI")
    SVC_MOSQUITTO=$(json_escape "$SVC_MOSQUITTO")
    SVC_BRIDGE=$(json_escape "$SVC_BRIDGE")
    MPLC_STATUS=$(json_escape "$MPLC_STATUS")
    MPLC_UNIT=$(json_escape "${MPLC_UNIT_RAW:-}")
}

gather_hardware_metrics() {
    HW_POLL_DISABLED=0
    if ! status_block_enabled hardware; then
        HW_POLL_DISABLED=1
        sa02m_hw_metrics_cache_refresh
        return 0
    fi

    sa02m_hw_collect_metrics
    sa02m_hw_metrics_cache_save
    [ "${HW_VARIANT:-}" = "sa02m-2eth" ] && HW_DO=null
}

gather_main_metrics() {
    SVC_CODESYS="unknown"
    SVC_FCGI="unknown"
    SVC_MOSQUITTO="unknown"
    SVC_BRIDGE="unknown"
    MPLC_STATUS="unknown"
    MPLC_UPTIME_S=0
    MPLC_UNIT=""
    OPTIONAL_SVCS_JSON="[]"
    SVC_CODESYS_UPTIME_S=0
    SVC_FCGIWRAP_UPTIME_S=0
    SVC_MOSQUITTO_UPTIME_S=0
    SVC_BRIDGE_UPTIME_S=0
    SVC_CODESYS_INSTALLED=0
    SVC_FCGIWRAP_INSTALLED=0
    SVC_MOSQUITTO_INSTALLED=0
    SVC_BRIDGE_INSTALLED=0
    MPLC_INSTALLED=0

    gather_storage_metrics
    gather_uptime_metrics
    gather_network_metrics
    gather_load_metrics
    gather_system_metrics
    gather_services_metrics
    gather_hardware_metrics
    gather_time_metrics
}

# ── JSON rendering ────────────────────────────────────────────────────────────
print_priority_json() {
    cat <<JSON
{
  "cpu_usage": ${CPU_USAGE},
  "ram_total_kb": ${RAM_TOTAL},
  "ram_used_kb": ${RAM_USED},
  "ram_free_kb": ${RAM_AVAIL},
  "ram_pct": ${RAM_PCT},
  "swap_total_kb": ${SWAP_TOTAL},
  "swap_used_kb": ${SWAP_USED},
  "swap_pct": ${SWAP_PCT},
  "temp_c": ${TEMP},
  "disk_total_kb": ${DISK_TOTAL},
  "disk_used_kb": ${DISK_USED},
  "disk_free_kb": ${DISK_FREE},
  "disk_pct": ${DISK_PCT}
}
JSON
}

print_cpu_json() {
    cat <<JSON
{
  "cpu_usage": ${CPU_USAGE}
}
JSON
}

print_ram_json() {
    cat <<JSON
{
  "ram_total_kb": ${RAM_TOTAL},
  "ram_used_kb": ${RAM_USED},
  "ram_free_kb": ${RAM_AVAIL},
  "ram_pct": ${RAM_PCT},
  "swap_total_kb": ${SWAP_TOTAL},
  "swap_used_kb": ${SWAP_USED},
  "swap_pct": ${SWAP_PCT}
}
JSON
}

print_temp_json() {
    cat <<JSON
{
  "temp_c": ${TEMP}
}
JSON
}

print_disk_json() {
    cat <<JSON
{
  "disk_total_kb": ${DISK_TOTAL},
  "disk_used_kb": ${DISK_USED},
  "disk_free_kb": ${DISK_FREE},
  "disk_pct": ${DISK_PCT}
}
JSON
}

print_storage_json() {
    cat <<JSON
{
  "disk_io_read_b": ${DISK_IO_READ},
  "disk_io_write_b": ${DISK_IO_WRITE},
  "usb_mounted": ${USB_M},
  "usb_total_kb": ${USB_TOTAL},
  "usb_used_kb": ${USB_USED},
  "usb_free_kb": ${USB_FREE},
  "usb_pct": ${USB_PCT},
  "sd_mounted": ${SD_M},
  "sd_total_kb": ${SD_TOTAL},
  "sd_used_kb": ${SD_USED},
  "sd_free_kb": ${SD_FREE},
  "sd_pct": ${SD_PCT},
  "usb_modem_present": ${USB_MODEM_PRESENT},
  "usb_modem_vendor": "${USB_MODEM_VENDOR}",
  "usb_modem_model": "${USB_MODEM_MODEL}",
  "usb_modem_iface": "${USB_MODEM_IFACE}",
  "usb_modem_ip": "${USB_MODEM_IP}",
  "usb_modem_state": "${USB_MODEM_STATE}",
  "usb_modem_rx": ${USB_MODEM_RX},
  "usb_modem_tx": ${USB_MODEM_TX}
}
JSON
}

print_time_json() {
    cat <<JSON
{
  "datetime_sys": "${DATETIME_SYS_JSON}",
  "rtc_datetime": "${RTC_JSON}"
}
JSON
}

print_uptime_json() {
    cat <<JSON
{
  "uptime_sec": ${UPTIME_SEC},
  "uptime_str": "${UPTIME_D}д ${UPTIME_H}ч ${UPTIME_M}м"
}
JSON
}

print_network_json() {
    cat <<JSON
{
  "net_rx_bytes": ${NET0_RX},
  "net_tx_bytes": ${NET0_TX},
  "net1_rx_bytes": ${NET1_RX},
  "net1_tx_bytes": ${NET1_TX},
  "eth0_operstate": "${ETH0_ST}",
  "eth1_operstate": "${ETH1_ST}",
  "eth0_ip": "${ETH0_IP}",
  "eth1_ip": "${ETH1_IP}",
  "eth0_mode": "${ETH0_MODE}",
  "eth1_mode": "${ETH1_MODE}",
  "ip": "${IP}"
}
JSON
}

print_load_json() {
    cat <<JSON
{
  "load_1": ${LOAD_1},
  "load_5": ${LOAD_5},
  "load_15": ${LOAD_15},
  "proc_running": ${PROC_RUN},
  "proc_total": ${PROC_TOT},
  "cpu_freq_mhz": ${CPU_FREQ_MHZ},
  "cpu_throttle": ${CPU_THROTTLE}
}
JSON
}

print_system_json() {
    cat <<JSON
{
  "board": "${BOARD}",
  "cpu_model": "${CPU_MODEL}",
  "armbian_version": "${ARMBIAN_VER}",
  "kernel": "${KERNEL_VER}",
  "kernel_is_rt": ${KERNEL_IS_RT:-0},
  "cpu_profile": "${CPU_PROFILE:-adaptive}",
  "cpu_governor": "${CPU_GOVERNOR:-}",
  "cpu_freq_mhz": ${CPU_FREQ_MHZ:-0},
  "cpu_profile_ui_available": ${CPU_PROFILE_UI_AVAILABLE:-0},
  "cpu_min_mhz": ${CPU_MIN_MHZ:-0},
  "cpu_max_mhz_cap": ${CPU_MAX_MHZ_CAP:-0},
  "storage_auto_format": ${STORAGE_AUTO_FORMAT_UI},
  "storage_mount_installed": ${STORAGE_MOUNT_INSTALLED}
}
JSON
}

print_services_json() {
    cat <<JSON
{
  "svc_codesys": "${SVC_CODESYS}",
  "svc_codesys_uptime_s": ${SVC_CODESYS_UPTIME_S},
  "svc_codesys_installed": ${SVC_CODESYS_INSTALLED},
  "svc_fcgiwrap": "${SVC_FCGI}",
  "svc_fcgiwrap_uptime_s": ${SVC_FCGIWRAP_UPTIME_S},
  "svc_fcgiwrap_installed": ${SVC_FCGIWRAP_INSTALLED},
  "svc_mosquitto": "${SVC_MOSQUITTO}",
  "svc_mosquitto_uptime_s": ${SVC_MOSQUITTO_UPTIME_S},
  "svc_mosquitto_installed": ${SVC_MOSQUITTO_INSTALLED},
  "svc_bridge": "${SVC_BRIDGE}",
  "svc_bridge_uptime_s": ${SVC_BRIDGE_UPTIME_S},
  "svc_bridge_installed": ${SVC_BRIDGE_INSTALLED},
  "mplc_status": "${MPLC_STATUS}",
  "mplc_unit": "${MPLC_UNIT}",
  "mplc_uptime_s": ${MPLC_UPTIME_S},
  "mplc_installed": ${MPLC_INSTALLED},
  "optional_services": ${OPTIONAL_SVCS_JSON:-[]}
}
JSON
}

print_hardware_json() {
    cat <<JSON
{
  "hw_backend": "$(json_escape "${HW_BACKEND:-unknown}")",
  "hw_configured": ${HW_CFG},
  "hw_poll_disabled": ${HW_POLL_DISABLED:-0},
  "hw_i2c_expander_absent": ${HW_I2C_EXP_ABS},
  "hw_i2c_busy": ${HW_I2C_BUSY},
  "hw_pin_do": ${PIN_DO},
  "hw_pin_beeper": ${PIN_BEEP},
  "hw_pin_alarm_led": ${PIN_LED},
  "hw_pin_usb_power": ${PIN_USB},
  "hw_do": ${HW_DO},
  "hw_beeper": ${HW_BEEP},
  "hw_alarm_led": ${HW_LED},
  "hw_usb_power": ${HW_USB},
  "hw_variant": "${HW_VARIANT}"
}
JSON
}

print_main_json() {
    cat <<JSON
{
  "cpu_freq_mhz": ${CPU_FREQ_MHZ},
  "cpu_throttle": ${CPU_THROTTLE},
  "cpu_model": "${CPU_MODEL}",
  "disk_io_read_b": ${DISK_IO_READ},
  "disk_io_write_b": ${DISK_IO_WRITE},
  "usb_mounted": ${USB_M},
  "usb_total_kb": ${USB_TOTAL},
  "usb_used_kb": ${USB_USED},
  "usb_free_kb": ${USB_FREE},
  "usb_pct": ${USB_PCT},
  "sd_mounted": ${SD_M},
  "sd_total_kb": ${SD_TOTAL},
  "sd_used_kb": ${SD_USED},
  "sd_free_kb": ${SD_FREE},
  "sd_pct": ${SD_PCT},
  "usb_modem_present": ${USB_MODEM_PRESENT},
  "usb_modem_vendor": "${USB_MODEM_VENDOR}",
  "usb_modem_model": "${USB_MODEM_MODEL}",
  "usb_modem_iface": "${USB_MODEM_IFACE}",
  "usb_modem_ip": "${USB_MODEM_IP}",
  "usb_modem_state": "${USB_MODEM_STATE}",
  "usb_modem_rx": ${USB_MODEM_RX},
  "usb_modem_tx": ${USB_MODEM_TX},
  "datetime_sys": "${DATETIME_SYS_JSON}",
  "rtc_datetime": "${RTC_JSON}",
  "uptime_sec": ${UPTIME_SEC},
  "uptime_str": "${UPTIME_D}д ${UPTIME_H}ч ${UPTIME_M}м",
  "load_1": ${LOAD_1},
  "load_5": ${LOAD_5},
  "load_15": ${LOAD_15},
  "proc_running": ${PROC_RUN},
  "proc_total": ${PROC_TOT},
  "net_rx_bytes": ${NET0_RX},
  "net_tx_bytes": ${NET0_TX},
  "net1_rx_bytes": ${NET1_RX},
  "net1_tx_bytes": ${NET1_TX},
  "eth0_operstate": "${ETH0_ST}",
  "eth1_operstate": "${ETH1_ST}",
  "eth0_ip": "${ETH0_IP}",
  "eth1_ip": "${ETH1_IP}",
  "eth0_mode": "${ETH0_MODE}",
  "eth1_mode": "${ETH1_MODE}",
  "svc_codesys": "${SVC_CODESYS}",
  "svc_codesys_uptime_s": ${SVC_CODESYS_UPTIME_S},
  "svc_codesys_installed": ${SVC_CODESYS_INSTALLED},
  "svc_fcgiwrap": "${SVC_FCGI}",
  "svc_fcgiwrap_uptime_s": ${SVC_FCGIWRAP_UPTIME_S},
  "svc_fcgiwrap_installed": ${SVC_FCGIWRAP_INSTALLED},
  "svc_mosquitto": "${SVC_MOSQUITTO}",
  "svc_mosquitto_uptime_s": ${SVC_MOSQUITTO_UPTIME_S},
  "svc_mosquitto_installed": ${SVC_MOSQUITTO_INSTALLED},
  "svc_bridge": "${SVC_BRIDGE}",
  "svc_bridge_uptime_s": ${SVC_BRIDGE_UPTIME_S},
  "svc_bridge_installed": ${SVC_BRIDGE_INSTALLED},
  "mplc_status": "${MPLC_STATUS}",
  "mplc_unit": "${MPLC_UNIT}",
  "mplc_uptime_s": ${MPLC_UPTIME_S},
  "mplc_installed": ${MPLC_INSTALLED},
  "optional_services": ${OPTIONAL_SVCS_JSON:-[]},
  "board": "${BOARD}",
  "armbian_version": "${ARMBIAN_VER}",
  "kernel": "${KERNEL_VER}",
  "ip": "${IP}",
  "storage_auto_format": ${STORAGE_AUTO_FORMAT_UI},
  "storage_mount_installed": ${STORAGE_MOUNT_INSTALLED},
  "hw_backend": "$(json_escape "${HW_BACKEND:-unknown}")",
  "hw_configured": ${HW_CFG},
  "hw_poll_disabled": ${HW_POLL_DISABLED:-0},
  "hw_i2c_expander_absent": ${HW_I2C_EXP_ABS},
  "hw_i2c_busy": ${HW_I2C_BUSY},
  "hw_pin_do": ${PIN_DO},
  "hw_pin_beeper": ${PIN_BEEP},
  "hw_pin_alarm_led": ${PIN_LED},
  "hw_pin_usb_power": ${PIN_USB},
  "hw_do": ${HW_DO},
  "hw_beeper": ${HW_BEEP},
  "hw_alarm_led": ${HW_LED},
  "hw_usb_power": ${HW_USB},
  "hw_variant": "${HW_VARIANT}"
}
JSON
}

print_core_json() {
    cat <<JSON
{
  "cpu_usage": ${CPU_USAGE},
  "cpu_freq_mhz": ${CPU_FREQ_MHZ},
  "cpu_throttle": ${CPU_THROTTLE},
  "cpu_model": "${CPU_MODEL}",
  "ram_total_kb": ${RAM_TOTAL},
  "ram_used_kb": ${RAM_USED},
  "ram_free_kb": ${RAM_AVAIL},
  "ram_pct": ${RAM_PCT},
  "swap_total_kb": ${SWAP_TOTAL},
  "swap_used_kb": ${SWAP_USED},
  "swap_pct": ${SWAP_PCT},
  "temp_c": ${TEMP},
  "disk_total_kb": ${DISK_TOTAL},
  "disk_used_kb": ${DISK_USED},
  "disk_free_kb": ${DISK_FREE},
  "disk_pct": ${DISK_PCT},
  "disk_io_read_b": ${DISK_IO_READ},
  "disk_io_write_b": ${DISK_IO_WRITE},
  "usb_mounted": ${USB_M},
  "usb_total_kb": ${USB_TOTAL},
  "usb_used_kb": ${USB_USED},
  "usb_free_kb": ${USB_FREE},
  "usb_pct": ${USB_PCT},
  "sd_mounted": ${SD_M},
  "sd_total_kb": ${SD_TOTAL},
  "sd_used_kb": ${SD_USED},
  "sd_free_kb": ${SD_FREE},
  "sd_pct": ${SD_PCT},
  "usb_modem_present": ${USB_MODEM_PRESENT},
  "usb_modem_vendor": "${USB_MODEM_VENDOR}",
  "usb_modem_model": "${USB_MODEM_MODEL}",
  "usb_modem_iface": "${USB_MODEM_IFACE}",
  "usb_modem_ip": "${USB_MODEM_IP}",
  "usb_modem_state": "${USB_MODEM_STATE}",
  "usb_modem_rx": ${USB_MODEM_RX},
  "usb_modem_tx": ${USB_MODEM_TX},
  "datetime_sys": "${DATETIME_SYS_JSON}",
  "rtc_datetime": "${RTC_JSON}",
  "uptime_sec": ${UPTIME_SEC},
  "uptime_str": "${UPTIME_D}д ${UPTIME_H}ч ${UPTIME_M}м",
  "load_1": ${LOAD_1},
  "load_5": ${LOAD_5},
  "load_15": ${LOAD_15},
  "proc_running": ${PROC_RUN},
  "proc_total": ${PROC_TOT},
  "net_rx_bytes": ${NET0_RX},
  "net_tx_bytes": ${NET0_TX},
  "net1_rx_bytes": ${NET1_RX},
  "net1_tx_bytes": ${NET1_TX},
  "eth0_operstate": "${ETH0_ST}",
  "eth1_operstate": "${ETH1_ST}",
  "eth0_ip": "${ETH0_IP}",
  "eth1_ip": "${ETH1_IP}",
  "eth0_mode": "${ETH0_MODE}",
  "eth1_mode": "${ETH1_MODE}",
  "svc_codesys": "${SVC_CODESYS}",
  "svc_codesys_uptime_s": ${SVC_CODESYS_UPTIME_S},
  "svc_codesys_installed": ${SVC_CODESYS_INSTALLED},
  "svc_fcgiwrap": "${SVC_FCGI}",
  "svc_fcgiwrap_uptime_s": ${SVC_FCGIWRAP_UPTIME_S},
  "svc_fcgiwrap_installed": ${SVC_FCGIWRAP_INSTALLED},
  "svc_mosquitto": "${SVC_MOSQUITTO}",
  "svc_mosquitto_uptime_s": ${SVC_MOSQUITTO_UPTIME_S},
  "svc_mosquitto_installed": ${SVC_MOSQUITTO_INSTALLED},
  "svc_bridge": "${SVC_BRIDGE}",
  "svc_bridge_uptime_s": ${SVC_BRIDGE_UPTIME_S},
  "svc_bridge_installed": ${SVC_BRIDGE_INSTALLED},
  "mplc_status": "${MPLC_STATUS}",
  "mplc_unit": "${MPLC_UNIT}",
  "mplc_uptime_s": ${MPLC_UPTIME_S},
  "mplc_installed": ${MPLC_INSTALLED},
  "optional_services": ${OPTIONAL_SVCS_JSON:-[]},
  "board": "${BOARD}",
  "armbian_version": "${ARMBIAN_VER}",
  "kernel": "${KERNEL_VER}",
  "ip": "${IP}",
  "storage_auto_format": ${STORAGE_AUTO_FORMAT_UI},
  "storage_mount_installed": ${STORAGE_MOUNT_INSTALLED},
  "hw_backend": "$(json_escape "${HW_BACKEND:-unknown}")",
  "hw_configured": ${HW_CFG},
  "hw_poll_disabled": ${HW_POLL_DISABLED:-0},
  "hw_i2c_expander_absent": ${HW_I2C_EXP_ABS},
  "hw_i2c_busy": ${HW_I2C_BUSY},
  "hw_pin_do": ${PIN_DO},
  "hw_pin_beeper": ${PIN_BEEP},
  "hw_pin_alarm_led": ${PIN_LED},
  "hw_pin_usb_power": ${PIN_USB},
  "hw_do": ${HW_DO},
  "hw_beeper": ${HW_BEEP},
  "hw_alarm_led": ${HW_LED},
  "hw_usb_power": ${HW_USB},
  "hw_variant": "${HW_VARIANT}",
  "rs485": []
}
JSON
}

print_full_json() {
    cat <<JSON
{
  "cpu_usage": ${CPU_USAGE},
  "cpu_freq_mhz": ${CPU_FREQ_MHZ},
  "cpu_throttle": ${CPU_THROTTLE},
  "cpu_model": "${CPU_MODEL}",
  "ram_total_kb": ${RAM_TOTAL},
  "ram_used_kb": ${RAM_USED},
  "ram_free_kb": ${RAM_AVAIL},
  "ram_pct": ${RAM_PCT},
  "swap_total_kb": ${SWAP_TOTAL},
  "swap_used_kb": ${SWAP_USED},
  "swap_pct": ${SWAP_PCT},
  "temp_c": ${TEMP},
  "disk_total_kb": ${DISK_TOTAL},
  "disk_used_kb": ${DISK_USED},
  "disk_free_kb": ${DISK_FREE},
  "disk_pct": ${DISK_PCT},
  "disk_io_read_b": ${DISK_IO_READ},
  "disk_io_write_b": ${DISK_IO_WRITE},
  "usb_mounted": ${USB_M},
  "usb_total_kb": ${USB_TOTAL},
  "usb_used_kb": ${USB_USED},
  "usb_free_kb": ${USB_FREE},
  "usb_pct": ${USB_PCT},
  "sd_mounted": ${SD_M},
  "sd_total_kb": ${SD_TOTAL},
  "sd_used_kb": ${SD_USED},
  "sd_free_kb": ${SD_FREE},
  "sd_pct": ${SD_PCT},
  "usb_modem_present": ${USB_MODEM_PRESENT},
  "usb_modem_vendor": "${USB_MODEM_VENDOR}",
  "usb_modem_model": "${USB_MODEM_MODEL}",
  "usb_modem_iface": "${USB_MODEM_IFACE}",
  "usb_modem_ip": "${USB_MODEM_IP}",
  "usb_modem_state": "${USB_MODEM_STATE}",
  "usb_modem_rx": ${USB_MODEM_RX},
  "usb_modem_tx": ${USB_MODEM_TX},
  "datetime_sys": "${DATETIME_SYS_JSON}",
  "rtc_datetime": "${RTC_JSON}",
  "uptime_sec": ${UPTIME_SEC},
  "uptime_str": "${UPTIME_D}д ${UPTIME_H}ч ${UPTIME_M}м",
  "load_1": ${LOAD_1},
  "load_5": ${LOAD_5},
  "load_15": ${LOAD_15},
  "proc_running": ${PROC_RUN},
  "proc_total": ${PROC_TOT},
  "net_rx_bytes": ${NET0_RX},
  "net_tx_bytes": ${NET0_TX},
  "net1_rx_bytes": ${NET1_RX},
  "net1_tx_bytes": ${NET1_TX},
  "eth0_operstate": "${ETH0_ST}",
  "eth1_operstate": "${ETH1_ST}",
  "eth0_ip": "${ETH0_IP}",
  "eth1_ip": "${ETH1_IP}",
  "eth0_mode": "${ETH0_MODE}",
  "eth1_mode": "${ETH1_MODE}",
  "svc_codesys": "${SVC_CODESYS}",
  "svc_codesys_uptime_s": ${SVC_CODESYS_UPTIME_S},
  "svc_codesys_installed": ${SVC_CODESYS_INSTALLED},
  "svc_fcgiwrap": "${SVC_FCGI}",
  "svc_fcgiwrap_uptime_s": ${SVC_FCGIWRAP_UPTIME_S},
  "svc_fcgiwrap_installed": ${SVC_FCGIWRAP_INSTALLED},
  "svc_mosquitto": "${SVC_MOSQUITTO}",
  "svc_mosquitto_uptime_s": ${SVC_MOSQUITTO_UPTIME_S},
  "svc_mosquitto_installed": ${SVC_MOSQUITTO_INSTALLED},
  "svc_bridge": "${SVC_BRIDGE}",
  "svc_bridge_uptime_s": ${SVC_BRIDGE_UPTIME_S},
  "svc_bridge_installed": ${SVC_BRIDGE_INSTALLED},
  "mplc_status": "${MPLC_STATUS}",
  "mplc_unit": "${MPLC_UNIT}",
  "mplc_uptime_s": ${MPLC_UPTIME_S},
  "mplc_installed": ${MPLC_INSTALLED},
  "optional_services": ${OPTIONAL_SVCS_JSON:-[]},
  "board": "${BOARD}",
  "armbian_version": "${ARMBIAN_VER}",
  "kernel": "${KERNEL_VER}",
  "ip": "${IP}",
  "storage_auto_format": ${STORAGE_AUTO_FORMAT_UI},
  "storage_mount_installed": ${STORAGE_MOUNT_INSTALLED},
  "hw_backend": "$(json_escape "${HW_BACKEND:-unknown}")",
  "hw_configured": ${HW_CFG},
  "hw_poll_disabled": ${HW_POLL_DISABLED:-0},
  "hw_i2c_expander_absent": ${HW_I2C_EXP_ABS},
  "hw_i2c_busy": ${HW_I2C_BUSY},
  "hw_pin_do": ${PIN_DO},
  "hw_pin_beeper": ${PIN_BEEP},
  "hw_pin_alarm_led": ${PIN_LED},
  "hw_pin_usb_power": ${PIN_USB},
  "hw_do": ${HW_DO},
  "hw_beeper": ${HW_BEEP},
  "hw_alarm_led": ${HW_LED},
  "hw_usb_power": ${HW_USB},
  "hw_variant": "${HW_VARIANT}",
  "rs485": [${RS485_JSON}]
}
JSON
}

build_priority_json() {
    gather_priority_metrics
    print_priority_json
}

build_cpu_json() {
    CPU_USAGE=$(cpu_usage)
    print_cpu_json
}

build_ram_json() {
    local ram_data
    ram_data=($(ram_stats))
    RAM_TOTAL=${ram_data[0]:-0}
    RAM_USED=${ram_data[1]:-0}
    RAM_AVAIL=${ram_data[2]:-0}
    SWAP_TOTAL=${ram_data[3]:-0}
    SWAP_USED=${ram_data[4]:-0}
    RAM_PCT=0
    SWAP_PCT=0
    (( RAM_TOTAL > 0 )) && RAM_PCT=$(( RAM_USED * 100 / RAM_TOTAL ))
    (( SWAP_TOTAL > 0 )) && SWAP_PCT=$(( SWAP_USED * 100 / SWAP_TOTAL ))
    print_ram_json
}

build_temp_json() {
    TEMP=$(cpu_temp)
    print_temp_json
}

build_disk_json() {
    local disk_data
    disk_data=($(root_disk_usage_kb))
    DISK_TOTAL=${disk_data[0]:-0}
    DISK_USED=${disk_data[1]:-0}
    DISK_FREE=${disk_data[2]:-0}
    DISK_PCT=0
    (( DISK_TOTAL > 0 )) && DISK_PCT=$(( DISK_USED * 100 / DISK_TOTAL ))
    print_disk_json
}

build_storage_json() {
    gather_storage_metrics
    print_storage_json
}

build_time_json() {
    gather_time_metrics
    print_time_json
}

build_uptime_json() {
    gather_uptime_metrics
    print_uptime_json
}

build_network_json() {
    gather_network_metrics
    print_network_json
}

build_load_json() {
    gather_load_metrics
    print_load_json
}

build_system_json() {
    gather_system_metrics
    print_system_json
}

build_services_json() {
    gather_services_metrics
    print_services_json
}

build_hardware_json() {
    gather_hardware_metrics
    print_hardware_json
}

build_main_json() {
    gather_main_metrics
    print_main_json
}

build_core_json() {
    gather_priority_metrics
    gather_main_metrics
    print_core_json
}

build_full_json() {
    gather_priority_metrics
    gather_main_metrics
    RS485_JSON=$(build_rs485_array)
    print_full_json
}

case "$STATUS_PART" in
    cpu)
        build_cpu_json
        ;;
    temp)
        build_temp_json
        ;;
    ram)
        build_ram_json
        ;;
    disk)
        build_disk_json
        ;;
    storage)
        cache_print_or_build "${CACHE_DIR}/storage.json" 10 build_storage_json
        ;;
    time)
        build_time_json
        ;;
    uptime)
        build_uptime_json
        ;;
    network)
        cache_print_or_build "${CACHE_DIR}/network.json" 8 build_network_json
        ;;
    load)
        build_load_json
        ;;
    system)
        cache_print_or_build "${CACHE_DIR}/system.json" 30 build_system_json
        ;;
    services)
        cache_print_or_build "${CACHE_DIR}/services.json" 45 build_services_json
        ;;
    hardware)
        cache_print_or_build "${CACHE_DIR}/hardware.json" 10 build_hardware_json
        ;;
    priority)
        build_priority_json
        ;;
    main)
        build_main_json
        ;;
    rs485)
        cache_print_or_build "${CACHE_DIR}/rs485.json" 8 build_rs485_json
        ;;
    blocks)
        cache_print_or_build "${CACHE_DIR}/blocks.json" 60 build_blocks_json
        ;;
    core)
        build_core_json
        ;;
    *)
        build_full_json
        ;;
esac
