#!/bin/sh
# sa02m-eth-led-lib.sh — carrier → LED mapping by hardware variant.
# sa02m-1eth: eth0_link ← end0
# sa02m-2eth: eth0_link ← end1, eth1_link ← end0
#
# Netdev trigger on platform gpio-led is unavailable; brightness is set manually.

sa02m_hw_variant() {
    case "${SA02M_HW_VARIANT:-}" in
        sa02m-1eth|sa02m-2eth) printf '%s\n' "${SA02M_HW_VARIANT}"; return 0 ;;
    esac
    if [ -f /etc/sa02m_hw_variant.conf ]; then
        v=$(awk -F= '/^SA02M_HW_VARIANT=/{gsub(/^[ \t"]+|[ \t"]+$/,"",$2);print $2;exit}' \
            /etc/sa02m_hw_variant.conf 2>/dev/null)
        case "$v" in
            sa02m-1eth|sa02m-2eth) printf '%s\n' "$v"; return 0 ;;
        esac
    fi
    printf '%s\n' sa02m-1eth
}

sa02m_led_off() {
    led=$1
    [ -d "$led" ] || return 0
    echo none > "$led/trigger" 2>/dev/null || true
    echo 0 > "$led/brightness" 2>/dev/null || true
}

sa02m_led_sync_pair() {
    led=$1
    iface=$2
    [ -d "$led" ] || return 0
    if [ -z "$iface" ] || [ ! -d "/sys/class/net/${iface}" ]; then
        sa02m_led_off "$led"
        return 0
    fi
    echo none > "$led/trigger" 2>/dev/null || true
    carrier=$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo 0)
    if [ "$carrier" = "1" ]; then
        echo 1 > "$led/brightness" 2>/dev/null || true
    else
        echo 0 > "$led/brightness" 2>/dev/null || true
    fi
}

sa02m_led_sync_all() {
    variant=$(sa02m_hw_variant)
    case "$variant" in
        sa02m-2eth)
            sa02m_led_sync_pair /sys/class/leds/eth0_link end1
            sa02m_led_sync_pair /sys/class/leds/eth1_link end0
            ;;
        *)
            sa02m_led_sync_pair /sys/class/leds/eth0_link end0
            sa02m_led_off /sys/class/leds/eth1_link
            ;;
    esac
}
