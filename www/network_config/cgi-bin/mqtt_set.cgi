#!/bin/bash
# shellcheck disable=SC1091
# mqtt_set.cgi — write a device output (DO / DTV coil) via the local broker.
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
    echo '{"ok":false,"error":"csrf","error_code":"E_CSRF"}'
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
# and DTV writable coils. (ao_N = live analog setpoint, holding reg 33+ch-1.)
if ! [[ "$CONTROL" =~ ^(do_([1-9]|1[0-6])|ao_([1-9]|1[0-2])|buzzer|leds)$ ]]; then
    echo '{"ok":false,"error":"bad_control"}'
    exit 0
fi

# value: DO/buzzer/leds are strict 0/1; ao_ is an integer setpoint 0..1000
# (= 0..10.00 V). Fail-closed either way; the regex bars any non-digit, so VAL
# never reaches a shell word / arithmetic as attacker text.
case "$CONTROL" in
    ao_*)
        if ! [[ "$VAL" =~ ^[0-9]{1,4}$ ]] || (( 10#$VAL > 1000 )); then
            echo '{"ok":false,"error":"bad_value"}'
            exit 0
        fi
        VAL=$((10#$VAL))   # normalize (drop leading zeros → valid JSON number + clean payload)
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
