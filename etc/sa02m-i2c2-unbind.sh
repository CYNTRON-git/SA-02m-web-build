#!/bin/sh
# sa02m-i2c2-unbind.sh  — called by udev when i2c-2 is added.
# Unbinds the platform i2c controller to stop bus-recovery IRQ storms
# caused by the expansion board holding SDA low.
# Runs before any systemd service (including sa02m-pre-start).

PDEV=$(readlink /sys/class/i2c-adapter/i2c-2 2>/dev/null \
       | sed 's|.*soc/\([^/]*\)/.*|\1|')

if [ -z "$PDEV" ] || [ "$PDEV" = "$(readlink /sys/class/i2c-adapter/i2c-2 2>/dev/null)" ]; then
    logger -t sa02m-i2c2 "unbind: could not resolve i2c-2 platform device"
    exit 0
fi

for drv in /sys/bus/platform/drivers/mv64xxx_i2c \
           /sys/bus/platform/drivers/i2c-sunxi; do
    [ -f "${drv}/unbind" ] || continue
    printf '%s' "$PDEV" > "${drv}/unbind" 2>/dev/null || continue
    logger -t sa02m-i2c2 "i2c-2 ($PDEV) unbound via udev — expansion board SDA lockup prevented"
    exit 0
done

logger -t sa02m-i2c2 "i2c-2 ($PDEV) unbind: driver not found in platform drivers"
