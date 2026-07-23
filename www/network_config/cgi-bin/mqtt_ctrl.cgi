#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    web_session_check_cookie && return 0
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

# Bridge start/stop via CTL does unmask/enable/mask and can take many seconds.
# Return JSON immediately and finish in background so the UI confirm/save path
# is not blocked (toggle confirm must stay instant).
bridge_ctl_async() {
    _act=$1
    case "$_act" in stop|start) ;; *) echo 1; return ;; esac
    echo "$(date '+%Y-%m-%d %H:%M:%S') mqtt_ctrl.cgi: bridge ${_act} async (web)" >>"$LOG" 2>&1
    if [ -x "$CTL" ]; then
        setsid /bin/bash -c "sudo -n '$CTL' '$_act' mqtt-bridge >>'$LOG' 2>&1" </dev/null >/dev/null 2>&1 &
        echo 0
        return
    fi
    setsid /bin/bash -c "sudo /usr/bin/systemctl '$_act' sa02m-modbus-mqtt >>'$LOG' 2>&1" </dev/null >/dev/null 2>&1 &
    echo 0
}

run_svc_async() {
    setsid /bin/bash -c "sudo /usr/bin/systemctl '$1' '$2' >>'$LOG' 2>&1" </dev/null >/dev/null 2>&1 &
    echo 0
}

case "$ACTION" in
    restart_mosquitto)  rc=$(run_svc_async restart mosquitto) ;;
    start_mosquitto)    rc=$(run_svc_async start   mosquitto) ;;
    stop_mosquitto)     rc=$(run_svc_async stop    mosquitto) ;;
    restart_bridge)     rc=$(run_svc_async restart sa02m-modbus-mqtt) ;;
    stop_bridge)        rc=$(bridge_ctl_async stop) ;;
    start_bridge)       rc=$(bridge_ctl_async start) ;;
    restart_telemetry)  rc=$(run_svc_async restart sa02m-telemetry) ;;
    start_telemetry)    rc=$(run_svc_async start   sa02m-telemetry) ;;
    stop_telemetry)     rc=$(run_svc_async stop    sa02m-telemetry) ;;
    *)  echo '{"ok":false,"error":"unknown_action"}'; exit 0 ;;
esac

if [ "${rc:-1}" = "0" ]; then
    echo "{\"ok\":true,\"action\":\"${ACTION}\",\"pending\":true}"
else
    echo "{\"ok\":false,\"action\":\"${ACTION}\",\"error\":\"systemctl_rc_${rc}\"}"
fi
