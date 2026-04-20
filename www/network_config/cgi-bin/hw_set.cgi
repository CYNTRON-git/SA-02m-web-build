#!/bin/bash
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    [[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] && return 0
    return 1
}

if ! check_auth; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

HW_CONF="/etc/sa02m_hw.conf"
SA02M_GPIO_DO=""
SA02M_GPIO_BEEPER=""
SA02M_GPIO_ALARM_LED=""
SA02M_GPIO_USB_POWER=""
[ -f "$HW_CONF" ] && . "$HW_CONF" 2>/dev/null

read -r POST_DATA
decode() {
    echo "$POST_DATA" | sed -n "s/^.*$1=\([^&]*\).*$/\1/p" \
        | sed 's/%\([0-9A-F][0-9A-F]\)/\\x\1/gI' \
        | xargs -0 printf '%b'
}

CH=$(decode channel)
VAL=$(decode value)

case "$CH" in
    do)         PIN=$SA02M_GPIO_DO ;;
    beeper)     PIN=$SA02M_GPIO_BEEPER ;;
    alarm_led)  PIN=$SA02M_GPIO_ALARM_LED ;;
    usb_power)  PIN=$SA02M_GPIO_USB_POWER ;;
    *)          echo '{"ok":false,"error":"bad_channel"}'; exit 0 ;;
esac

if [ -z "$PIN" ] || ! [[ "$PIN" =~ ^[0-9]+$ ]]; then
    echo '{"ok":false,"error":"gpio_not_configured"}'
    exit 0
fi

if [ "$VAL" != "0" ] && [ "$VAL" != "1" ]; then
    echo '{"ok":false,"error":"bad_value"}'
    exit 0
fi

gpio_export_out() {
    local n=$1
    if [ ! -d "/sys/class/gpio/gpio${n}" ]; then
        echo "$n" | sudo tee /sys/class/gpio/export >/dev/null 2>&1 || true
        sleep 0.08
    fi
    [ -d "/sys/class/gpio/gpio${n}" ] || return 1
    echo out | sudo tee "/sys/class/gpio/gpio${n}/direction" >/dev/null 2>&1 || return 1
    return 0
}

if ! gpio_export_out "$PIN"; then
    echo '{"ok":false,"error":"gpio_export_failed"}'
    exit 0
fi

if echo "$VAL" | sudo tee "/sys/class/gpio/gpio${PIN}/value" >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] hw_set.cgi: channel=$CH gpio=$PIN value=$VAL" >> /var/log/sa02m_install.log 2>&1
    echo "{\"ok\":true,\"channel\":\"${CH}\",\"value\":${VAL}}"
else
    echo '{"ok":false,"error":"write_failed"}'
fi
