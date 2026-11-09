#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# mqtt_monitor_poll.cgi — JSON-снимок для монитора (короткий запрос, не SSE)
echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

check_auth() {
    web_session_check_cookie && return 0
    return 1
}
if ! check_auth; then echo '{"ok":false,"error":"unauthorized","events":[]}'; exit 0; fi

POLL_PY="${SA02M_BRIDGE_LIB_DIR:-/opt/sa02m-modbus-mqtt}/mqtt_monitor_poll.py"
if [ -z "${QUERY_STRING:-}" ]; then
    echo '{"ok":false,"error":"device required","events":[]}'
    exit 0
fi

if [ ! -f "$POLL_PY" ]; then
    echo '{"ok":false,"error":"poll not installed","events":[]}'
    exit 0
fi

/usr/bin/python3 "$POLL_PY" "$QUERY_STRING"
