#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# GET  → reads /etc/sa02m-gateway.yaml, returns JSON
# POST → accepts JSON, writes YAML, sends SIGHUP to daemon

CONFIG_FILE="/etc/sa02m-gateway.yaml"

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    web_session_check_cookie && return 0
    return 1
}
if ! check_auth; then echo '{"error":"unauthorized"}'; exit 0; fi

# ── GET: YAML → JSON ──────────────────────────────────────────────────────────
if [ "$REQUEST_METHOD" = "GET" ]; then
    if [ ! -f "$CONFIG_FILE" ]; then
        python3 - <<'PYEOF'
import json
default_ports = {}
for i, name in enumerate(['COM1','COM2','COM3','COM4','COM5'], start=1):
    default_ports[name] = {
        "enabled": False,
        "mode": "modbus_tcp",
        "tcp_port": 501 + i,
        "baudrate": 115200 if name in ('COM4', 'COM5') else 19200,
        "parity": "none",
        "stopbits": 1,
        "databits": 8,
        "fast_modbus_probe": True,
        # Network access defaults: all interfaces, no IP filtering — the
        # behaviour every release before 1.0.6.24 had.
        "bind": "0.0.0.0",
        "allow_from": [],
    }
print(json.dumps({"ports": default_ports}))
PYEOF
        exit 0
    fi
    python3 - <<'PYEOF'
import sys, json, yaml, pathlib
try:
    with open("/etc/sa02m-gateway.yaml") as f:
        data = yaml.safe_load(f) or {}
    print(json.dumps({"ports": data.get("ports", {})}))
except Exception as e:
    print(json.dumps({"error": str(e), "ports": {}}))
PYEOF
    exit 0
fi

# ── POST: JSON → YAML ─────────────────────────────────────────────────────────
if [ "$REQUEST_METHOD" = "POST" ]; then
    # CSRF BEFORE any mutation (policy: docs/decisions/selective-csrf-policy.md).
    # Headers already emitted at top, so validate inline.
    if ! web_csrf_validate; then
        echo '{"ok":false,"error":"csrf","error_code":"E_CSRF"}'
        exit 0
    fi
    TMP_IN=$(mktemp /tmp/sa02m-gwcfg-in.XXXXXX)
    TMP_OUT=$(mktemp /tmp/sa02m-gwcfg-out.XXXXXX)
    trap "rm -f '$TMP_IN' '$TMP_OUT'" EXIT

    dd bs=1 count="${CONTENT_LENGTH:-0}" 2>/dev/null > "$TMP_IN"

    RESULT=$(python3 - "$TMP_IN" "$TMP_OUT" <<'PYEOF'
import sys, json, yaml, pathlib, ipaddress

VALID_MODES     = {'disabled', 'modbus_tcp', 'rtu_over_tcp', 'transparent'}
VALID_PARITIES  = {'none', 'even', 'odd'}
VALID_STOPBITS  = {1, 2}
VALID_BAUDRATES = {1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200}

# Network access (1.0.6.24). These two keys MUST be carried through here: this
# endpoint rewrites the whole per-port map from the enumerated keys below, so a
# key it does not know is erased on the next save from the panel — an allow-list
# that silently disappears is worse than none. Semantics and the fail-closed
# rule are the daemon's (opt/sa02m-serial-gateway/serial_gateway.py); this side
# refuses the WHOLE save on a malformed value rather than writing a config the
# daemon will then refuse to start a port on.

def norm_bind(name, pcfg, errs):
    raw = pcfg.get('bind', '')
    if raw is None:
        raw = ''
    if not isinstance(raw, str):
        errs.append(f"{name}: bind must be an IP address")
        return '0.0.0.0'
    value = raw.strip() or '0.0.0.0'
    try:
        ipaddress.ip_address(value)
    except ValueError:
        errs.append(f"{name}: bind '{value}' is not an IP address")
    return value

def norm_allow_from(name, pcfg, errs):
    raw = pcfg.get('allow_from', [])
    if raw is None:
        raw = []
    if isinstance(raw, str):
        items = [p for p in raw.replace(',', ' ').split() if p]
    elif isinstance(raw, list):
        items = raw
    else:
        errs.append(f"{name}: allow_from must be a list of IP addresses")
        return []
    out = []
    for item in items:
        if not isinstance(item, str):
            errs.append(f"{name}: allow_from entry must be text")
            continue
        entry = item.strip()
        if not entry:
            continue
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            errs.append(f"{name}: allow_from '{entry}' is not an IP address or range")
            continue
        out.append(entry)
    return out

def validate_port(name, pcfg):
    errs = []
    mode = pcfg.get('mode', 'disabled')
    if mode not in VALID_MODES:
        errs.append(f"{name}: unknown mode '{mode}'")
    baud = int(pcfg.get('baudrate', 115200 if name in ('COM4', 'COM5') else 19200))
    if baud not in VALID_BAUDRATES:
        errs.append(f"{name}: invalid baudrate {baud}")
    par = str(pcfg.get('parity', 'none')).lower()
    if par not in VALID_PARITIES:
        errs.append(f"{name}: invalid parity '{par}'")
    sb = pcfg.get('stopbits', 1)
    if int(sb) not in VALID_STOPBITS:
        errs.append(f"{name}: invalid stopbits {sb}")
    port = int(pcfg.get('tcp_port', 502))
    if not (1 <= port <= 65535):
        errs.append(f"{name}: tcp_port {port} out of range")
    return errs

try:
    with open(sys.argv[1]) as f:
        data = json.load(f)

    ports_in = data.get('ports', {})
    if not isinstance(ports_in, dict):
        raise ValueError("'ports' must be an object")

    all_errors = []
    ports_out  = {}
    for name, pcfg in ports_in.items():
        if not isinstance(pcfg, dict):
            continue
        all_errors += validate_port(name, pcfg)
        ports_out[name] = {
            "enabled":           bool(pcfg.get('enabled', False)),
            "mode":              str(pcfg.get('mode', 'disabled')),
            "tcp_port":          int(pcfg.get('tcp_port', 502)),
            "baudrate":          int(pcfg.get('baudrate', 115200 if name in ('COM4', 'COM5') else 19200)),
            "parity":            str(pcfg.get('parity', 'none')).lower(),
            "stopbits":          int(pcfg.get('stopbits', 1)),
            "databits":          int(pcfg.get('databits', 8)),
            "fast_modbus_probe": bool(pcfg.get('fast_modbus_probe', True)),
            "bind":              norm_bind(name, pcfg, all_errors),
            "allow_from":        norm_allow_from(name, pcfg, all_errors),
        }

    if all_errors:
        print(json.dumps({"ok": False, "error": "; ".join(all_errors)}))
        sys.exit(0)

    yaml_out = {"ports": ports_out}
    tmp_path = pathlib.Path(sys.argv[2])
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("# SA-02m RS-485\u2192Ethernet gateway configuration\n")
        f.write("# Managed by web UI. Manual edits are preserved on reload.\n\n")
        yaml.dump(yaml_out, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)
    print(json.dumps({"ok": True}))
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}))
PYEOF
)

    if ! echo "$RESULT" | python3 -c \
        "import sys,json; sys.exit(0 if json.load(sys.stdin).get('ok') else 1)" 2>/dev/null; then
        echo "$RESULT"
        exit 0
    fi

    if ! sudo /usr/local/sbin/sa02m-gateway-config-apply.sh "$TMP_OUT" 2>/dev/null; then
        echo '{"ok":false,"error":"config_apply_failed"}'
        exit 0
    fi

    echo '{"ok":true}'
    exit 0
fi

echo '{"error":"method_not_allowed"}'
