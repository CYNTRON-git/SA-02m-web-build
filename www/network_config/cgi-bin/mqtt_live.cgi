#!/bin/bash
# mqtt_live.cgi — снимок каналов: кэш моста (мс) или python/paho (резерв)
echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
    return 1
}
if ! check_auth; then echo '{"ok":false,"error":"unauthorized","controls":{}}'; exit 0; fi

SNAP_PY="/opt/sa02m-modbus-mqtt/mqtt_live_snapshot.py"
CACHE_DIR="/run/sa02m-modbus-mqtt"
DEVICE=""
if [ -n "${QUERY_STRING:-}" ]; then
    DEVICE=$(printf '%s' "${QUERY_STRING}" | tr '&' '\n' | sed -n 's/^device=\(.*\)$/\1/p' | head -1)
    DEVICE=$(printf '%b' "${DEVICE//%/\\x}")
fi

if [ -z "$DEVICE" ]; then
    echo '{"ok":false,"error":"device required","controls":{}}'
    exit 0
fi

case "$DEVICE" in
    *[!a-zA-Z0-9._-]*|'')
        echo '{"ok":false,"error":"invalid device","controls":{}}'
        exit 0
        ;;
esac

CACHE_FILE="${CACHE_DIR}/${DEVICE}.json"
if [ -f "$CACHE_FILE" ]; then
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

/usr/bin/python3 "$SNAP_PY" "$DEVICE" 2>/dev/null
