#!/bin/bash
# GET  → читает /etc/sa02m-modbus-mqtt.yaml, возвращает JSON-представление
# POST → принимает JSON, сохраняет YAML, опционально перезапускает мост

CONFIG_FILE="/etc/sa02m-modbus-mqtt.yaml"

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
    return 1
}
if ! check_auth; then echo '{"error":"unauthorized"}'; exit 0; fi

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g; :a;N;$!ba;s/\n/ /g'; }

if [ "$REQUEST_METHOD" = "GET" ]; then
    # Читаем YAML и конвертируем в JSON через Python
    if [ ! -f "$CONFIG_FILE" ]; then
        echo '{"devices":[],"mqtt":{"broker":"127.0.0.1","port":1883,"qos":1,"retain":true}}'
        exit 0
    fi
    python3 - <<'PYEOF'
import sys, json, yaml, pathlib
cfg_path = pathlib.Path("/etc/sa02m-modbus-mqtt.yaml")
try:
    with open(cfg_path) as f:
        data = yaml.safe_load(f) or {}
    # Remove comments-only keys, keep structure
    out = {
        "mqtt": data.get("mqtt", {}),
        "devices": data.get("devices", []) or [],
    }
    print(json.dumps(out))
except Exception as e:
    print(json.dumps({"error": str(e), "devices": [], "mqtt": {}}))
PYEOF
    exit 0
fi

if [ "$REQUEST_METHOD" = "POST" ]; then
    # Read POST body safely to a temp file; avoid shell variable injection
    TMP_IN=$(mktemp /tmp/sa02m-mqcfg-in.XXXXXX)
    TMP_CHK=$(mktemp /tmp/sa02m-mqcfg-chk.XXXXXX)
    trap "rm -f '$TMP_IN' '$TMP_CHK'" EXIT

    dd bs=1 count="${CONTENT_LENGTH:-0}" 2>/dev/null > "$TMP_IN"

    # Validate JSON
    if ! python3 -c "import sys,json; json.load(open('$TMP_IN'))" 2>/dev/null; then
        echo '{"ok":false,"error":"invalid_json"}'
        exit 0
    fi

    # Convert JSON → YAML and write atomically
    python3 - "$TMP_IN" <<'PYEOF'
import sys, json, yaml, pathlib, os

try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
except Exception as e:
    print(json.dumps({"ok": False, "error": f"json: {e}"}))
    sys.exit(0)

cfg_path = pathlib.Path("/etc/sa02m-modbus-mqtt.yaml")
tmp_path = cfg_path.with_suffix(".yaml.tmp")

try:
    with open(tmp_path, "w") as f:
        f.write("# SA-02m Modbus\u2192MQTT bridge configuration\n")
        f.write("# Managed by web UI. Edit manually or via MQTT tab.\n\n")
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, cfg_path)
    print(json.dumps({"ok": True}))
except Exception as e:
    if tmp_path.exists():
        tmp_path.unlink(missing_ok=True)
    print(json.dumps({"ok": False, "error": str(e)}))
PYEOF

    # Restart bridge if requested
    if python3 -c "import sys,json; d=json.load(open('$TMP_IN')); sys.exit(0 if d.get('restart') else 1)" 2>/dev/null; then
        sudo /usr/bin/systemctl restart sa02m-modbus-mqtt 2>/dev/null || true
    fi
    exit 0
fi

echo '{"error":"method_not_allowed"}'
