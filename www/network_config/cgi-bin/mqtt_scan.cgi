#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# mqtt_scan.cgi — Modbus bus scanner for MQTT device discovery
echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

check_auth() {
    web_session_check_cookie && return 0
    return 1
}
if ! check_auth; then echo '{"ok":false,"error":"unauthorized","devices":[]}'; exit 0; fi

SCAN_PY="/opt/sa02m-modbus-mqtt/mqtt_bus_scan.py"
TMP=$(mktemp /tmp/sa02m-mqttscan.XXXXXX)
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

# Validate the scan params BEFORE handing the file to the root scanner:
# port must be a /dev serial path, baudrate/max_addr bounded integers. The
# params are attacker-supplied and the scanner runs as root.
if ! python3 - "$TMP" <<'PYEOF'
import json, re, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        p = json.load(f)
except Exception:
    sys.exit(1)
port = str(p.get("port", ""))
if not re.fullmatch(r"/dev/[A-Za-z0-9_-]+", port):
    sys.exit(1)
for key, lo, hi in (("baudrate", 300, 4000000), ("max_addr", 1, 247)):
    if key in p and p[key] not in ("", None):
        try:
            v = int(p[key])
        except (TypeError, ValueError):
            sys.exit(1)
        if not (lo <= v <= hi):
            sys.exit(1)
sys.exit(0)
PYEOF
then
    echo '{"ok":false,"error":"invalid scan parameters (port/baudrate/max_addr)","devices":[]}'
    exit 0
fi

if [ ! -x "$SCAN_PY" ] && [ ! -f "$SCAN_PY" ]; then
    echo '{"ok":false,"error":"scanner not installed","devices":[]}'
    exit 0
fi

sudo /usr/bin/python3 "$SCAN_PY" "$TMP" 2>/dev/null \
    || /usr/bin/python3 "$SCAN_PY" "$TMP"
