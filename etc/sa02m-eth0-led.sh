#!/bin/sh
# sa02m-eth0-led.sh — sync link LEDs by carrier and hardware variant.
LIB=/usr/local/lib/sa02m-eth-led-lib.sh
[ -f "$LIB" ] || exit 0
. "$LIB"
sa02m_led_sync_all
