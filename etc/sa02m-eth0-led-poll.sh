#!/bin/sh
# sa02m-eth0-led-poll.sh — быстрый поллинг carrier end0, обновляет LED за 1-2 сек.
LED=/sys/class/leds/eth0_link
IFACE=end0

[ -d "$LED" ] || exit 0
echo none > "$LED/trigger" 2>/dev/null || true

last=""
while true; do
    carrier=$(cat "/sys/class/net/${IFACE}/carrier" 2>/dev/null || echo 0)
    if [ "$carrier" != "$last" ]; then
        if [ "$carrier" = "1" ]; then
            echo 1 > "$LED/brightness" 2>/dev/null || true
        else
            echo 0 > "$LED/brightness" 2>/dev/null || true
        fi
        last="$carrier"
    fi
    sleep 1
done
