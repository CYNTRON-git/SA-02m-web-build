#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# mqtt_tcp_probe.cgi — «Проверить связь» in the add-device dialog: ONE read-only
# Modbus TCP request (FC03, 1 register at 0) to the typed host:tcp_port/unit,
# from the board as www-data, before the entry is saved. Request/response shape,
# the verdict enum and the bound rule (what this endpoint may never do on the
# network) have one home: docs/contracts/bridge-modbus-tcp.md §10. Gate:
# scripts/dev/test-mqtt-tcp-probe.sh (row mqtt-tcp-probe-contract).

# Server env only — fcgiwrap passes a request nothing but HTTP_* variables, so a
# client cannot choose these; the harness redirects them (the mqtt_config.cgi /
# mqtt_scan.cgi precedent). The entry travels to python as a file path in argv,
# never interpolated into source or a shell word.
BRIDGE_LIB_DIR="${SA02M_BRIDGE_LIB_DIR:-/opt/sa02m-modbus-mqtt}"
PROBE_LOCK="${SA02M_PROBE_LOCK:-/tmp/sa02m-tcp-probe.lock}"
PROBE_BUDGET_S="${SA02M_PROBE_BUDGET_S:-8}"
BODY_MAX=4096

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

if ! web_session_check_cookie; then echo '{"ok":false,"error":"unauthorized"}'; exit 0; fi

# POST-only + the token BEFORE any socket work: a GET would be a Lax-defeating
# CSRF vector that makes the board knock LAN ports for a cross-site page.
# Policy: docs/decisions/selective-csrf-policy.md; gate: cgi-csrf-policy.
if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
    echo '{"ok":false,"error":"method_not_allowed"}'
    exit 0
fi
# Headers are already on the wire, so validate inline (the mqtt_config.cgi shape).
if ! web_csrf_validate; then
    web_csrf_error_body
    exit 0
fi

# Untrusted-input floor: a dialog entry is ~120 bytes; CONTENT_LENGTH itself is
# client-sent, so digits only, and anything past the cap is not read at all.
LEN="${CONTENT_LENGTH:-0}"
case "$LEN" in ''|*[!0-9]*) LEN=0 ;; esac
if [ "$LEN" -gt "$BODY_MAX" ]; then
    echo '{"ok":false,"error":"body_too_large"}'
    exit 0
fi
TMP_IN=$(mktemp /tmp/sa02m-tcpprobe-in.XXXXXX) || TMP_IN=""
if [ -z "$TMP_IN" ]; then
    echo '{"ok":false,"error":"probe_failed"}'
    exit 0
fi
trap 'rm -f "$TMP_IN"' EXIT
dd bs=1 count="$LEN" 2>/dev/null > "$TMP_IN"

# One python process, bounded by the shell: the probe's own socket timeouts
# (1 s connect + 1 s reply) plus the interpreter start fit well inside the
# budget, and a wedged probe can never hold a fcgiwrap worker to nginx's 60 s.
OUT=$(timeout -k 1 "$PROBE_BUDGET_S" python3 - "$TMP_IN" "$BRIDGE_LIB_DIR" "$PROBE_LOCK" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        entry = json.load(f)
except Exception:
    print(json.dumps({"ok": False, "error": "invalid_json"}))
    sys.exit(0)
try:
    sys.path.insert(0, sys.argv[2])
    import bridge_probe
except Exception as e:
    # Fail closed, never silently: fcgiwrap hands stderr to the nginx error log.
    print("mqtt_tcp_probe.cgi: probe module unavailable - cannot import "
          "bridge_probe from %s: %s" % (sys.argv[2], e), file=sys.stderr)
    print(json.dumps({"ok": False, "error": "probe_unavailable"}))
    sys.exit(0)
# validate (bridge_bus, the save's rules) -> single-flight lock -> one FC03.
print(json.dumps(bridge_probe.probe(entry, sys.argv[3])))
PYEOF
)
rc=$?
if [ -n "$OUT" ]; then
    printf '%s\n' "$OUT"
elif [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo '{"ok":false,"error":"probe_timeout"}'
else
    echo '{"ok":false,"error":"probe_failed"}'
fi
exit 0
