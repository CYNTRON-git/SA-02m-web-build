#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# SSE MQTT monitor — делегирует в Python (без shell+python на каждое сообщение).
check_auth() {
    web_session_check_cookie && return 0
    return 1
}
if ! check_auth; then
    echo "Content-type: application/json"
    echo ""
    echo '{"error":"unauthorized"}'
    exit 0
fi

STREAM_PY="/opt/sa02m-modbus-mqtt/mqtt_monitor_stream.py"
if [ ! -f "$STREAM_PY" ]; then
    echo "Content-type: text/event-stream"
    echo ""
    echo "event: error"
    echo "data: mqtt_monitor_stream.py not installed"
    echo ""
    exit 0
fi

# One stream holds one fcgiwrap worker for its whole life, so it is capped at
# 60 s (A8-07); a client that wants more reconnects. nginx also allows a single
# stream per session (limit_conn in etc/nginx/network_config.conf).
runtime=(/usr/bin/python3 -u "$STREAM_PY")
if [[ -e /var/lib/sa02m-python-activation || -L /var/lib/sa02m-python-activation ||
      ( -x /usr/local/libexec/sa02m-python-consumer && -r /opt/sa02m-python-manager/launch.py &&
        -r /opt/sa02m-python-manager/consumers.json ) ]]; then
    runtime=(/usr/local/libexec/sa02m-python-consumer mqtt-monitor)
fi

exec timeout -k 2 60 "${runtime[@]}" "${QUERY_STRING:-}"
