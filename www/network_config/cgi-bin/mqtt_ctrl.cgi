#!/bin/bash
. "$(dirname "$0")/lib_web_session.sh"
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    web_session_check_cookie "${HTTP_COOKIE:-}" && return 0
    return 1
}
if ! check_auth; then echo '{"ok":false,"error":"unauthorized"}'; exit 0; fi
if [ "$REQUEST_METHOD" != "POST" ]; then echo '{"ok":false,"error":"method"}'; exit 0; fi

TMP=$(mktemp /tmp/sa02m-mqctrl.XXXXXX)
trap "rm -f '$TMP'" EXIT
dd bs=1 count="${CONTENT_LENGTH:-0}" 2>/dev/null > "$TMP"

ACTION=$(python3 -c "import sys,json; print(json.load(open('$TMP')).get('action',''))" 2>/dev/null || true)

CTL=/usr/local/sbin/sa02m-web-service-ctl.sh
LOG=/var/log/sa02m_install.log

run_svc() { sudo /usr/bin/systemctl "$1" "$2" >/dev/null 2>&1; echo $?; }

bridge_ctl() {
    _act=$1
    case "$_act" in stop|start) ;; *) echo 1; return ;; esac
    if [ -x "$CTL" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') mqtt_ctrl.cgi: bridge ${_act} (web)" >>"$LOG" 2>&1
        OUT=$(sudo -n "$CTL" "$_act" mqtt-bridge 2>&1) || true
        printf '%s\n' "$OUT" >>"$LOG" 2>&1
        case "$OUT" in *'"ok":true'*) echo 0 ;; *) echo 1 ;; esac
        return
    fi
    run_svc "$_act" sa02m-modbus-mqtt
}

case "$ACTION" in
    restart_mosquitto)  rc=$(run_svc restart mosquitto) ;;
    start_mosquitto)    rc=$(run_svc start   mosquitto) ;;
    stop_mosquitto)     rc=$(run_svc stop    mosquitto) ;;
    restart_bridge)     rc=$(run_svc restart sa02m-modbus-mqtt) ;;
    stop_bridge)        rc=$(bridge_ctl stop) ;;
    start_bridge)       rc=$(bridge_ctl start) ;;
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
