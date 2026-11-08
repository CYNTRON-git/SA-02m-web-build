#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# GET  → читает /etc/sa02m-modbus-mqtt.yaml, возвращает JSON-представление
# POST → принимает JSON, сохраняет YAML, опционально перезапускает мост

CONFIG_FILE="/etc/sa02m-modbus-mqtt.yaml"
# The bridge's transport validator (bridge_bus.py, stdlib-only) — the one home
# of the Modbus TCP entry rules (docs/contracts/bridge-modbus-tcp.md). Server
# env only: fcgiwrap passes a request nothing but HTTP_* variables. Handed to
# python as argv, never interpolated into its source.
BRIDGE_LIB_DIR="${SA02M_BRIDGE_LIB_DIR:-/opt/sa02m-modbus-mqtt}"

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
    # Читаем YAML и конвертируем в JSON через Python. `capabilities` (what the
    # add-device modal may offer over Modbus TCP) comes from the validator
    # itself; without it the field is absent and the modal hides Ethernet.
    python3 - "$BRIDGE_LIB_DIR" <<'PYEOF'
import sys, json, yaml, pathlib
cfg_path = pathlib.Path("/etc/sa02m-modbus-mqtt.yaml")


def capabilities():
    try:
        sys.path.insert(0, sys.argv[1])
        import bridge_bus
        return bridge_bus.capabilities()
    except Exception as e:
        # Fail closed (no Ethernet option), but never silently: fcgiwrap
        # hands stderr to the nginx error log.
        print("mqtt_config.cgi: Modbus TCP validator unavailable - cannot "
              "import bridge_bus from %s: %s; the Ethernet option is hidden"
              % (sys.argv[1], e), file=sys.stderr)
        return None


try:
    if cfg_path.is_file():
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}
        # Remove comments-only keys, keep structure
        out = {
            "mqtt": data.get("mqtt", {}),
            "devices": data.get("devices", []) or [],
        }
    else:
        out = {"devices": [],
               "mqtt": {"broker": "127.0.0.1", "port": 1883, "qos": 1, "retain": True}}
    caps = capabilities()
    if caps is not None:
        out["capabilities"] = caps
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
        web_csrf_error_body
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

    RESULT=$(python3 - "$TMP_IN" "$TMP_OUT" "$BRIDGE_LIB_DIR" "$CONFIG_FILE" <<'PYEOF'
import sys, json, yaml, pathlib


def check_template_choices(devices):
    selected = [d for d in devices if isinstance(d, dict) and "fmb_event_channels" in d]
    if not selected:
        return
    # A temporarily unavailable template must not block edits to unrelated devices.
    try:
        with open(sys.argv[4], encoding="utf-8") as f:
            old_config = yaml.safe_load(f)
        old_devices = old_config.get("devices") if isinstance(old_config, dict) else None
    except (OSError, ValueError, OverflowError, RecursionError, yaml.YAMLError):
        old_devices = None
    # Preserve the stored type: 42 and "42" are distinct config entries.
    # In particular an old YAML boolean must never act as numeric ID 0 or 1.
    rows = old_devices if isinstance(old_devices, list) else []
    previous = {d["id"]: d for d in rows if isinstance(d, dict) and valid_device_id(d.get("id"))}
    catalogs = {}
    for dev in selected:
        names = dev["fmb_event_channels"]
        refusal = {"ok": False, "error": "invalid_device", "id": dev.get("id"),
                   "reason": "invalid_template_event_channels"}
        if (not isinstance(names, list) or not all(isinstance(n, str) and n for n in names)
                or len(set(names)) != len(names)):
            return refusal
        if not names:
            continue
        if dev.get("type") != "template" or dev.get("transport") not in (None, "rtu"):
            return refusal
        old = previous.get(dev["id"])  # the incoming ID has already passed valid_device_id
        if (old and old.get("transport") in (None, "rtu") and
                all(old.get(key) == dev.get(key) for key in
                    ("type", "template", "port", "address", "fmb_event_channels", "fast_modbus"))):
            continue
        template = dev.get("template")
        if not isinstance(template, str) or not template:
            return refusal
        device_id = str(dev["id"])
        # The complete MQTT metadata topic must fit for this device. IDs of
        # the same UTF-8 length have the same eligible names for a template.
        key = (template, len(device_id.encode("utf-8")))
        if key not in catalogs:
            try:
                from bridge_template_channels import channel_catalog, resolve_template_path
                with resolve_template_path(template).open(encoding="utf-8") as f:
                    catalogs[key] = {ch["name"] for ch in channel_catalog(json.load(f), device_id) if ch["fast_eligible"]}
            except (ImportError, OSError, ValueError, TypeError, OverflowError, RuntimeError):
                refusal["reason"] = "template_event_catalog_unavailable"
                return refusal
        if not set(names) <= catalogs[key]:
            return refusal


try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    data.pop("restart", None)
    # GET-computed, server-owned: a bundle that echoes its whole config back
    # (every mqtt.js before 1.0.6.56) must not persist it into the YAML.
    data.pop("capabilities", None)
    for dev in data.get("devices") or []:
        if isinstance(dev, dict):
            dev.pop("restart", None)
    # Modbus TCP entries: refuse the WHOLE save on the first invalid one, with
    # the bridge's own reason code, before anything is written. The bridge
    # loader re-checks (the YAML is www-data-writable); this is the early,
    # explainable refusal. The MQTT topic ID applies to every transport.
    devices = data.get("devices") or []
    sys.path.insert(0, sys.argv[3])
    if devices:
        try:
            from bridge_meta import valid_device_id
        except ImportError as e:
            print("mqtt_config.cgi: MQTT device ID validator unavailable: %s" % e, file=sys.stderr)
            print(json.dumps({"ok": False, "error": "mqtt_id_validator_unavailable"}))
            sys.exit(0)
        for device in devices:
            if isinstance(device, dict) and not valid_device_id(device.get("id")):
                print(json.dumps({"ok": False, "error": "invalid_device", "id": device.get("id"),
                                  "reason": "invalid_device_id"}))
                sys.exit(0)
    try:
        import bridge_bus
    except Exception as e:
        print("mqtt_config.cgi: Modbus TCP validator unavailable - cannot "
              "import bridge_bus from %s: %s; TCP entries are refused"
              % (sys.argv[3], e), file=sys.stderr)
        bridge_bus = None
    if bridge_bus is None:
        # Fail closed: without the validator a TCP entry cannot be vouched for.
        if any(isinstance(d, dict) and d.get("transport") not in (None, "rtu")
               for d in devices):
            print(json.dumps({"ok": False, "error": "transport_validator_unavailable"}))
            sys.exit(0)
    else:
        refused = bridge_bus.validate_devices(devices)
        if refused:
            print(json.dumps({"ok": False, "error": "invalid_device",
                              "id": refused[0]["id"], "reason": refused[0]["reason"]}))
            sys.exit(0)
    refusal = check_template_choices(devices)
    if refusal:
        print(json.dumps(refusal))
        sys.exit(0)
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
