#!/bin/bash
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-cache"
echo ""

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
    *part=core*)     STATUS_PART=core ;;
esac

allow_public_part() {
    case "$1" in
        cpu|temp|ram|disk) return 0 ;;
        *) return 1 ;;
    esac
}

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
    return 1
}

if ! allow_public_part "$STATUS_PART" && ! check_auth; then
    echo '{"error":"unauthorized"}'
    exit 0
fi

HW_CONF="/etc/sa02m_hw.conf"
SA02M_GPIO_DO=""
SA02M_GPIO_BEEPER=""
SA02M_GPIO_ALARM_LED=""
SA02M_GPIO_USB_POWER=""
[ -f "$HW_CONF" ] && . "$HW_CONF" 2>/dev/null

CACHE_DIR="/tmp/sa02m_status_cache"
mkdir -p "$CACHE_DIR" 2>/dev/null || true
OPTIONAL_SVCS_JSON="[]"
SVC_NGINX_UPTIME_S=0
SVC_FCGIWRAP_UPTIME_S=0

# ── Helpers ───────────────────────────────────────────────────────────────────
json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g; :a;N;$!ba;s/\n/ /g'
}

cache_is_fresh() {
    local file=$1 ttl=$2 now mtime
    [ -f "$file" ] || return 1
    now=$(date +%s 2>/dev/null || echo 0)
    mtime=$(stat -c %Y "$file" 2>/dev/null || echo 0)
    (( now - mtime < ttl ))
}

cache_print_or_build() {
    local file=$1 ttl=$2 builder=$3 tmp
    if cache_is_fresh "$file" "$ttl"; then
        cat "$file"
        return 0
    fi
    tmp="${file}.$$"
    if "$builder" > "$tmp"; then
        mv "$tmp" "$file" 2>/dev/null || cp "$tmp" "$file" 2>/dev/null
        # После mv временный путь исчез — читаем уже атомарно записанный кэш.
        cat "$file"
        rm -f "$tmp"
        return 0
    fi
    rm -f "$tmp"
    return 1
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
    systemctl is-active "$1" 2>/dev/null
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
    local unit=$1 pid boot_j clock_hz proc_start up
    command -v systemctl >/dev/null 2>&1 || { echo 0; return; }
    systemctl is-active --quiet "$unit" 2>/dev/null || { echo 0; return; }
    pid=$(systemctl show -p MainPID --value "$unit" 2>/dev/null | head -n1 | tr -d '\r')
    case "$pid" in ''|0) echo 0; return ;; esac
    [ -r "/proc/${pid}/stat" ] || { echo 0; return; }
    boot_j=$(awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || echo 0)
    clock_hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
    proc_start=$(( boot_j / clock_hz ))
    up=$(( UPTIME_SEC - proc_start ))
    (( up < 0 )) && up=0
    echo "$up"
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
        load=$(systemctl show -p LoadState --value "$u" 2>/dev/null | head -n1 | tr -d '\r')
        case "$load" in not-found|'') continue ;; esac
        id_raw=$(systemctl show -p Id --value "$u" 2>/dev/null | head -n1 | tr -d '\r')
        [ -z "$id_raw" ] && id_raw=$u
        id_disp=$(unit_display_id "$id_raw")
        [ -z "$id_disp" ] && continue
        case "$seen" in *" ${id_disp} "*) continue ;; esac
        seen="${seen}${id_disp} "
        # Не использовать «is-active || echo inactive» — при failed/activating попадёт два слова.
        st_raw=$(systemctl show -p ActiveState --value "$id_raw" 2>/dev/null | head -n1 | tr -d '\r')
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

net_iface_stats() {
    local iface=$1 rx=0 tx=0
    if [ -r "/sys/class/net/${iface}/statistics/rx_bytes" ]; then
        IFS= read -r rx < "/sys/class/net/${iface}/statistics/rx_bytes"
        IFS= read -r tx < "/sys/class/net/${iface}/statistics/tx_bytes"
    fi
    echo "${rx:-0} ${tx:-0}"
}

cpu_usage() {
    local c1 c2
    read -r c1 < /proc/stat
    sleep 0.1
    read -r c2 < /proc/stat
    local a1=($c1) a2=($c2) total1=0 total2=0
    local idle1=${a1[4]:-0} idle2=${a2[4]:-0}
    for v in "${a1[@]:1}"; do (( total1 += v )); done
    for v in "${a2[@]:1}"; do (( total2 += v )); done
    local dt=$(( total2 - total1 )) di=$(( idle2 - idle1 ))
    (( dt > 0 )) && echo $(( (dt - di) * 100 / dt )) || echo 0
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
    df / 2>/dev/null | awk 'NR==2{gsub(/[0-9]+$/,"",$1); print $1}' | xargs basename 2>/dev/null
}

removable_mounted() {
    local mp=$1
    if command -v findmnt >/dev/null 2>&1; then
        findmnt -n "$mp" >/dev/null 2>&1 && return 0
    fi
    mount 2>/dev/null | grep -qF " ${mp} " && return 0
    return 1
}

removable_df_kb() {
    local mp=$1
    df "$mp" 2>/dev/null | awk 'NR==2{print $2,$3,$4}'
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
SERIAL_DRIVER_FILES=""
for _f in /proc/tty/driver/*; do
    [ -f "$_f" ] && SERIAL_DRIVER_FILES="$SERIAL_DRIVER_FILES $_f"
done

rs485_tty_in_use() {
    local dev=$1
    [ -z "$dev" ] && return 1
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

rs485_port_json() {
    local num=$1 dev="/dev/RS-485-${num}"

    if [ ! -e "$dev" ]; then
        printf '{"n":%d,"dev":"","st":"absent","open":0,"tx":0,"rx":0,"fe":0,"pe":0,"oe":0}' "$num"
        return
    fi

    local real ttyname portidx line tx rx fe pe oe inuse
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
        for _f in $SERIAL_DRIVER_FILES; do
            line=$(awk -v idx="$portidx" '$1 ~ ("^" idx ":") { print; exit }' "$_f" 2>/dev/null)
            [ -n "$line" ] && break
        done
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

    printf '{"n":%d,"dev":"%s","st":"present","open":%d,"tx":%s,"rx":%s,"fe":%s,"pe":%s,"oe":%s}' \
        "$num" "$(json_escape "$ttyname")" "$inuse" "$tx" "$rx" "$fe" "$pe" "$oe"
}

build_rs485_array() {
    local json="" i
    for i in 0 1 2 3 4; do
        [ -n "$json" ] && json="${json},"
        json="${json}$(rs485_port_json "$i")"
    done
    printf '%s' "$json"
}

build_rs485_json() {
    printf '{"rs485":[%s]}\n' "$(build_rs485_array)"
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
    if removable_mounted /media/sdcard; then
        SD_M=1
        sd_data=($(removable_df_kb /media/sdcard))
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
}

gather_time_metrics() {
    local rtc_date rtc_time
    DATETIME_SYS=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
    DATETIME_SYS_JSON=$(json_escape "$DATETIME_SYS")
    RTC_DT=""
    if [ -r /sys/class/rtc/rtc0/date ] && [ -r /sys/class/rtc/rtc0/time ]; then
        IFS= read -r rtc_date < /sys/class/rtc/rtc0/date
        IFS= read -r rtc_time < /sys/class/rtc/rtc0/time
        RTC_DT="${rtc_date:-} ${rtc_time:-}"
    elif command -v hwclock >/dev/null 2>&1; then
        RTC_DT=$(hwclock -r 2>/dev/null | head -1 | tr -d '\r')
    fi
    RTC_JSON=$(json_escape "$RTC_DT")
}

gather_uptime_metrics() {
    UPTIME_SEC=$(awk '{printf "%d",$1}' /proc/uptime 2>/dev/null)
    UPTIME_D=$(( UPTIME_SEC / 86400 ))
    UPTIME_H=$(( (UPTIME_SEC % 86400) / 3600 ))
    UPTIME_M=$(( (UPTIME_SEC % 3600) / 60 ))
}

gather_network_metrics() {
    local net0 net1
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

    IP=$(awk '/^[[:space:]]*address /{split($2,a,"/");print a[1];exit}' /etc/network/interfaces.d/eth0.conf 2>/dev/null)
    [ -n "$IP" ] || IP=$(hostname -I 2>/dev/null | awk '{print $1}')

    ETH1_ST=$(json_escape "$ETH1_ST")
    IP=$(json_escape "$IP")
}

gather_load_metrics() {
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

gather_system_metrics() {
    BOARD_RAW=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || awk -F: '/^Hardware/{gsub(/^[ \t]+/,"",$2);print $2;exit}' /proc/cpuinfo 2>/dev/null)
    BOARD=$(json_escape "${BOARD_RAW:-—}")
    CPU_MODEL=$(awk -F: '/^model name|^Processor/{gsub(/^[ \t]+/,"",$2);print $2;exit}' /proc/cpuinfo 2>/dev/null)
    CPU_MODEL=$(json_escape "$CPU_MODEL")
    KERNEL_VER=$(json_escape "$(uname -r 2>/dev/null)")
    STORAGE_AUTO_FORMAT_UI=1
    STORAGE_MOUNT_INSTALLED=0
    [ -x /usr/local/bin/storage-mount.sh ] && STORAGE_MOUNT_INSTALLED=1
    if [ -f /etc/sa02m_storage.conf ]; then
        # shellcheck source=/dev/null
        . /etc/sa02m_storage.conf 2>/dev/null || true
    fi
    case "${STORAGE_AUTO_FORMAT:-1}" in
        1|yes|true|on|ON|Y) STORAGE_AUTO_FORMAT_UI=1 ;;
        *) STORAGE_AUTO_FORMAT_UI=0 ;;
    esac
}

gather_services_metrics() {
    # Статус опроса RS-485: на части образов активен mplc4.service / процесс mplc4,
    # а mplc.service — только алиас или отсутствует. Раньше учитывался только pgrep -x mplc,
    # из-за чего дашборд показывал «Неактивен», хотя порт удерживал опрос.
    # MPLC_UNIT — короткое имя для UI (например mplc4), из systemctl Id / cgroup.
    local proc_pids active_unit mpl_pid cg_unit mpl_slice try up_try
    OPTIONAL_SVCS_JSON="[]"
    mpl_slice=""
    SVC_NGINX=$(svc_is_active nginx)
    SVC_FCGI=$(svc_is_active fcgiwrap)

    MPLC_STATUS="inactive"
    MPLC_UPTIME_S=0
    MPLC_UNIT_RAW=""
    SVC_NGINX_UPTIME_S=0
    SVC_FCGIWRAP_UPTIME_S=0
    gather_uptime_metrics

    SVC_NGINX_UPTIME_S=$(unit_uptime_seconds nginx.service)
    (( SVC_NGINX_UPTIME_S == 0 )) && SVC_NGINX_UPTIME_S=$(unit_uptime_seconds nginx)
    SVC_FCGIWRAP_UPTIME_S=$(unit_uptime_seconds fcgiwrap.service)
    (( SVC_FCGIWRAP_UPTIME_S == 0 )) && SVC_FCGIWRAP_UPTIME_S=$(unit_uptime_seconds fcgiwrap)

    mpl_pid=""
    active_unit=""
    if command -v systemctl >/dev/null 2>&1; then
        for u in mplc.service mplc mplc4.service mplc4; do
            if systemctl is-active --quiet "$u" 2>/dev/null; then
                active_unit=$u
                mpl_pid=$(systemctl show -p MainPID --value "$u" 2>/dev/null | head -n1 | tr -d '\r')
                case "$mpl_pid" in ''|0) mpl_pid="" ;; esac
                break
            fi
        done
    fi

    if [ -n "$active_unit" ]; then
        MPLC_STATUS="active"
        MPLC_UNIT_RAW=$(systemctl show -p Id --value "$active_unit" 2>/dev/null | head -n1 | tr -d '\r')
        mpl_slice=$MPLC_UNIT_RAW
    fi

    if [ -z "$mpl_pid" ]; then
        proc_pids=$(pgrep -x mplc 2>/dev/null || true)
        [ -z "$proc_pids" ] && proc_pids=$(pgrep -x mplc4 2>/dev/null || true)
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

    if [ -z "$MPLC_UNIT_RAW" ] && command -v systemctl >/dev/null 2>&1; then
        for u in mplc4.service mplc.service; do
            case "$(systemctl show -p LoadState --value "$u" 2>/dev/null | head -n1 | tr -d '\r')" in
                not-found|'') continue ;;
            esac
            MPLC_UNIT_RAW=$(systemctl show -p Id --value "$u" 2>/dev/null | head -n1 | tr -d '\r')
            [ -n "$MPLC_UNIT_RAW" ] && mpl_slice=$MPLC_UNIT_RAW
            [ -n "$MPLC_UNIT_RAW" ] && break
        done
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

    MPLC_UNIT_RAW=$(unit_display_id "${MPLC_UNIT_RAW:-}")
    gather_optional_platform_services

    SVC_NGINX=$(json_escape "$SVC_NGINX")
    SVC_FCGI=$(json_escape "$SVC_FCGI")
    MPLC_STATUS=$(json_escape "$MPLC_STATUS")
    MPLC_UNIT=$(json_escape "${MPLC_UNIT_RAW:-}")
}

gather_hardware_metrics() {
    HW_DO=$(gpio_state "$SA02M_GPIO_DO")
    HW_BEEP=$(gpio_state "$SA02M_GPIO_BEEPER")
    HW_LED=$(gpio_state "$SA02M_GPIO_ALARM_LED")
    HW_USB=$(gpio_state "$SA02M_GPIO_USB_POWER")

    HW_CFG=0
    [[ "$SA02M_GPIO_DO" =~ ^[0-9]+$ ]] && HW_CFG=1
    [[ "$SA02M_GPIO_BEEPER" =~ ^[0-9]+$ ]] && HW_CFG=1
    [[ "$SA02M_GPIO_ALARM_LED" =~ ^[0-9]+$ ]] && HW_CFG=1
    [[ "$SA02M_GPIO_USB_POWER" =~ ^[0-9]+$ ]] && HW_CFG=1

    PIN_DO=0
    PIN_BEEP=0
    PIN_LED=0
    PIN_USB=0
    [[ "$SA02M_GPIO_DO" =~ ^[0-9]+$ ]] && PIN_DO=1
    [[ "$SA02M_GPIO_BEEPER" =~ ^[0-9]+$ ]] && PIN_BEEP=1
    [[ "$SA02M_GPIO_ALARM_LED" =~ ^[0-9]+$ ]] && PIN_LED=1
    [[ "$SA02M_GPIO_USB_POWER" =~ ^[0-9]+$ ]] && PIN_USB=1

    HW_I2C_EXP_ABS=$(i2c_expander_absent)
}

gather_main_metrics() {
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
  "sd_pct": ${SD_PCT}
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
  "eth1_operstate": "${ETH1_ST}",
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
  "kernel": "${KERNEL_VER}",
  "storage_auto_format": ${STORAGE_AUTO_FORMAT_UI},
  "storage_mount_installed": ${STORAGE_MOUNT_INSTALLED}
}
JSON
}

print_services_json() {
    cat <<JSON
{
  "svc_nginx": "${SVC_NGINX}",
  "svc_nginx_uptime_s": ${SVC_NGINX_UPTIME_S},
  "svc_fcgiwrap": "${SVC_FCGI}",
  "svc_fcgiwrap_uptime_s": ${SVC_FCGIWRAP_UPTIME_S},
  "mplc_status": "${MPLC_STATUS}",
  "mplc_unit": "${MPLC_UNIT}",
  "mplc_uptime_s": ${MPLC_UPTIME_S},
  "optional_services": ${OPTIONAL_SVCS_JSON:-[]}
}
JSON
}

print_hardware_json() {
    cat <<JSON
{
  "hw_configured": ${HW_CFG},
  "hw_i2c_expander_absent": ${HW_I2C_EXP_ABS},
  "hw_pin_do": ${PIN_DO},
  "hw_pin_beeper": ${PIN_BEEP},
  "hw_pin_alarm_led": ${PIN_LED},
  "hw_pin_usb_power": ${PIN_USB},
  "hw_do": ${HW_DO},
  "hw_beeper": ${HW_BEEP},
  "hw_alarm_led": ${HW_LED},
  "hw_usb_power": ${HW_USB}
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
  "eth1_operstate": "${ETH1_ST}",
  "svc_nginx": "${SVC_NGINX}",
  "svc_nginx_uptime_s": ${SVC_NGINX_UPTIME_S},
  "svc_fcgiwrap": "${SVC_FCGI}",
  "svc_fcgiwrap_uptime_s": ${SVC_FCGIWRAP_UPTIME_S},
  "mplc_status": "${MPLC_STATUS}",
  "mplc_unit": "${MPLC_UNIT}",
  "mplc_uptime_s": ${MPLC_UPTIME_S},
  "optional_services": ${OPTIONAL_SVCS_JSON:-[]},
  "board": "${BOARD}",
  "kernel": "${KERNEL_VER}",
  "ip": "${IP}",
  "hw_configured": ${HW_CFG},
  "hw_i2c_expander_absent": ${HW_I2C_EXP_ABS},
  "hw_pin_do": ${PIN_DO},
  "hw_pin_beeper": ${PIN_BEEP},
  "hw_pin_alarm_led": ${PIN_LED},
  "hw_pin_usb_power": ${PIN_USB},
  "hw_do": ${HW_DO},
  "hw_beeper": ${HW_BEEP},
  "hw_alarm_led": ${HW_LED},
  "hw_usb_power": ${HW_USB}
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
  "eth1_operstate": "${ETH1_ST}",
  "svc_nginx": "${SVC_NGINX}",
  "svc_nginx_uptime_s": ${SVC_NGINX_UPTIME_S},
  "svc_fcgiwrap": "${SVC_FCGI}",
  "svc_fcgiwrap_uptime_s": ${SVC_FCGIWRAP_UPTIME_S},
  "mplc_status": "${MPLC_STATUS}",
  "mplc_unit": "${MPLC_UNIT}",
  "mplc_uptime_s": ${MPLC_UPTIME_S},
  "optional_services": ${OPTIONAL_SVCS_JSON:-[]},
  "board": "${BOARD}",
  "kernel": "${KERNEL_VER}",
  "ip": "${IP}",
  "hw_configured": ${HW_CFG},
  "hw_i2c_expander_absent": ${HW_I2C_EXP_ABS},
  "hw_pin_do": ${PIN_DO},
  "hw_pin_beeper": ${PIN_BEEP},
  "hw_pin_alarm_led": ${PIN_LED},
  "hw_pin_usb_power": ${PIN_USB},
  "hw_do": ${HW_DO},
  "hw_beeper": ${HW_BEEP},
  "hw_alarm_led": ${HW_LED},
  "hw_usb_power": ${HW_USB},
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
  "eth1_operstate": "${ETH1_ST}",
  "svc_nginx": "${SVC_NGINX}",
  "svc_nginx_uptime_s": ${SVC_NGINX_UPTIME_S},
  "svc_fcgiwrap": "${SVC_FCGI}",
  "svc_fcgiwrap_uptime_s": ${SVC_FCGIWRAP_UPTIME_S},
  "mplc_status": "${MPLC_STATUS}",
  "mplc_unit": "${MPLC_UNIT}",
  "mplc_uptime_s": ${MPLC_UPTIME_S},
  "optional_services": ${OPTIONAL_SVCS_JSON:-[]},
  "board": "${BOARD}",
  "kernel": "${KERNEL_VER}",
  "ip": "${IP}",
  "hw_configured": ${HW_CFG},
  "hw_i2c_expander_absent": ${HW_I2C_EXP_ABS},
  "hw_pin_do": ${PIN_DO},
  "hw_pin_beeper": ${PIN_BEEP},
  "hw_pin_alarm_led": ${PIN_LED},
  "hw_pin_usb_power": ${PIN_USB},
  "hw_do": ${HW_DO},
  "hw_beeper": ${HW_BEEP},
  "hw_alarm_led": ${HW_LED},
  "hw_usb_power": ${HW_USB},
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
        build_network_json
        ;;
    load)
        build_load_json
        ;;
    system)
        cache_print_or_build "${CACHE_DIR}/system.json" 30 build_system_json
        ;;
    services)
        cache_print_or_build "${CACHE_DIR}/services.json" 5 build_services_json
        ;;
    hardware)
        cache_print_or_build "${CACHE_DIR}/hardware.json" 10 build_hardware_json
        ;;
    priority)
        build_priority_json
        ;;
    main)
        cache_print_or_build "${CACHE_DIR}/main.json" 10 build_main_json
        ;;
    rs485)
        cache_print_or_build "${CACHE_DIR}/rs485.json" 4 build_rs485_json
        ;;
    core)
        build_core_json
        ;;
    *)
        build_full_json
        ;;
esac
