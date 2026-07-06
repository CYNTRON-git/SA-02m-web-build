#!/bin/bash
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-cache"
echo ""

[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || { echo '{"error":"unauthorized"}'; exit 0; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_rtc.sh
. "$SCRIPT_DIR/lib_rtc.sh"

timeout_run() {
    local sec=${1:-2}
    shift || true
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" "$@"
    else
        "$@"
    fi
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
    tz=$(printf '%s' "${tz:-}" | tr -d '\r\n\t ')
    [ -n "$tz" ] || tz="UTC"
    # Дефолт устройства СА-02м — Москва; Etc/UTC из образа не показываем в UI как «лишнюю» зону
    case "$tz" in
        UTC|Etc/UTC|Etc/Universal|Etc/Zulu|Etc/GMT|Etc/GMT-0|Etc/GMT+0|Etc/GMT0|GMT|Universal|Zulu)
            tz='Europe/Moscow'
            ;;
    esac
    printf '%s' "$tz"
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
DT=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)

RTC_DT=""
if RTC_DT=$(read_rtc_datetime 2>/dev/null); then
    :
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
