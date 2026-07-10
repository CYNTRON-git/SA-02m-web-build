#!/bin/bash
. "$(dirname "$0")/lib_web_session.sh"
# mqtt_monitor_poll.cgi — JSON-снимок для монитора (короткий запрос, не SSE)
echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

check_auth() {
    web_session_check_cookie "${HTTP_COOKIE:-}" && return 0
    return 1
}
if ! check_auth; then echo '{"ok":false,"error":"unauthorized","events":[]}'; exit 0; fi

POLL_PY="/opt/sa02m-modbus-mqtt/mqtt_monitor_poll.py"
DEVICE=""
if [ -n "${QUERY_STRING:-}" ]; then
    DEVICE=$(printf '%s' "${QUERY_STRING}" | tr '&' '\n' | sed -n 's/^device=\(.*\)$/\1/p' | head -1)
    DEVICE=$(printf '%b' "${DEVICE//%/\\x}")
fi

if [ -z "$DEVICE" ]; then
    echo '{"ok":false,"error":"device required","events":[]}'
    exit 0
fi

case "$DEVICE" in
    *[!a-zA-Z0-9._-]*|'')
        echo '{"ok":false,"error":"invalid device","events":[]}'
        exit 0
        ;;
esac

if [ ! -f "$POLL_PY" ]; then
    echo '{"ok":false,"error":"poll not installed","events":[]}'
    exit 0
fi

/usr/bin/python3 "$POLL_PY" "device=${DEVICE}" 2>/dev/null
