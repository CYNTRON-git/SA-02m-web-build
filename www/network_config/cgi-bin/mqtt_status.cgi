#!/bin/bash
. "$(dirname "$0")/lib_web_session.sh"
# MQTT broker / bridge status + параметры внешнего подключения (mqttuser).

check_auth() {
    web_session_check_cookie "${HTTP_COOKIE:-}" && return 0
    return 1
}
if ! check_auth; then
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    echo '{"error":"unauthorized"}'
    exit 0
fi

mosq_active=0
mosq_uptime_s=0
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
elif command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet mosquitto.service 2>/dev/null; then
    mosq_active=1
fi

bridge_active=0
bridge_disabled=0
if pgrep -f "modbus_mqtt_bridge" >/dev/null 2>&1; then
    bridge_active=1
elif command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet sa02m-modbus-mqtt.service 2>/dev/null; then
    bridge_active=1
fi

telemetry_active=0
telemetry_disabled=0
if pgrep -f "sa02m_telemetry" >/dev/null 2>&1; then
    telemetry_active=1
fi

mosq_disabled=0

# Единый источник для управляемых служб — sa02m-web-service-ctl.sh list
# (как «Управление → Службы» и status.cgi): user_disabled/mask важнее pgrep.
load_svc_ctl_mqtt_states() {
    local CTL=/usr/local/sbin/sa02m-web-service-ctl.sh
    [ -x "$CTL" ] || return 0
    command -v python3 >/dev/null 2>&1 || return 0
    local tmp
    tmp=$(mktemp /tmp/sa02m-mqttsvc.XXXXXX)
    if ! sudo -n "$CTL" list >"$tmp" 2>/dev/null; then
        rm -f "$tmp"
        return 0
    fi
    while IFS='|' read -r _sid _active _disabled; do
        case "$_sid" in
            mosquitto)
                if [ "$_disabled" = 1 ]; then
                    mosq_disabled=1
                    mosq_active=0
                elif [ "$_active" = 1 ]; then
                    mosq_active=1
                    mosq_disabled=0
                else
                    mosq_active=0
                    mosq_disabled=0
                fi
                ;;
            mqtt-bridge)
                if [ "$_disabled" = 1 ]; then
                    bridge_disabled=1
                    bridge_active=0
                elif [ "$_active" = 1 ]; then
                    bridge_active=1
                    bridge_disabled=0
                else
                    bridge_active=0
                    bridge_disabled=0
                fi
                ;;
            mqtt-telemetry)
                if [ "$_disabled" = 1 ]; then
                    telemetry_disabled=1
                    telemetry_active=0
                elif [ "$_active" = 1 ]; then
                    telemetry_active=1
                    telemetry_disabled=0
                else
                    telemetry_active=0
                    telemetry_disabled=0
                fi
                ;;
        esac
    done < <(python3 - "$tmp" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
for s in d.get("services", []):
    sid = s.get("id") or ""
    if not sid:
        continue
    if s.get("user_disabled") or s.get("masked"):
        active = 0
        disabled = 1
    elif s.get("active") == "active":
        active = 1
        disabled = 0
    else:
        active = 0
        disabled = 0
    print(f"{sid}|{active}|{disabled}")
PY
    )
    rm -f "$tmp"
}

load_svc_ctl_mqtt_states

clients_connected=0
if command -v mosquitto_sub >/dev/null 2>&1 && (( mosq_active == 1 )); then
    c=$(timeout 0.5 mosquitto_sub -h 127.0.0.1 -t '$SYS/broker/clients/connected' \
        -C 1 -W 1 2>/dev/null | tr -d '[:space:]') || c=""
    [[ "$c" =~ ^[0-9]+$ ]] && clients_connected=$c
fi

PRIMARY_HOST=""
if command -v ip >/dev/null 2>&1; then
    for _iface in end0 end1; do
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
try:
    r = subprocess.run(
        ["sudo", "-n", "/usr/local/sbin/sa02m-mqtt-external-info.py"],
        capture_output=True,
        text=True,
        timeout=3,
    )
    if r.returncode == 0 and r.stdout.strip():
        ext.update(json.loads(r.stdout))
except Exception:
    pass

if not (ext.get("mqtt_password") or "").strip():
    try:
        r = subprocess.run(
            ["sudo", "-n", "cat", "/etc/sa02m_mqtt.env"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            parsed = _parse_mqtt_env_text(r.stdout)
            ext["mqtt_user"] = parsed.get("mqtt_user") or ext.get("mqtt_user")
            ext["mqtt_password"] = parsed.get("mqtt_password") or ""
    except Exception:
        pass

print("Content-type: application/json; charset=UTF-8")
print("Cache-Control: no-store")
print()
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
