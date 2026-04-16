#!/bin/bash
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-cache"
echo ""

check_auth() {
    [[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] && return 0
    return 1
}
if ! check_auth; then echo '{"error":"unauthorized"}'; exit 0; fi

HW_CONF="/etc/sa02m_hw.conf"
SA02M_GPIO_DO=""; SA02M_GPIO_BEEPER=""; SA02M_GPIO_ALARM_LED=""
[ -f "$HW_CONF" ] && . "$HW_CONF" 2>/dev/null

# ── Helpers ───────────────────────────────────────────────────────────────────
gpio_state() {
    local n=$1
    if [ -z "$n" ] || ! [[ "$n" =~ ^[0-9]+$ ]]; then echo -1; return; fi
    [ -f "/sys/class/gpio/gpio${n}/value" ] && tr -d '\n' < "/sys/class/gpio/gpio${n}/value" || echo -1
}

svc_is_active() { systemctl is-active "$1" 2>/dev/null; }

net_iface_stats() {
    local iface=$1
    if [ -f "/sys/class/net/${iface}/statistics/rx_bytes" ]; then
        echo "$(cat /sys/class/net/${iface}/statistics/rx_bytes 2>/dev/null || echo 0) $(cat /sys/class/net/${iface}/statistics/tx_bytes 2>/dev/null || echo 0)"
    else
        echo "0 0"
    fi
}

# ── CPU usage (100ms sample) ──────────────────────────────────────────────────
cpu_usage() {
    local c1 c2; read -r c1 < /proc/stat; sleep 0.1; read -r c2 < /proc/stat
    local a1=($c1) a2=($c2) idle1=${c1##* } total1=0 total2=0
    idle1=${a1[4]}; local idle2=${a2[4]}
    for v in "${a1[@]:1}"; do (( total1 += v )); done
    for v in "${a2[@]:1}"; do (( total2 += v )); done
    local dt=$(( total2 - total1 )) di=$(( idle2 - idle1 ))
    (( dt > 0 )) && echo $(( (dt - di) * 100 / dt )) || echo 0
}

# ── RAM ───────────────────────────────────────────────────────────────────────
ram_stats() {
    local total=0 avail=0 swap_total=0 swap_free=0
    while IFS=: read -r k v; do
        v=$(echo "$v" | tr -d ' kB')
        case "$k" in
            MemTotal)    total=$v ;;
            MemAvailable) avail=$v ;;
            SwapTotal)   swap_total=$v ;;
            SwapFree)    swap_free=$v ;;
        esac
    done < /proc/meminfo
    local used=$(( total - avail ))
    local swap_used=$(( swap_total - swap_free ))
    echo "$total $used $avail $swap_total $swap_used"
}

# ── Temperature (hottest zone) ────────────────────────────────────────────────
cpu_temp() {
    local t=0 raw
    for z in /sys/class/thermal/thermal_zone*/temp; do
        [ -f "$z" ] || continue
        raw=$(cat "$z" 2>/dev/null)
        (( raw > t )) && t=$raw
    done
    echo $(( t / 1000 ))
}

# ── RS-485 serial statistics ──────────────────────────────────────────────────
# Build list of /proc/tty/driver/* files once
SERIAL_DRIVER_FILES=""
for _f in /proc/tty/driver/*; do [ -f "$_f" ] && SERIAL_DRIVER_FILES="$SERIAL_DRIVER_FILES $_f"; done

rs485_port_json() {
    local num=$1
    local dev="/dev/RS-485-${num}"

    if [ ! -e "$dev" ]; then
        printf '{"n":%d,"dev":"","st":"absent","open":0,"tx":0,"rx":0,"fe":0,"pe":0,"oe":0}' "$num"
        return
    fi

    local real ttyname portidx
    real=$(readlink -f "$dev" 2>/dev/null); [ -z "$real" ] && real="$dev"
    ttyname=$(basename "$real")
    portidx=$(echo "$ttyname" | tr -dc '0-9')

    local tx=0 rx=0 fe=0 pe=0 oe=0 line=""
    if [ -n "$portidx" ]; then
        for _f in $SERIAL_DRIVER_FILES; do
            line=$(grep "^${portidx}:[[:space:]]" "$_f" 2>/dev/null | head -1)
            [ -n "$line" ] && break
        done
        if [ -n "$line" ]; then
            tx=$(echo "$line" | grep -o 'tx:[0-9]*' | cut -d: -f2); tx=${tx:-0}
            rx=$(echo "$line" | grep -o 'rx:[0-9]*' | cut -d: -f2); rx=${rx:-0}
            fe=$(echo "$line" | grep -o 'fe:[0-9]*' | cut -d: -f2); fe=${fe:-0}
            pe=$(echo "$line" | grep -o 'pe:[0-9]*' | cut -d: -f2); pe=${pe:-0}
            oe=$(echo "$line" | grep -o 'oe:[0-9]*' | cut -d: -f2); oe=${oe:-0}
        fi
    fi

    local inuse=0
    if command -v fuser >/dev/null 2>&1; then
        fuser "$real" >/dev/null 2>&1 && inuse=1
    elif command -v lsof >/dev/null 2>&1; then
        lsof "$real" >/dev/null 2>&1 && inuse=1
    fi

    printf '{"n":%d,"dev":"%s","st":"present","open":%d,"tx":%s,"rx":%s,"fe":%s,"pe":%s,"oe":%s}' \
        "$num" "$ttyname" "$inuse" "$tx" "$rx" "$fe" "$pe" "$oe"
}

RS485_JSON=""
for _i in 0 1 2 3 4; do
    [ -n "$RS485_JSON" ] && RS485_JSON="${RS485_JSON},"
    RS485_JSON="${RS485_JSON}$(rs485_port_json $_i)"
done

# ── Gather all metrics ────────────────────────────────────────────────────────
CPU_USAGE=$(cpu_usage)
RAM_DATA=($(ram_stats))
RAM_TOTAL=${RAM_DATA[0]}; RAM_USED=${RAM_DATA[1]}; RAM_AVAIL=${RAM_DATA[2]}
SWAP_TOTAL=${RAM_DATA[3]}; SWAP_USED=${RAM_DATA[4]}
RAM_PCT=0;  (( RAM_TOTAL  > 0 )) && RAM_PCT=$(( RAM_USED  * 100 / RAM_TOTAL  ))
SWAP_PCT=0; (( SWAP_TOTAL > 0 )) && SWAP_PCT=$(( SWAP_USED * 100 / SWAP_TOTAL ))

TEMP=$(cpu_temp)

DISK_DATA=($(df / 2>/dev/null | awk 'NR==2{print $2,$3,$4}'))
DISK_TOTAL=${DISK_DATA[0]:-0}; DISK_USED=${DISK_DATA[1]:-0}; DISK_FREE=${DISK_DATA[2]:-0}
DISK_PCT=0; (( DISK_TOTAL > 0 )) && DISK_PCT=$(( DISK_USED * 100 / DISK_TOTAL ))

UPTIME_SEC=$(awk '{printf "%d",$1}' /proc/uptime)
UPTIME_D=$(( UPTIME_SEC/86400 )); UPTIME_H=$(( (UPTIME_SEC%86400)/3600 )); UPTIME_M=$(( (UPTIME_SEC%3600)/60 ))

NET0=($(net_iface_stats eth0)); NET0_RX=${NET0[0]:-0}; NET0_TX=${NET0[1]:-0}

ETH1_ST="absent"; NET1_RX=0; NET1_TX=0
if [ -d /sys/class/net/eth1 ]; then
    ETH1_ST=$(cat /sys/class/net/eth1/operstate 2>/dev/null | sed 's/"/\\"/g')
    NET1=($(net_iface_stats eth1)); NET1_RX=${NET1[0]:-0}; NET1_TX=${NET1[1]:-0}
fi

HW_DO=$(gpio_state "$SA02M_GPIO_DO")
HW_BEEP=$(gpio_state "$SA02M_GPIO_BEEPER")
HW_LED=$(gpio_state "$SA02M_GPIO_ALARM_LED")
HW_CFG=0
[[ "$SA02M_GPIO_DO"       =~ ^[0-9]+$ ]] && HW_CFG=1
[[ "$SA02M_GPIO_BEEPER"   =~ ^[0-9]+$ ]] && HW_CFG=1
[[ "$SA02M_GPIO_ALARM_LED" =~ ^[0-9]+$ ]] && HW_CFG=1

SVC_NGINX=$(svc_is_active nginx)
SVC_FCGI=$(svc_is_active fcgiwrap)

IP=$(awk '/^[[:space:]]*address /{split($2,a,"/");print a[1];exit}' /etc/network/interfaces.d/eth0.conf 2>/dev/null \
     || hostname -I 2>/dev/null | awk '{print $1}')

# ── Load averages ─────────────────────────────────────────────────────────────
LOAD_RAW=($(cat /proc/loadavg 2>/dev/null))
LOAD_1=${LOAD_RAW[0]:-0}; LOAD_5=${LOAD_RAW[1]:-0}; LOAD_15=${LOAD_RAW[2]:-0}
PROC_STAT=${LOAD_RAW[3]:-0/0}
PROC_RUN=${PROC_STAT%%/*}; PROC_TOT=${PROC_STAT##*/}

# ── CPU frequency ─────────────────────────────────────────────────────────────
CPU_FREQ_KHZ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo 0)
CPU_MAX_KHZ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || echo 0)
CPU_FREQ_MHZ=$(( CPU_FREQ_KHZ / 1000 ))
CPU_MAX_MHZ=$(( CPU_MAX_KHZ / 1000 ))
# CPU throttle %
CPU_THROTTLE=0
(( CPU_MAX_KHZ > 0 )) && CPU_THROTTLE=$(( CPU_FREQ_KHZ * 100 / CPU_MAX_KHZ ))

# ── CPU temperature zones (individual) ───────────────────────────────────────
TEMP_ZONES=""
_tz_i=0
for _tz in /sys/class/thermal/thermal_zone*/temp; do
    [ -f "$_tz" ] || continue
    _tz_type=$(cat "${_tz%temp}type" 2>/dev/null | sed 's/"/\\"/g' || echo "zone${_tz_i}")
    _tz_raw=$(cat "$_tz" 2>/dev/null)
    _tz_c=$(( _tz_raw / 1000 ))
    [ -n "$TEMP_ZONES" ] && TEMP_ZONES="${TEMP_ZONES},"
    TEMP_ZONES="${TEMP_ZONES}{\"type\":\"${_tz_type}\",\"c\":${_tz_c}}"
    (( _tz_i++ ))
done

# ── Board and kernel ──────────────────────────────────────────────────────────
KERNEL_VER=$(uname -r 2>/dev/null | sed 's/"/\\"/g')
BOARD_RAW=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null \
    || awk -F: '/^Hardware/{gsub(/^[ \t]+/,"",$2);print $2;exit}' /proc/cpuinfo 2>/dev/null)
BOARD=$(echo "${BOARD_RAW:-—}" | sed 's/"/\\"/g' | tr -d '\n\r')

CPU_MODEL=$(awk -F: '/^model name|^Processor/{gsub(/^[ \t]+/,"",$2);print $2;exit}' /proc/cpuinfo 2>/dev/null \
    | sed 's/"/\\"/g')

# ── mplc process ─────────────────────────────────────────────────────────────
MPLC_STATUS="inactive"
MPLC_PID=""
MPLC_UPTIME_S=0
if pgrep -x mplc >/dev/null 2>&1; then
    MPLC_STATUS="active"
    MPLC_PID=$(pgrep -x mplc | head -1)
    # Approximate uptime from /proc/PID/stat field 22 (starttime in jiffies)
    if [ -n "$MPLC_PID" ] && [ -f "/proc/${MPLC_PID}/stat" ]; then
        BOOT_JIFFIES=$(awk '{print $22}' "/proc/${MPLC_PID}/stat" 2>/dev/null || echo 0)
        CLOCK_HZ=$(getconf CLK_TCK 2>/dev/null || echo 100)
        PROC_START_S=$(( BOOT_JIFFIES / CLOCK_HZ ))
        MPLC_UPTIME_S=$(( UPTIME_SEC - PROC_START_S ))
        (( MPLC_UPTIME_S < 0 )) && MPLC_UPTIME_S=0
    fi
fi

# ── Disk I/O stats ────────────────────────────────────────────────────────────
ROOT_DEV=$(df / 2>/dev/null | awk 'NR==2{gsub(/[0-9]+$/,"",$1); print $1}' | xargs basename 2>/dev/null || echo "")
DISK_IO_READ=0; DISK_IO_WRITE=0
if [ -n "$ROOT_DEV" ] && [ -f "/sys/block/${ROOT_DEV}/stat" ]; then
    _stat=($(cat "/sys/block/${ROOT_DEV}/stat" 2>/dev/null))
    DISK_IO_READ=$(( ${_stat[2]:-0} * 512 ))   # sectors read → bytes
    DISK_IO_WRITE=$(( ${_stat[6]:-0} * 512 ))  # sectors written → bytes
fi

# ── Output JSON ──────────────────────────────────────────────────────────────
cat <<JSON
{
  "cpu_usage": ${CPU_USAGE},
  "cpu_freq_mhz": ${CPU_FREQ_MHZ},
  "cpu_max_mhz": ${CPU_MAX_MHZ},
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
  "temp_zones": [${TEMP_ZONES}],
  "disk_total_kb": ${DISK_TOTAL},
  "disk_used_kb": ${DISK_USED},
  "disk_free_kb": ${DISK_FREE},
  "disk_pct": ${DISK_PCT},
  "disk_io_read_b": ${DISK_IO_READ},
  "disk_io_write_b": ${DISK_IO_WRITE},
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
  "svc_fcgiwrap": "${SVC_FCGI}",
  "mplc_status": "${MPLC_STATUS}",
  "mplc_uptime_s": ${MPLC_UPTIME_S},
  "board": "${BOARD}",
  "kernel": "${KERNEL_VER}",
  "ip": "${IP}",
  "hw_configured": ${HW_CFG},
  "hw_do": ${HW_DO},
  "hw_beeper": ${HW_BEEP},
  "hw_alarm_led": ${HW_LED},
  "rs485": [${RS485_JSON}]
}
JSON
