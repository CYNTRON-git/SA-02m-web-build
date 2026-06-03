#!/bin/bash
# SA-02m: end1 cold-boot autoneg recovery
#
# Runs once at ~30 s after boot.  If end1 carrier is already up → exits
# immediately without touching anything.  If not → restarts autoneg once
# via ethtool -r and waits up to 15 s for link.
#
# Does NOT do unbind/rebind, does NOT loop — one attempt then done.

IFACE=end1
WAIT_INITIAL=30   # seconds to wait before first carrier check
WAIT_AUTONEG=15   # seconds to wait after ethtool -r for link

log() { echo "[$(date '+%H:%M:%S')] sa02m-end1-coldboot: $*"; }

sleep "$WAIT_INITIAL"

carrier=$(cat "/sys/class/net/${IFACE}/carrier" 2>/dev/null || echo 0)
if [ "$carrier" = "1" ]; then
    log "${IFACE}: carrier already up — nothing to do"
    exit 0
fi

log "${IFACE}: no carrier after ${WAIT_INITIAL}s — restarting autoneg (ethtool -r)"
ip link set "$IFACE" up 2>/dev/null || true
ethtool -r "$IFACE" 2>/dev/null || mii-tool -r "$IFACE" 2>/dev/null || true

sleep "$WAIT_AUTONEG"

carrier=$(cat "/sys/class/net/${IFACE}/carrier" 2>/dev/null || echo 0)
if [ "$carrier" = "1" ]; then
    log "${IFACE}: link UP after autoneg restart"
else
    log "${IFACE}: still no carrier after autoneg restart — check cable or power-cycle"
fi
