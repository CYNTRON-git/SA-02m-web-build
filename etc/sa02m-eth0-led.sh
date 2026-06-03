#!/bin/sh
# sa02m-eth0-led.sh — LED eth0_link по carrier end0 (platform gpio-led, без netdev trigger).
LED=/sys/class/leds/eth0_link
IFACE=end0

[ -d "$LED" ] || exit 0

echo none > "$LED/trigger" 2>/dev/null || true
carrier=$(cat "/sys/class/net/${IFACE}/carrier" 2>/dev/null || echo 0)
if [ "$carrier" = "1" ]; then
    echo 1 > "$LED/brightness" 2>/dev/null || exit 0
else
    echo 0 > "$LED/brightness" 2>/dev/null || exit 0
fi
