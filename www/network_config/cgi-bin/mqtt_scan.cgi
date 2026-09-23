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

# SA02M_MQTT_SCAN_PY: the behavioural harness (scripts/dev/test-cgi-csrf-behaviour.sh)
# points it at a fixture. Process environment only — nginx/fcgiwrap set no
# SA02M_* name, so a client cannot choose the path (the SA02M_WEB_BUILD_STATEDIR
# precedent in web_update_apply.cgi).
SCAN_PY="${SA02M_MQTT_SCAN_PY:-/opt/sa02m-modbus-mqtt/mqtt_bus_scan.py}"
TMP=$(mktemp /tmp/sa02m-mqttscan.XXXXXX)
trap "rm -f '$TMP'" EXIT

# POST-only — a GET scan is a Lax-defeating CSRF vector (top-level navigation
# still sends the session cookie) that puts root traffic on a live RS-485 bus.
# policy: docs/decisions/selective-csrf-policy.md; gate: cgi-csrf-policy.
if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
    echo '{"ok":false,"error":"method_not_allowed","devices":[]}'
    exit 0
fi
# CSRF BEFORE the mutation. Headers are already on the wire, so validate inline
# and print the shared shape (web_csrf_require would re-emit them into the body).
if ! web_csrf_validate; then
    printf '{"ok":false,"error":"csrf","error_code":"E_CSRF","reason":"%s","devices":[]}
' "$(web_csrf_fail_reason)"
    exit 0
fi

dd bs=1 count="${CONTENT_LENGTH:-0}" 2>/dev/null > "$TMP"

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
