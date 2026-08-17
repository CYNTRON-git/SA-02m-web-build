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

python3 - "$TEMPLATES_DIR" <<'PYEOF'
import sys, json, pathlib
base = pathlib.Path(sys.argv[1])
out = []
if base.is_dir():
    for f in sorted(base.glob("*.json")):
        name = f.stem
        if name.startswith("config-"):
            name = name[len("config-"):]
        title = name
        device_type = ""
        try:
            with open(f, encoding="utf-8") as fh:
                doc = json.load(fh)
            dev = doc.get("device", {}) if isinstance(doc, dict) else {}
            if not isinstance(dev, dict):
                dev = {}
            device_type = str(dev.get("device_type", "") or "")
            title = str(dev.get("title") or dev.get("name") or name)
        except Exception:
            pass
        # verified:false — register maps are unverified against hardware until
        # the operator bench-confirms them (docs/contracts/template-device.md).
        out.append({"name": name, "title": title,
                    "device_type": device_type, "verified": False})
print(json.dumps({"ok": True, "templates": out}))
PYEOF
