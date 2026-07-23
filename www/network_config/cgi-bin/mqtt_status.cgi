#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# MQTT broker / bridge status + параметры внешнего подключения (mqttuser).
# Fast path: pgrep + systemctl is-active/is-enabled (no full svcctl list).

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

mosq_active=0
mosq_uptime_s=0
mosq_disabled=0
# masked|disabled — same user_disabled-or-masked semantics as the Управление
# tab (status.cgi svc-ctl mapping); disable-without-mask must render the same.
en=$(unit_enabled_state mosquitto.service)
case "$en" in
    masked|disabled) mosq_disabled=1 ;;
esac
if (( mosq_disabled == 0 )); then
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
export PRIMARY_HOST

python3 <<'PY'
import json
import os
import subprocess

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


ext = {"host": "", "mqtt_user": "mqttuser", "mqtt_password": ""}
# Prefer direct env file (fast); external-info.py is optional fallback.
try:
    r = subprocess.run(
        ["sudo", "-n", "cat", "/etc/sa02m_mqtt.env"],
        capture_output=True,
        text=True,
        timeout=1,
    )
    if r.returncode == 0 and r.stdout.strip():
        parsed = _parse_mqtt_env_text(r.stdout)
        ext["mqtt_user"] = parsed.get("mqtt_user") or ext.get("mqtt_user")
        ext["mqtt_password"] = parsed.get("mqtt_password") or ""
except Exception:
    pass

if not (ext.get("mqtt_password") or "").strip():
    try:
        r = subprocess.run(
            ["sudo", "-n", "/usr/local/sbin/sa02m-mqtt-external-info.py"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if r.returncode == 0 and r.stdout.strip():
            ext.update(json.loads(r.stdout))
    except Exception:
        pass

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
            "port_local": 1883,
            "port_external": 1884,
            "host": ext.get("host") or os.environ.get("PRIMARY_HOST") or "",
            "mqtt_user": ext.get("mqtt_user") or "mqttuser",
            "mqtt_password": ext.get("mqtt_password") or "",
        },
        ensure_ascii=False,
    )
)
PY
