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

exec /usr/bin/python3 -u "$STREAM_PY" "${QUERY_STRING:-}"
