#!/bin/bash
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
    return 1
}
if ! check_auth; then echo '{"ok":false,"error":"unauthorized"}'; exit 0; fi
if [ "$REQUEST_METHOD" != "POST" ]; then echo '{"ok":false,"error":"method"}'; exit 0; fi

TMP=$(mktemp /tmp/sa02m-mqctrl.XXXXXX)
trap "rm -f '$TMP'" EXIT
dd bs=1 count="${CONTENT_LENGTH:-0}" 2>/dev/null > "$TMP"

ACTION=$(python3 -c "import sys,json; print(json.load(open('$TMP')).get('action',''))" 2>/dev/null || true)

run_svc() { sudo /usr/bin/systemctl "$1" "$2" >/dev/null 2>&1; echo $?; }

case "$ACTION" in
    restart_mosquitto)  rc=$(run_svc restart mosquitto) ;;
    start_mosquitto)    rc=$(run_svc start   mosquitto) ;;
    stop_mosquitto)     rc=$(run_svc stop    mosquitto) ;;
    restart_bridge)     rc=$(run_svc restart sa02m-modbus-mqtt) ;;
    stop_bridge)        rc=$(run_svc stop    sa02m-modbus-mqtt) ;;
    start_bridge)       rc=$(run_svc start   sa02m-modbus-mqtt) ;;
    restart_telemetry)  rc=$(run_svc restart sa02m-telemetry) ;;
    start_telemetry)    rc=$(run_svc start   sa02m-telemetry) ;;
    stop_telemetry)     rc=$(run_svc stop    sa02m-telemetry) ;;
    *)  echo '{"ok":false,"error":"unknown_action"}'; exit 0 ;;
esac

if [ "${rc:-1}" = "0" ]; then
    echo "{\"ok\":true,\"action\":\"${ACTION}\"}"
else
    echo "{\"ok\":false,\"action\":\"${ACTION}\",\"error\":\"systemctl_rc_${rc}\"}"
fi
