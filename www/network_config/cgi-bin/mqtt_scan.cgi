#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# mqtt_scan.cgi — Modbus bus scanner for MQTT device discovery
echo "Content-Type: application/x-ndjson; charset=utf-8"
echo "Cache-Control: no-cache"
echo "X-Accel-Buffering: no"
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

# Validate the scan params BEFORE handing the file to the root scanner.
# COM: port must be a /dev serial path. Gateway: strict IPv4 + TCP port, the
# same nets mqtt_bus_scan.gateway_endpoint refuses (loopback is not a gateway).
# baudrate/max_addr are bounded integers. The params are attacker-supplied and
# the scanner runs as root.
if ! python3 - "$TMP" <<'PYEOF'
import ipaddress, json, re, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        p = json.load(f)
except Exception:
    sys.exit(1)
via = str(p.get("via") or "com").strip().lower()
if via == "com":
    port = str(p.get("port", ""))
    if not re.fullmatch(r"/dev/COM[1-5]", port):
        sys.exit(1)
elif via == "gateway":
    octet = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])"
    host = str(p.get("host") or "").strip()
    if not re.fullmatch(octet + "(?:\\." + octet + "){3}", host):
        sys.exit(1)
    ip = ipaddress.IPv4Address(host)
    forbidden = tuple(ipaddress.IPv4Network(n) for n in (
        "127.0.0.0/8", "0.0.0.0/32", "224.0.0.0/4", "240.0.0.0/4"))
    if any(ip in net for net in forbidden):
        sys.exit(1)
    try:
        tcp_port = int(p.get("tcp_port"))
    except (TypeError, ValueError):
        sys.exit(1)
    if not 1 <= tcp_port <= 65535:
        sys.exit(1)
else:
    sys.exit(1)
bounds = (("max_addr", 1, 247),)
if via == "com":
    bounds = (("baudrate", 300, 4000000), ("max_addr", 1, 247))
for key, lo, hi in bounds:
    if key in p and p[key] not in ("", None):
        try:
            v = int(p[key])
        except (TypeError, ValueError):
            sys.exit(1)
        if not (lo <= v <= hi):
            sys.exit(1)
if via == "gateway" and "baudrate" in p and p.get("baudrate") not in ("", None):
    try:
        baud = int(p.get("baudrate"))
    except (TypeError, ValueError):
        sys.exit(1)
    if not 300 <= baud <= 4000000:
        sys.exit(1)
# phase is optional. Absent runs fast then the address sweep in one process.
# A present value is only the whitelist — anything else fails closed before sudo.
if "phase" in p and p.get("phase") not in ("", None):
    phase = p.get("phase")
    if not isinstance(phase, str) or phase.strip().lower() not in ("fast", "standard"):
        sys.exit(1)
if "known_addrs" in p and p.get("known_addrs") not in ("", None):
    known = p.get("known_addrs")
    if not isinstance(known, list) or len(known) > 247:
        sys.exit(1)
    for item in known:
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 247:
            sys.exit(1)
if "addr_from" in p and p.get("addr_from") not in ("", None):
    raw = p.get("addr_from")
    if isinstance(raw, bool):
        sys.exit(1)
    try:
        lo = int(raw)
        hi = int(p.get("max_addr", 32))
    except (TypeError, ValueError):
        sys.exit(1)
    if not 1 <= lo <= hi <= 247:
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
