#!/bin/bash
. "$(dirname "$0")/lib_web_session.sh"
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

check_auth() {
    web_session_check_cookie "${HTTP_COOKIE:-}" && return 0
    return 1
}

if ! check_auth; then
    echo '{"ok":false,"error":"unauthorized"}'
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
    if sa02m_hw_i2c_write_channel "$CH" "$VAL"; then
        sa02m_hw_metrics_cache_patch_channel "$CH" "$VAL" 2>/dev/null || true
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] hw_set.cgi: backend=i2c_expander channel=$CH value=$VAL bus=${SA02M_I2C_EXP_BUS} addr=${SA02M_I2C_EXP_ADDR}" >> /var/log/sa02m_install.log 2>&1
        echo "{\"ok\":true,\"channel\":\"${CH}\",\"value\":${VAL}}"
        exit 0
    fi

    case "$?" in
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
