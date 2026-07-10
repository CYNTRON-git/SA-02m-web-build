#!/bin/bash
. "$(dirname "$0")/lib_web_session.sh"
# mqtt_scan.cgi — Modbus bus scanner for MQTT device discovery
echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

check_auth() {
    web_session_check_cookie "${HTTP_COOKIE:-}" && return 0
    return 1
}
if ! check_auth; then echo '{"ok":false,"error":"unauthorized","devices":[]}'; exit 0; fi

SCAN_PY="/opt/sa02m-modbus-mqtt/mqtt_bus_scan.py"
# Фиксированный staging (sudoers без wildcard). rm перед записью снимает чужой symlink.
TMP=/tmp/sa02m-mqtt-scan.json
rm -f "$TMP"
trap "rm -f '$TMP'" EXIT

if [ "${REQUEST_METHOD:-GET}" = "POST" ]; then
    dd bs=1 count="${CONTENT_LENGTH:-0}" 2>/dev/null > "$TMP"
else
    # GET fallback: ?port=/dev/COM1&baudrate=115200&max_addr=32
    python3 - "$QUERY_STRING" "$TMP" <<'PYEOF'
import json, sys, urllib.parse as up
qs = sys.argv[1] if len(sys.argv) > 1 else ""
out = sys.argv[2] if len(sys.argv) > 2 else "/dev/null"
params = {k: v for k, v in up.parse_qsl(qs)} if qs else {}
with open(out, "w", encoding="utf-8") as f:
    json.dump(params, f)
PYEOF
fi

if [ ! -x "$SCAN_PY" ] && [ ! -f "$SCAN_PY" ]; then
    echo '{"ok":false,"error":"scanner not installed","devices":[]}'
    exit 0
fi

sudo /usr/bin/python3 "$SCAN_PY" /tmp/sa02m-mqtt-scan.json 2>/dev/null \
    || /usr/bin/python3 "$SCAN_PY" "$TMP"
