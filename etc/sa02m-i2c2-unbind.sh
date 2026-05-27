#!/bin/sh
# sa02m-i2c2-unbind.sh — called by udev when i2c-2 is added.
# Unbinds the platform i2c controller to stop bus-recovery IRQ storms
# caused by the expansion board holding SDA low.
# Skipped if sa02m-pre-start.sh already did recovery (flag in /run).

# Flag set by sa02m-pre-start.sh BEFORE rebind — i2c-2 is intentionally bound.
[ -f /run/sa02m-i2c2-recovered ] && exit 0

# Use $DEVPATH from udev environment (always set, reliable at add time):
# DEVPATH=/devices/platform/soc/1c2b800.i2c/i2c-2 → extract 1c2b800.i2c
PDEV=$(printf '%s' "${DEVPATH:-}" | sed 's|.*/soc/\([^/]*\)/.*|\1|')

# Fallback: readlink (slower but works outside udev context)
if [ -z "$PDEV" ] || [ "$PDEV" = "${DEVPATH:-}" ]; then
    PDEV=$(readlink /sys/class/i2c-adapter/i2c-2 2>/dev/null \
           | sed 's|.*soc/\([^/]*\)/.*|\1|')
fi

if [ -z "$PDEV" ]; then
    logger -t sa02m-i2c2 "unbind: could not resolve i2c-2 platform device (DEVPATH=${DEVPATH:-unset})"
    exit 1
fi

for drv in /sys/bus/platform/drivers/mv64xxx_i2c \
           /sys/bus/platform/drivers/i2c-sunxi; do
    [ -f "${drv}/unbind" ] || continue
    printf '%s' "$PDEV" > "${drv}/unbind" 2>/dev/null || continue
    logger -t sa02m-i2c2 "i2c-2 ($PDEV) unbound via udev — IRQ storm prevented"
    exit 0
done

logger -t sa02m-i2c2 "i2c-2 ($PDEV) unbind: driver not found in platform drivers"
