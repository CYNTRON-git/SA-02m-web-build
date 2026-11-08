#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# mqtt_live.cgi — снимок каналов: кэш моста (мс) или python/paho (резерв)
echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

check_auth() {
    web_session_check_cookie && return 0
    return 1
}
if ! check_auth; then echo '{"ok":false,"error":"unauthorized","controls":{}}'; exit 0; fi

SNAP_PY="${SA02M_BRIDGE_LIB_DIR:-/opt/sa02m-modbus-mqtt}/mqtt_live_snapshot.py"
CACHE_DIR="${SA02M_MQTT_LIVE_CACHE:-/run/sa02m-modbus-mqtt}"
if [ -z "${QUERY_STRING:-}" ]; then
    echo '{"ok":false,"error":"device required","controls":{}}'
    exit 0
fi

# Keep the fast path for a plain, literal filename. Pass all encoded or
# hierarchical IDs to Python unchanged so NUL cannot disappear in bash.
CACHE_FILE=""
if [[ "$QUERY_STRING" =~ ^device=[a-zA-Z0-9_-][a-zA-Z0-9._-]{0,199}$ &&
      "$QUERY_STRING" != device=_roster && "$QUERY_STRING" != *.profile && "$QUERY_STRING" != *.events ]]; then
    DEVICE=${QUERY_STRING#device=}
    CACHE_FILE="${CACHE_DIR}/${DEVICE}.json"
fi
if [ -n "${CACHE_FILE:-}" ] && [ -f "$CACHE_FILE" ]; then
    now=$(date +%s)
    mtime=$(stat -c %Y "$CACHE_FILE" 2>/dev/null || echo 0)
    age=$((now - mtime))
    if [ "$age" -le 10 ]; then
        if grep -qF "\"device\": \"${DEVICE}\"" "$CACHE_FILE" 2>/dev/null \
            || grep -qF "\"device\":\"${DEVICE}\"" "$CACHE_FILE" 2>/dev/null; then
            if grep -q '"source"' "$CACHE_FILE" 2>/dev/null; then
                cat "$CACHE_FILE"
            else
                sed '1s/^{/{ "source":"cache",/' "$CACHE_FILE"
            fi
            exit 0
        fi
    fi
fi

if [ ! -f "$SNAP_PY" ]; then
    echo '{"ok":false,"error":"snapshot not installed","controls":{}}'
    exit 0
fi

if [ -n "$CACHE_FILE" ]; then
    exec /usr/bin/python3 "$SNAP_PY" "$DEVICE"
fi
exec /usr/bin/python3 "$SNAP_PY"
