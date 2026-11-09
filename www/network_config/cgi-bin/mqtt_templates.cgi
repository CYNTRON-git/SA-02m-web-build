#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# GET → lists device templates in the bridge drop-in dir as JSON.
# Read-only, auth-guarded, no mutation → no CSRF (selective-csrf-policy.md).
# Backs the MQTT add-by-template picker (type: template devices).

TEMPLATES_DIR="${SA02M_WB_TEMPLATES_DIR:-/opt/sa02m-modbus-mqtt/templates}"

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

if ! web_session_check_cookie; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

if [ "$REQUEST_METHOD" != "GET" ]; then
    echo '{"ok":false,"error":"method_not_allowed"}'
    exit 0
fi

. "$(dirname "$0")/lib_mqtt_write_controls.sh"
python3 - "$TEMPLATES_DIR" "${SA02M_BRIDGE_LIB_DIR:-/opt/sa02m-modbus-mqtt}" "${MQTT_WEB_WRITE_PATTERN:-}" <<'PYEOF'
import sys, json, pathlib, re
sys.path.insert(0, sys.argv[2])
try:
    from bridge_template_channels import channel_catalog, resolve_template_path
except ImportError:
    print(json.dumps({"ok": False, "error": "template_catalog_unavailable"}))
    sys.exit(0)
base = pathlib.Path(sys.argv[1])
out = []
try:
    names = sorted({f.stem.removeprefix("config-") for f in base.iterdir() if f.suffix == ".json"})
except OSError:
    print(json.dumps({"ok": False, "error": "template_catalog_unavailable"}))
    sys.exit(0)
for name in names:
    try:
        with resolve_template_path(name).open(encoding="utf-8") as fh:
            doc = json.load(fh)
        dev = doc.get("device") if isinstance(doc, dict) else None
        if not isinstance(dev, dict):
            continue
        device_type = str(dev.get("device_type") or "")
        title = str(dev.get("title") or dev.get("name") or name)
        channels = channel_catalog(doc)
        for ch in channels:
            ch["web_write"] = (not ch["readonly"] and
                               bool(re.fullmatch(sys.argv[3], ch["name"])))
    except (OSError, ValueError, TypeError, OverflowError, RuntimeError) as e:
        # RuntimeError includes nested JSON/channel trees and symlink loops.
        print("mqtt_templates.cgi: skipping %r (%s)" % (name, type(e).__name__), file=sys.stderr)
        continue
    # A parsed register map still needs independent hardware verification.
    out.append({"name": name, "title": title, "device_type": device_type,
                "verified": False, "channels": channels})
print(json.dumps({"ok": True, "templates": out}))
PYEOF
