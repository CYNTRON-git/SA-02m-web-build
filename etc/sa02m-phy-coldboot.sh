#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-phy-coldboot.sh  —  IP101A cold-boot PHY hardware reset recovery
#
# Problem: on the first cold power-on, IP101A PHY may not link because both
# the SA-02m and the link partner (switch) start simultaneously.  The kernel
# driver probe happens at ~12 s; the switch may not be ready yet.
# ip link set down/up does NOT re-trigger the GPIO reset — only driver
# unbind/rebind calls phy_probe() → phy_device_reset() → GPIO assert/deassert.
#
# This service runs 15 s after boot and retries PHY hard-reset every 30 s for
# up to PHY_COLDBOOT_MAX_TRIES attempts (default 12 ≈ 6 min) to cover
# switches that need longer to come up.
# ═══════════════════════════════════════════════════════════════════════════

IFACE=end1
PHY_DEV=stmmac-1:00
PHY_COLDBOOT_MAX_TRIES=${PHY_COLDBOOT_MAX_TRIES:-12}   # max attempts (12 × 30s = 6 min)
WAIT_INITIAL=${WAIT_INITIAL:-15}                        # seconds before first attempt
WAIT_BETWEEN=${WAIT_BETWEEN:-30}                        # seconds between attempts

LOG_TAG="sa02m-phy-coldboot"

log() { echo "[$(date '+%H:%M:%S')] ${LOG_TAG}: $*"; }

# Source overrides if present
[ -f /etc/sa02m_network.conf ] && . /etc/sa02m_network.conf

# ── Initial delay: give kernel time to finish GMAC/PHY probe ──────────────
sleep "$WAIT_INITIAL"

# ── Warm-boot check ────────────────────────────────────────────────────────
carrier=$(cat "/sys/class/net/${IFACE}/carrier" 2>/dev/null || echo 0)
if [ "$carrier" = "1" ]; then
    log "${IFACE}: carrier already up, skipping cold-boot reset"
    exit 0
fi

# ── Locate PHY driver sysfs path ──────────────────────────────────────────
PHY_DRV_PATH=$(readlink -f "/sys/bus/mdio_bus/devices/${PHY_DEV}/driver" 2>/dev/null) || true
if [ -z "$PHY_DRV_PATH" ] || [ ! -w "${PHY_DRV_PATH}/unbind" ]; then
    log "PHY driver for ${PHY_DEV} not found or not writable — fallback to ethtool -r"
    ip link set "$IFACE" up 2>/dev/null || true
    sleep 2
    ethtool -r "$IFACE" 2>/dev/null || mii-tool -r "$IFACE" 2>/dev/null || true
    exit 0
fi

log "${IFACE}: starting cold-boot PHY hard-reset loop (${PHY_COLDBOOT_MAX_TRIES} attempts, PHY=${PHY_DEV})"

for i in $(seq 1 "$PHY_COLDBOOT_MAX_TRIES"); do
    carrier=$(cat "/sys/class/net/${IFACE}/carrier" 2>/dev/null || echo 0)
    if [ "$carrier" = "1" ]; then
        log "${IFACE}: carrier UP after attempt ${i} — cold-boot recovery succeeded"
        exit 0
    fi

    log "${IFACE}: attempt ${i}/${PHY_COLDBOOT_MAX_TRIES} — PHY GPIO hard reset (unbind/rebind ${PHY_DEV})"
    ip link set "$IFACE" down 2>/dev/null || true
    sleep 1

    # Unbind triggers phy_detach(); bind triggers phy_probe() → phy_device_reset()
    printf '%s' "$PHY_DEV" > "${PHY_DRV_PATH}/unbind" 2>/dev/null || true
    sleep 0.5
    printf '%s' "$PHY_DEV" > "${PHY_DRV_PATH}/bind"   2>/dev/null || true
    sleep 1  # wait for reset-deassert-us (500 ms) + PHY internal init

    ip link set "$IFACE" up 2>/dev/null || true

    # Wait for autoneg (IP101A typically 2-3 s; add margin for slow switches)
    sleep "$WAIT_BETWEEN"
done

log "${IFACE}: ${PHY_COLDBOOT_MAX_TRIES} attempts exhausted — handing off to net-watchdog"
exit 0
