#!/bin/bash
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-cache"
echo ""

[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || { echo '{"error":"unauthorized"}'; exit 0; }

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
TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "UTC")
DT=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)

RTC_DT=""
if [ -r /sys/class/rtc/rtc0/date ] && [ -r /sys/class/rtc/rtc0/time ]; then
    RTC_DT="$(tr -d '\n\r' < /sys/class/rtc/rtc0/date) $(tr -d '\n\r' < /sys/class/rtc/rtc0/time)"
elif command -v hwclock >/dev/null 2>&1; then
    RTC_DT=$(hwclock -r 2>/dev/null | head -1 | tr -d '\r')
fi
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
