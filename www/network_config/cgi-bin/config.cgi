#!/bin/bash
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-cache"
echo ""

[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || { echo '{"error":"unauthorized"}'; exit 0; }

timeout_run() {
    local sec=${1:-2}
    shift || true
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" "$@"
    else
        "$@"
    fi
}

rtc_hwclock_bin() {
    local bin
    for bin in /sbin/hwclock /usr/sbin/hwclock "$(command -v hwclock 2>/dev/null)"; do
        [ -n "${bin:-}" ] || continue
        [ -x "$bin" ] || continue
        printf '%s' "$bin"
        return 0
    done
    return 1
}

rtc_hwclock_read() {
    local dev="${1:-}" hw
    hw=$(rtc_hwclock_bin) || return 1
    if [ -n "$dev" ]; then
        timeout_run 2 "$hw" -r -f "$dev"
    else
        timeout_run 2 "$hw" -r
    fi
}

rtc_hwclock_sync() {
    local dev="${1:-}" hw rc
    hw=$(rtc_hwclock_bin) || return 1
    if [ -n "$dev" ]; then
        timeout_run 3 "$hw" -s -f "$dev" >/dev/null 2>&1
        rc=$?
        if [ $rc -ne 0 ] && command -v sudo >/dev/null 2>&1; then
            timeout_run 3 sudo -n "$hw" -s -f "$dev" >/dev/null 2>&1
            return $?
        fi
        return $rc
    fi
    timeout_run 3 "$hw" -s >/dev/null 2>&1
}

rtc_try_attach_ds3231() {
    local new_device=/sys/class/i2c-adapter/i2c-0/new_device i
    [ -c /dev/rtc1 ] && return 0
    [ -e "$new_device" ] || return 1

    if [ -w "$new_device" ]; then
        printf '%s\n' 'ds3231 0x68' > "$new_device" 2>/dev/null || true
    elif command -v sudo >/dev/null 2>&1 && command -v tee >/dev/null 2>&1; then
        printf '%s\n' 'ds3231 0x68' | timeout_run 2 sudo -n tee "$new_device" >/dev/null 2>&1 || true
    fi

    for i in 1 2 3 4 5; do
        [ -c /dev/rtc1 ] || break
        rtc_hwclock_sync /dev/rtc1 || true
        if rtc_hwclock_read /dev/rtc1 >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done

    [ -c /dev/rtc1 ]
}

read_timezone() {
    local tz=""
    if [ -r /etc/timezone ]; then
        IFS= read -r tz < /etc/timezone
    fi
    if [ -z "${tz:-}" ] && [ -L /etc/localtime ]; then
        tz=$(readlink /etc/localtime 2>/dev/null | sed 's#^.*zoneinfo/##')
    fi
    if [ -z "${tz:-}" ] && command -v timedatectl >/dev/null 2>&1; then
        tz=$(timeout_run 2 timedatectl show --property=Timezone --value 2>/dev/null | head -1 | tr -d '\r')
    fi
    printf '%s' "${tz:-UTC}"
}

rtc_sysfs_datetime() {
    local rtc_dir="" fallback="" name rtc_name rtc_date rtc_time
    for name in /sys/class/rtc/rtc*; do
        [ -d "$name" ] || continue
        [ -r "$name/date" ] || continue
        [ -r "$name/time" ] || continue
        if [ -r "$name/name" ]; then
            IFS= read -r rtc_name < "$name/name"
            case "${rtc_name:-}" in
                *pcf8563*|*ds3231*)
                    rtc_dir=$name
                    break
                    ;;
            esac
        fi
        [ -z "$fallback" ] && fallback=$name
    done

    [ -n "${rtc_dir:-}" ] || rtc_dir=$fallback
    [ -n "${rtc_dir:-}" ] || return 1

    IFS= read -r rtc_date < "$rtc_dir/date" || return 1
    IFS= read -r rtc_time < "$rtc_dir/time" || return 1
    printf '%s %s' "${rtc_date:-}" "${rtc_time:-}"
}

read_rtc_datetime() {
    local out=""
    if out=$(rtc_sysfs_datetime 2>/dev/null); then
        printf '%s' "$out"
        return 0
    fi
    rtc_try_attach_ds3231 >/dev/null 2>&1 || true
    if out=$(rtc_sysfs_datetime 2>/dev/null); then
        printf '%s' "$out"
        return 0
    fi
    out=$(rtc_hwclock_read /dev/rtc1 2>/dev/null | head -1 | tr -d '\r')
    if [ -n "${out:-}" ]; then
        printf '%s' "$out"
        return 0
    fi
    out=$(rtc_hwclock_read 2>/dev/null | head -1 | tr -d '\r')
    [ -n "${out:-}" ] || return 1
    printf '%s' "$out"
}

read_iface_conf() {
    local f=$1
    local ip="" netmask="" gateway="" dns="" enabled="false"
    if [ -f "$f" ] && grep -qE '^[[:space:]]*iface[[:space:]].*[[:space:]]inet[[:space:]]+static([[:space:]]|$)' "$f"; then
        enabled="true"
        ip=$(awk      '/^[[:space:]]*address/{gsub(/\/[0-9]+/,"",$2); print $2; exit}' "$f")
        netmask=$(awk '/^[[:space:]]*netmask/{print $2; exit}' "$f")
        gateway=$(awk '/^[[:space:]]*gateway/{print $2; exit}' "$f")
        dns=$(awk     '/^[[:space:]]*dns-nameservers/{$1=""; gsub(/^[ \t]+/,"",$0); print; exit}' "$f")
    elif [ -f "$f" ]; then
        # dhcp и т.п. — поля статики не читаем
        :
    fi
    printf '{"enabled":%s,"ip":"%s","netmask":"%s","gateway":"%s","dns":"%s"}' \
        "$enabled" "${ip:-}" "${netmask:-}" "${gateway:-}" "${dns:-}"
}

ETH0=$(read_iface_conf /etc/network/interfaces.d/eth0.conf)
ETH1=$(read_iface_conf /etc/network/interfaces.d/eth1.conf)
TZ=$(read_timezone)
RTC_DT=""
RTC_DT=$(read_rtc_datetime 2>/dev/null || true)
DT=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
RTC_JSON=$(printf '%s' "$RTC_DT" | sed 's/\\/\\\\/g; s/"/\\"/g')
DT_JSON=$(printf '%s' "$DT" | sed 's/\\/\\\\/g; s/"/\\"/g')
TZ_JSON=$(printf '%s' "$TZ" | sed 's/\\/\\\\/g; s/"/\\"/g')

cat <<JSON
{
  "eth0": ${ETH0},
  "eth1": ${ETH1},
  "timezone": "${TZ_JSON}",
  "datetime": "${DT_JSON}",
  "rtc_datetime": "${RTC_JSON}"
}
JSON
