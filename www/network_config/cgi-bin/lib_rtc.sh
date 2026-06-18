#!/bin/bash
# Shared RTC helpers for network_config CGI (read external battery-backed RTC).

sa02m_rtc_timeout_run() {
    local sec=${1:-2}
    shift || true
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" "$@"
    else
        "$@"
    fi
}

sa02m_rtc_bcd_byte() {
    local hex=${1#0x}
    hex=${hex,,}
    local v=0
    [[ "$hex" =~ ^[0-9a-f]{1,2}$ ]] || return 1
    v=$((16#$hex))
    printf '%d' $(( (v & 0xF) + 10 * ((v >> 4) & 0xF) ))
}

sa02m_rtc_is_internal_node() {
    local rtc=$1 devlink base
    devlink=$(readlink -f "/sys/class/rtc/${rtc}/device" 2>/dev/null) || devlink=""
    case "$devlink" in
        *1c20400.rtc*|*sun6i-rtc*) return 0 ;;
    esac
    base=$(basename "$devlink" 2>/dev/null)
    case "$base" in
        1c20400.rtc) return 0 ;;
    esac
    return 1
}

sa02m_rtc_i2c_tool() {
    local tool
    for tool in i2cget /usr/sbin/i2cget /usr/bin/i2cget; do
        if command -v "${tool##*/}" >/dev/null 2>&1; then
            command -v "${tool##*/}" 2>/dev/null
            return 0
        fi
        [ -x "$tool" ] || continue
        printf '%s\n' "$tool"
        return 0
    done
    return 1
}

sa02m_rtc_i2c_read_reg() {
    local bus=$1 addr=$2 reg=$3 tool raw
    tool=$(sa02m_rtc_i2c_tool) || return 1
    raw=$(sa02m_rtc_timeout_run 2 sudo -n "$tool" -y "$bus" "$addr" "$reg" 2>/dev/null) \
        || raw=$(sa02m_rtc_timeout_run 2 "$tool" -y "$bus" "$addr" "$reg" 2>/dev/null) \
        || return 1
    case "$raw" in
        0x[0-9a-fA-F][0-9a-fA-F]) printf '%s' "$raw"; return 0 ;;
    esac
    return 1
}

sa02m_rtc_find_i2c_chip() {
    local want=$1 dev entry bus addr name
    SA02M_RTC_I2C_BUS=""
    SA02M_RTC_I2C_ADDR=""
    for dev in /sys/bus/i2c/devices/*-*; do
        [ -d "$dev" ] || continue
        name=$(tr -d '\r\n' < "$dev/name" 2>/dev/null) || continue
        [ "$name" = "$want" ] || continue
        entry=${dev##*/}
        bus=${entry%-*}
        addr=0x${entry#*-}
        SA02M_RTC_I2C_BUS=$bus
        SA02M_RTC_I2C_ADDR=$addr
        return 0
    done
    return 1
}

sa02m_rtc_valid_ymd() {
    local y=$1 mo=$2 d=$3
    [ "$y" -ge 2020 ] 2>/dev/null || return 1
    [ "$y" -le 2099 ] 2>/dev/null || return 1
    [ "$mo" -ge 1 ] 2>/dev/null || return 1
    [ "$mo" -le 12 ] 2>/dev/null || return 1
    [ "$d" -ge 1 ] 2>/dev/null || return 1
    [ "$d" -le 31 ] 2>/dev/null || return 1
    return 0
}

# DS3231: time regs 0x00..0x06 (sec..year). Kernel driver rtc-ds3231 may be absent.
read_ds3231_i2c_datetime() {
    local bus addr raw s m h dom mo y
    sa02m_rtc_find_i2c_chip ds3231 || return 1
    bus=$SA02M_RTC_I2C_BUS
    addr=$SA02M_RTC_I2C_ADDR

    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x00) || return 1
    s=$(sa02m_rtc_bcd_byte "$raw") || return 1
    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x01) || return 1
    m=$(sa02m_rtc_bcd_byte "$raw") || return 1
    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x02) || return 1
    h=$(sa02m_rtc_bcd_byte "$raw") || return 1
    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x04) || return 1
    dom=$(sa02m_rtc_bcd_byte "$raw") || return 1
    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x05) || return 1
    mo=$(sa02m_rtc_bcd_byte "$raw") || return 1
    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x06) || return 1
    y=$(sa02m_rtc_bcd_byte "$raw") || return 1
    y=$((2000 + y))

    sa02m_rtc_valid_ymd "$y" "$mo" "$dom" || return 1
    [ "$h" -le 23 ] 2>/dev/null || return 1
    [ "$m" -le 59 ] 2>/dev/null || return 1
    [ "$s" -le 59 ] 2>/dev/null || return 1

    printf '%04d-%02d-%02d %02d:%02d:%02d' "$y" "$mo" "$dom" "$h" "$m" "$s"
    return 0
}

# PCF8563: time regs 0x02..0x08 (sec..year) on some SA-02m boards.
read_pcf8563_i2c_datetime() {
    local bus addr raw s m h dom mo y
    sa02m_rtc_find_i2c_chip pcf8563 || return 1
    bus=$SA02M_RTC_I2C_BUS
    addr=$SA02M_RTC_I2C_ADDR

    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x02) || return 1
    s=$(sa02m_rtc_bcd_byte "$raw") || return 1
    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x03) || return 1
    m=$(sa02m_rtc_bcd_byte "$raw") || return 1
    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x04) || return 1
    h=$(sa02m_rtc_bcd_byte "$raw") || return 1
    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x05) || return 1
    dom=$(sa02m_rtc_bcd_byte "$raw") || return 1
    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x06) || return 1
    mo=$(sa02m_rtc_bcd_byte "$raw") || return 1
    raw=$(sa02m_rtc_i2c_read_reg "$bus" "$addr" 0x07) || return 1
    y=$(sa02m_rtc_bcd_byte "$raw") || return 1
    y=$((2000 + y))

    sa02m_rtc_valid_ymd "$y" "$mo" "$dom" || return 1
    [ "$h" -le 23 ] 2>/dev/null || return 1
    [ "$m" -le 59 ] 2>/dev/null || return 1
    [ "$s" -le 59 ] 2>/dev/null || return 1

    printf '%04d-%02d-%02d %02d:%02d:%02d' "$y" "$mo" "$dom" "$h" "$m" "$s"
    return 0
}

read_rtc_from_sysfs() {
    local rtc=$1 rtc_d rtc_t
    [ -r "/sys/class/rtc/${rtc}/date" ] && [ -r "/sys/class/rtc/${rtc}/time" ] || return 1
    sa02m_rtc_is_internal_node "$rtc" && return 1
    IFS= read -r rtc_d < "/sys/class/rtc/${rtc}/date" 2>/dev/null || rtc_d=""
    IFS= read -r rtc_t < "/sys/class/rtc/${rtc}/time" 2>/dev/null || rtc_t=""
    [ -n "${rtc_d:-}" ] && [ -n "${rtc_t:-}" ] || return 1
    case "$rtc_d" in
        1970-*) return 1 ;;
    esac
    printf '%s %s' "$rtc_d" "$rtc_t"
    return 0
}

read_rtc_from_hwclock() {
    local dev=$1 raw hc cleaned
    [ -c "$dev" ] || return 1
    hc=$(command -v hwclock 2>/dev/null)
    [ -z "$hc" ] && [ -x /usr/sbin/hwclock ] && hc=/usr/sbin/hwclock
    [ -n "$hc" ] || return 1
    raw=$(sa02m_rtc_timeout_run 2 "$hc" --show --rtc="$dev" 2>/dev/null \
          || sa02m_rtc_timeout_run 2 sudo -n "$hc" --show --rtc="$dev" 2>/dev/null) || return 1
    [ -n "$raw" ] || return 1
    cleaned=$(printf '%s' "${raw%%.*}" | sed 's/+[0-9:]*$//' | tr -d '\n')
    [ -n "$cleaned" ] || return 1
    case "$cleaned" in
        1970-*) return 1 ;;
    esac
    printf '%s' "$cleaned"
    return 0
}

# External RTC for UI: rtc1 sysfs/hwclock, /dev/rtc if not rtc0, then DS3231/PCF8563 over I2C.
read_rtc_datetime() {
    local rtc devlink target
    for rtc in rtc1 rtc2 rtc3; do
        if read_rtc_from_sysfs "$rtc"; then
            return 0
        fi
    done
    if [ -c /dev/rtc1 ]; then
        read_rtc_from_hwclock /dev/rtc1 && return 0
    fi
    if [ -L /dev/rtc ]; then
        target=$(readlink -f /dev/rtc 2>/dev/null || true)
        case "$target" in
            */rtc0) ;;
            *)
                read_rtc_from_hwclock "$target" && return 0
                ;;
        esac
    fi
    read_ds3231_i2c_datetime && return 0
    read_pcf8563_i2c_datetime && return 0
    return 1
}
