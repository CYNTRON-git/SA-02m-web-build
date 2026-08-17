#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# GET  → читает /etc/sa02m-modbus-mqtt.yaml, возвращает JSON-представление
# POST → принимает JSON, сохраняет YAML, опционально перезапускает мост

CONFIG_FILE="/etc/sa02m-modbus-mqtt.yaml"

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    web_session_check_cookie && return 0
    return 1
}
if ! check_auth; then echo '{"error":"unauthorized"}'; exit 0; fi

# json_escape: shared escaper (web-code-rigor.md ## Bash CGI floors).
. "$(dirname "$0")/lib_web_json.sh"

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
    # CSRF BEFORE any mutation (policy: docs/decisions/selective-csrf-policy.md).
    # Headers already emitted at top, so validate inline.
    if ! web_csrf_validate; then
        echo '{"ok":false,"error":"csrf","error_code":"E_CSRF"}'
        exit 0
    fi
    TMP_IN=$(mktemp /tmp/sa02m-mqcfg-in.XXXXXX)
    TMP_OUT=$(mktemp /tmp/sa02m-mqcfg-out.XXXXXX)
    trap "rm -f '$TMP_IN' '$TMP_OUT'" EXIT

    dd bs=1 count="${CONTENT_LENGTH:-0}" 2>/dev/null > "$TMP_IN"

    if ! python3 -c "import json; json.load(open('$TMP_IN'))" 2>/dev/null; then
        echo '{"ok":false,"error":"invalid_json"}'
        exit 0
    fi

    RESULT=$(python3 - "$TMP_IN" "$TMP_OUT" <<'PYEOF'
import sys, json, yaml, pathlib
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    data.pop("restart", None)
    for dev in data.get("devices") or []:
        if isinstance(dev, dict):
            dev.pop("restart", None)
    tmp_path = pathlib.Path(sys.argv[2])
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("# SA-02m Modbus\u2192MQTT bridge configuration\n")
        f.write("# Managed by web UI. Edit manually or via MQTT tab.\n\n")
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print(json.dumps({"ok": True}))
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}))
PYEOF
)

    if ! echo "$RESULT" | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('ok') else 1)" 2>/dev/null; then
        echo "$RESULT"
        exit 0
    fi

    if ! sudo /usr/local/sbin/sa02m-mqtt-config-apply.sh "$TMP_OUT" 2>/dev/null; then
        echo '{"ok":false,"error":"config_apply_failed"}'
        exit 0
    fi

    # Reply first — bridge restart can take 10–20 s with a full fleet; do not
    # block the CGI/UI on systemctl (was: button stuck ~15 s on Save & Apply).
    WANT_RESTART=0
    if python3 -c "import sys,json; d=json.load(open('$TMP_IN')); sys.exit(0 if d.get('restart') else 1)" 2>/dev/null; then
        WANT_RESTART=1
    fi
    if [ "$WANT_RESTART" = 1 ]; then
        echo '{"ok":true,"restart":"pending"}'
        # setsid: survive CGI process-group teardown by lighttpd/busybox httpd
        setsid /bin/bash -c '
            LOG=/var/log/sa02m_install.log
            CTL=/usr/local/sbin/sa02m-web-service-ctl.sh
            echo "$(date "+%Y-%m-%d %H:%M:%S") mqtt_config.cgi: async bridge restart after save" >>"$LOG" 2>&1
            if [ -x "$CTL" ]; then
                sudo -n "$CTL" start mqtt-bridge >>"$LOG" 2>&1 || true
            fi
            sudo /usr/bin/systemctl restart sa02m-modbus-mqtt >>"$LOG" 2>&1 || true
        ' </dev/null >/dev/null 2>&1 &
    else
        echo '{"ok":true}'
    fi
    exit 0
fi

echo '{"error":"method_not_allowed"}'
