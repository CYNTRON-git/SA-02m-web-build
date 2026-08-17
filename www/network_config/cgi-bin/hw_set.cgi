#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

check_auth() {
    web_session_check_cookie && return 0
    return 1
}

if ! check_auth; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

# POST-only + CSRF BEFORE any mutation (body-driven I2C write).
# policy: docs/decisions/selective-csrf-policy.md. Headers already emitted.
if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
    echo '{"ok":false,"error":"method_not_allowed"}'
    exit 0
fi
if ! web_csrf_validate; then
    echo '{"ok":false,"error":"csrf","error_code":"E_CSRF"}'
    exit 0
fi

HW_CONF="/etc/sa02m_hw.conf"
. "$SCRIPT_DIR/lib_hw.sh"

read -r -n "${CONTENT_LENGTH:-0}" POST_DATA
decode() {
    echo "$POST_DATA" | sed -n "s/^.*$1=\([^&]*\).*$/\1/p" \
        | sed 's/%\([0-9A-F][0-9A-F]\)/\\x\1/gI' \
        | xargs -0 printf '%b'
}

CH=$(decode channel)
VAL=$(decode value)

case "$CH" in
    do|beeper|alarm_led|usb_power) ;;
    *)          echo '{"ok":false,"error":"bad_channel"}'; exit 0 ;;
esac

if ! sa02m_hw_channel_available "$CH"; then
    echo '{"ok":false,"error":"gpio_not_configured"}'
    exit 0
fi

if [ "$CH" = "usb_power" ] && [ "$VAL" = "reset" ]; then
    reset_sec=$(sa02m_hw_usb_reset_duration_sec)
    sa02m_hw_usb_power_reset_async
    rc=$?
    if [ "$rc" -eq 0 ]; then
        sa02m_hw_metrics_cache_patch_channel "$CH" 0 2>/dev/null || true
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] hw_set.cgi: usb_power reset ${reset_sec}s" >> /var/log/sa02m_install.log 2>&1
        echo "{\"ok\":true,\"channel\":\"${CH}\",\"value\":0,\"reset_sec\":${reset_sec},\"resetting\":true}"
        exit 0
    fi
    if [ "$rc" -eq 2 ]; then
        echo '{"ok":false,"error":"reset_busy"}'
        exit 0
    fi
    echo '{"ok":false,"error":"write_failed"}'
    exit 0
fi

if [ "$VAL" != "0" ] && [ "$VAL" != "1" ]; then
    echo '{"ok":false,"error":"bad_value"}'
    exit 0
fi

if sa02m_hw_use_i2c; then
    sa02m_hw_i2c_write_channel_web "$CH" "$VAL"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        sa02m_hw_metrics_cache_patch_channel "$CH" "$VAL" 2>/dev/null || true
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] hw_set.cgi: backend=i2c_expander channel=$CH value=$VAL bus=${SA02M_I2C_EXP_BUS} addr=${SA02M_I2C_EXP_ADDR}" >> /var/log/sa02m_install.log 2>&1
        if [ -n "${SA02M_HW_OVERRIDE_SEC:-}" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] hw_set.cgi: beeper override value=$VAL ttl_sec=${SA02M_HW_OVERRIDE_SEC}" >> /var/log/sa02m_install.log 2>&1
            echo "{\"ok\":true,\"channel\":\"${CH}\",\"value\":${VAL},\"override_sec\":${SA02M_HW_OVERRIDE_SEC}}"
        else
            echo "{\"ok\":true,\"channel\":\"${CH}\",\"value\":${VAL}}"
        fi
        exit 0
    fi

    case "$rc" in
        "$SA02M_HW_RC_BUSY"|"$SA02M_HW_RC_TIMEOUT") echo '{"ok":false,"error":"i2c_busy"}' ;;
        "$SA02M_HW_RC_TOOL") echo '{"ok":false,"error":"i2c_tools_missing"}' ;;
        *) echo '{"ok":false,"error":"write_failed"}' ;;
    esac
    exit 0
fi

if sa02m_hw_gpio_write_channel "$CH" "$VAL"; then
    sa02m_hw_metrics_cache_patch_channel "$CH" "$VAL" 2>/dev/null || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] hw_set.cgi: backend=gpio_sysfs channel=$CH value=$VAL" >> /var/log/sa02m_install.log 2>&1
    echo "{\"ok\":true,\"channel\":\"${CH}\",\"value\":${VAL}}"
else
    echo '{"ok":false,"error":"write_failed"}'
fi
