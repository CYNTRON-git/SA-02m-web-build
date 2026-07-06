#!/bin/sh
# sa02m-eth0-led-poll.sh — poll carrier, update link LEDs within 1-2 s.
LIB=/usr/local/lib/sa02m-eth-led-lib.sh
[ -f "$LIB" ] || exit 0
. "$LIB"

last=""

while true; do
    variant=$(sa02m_hw_variant)
    case "$variant" in
        sa02m-2eth)
            c0=$(cat /sys/class/net/eth1/carrier 2>/dev/null || echo 0)
            c1=$(cat /sys/class/net/eth0/carrier 2>/dev/null || echo 0)
            key="${variant}:${c0}:${c1}"
            ;;
        *)
            c0=$(cat /sys/class/net/eth0/carrier 2>/dev/null || echo 0)
            key="${variant}:${c0}:"
            ;;
    esac
    if [ "$key" != "$last" ]; then
        sa02m_led_sync_all
        last=$key
    fi
    sleep 1
done
