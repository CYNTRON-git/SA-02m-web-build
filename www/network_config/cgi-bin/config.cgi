#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-cache"
echo ""

web_session_check_cookie || { echo '{"error":"unauthorized"}'; exit 0; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_rtc.sh
. "$SCRIPT_DIR/lib_rtc.sh"
# shellcheck source=lib_net_iface.sh
. "$SCRIPT_DIR/lib_net_iface.sh"

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

# Board may use eth0/eth1 or legacy end0/end1 — JSON keys stay eth0/eth1 for the UI.
IF0=$(resolve_lan_iface eth0 end0)
IF1=$(resolve_lan_iface eth1 end1)
ETH0=$(read_iface_conf "$(lan_iface_conf "$IF0")")
ETH1=$(read_iface_conf "$(lan_iface_conf "$IF1")")

# ── KLogic coexistence detection ──────────────────────────────────────────
# Produced HERE and deliberately not in status.cgi: config.cgi is fetched once
# per page load, while every status.cgi part is on the 6-12 s rolling poll and
# must stay O(1) forks with no per-request filesystem scan (web-code-rigor.md
# resource model). Cost added here: two builtin tests plus one grep fork per
# interface. Every path is a literal — no request value reaches a path or a
# shell word. Absent/unreadable both yield false: fail closed toward "do not
# claim KLogic manages this interface".
# NOTE: klogic-install leaves /home/klogic at mode 0740 root, so the [ -x ]
# probe is inert under fcgiwrap's www-data uid — the world-readable unit file is
# the load-bearing half of this OR. Both are laid down regardless of whether an
# IP was ever applied; /home/klogic/set-ip0 only appears after the first apply,
# so keying on it would be a false negative on a fresh install.
KLOGIC_PRESENT=false
if [ -x /home/klogic/klogic-sa02 ] || [ -f /etc/systemd/system/klogic.service ]; then
    KLOGIC_PRESENT=true
fi
KLOGIC_HOOK0=false
KLOGIC_HOOK1=false
lan_iface_has_klogic_hook "$IF0" && KLOGIC_HOOK0=true
lan_iface_has_klogic_hook "$IF1" && KLOGIC_HOOK1=true

TZ=$(read_timezone)
DT=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)

RTC_DT=""
if RTC_DT=$(read_rtc_datetime 2>/dev/null); then
    :
fi
# RTC value is UTC (read_rtc_datetime invariant); emit a device-local copy so
# the time readouts fed from this endpoint (forms.js, refreshTimeReadouts) match
# «Текущее время». Empty/failed conversion -> empty; frontend falls back to UTC.
RTC_DT_LOCAL=""
if [ -n "${RTC_DT:-}" ]; then
    RTC_DT_LOCAL=$(date -d "${RTC_DT} UTC" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "")
fi
RTC_JSON=$(printf '%s' "$RTC_DT" | sed 's/\\/\\\\/g; s/"/\\"/g')
RTC_LOCAL_JSON=$(printf '%s' "$RTC_DT_LOCAL" | sed 's/\\/\\\\/g; s/"/\\"/g')
DT_JSON=$(printf '%s' "$DT" | sed 's/\\/\\\\/g; s/"/\\"/g')
TZ_JSON=$(printf '%s' "$TZ" | sed 's/\\/\\\\/g; s/"/\\"/g')

cat <<JSON
{
  "eth0": ${ETH0},
  "eth1": ${ETH1},
  "klogic": {"present":${KLOGIC_PRESENT},"eth0_hook":${KLOGIC_HOOK0},"eth1_hook":${KLOGIC_HOOK1}},
  "timezone": "${TZ_JSON}",
  "datetime": "${DT_JSON}",
  "rtc_datetime": "${RTC_JSON}",
  "rtc_datetime_local": "${RTC_LOCAL_JSON}"
}
JSON
