#!/bin/sh
IFACE=end1
log() { logger -t sa02m-end1-coldboot -- "$*"; }
[ -d "/sys/class/net/$IFACE" ] || exit 0
[ -f "/etc/network/interfaces.d/${IFACE}.conf" ] || exit 0
carrier=$(cat /sys/class/net/$IFACE/carrier 2>/dev/null || echo 0)
if [ "$carrier" = "1" ]; then
    log "$IFACE: carrier already up, skipping coldboot fix"
    exit 0
fi
log "$IFACE: no carrier at T+15s, attempting PHY recovery (cold-boot fix)"
ip link set "$IFACE" up 2>/dev/null || true
sleep 1
ethtool -r "$IFACE" 2>/dev/null || true
sleep 3
carrier=$(cat /sys/class/net/$IFACE/carrier 2>/dev/null || echo 0)
if [ "$carrier" = "1" ]; then
    log "$IFACE: link recovered after phylink restart"
else
    log "$IFACE: link still down after cold-boot fix attempt"
fi
exit 0
