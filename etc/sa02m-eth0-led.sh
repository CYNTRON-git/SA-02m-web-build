#!/bin/sh
# sa02m-eth0-led.sh — устанавливает netdev trigger на LED eth0_link.
# Вызывается из fix-eth.sh после подтверждения IP (PHY уже стабилен).
# netdev trigger активно опрашивает carrier и включает LED немедленно.
LED=/sys/class/leds/eth0_link
IFACE=eth0

[ -d "$LED" ] || exit 0

# PHY driver сбрасывает trigger в [none] после link-up (~0.3с).
# Небольшая задержка нужна при вызове из udev (carrier=1 event).
# При вызове из fix-eth.sh задержка выполнена там (sleep 5).
sleep 0.5

echo "netdev" > "$LED/trigger"     2>/dev/null || exit 0
echo "$IFACE"  > "$LED/device_name" 2>/dev/null || true
echo "1"       > "$LED/link"        2>/dev/null || true
