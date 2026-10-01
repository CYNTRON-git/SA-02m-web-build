#!/bin/bash
# shellcheck disable=SC1091
# mqtt_set.cgi — write a device output (DO / AO / AI sensor type / DTV coil /
# Carel AHU command / MTD262-MB holding) via the local broker.
# Publishes /devices/<id>/controls/<name>/on to 127.0.0.1:1883 (constants —
# the external 1884 listener is never reachable from here); the bridge does
# the Modbus write and confirms with a forced echo. Contract:
# docs/contracts/mqtt-set-endpoint.md
. "$(dirname "$0")/lib_web_auth.sh"
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    web_session_check_cookie && return 0
    return 1
}

if ! check_auth; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

if [ "${REQUEST_METHOD:-}" != "POST" ]; then
    echo '{"ok":false,"error":"post_required"}'
    exit 0
fi

# CSRF BEFORE any mutation (web-code-rigor.md ## Bash CGI floors; policy:
# docs/decisions/selective-csrf-policy.md; contract: mqtt-set-endpoint.md).
# Headers already emitted above, so validate inline and print the shared shape.
if ! web_csrf_validate; then
    web_csrf_error_body
    exit 0
fi

read -r -n "${CONTENT_LENGTH:-0}" POST_DATA
decode() {
    echo "$POST_DATA" | sed -n "s/^.*$1=\([^&]*\).*$/\1/p" \
        | sed 's/%\([0-9A-F][0-9A-F]\)/\\x\1/gI' \
        | xargs -0 printf '%b'
}

DEVICE=$(decode device)
CONTROL=$(decode control)
VAL=$(decode value)

# Allow-lists BEFORE any shell word (web-code-rigor.md, Bash CGI floors).
# device: same charset class as mqtt_live.cgi, plus a length cap.
case "$DEVICE" in
    *[!a-zA-Z0-9._-]*|'')
        echo '{"ok":false,"error":"bad_device"}'
        exit 0
        ;;
esac
if [ "${#DEVICE}" -gt 64 ]; then
    echo '{"ok":false,"error":"bad_device"}'
    exit 0
fi

# control: closed enum — MR-02m DO coils do_1..do_16, AO setpoints ao_1..ao_12,
# AI sensor type ai_type_1..ai_type_12, DTV writable coils, Carel commands the
# bridge already subscribes (unit_on / setpoint / setpoint_summer / fan_supply /
# fan_step), and the seven MTD262-MB writable holdings.
# (ao_N = live analog setpoint, holding reg 33+ch-1.
#  ai_type_N = AI sensor code 0..42, holding 400+7*(N-1).
#  Carel setpoint is °C 0..99 with one decimal; the bridge clamps per family.
#  fan_supply is an integer percent 0..100; fan_step is 1..10.
#  MTD values are the physical number the template poller inverts by scale.)
if ! [[ "$CONTROL" =~ ^(do_([1-9]|1[0-6])|ao_([1-9]|1[0-2])|ai_type_([1-9]|1[0-2])|buzzer|leds|unit_on|setpoint|setpoint_summer|fan_supply|fan_step|detection_distance|detection_shielding_distance|admission_confirmation_delay|departure_disappearance_delay|trigger_sensitivity|maintain_sensitivity|entrance_distance_reduction)$ ]]; then
    echo '{"ok":false,"error":"bad_control"}'
    exit 0
fi

# value: DO/buzzer/leds/unit_on are strict 0/1; ao_ is an integer setpoint
# 0..1000 (= 0..10.00 V); ai_type_ is an integer sensor code 0..42 (0 = off).
# Carel and MTD numbers are validated below and rewritten without leading
# zeros so the JSON value and the MQTT payload stay a number. Fail-closed:
# VAL never reaches a shell word as attacker text.
case "$CONTROL" in
    ao_*)
        if ! [[ "$VAL" =~ ^[0-9]{1,4}$ ]] || (( 10#$VAL > 1000 )); then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        VAL=$((10#$VAL))   # normalize (drop leading zeros → valid JSON number + clean payload)
        ;;
    ai_type_*)
        if ! [[ "$VAL" =~ ^[0-9]{1,2}$ ]] || (( 10#$VAL > 42 )); then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        VAL=$((10#$VAL))
        ;;
    unit_on)
        if [ "$VAL" != "0" ] && [ "$VAL" != "1" ]; then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        ;;
    setpoint|setpoint_summer)
        if ! [[ "$VAL" =~ ^([0-9]{1,2})(\.[0-9])?$ ]]; then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        _w=$((10#${BASH_REMATCH[1]}))
        _f="${BASH_REMATCH[2]}"
        if (( _w > 99 )); then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        if (( _w == 99 )) && [ -n "$_f" ] && [ "$_f" != ".0" ]; then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        VAL="${_w}${_f}"
        ;;
    fan_supply)
        if ! [[ "$VAL" =~ ^[0-9]{1,3}$ ]] || (( 10#$VAL > 100 )); then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        VAL=$((10#$VAL))
        ;;
    fan_step)
        if ! [[ "$VAL" =~ ^[0-9]{1,2}$ ]] || (( 10#$VAL < 1 || 10#$VAL > 10 )); then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        VAL=$((10#$VAL))
        ;;
    detection_distance|detection_shielding_distance|admission_confirmation_delay|departure_disappearance_delay|trigger_sensitivity|maintain_sensitivity|entrance_distance_reduction)
        if ! [[ "$VAL" =~ ^([0-9]{1,5})(\.[0-9]{1,2})?$ ]]; then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        _w=$((10#${BASH_REMATCH[1]}))
        _f="${BASH_REMATCH[2]}"
        if (( _w > 65535 )); then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        if (( _w == 65535 )) && [ -n "$_f" ] && [ "$_f" != ".0" ] && [ "$_f" != ".00" ]; then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        VAL="${_w}${_f}"
        ;;
    *)
        if [ "$VAL" != "0" ] && [ "$VAL" != "1" ]; then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        ;;
esac

TOPIC="/devices/${DEVICE}/controls/${CONTROL}/on"
# NO retain (-r) — a retained /on replays on bridge restart and re-toggles
# real outputs. Hard floor of this endpoint; asserted by the quality row
# `mqtt-set-contract` (.ai-dev/quality/checks/mqtt-set-contract.sh), which
# fails on `-r` in the publish argv and anywhere in this file.
if timeout 5 mosquitto_pub -h 127.0.0.1 -p 1883 -t "$TOPIC" -m "$VAL" >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] mqtt_set.cgi: device=$DEVICE control=$CONTROL value=$VAL" >> /var/log/sa02m_install.log 2>&1
    printf '{"ok":true,"device":"%s","control":"%s","value":%s}\n' "$DEVICE" "$CONTROL" "$VAL"
else
    echo '{"ok":false,"error":"publish_failed"}'
fi
