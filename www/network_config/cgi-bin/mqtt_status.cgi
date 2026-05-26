#!/bin/bash
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
    return 1
}
if ! check_auth; then echo '{"error":"unauthorized"}'; exit 0; fi

# Статус Mosquitto
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
fi

# Статус sa02m-modbus-mqtt
bridge_active=0
if pgrep -f "modbus_mqtt_bridge" >/dev/null 2>&1; then
    bridge_active=1
fi

# Статус sa02m-telemetry
telemetry_active=0
if pgrep -f "sa02m_telemetry" >/dev/null 2>&1; then
    telemetry_active=1
fi

# Клиенты брокера из $SYS (быстро, без блокировки)
clients_connected=0
if command -v mosquitto_sub >/dev/null 2>&1 && (( mosq_active == 1 )); then
    c=$(timeout 0.5 mosquitto_sub -h 127.0.0.1 -t '$SYS/broker/clients/connected' \
        -C 1 -W 1 2>/dev/null | tr -d '[:space:]') || c=""
    [[ "$c" =~ ^[0-9]+$ ]] && clients_connected=$c
fi

cat <<JSON
{
  "mosquitto_active": ${mosq_active},
  "mosquitto_uptime_s": ${mosq_uptime_s},
  "bridge_active": ${bridge_active},
  "telemetry_active": ${telemetry_active},
  "clients_connected": ${clients_connected},
  "port_local": 1883,
  "port_external": 1884
}
JSON
