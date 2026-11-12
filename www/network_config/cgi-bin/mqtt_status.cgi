#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# MQTT broker / bridge status + параметры внешнего подключения (mqttuser).
# Fast path: pgrep + systemctl is-active/is-enabled (no full svcctl list).
#
# GET (polled by the MQTT tab) never carries the external broker password:
# it would sit in every poll response, cache and proxy log (audit 2026-11-11
# A5-05). The password is answered only to `POST {"reveal":"password"}` with a
# valid session AND CSRF token, `Cache-Control: no-store`. Transport stays HTTP
# 200 (the CGI-layer idiom). Behaviour pinned by scripts/dev/test-mqtt-status-secret.sh.

check_auth() {
    web_session_check_cookie && return 0
    return 1
}
if ! check_auth; then
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    echo '{"error":"unauthorized"}'
    exit 0
fi

if [ "${REQUEST_METHOD:-GET}" = "POST" ]; then
    web_csrf_require
    body=""
    CL=$(printf '%s' "${CONTENT_LENGTH:-}" | tr -cd '0-9')
    if [ -n "$CL" ] && [ "$CL" -gt 0 ] 2>/dev/null; then
        [ "$CL" -gt 256 ] && CL=256   # the only accepted body is 21 bytes
        body=$(dd bs=1 count="$CL" 2>/dev/null) || body=""
    fi
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    # Allow-list: exactly {"reveal":"password"}, whitespace aside.
    if [ "$(printf '%s' "$body" | tr -d ' \t\r\n')" != '{"reveal":"password"}' ]; then
        echo '{"ok":false,"error":"bad_request"}'
        exit 0
    fi
    MQTT_ENV_TEXT=$(timeout 2 sudo -n cat /etc/sa02m_mqtt.env 2>/dev/null || true)
    MQTT_EXT_INFO=""
    case "$MQTT_ENV_TEXT" in
        *MQTT_PASS=?*|*MQTT_PASSWORD=?*) : ;;
        *) MQTT_EXT_INFO=$(timeout 2 sudo -n /usr/local/sbin/sa02m-mqtt-external-info.py 2>/dev/null || true) ;;
    esac
    export MQTT_ENV_TEXT MQTT_EXT_INFO
    python3 <<'PY'
import json
import os


def _parse_mqtt_env_text(text: str) -> dict:
    user, passwd = "mqttuser", ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("MQTT_USER="):
            user = line.split("=", 1)[1].strip().strip("'\"")
        elif line.startswith("MQTT_PASS=") or line.startswith("MQTT_PASSWORD="):
            passwd = line.split("=", 1)[1].strip().strip("'\"")
    return {"mqtt_user": user or "mqttuser", "mqtt_password": passwd}


ext = _parse_mqtt_env_text(os.environ.get("MQTT_ENV_TEXT", ""))
if not ext["mqtt_password"].strip():
    try:
        info = json.loads(os.environ.get("MQTT_EXT_INFO") or "{}")
        if isinstance(info, dict):
            ext["mqtt_user"] = str(info.get("mqtt_user") or ext["mqtt_user"])
            ext["mqtt_password"] = str(info.get("mqtt_password") or "")
    except ValueError:
        pass
print(json.dumps({"ok": True, "mqtt_user": ext["mqtt_user"],
                  "mqtt_password": ext["mqtt_password"]}, ensure_ascii=False))
PY
    exit 0
fi

# Emit headers immediately so the client is not waiting on a blank socket
# while we probe services (was: headers only after slow work → 5–10 s).
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

unit_enabled_state() {
    # enabled | disabled | masked | static | … — timeout: a wedged systemd
    # must not stall this polled endpoint (web-code-rigor "Timeouts everywhere").
    timeout 2 /usr/bin/systemctl is-enabled "$1" 2>/dev/null | head -n1 | tr -d '\r' || true
}

unit_is_active() {
    timeout 2 /usr/bin/systemctl is-active --quiet "$1" 2>/dev/null
}

port_listening() {
    timeout 1 ss -H -ltn "sport = :$1" 2>/dev/null | awk 'NR==1 { found=1 } END { exit(found ? 0 : 1) }'
}

mosq_active=0
mosq_uptime_s=0
mosq_disabled=0
# Runtime first. `systemctl disable` does not stop a live broker, so
# is-enabled=disabled must not paint «Отключен» while mosquitto is active.
# «Отключен» is only a broker that is not running and whose unit is
# disabled or masked. A down 1883/1884 listener is reported separately
# and does not flip the whole broker to disabled.
if pgrep -x mosquitto >/dev/null 2>&1; then
    mosq_active=1
    pid=$(pgrep -x mosquitto | head -1)
    if [ -n "$pid" ] && [ -r "/proc/${pid}/stat" ]; then
        boot_j=$(awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || echo 0)
        clock_hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
        uptime_sys=$(awk '{printf "%d",$1}' /proc/uptime 2>/dev/null || echo 0)
        mosq_uptime_s=$(( uptime_sys - boot_j / clock_hz ))
        (( mosq_uptime_s < 0 )) && mosq_uptime_s=0
    fi
elif unit_is_active mosquitto.service; then
    mosq_active=1
fi
if (( mosq_active == 0 )); then
    en=$(unit_enabled_state mosquitto.service)
    case "$en" in
        masked|disabled) mosq_disabled=1 ;;
    esac
fi

listen_local=0
listen_external=0
if command -v ss >/dev/null 2>&1; then
    port_listening 1883 && listen_local=1
    port_listening 1884 && listen_external=1
elif (( mosq_active == 1 )); then
    listen_local=1
    listen_external=1
fi

bridge_active=0
bridge_disabled=0
en=$(unit_enabled_state sa02m-modbus-mqtt.service)
case "$en" in
    masked|disabled) bridge_disabled=1 ;;
esac
if (( bridge_disabled == 0 )); then
    if pgrep -f "modbus_mqtt_bridge" >/dev/null 2>&1; then
        bridge_active=1
    elif unit_is_active sa02m-modbus-mqtt.service; then
        bridge_active=1
    fi
fi

telemetry_active=0
telemetry_disabled=0
en=$(unit_enabled_state sa02m-telemetry.service)
case "$en" in
    masked|disabled) telemetry_disabled=1 ;;
esac
if (( telemetry_disabled == 0 )); then
    if pgrep -f "sa02m_telemetry" >/dev/null 2>&1; then
        telemetry_active=1
    elif unit_is_active sa02m-telemetry.service; then
        telemetry_active=1
    fi
fi

# Clients count: short timeout only (must not dominate status latency).
clients_connected=0
if (( mosq_active == 1 )) && command -v mosquitto_sub >/dev/null 2>&1; then
    _cli=$(timeout 0.35 mosquitto_sub -h 127.0.0.1 -p 1883 -t '$SYS/broker/clients/connected' -C 1 -W 1 2>/dev/null | tr -dc '0-9' || true)
    [ -n "$_cli" ] && clients_connected=$_cli
fi

PRIMARY_HOST=""
if command -v ip >/dev/null 2>&1; then
    for _iface in eth0 eth1; do
        PRIMARY_HOST=$(ip -o -4 addr show dev "$_iface" 2>/dev/null | awk '{print $4}' | head -n1 | cut -d/ -f1 | tr -d '\r')
        [ -n "$PRIMARY_HOST" ] && break
    done
fi
[ -z "$PRIMARY_HOST" ] && PRIMARY_HOST=$(hostname -I 2>/dev/null | awk '{print $1}' | tr -d '\r')

export MOSQ_ACTIVE=$mosq_active
export MOSQ_UPTIME=$mosq_uptime_s
export MOSQ_DISABLED=$mosq_disabled
export BRIDGE_ACTIVE=$bridge_active
export BRIDGE_DISABLED=$bridge_disabled
export TELEMETRY_ACTIVE=$telemetry_active
export TELEMETRY_DISABLED=$telemetry_disabled
export CLIENTS_CONNECTED=$clients_connected
export LISTEN_LOCAL=$listen_local
export LISTEN_EXTERNAL=$listen_external
export PRIMARY_HOST

python3 <<'PY'
import json
import os

# No credential read on the poll path: the user is the installer's fixed
# `mqttuser` (scripts/05-mqtt.sh); the password is the POST reveal above.
print(
    json.dumps(
        {
            "mosquitto_active": int(os.environ.get("MOSQ_ACTIVE", 0)),
            "mosquitto_disabled": int(os.environ.get("MOSQ_DISABLED", 0)),
            "mosquitto_uptime_s": int(os.environ.get("MOSQ_UPTIME", 0)),
            "bridge_active": int(os.environ.get("BRIDGE_ACTIVE", 0)),
            "bridge_disabled": int(os.environ.get("BRIDGE_DISABLED", 0)),
            "telemetry_active": int(os.environ.get("TELEMETRY_ACTIVE", 0)),
            "telemetry_disabled": int(os.environ.get("TELEMETRY_DISABLED", 0)),
            "clients_connected": int(os.environ.get("CLIENTS_CONNECTED", 0)),
            "listen_local": int(os.environ.get("LISTEN_LOCAL", 0)),
            "listen_external": int(os.environ.get("LISTEN_EXTERNAL", 0)),
            "port_local": 1883,
            "port_external": 1884,
            "host": os.environ.get("PRIMARY_HOST") or "",
            "mqtt_user": "mqttuser",
        },
        ensure_ascii=False,
    )
)
PY
