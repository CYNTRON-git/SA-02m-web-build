#!/bin/sh
# Soft PHY renegotiate for configured eth0/eth1 after networking first bring-up.
# Complements net-watchdog / fix-eth; avoids "no ping until cable re-plug" on
# cold boot (IP101 / EMAC autoneg stall). Soft only — no MDIO unbind loops.
log() { logger -t sa02m-eth-coldboot -- "$*"; }

for IFACE in eth0 eth1; do
    [ -d "/sys/class/net/$IFACE" ] || continue
    [ -f "/etc/network/interfaces.d/${IFACE}.conf" ] || continue

    carrier=$(cat "/sys/class/net/$IFACE/carrier" 2>/dev/null || echo 0)
    if [ "$carrier" = "1" ]; then
        log "$IFACE: carrier already up, skip"
        continue
    fi

    log "$IFACE: no carrier — soft PHY restart"
    ip link set "$IFACE" up 2>/dev/null || true
    sleep 1
    ethtool -r "$IFACE" 2>/dev/null || mii-tool -r "$IFACE" 2>/dev/null || true
    sleep 3

    carrier=$(cat "/sys/class/net/$IFACE/carrier" 2>/dev/null || echo 0)
    if [ "$carrier" != "1" ]; then
        ip link set "$IFACE" down 2>/dev/null || true
        sleep 1
        ip link set "$IFACE" up 2>/dev/null || true
        ethtool -r "$IFACE" 2>/dev/null || mii-tool -r "$IFACE" 2>/dev/null || true
        sleep 3
        carrier=$(cat "/sys/class/net/$IFACE/carrier" 2>/dev/null || echo 0)
    fi

    if [ "$carrier" = "1" ]; then
        log "$IFACE: link recovered after cold-boot fix"
    else
        log "$IFACE: still no carrier after cold-boot fix"
    fi
    systemctl start --no-block "fix-eth@${IFACE}.service" 2>/dev/null || true
done
exit 0
