#!/bin/sh
# WSL2 mirrored: поднять NIC с LAN-адресами и задать src для 192.168.1.0/24.

apply_routes() {
    ip -4 -o addr show scope global 2>/dev/null | while read -r _ dev _ addr _; do
        dev="${dev%%@*}"
        ip link set "$dev" up 2>/dev/null || true
    done

    # First pass: add low-priority (metric 10) route for virtual/VPN 192.168.1.5 adapter.
    # Second pass: override with higher-priority (metric 5) route for the real physical LAN NIC
    # (any 192.168.1.x/24 that is NOT .5). Metric 5 < 10 so physical NIC wins.
    ip -4 -o addr show 2>/dev/null | while read -r _ dev _ addr _; do
        case "$addr" in
            192.168.1.5/*)
                dev="${dev%%@*}"
                ip link set "$dev" up 2>/dev/null || true
                ip route replace 192.168.1.0/24 dev "$dev" src 192.168.1.5 metric 10 2>/dev/null || true
                ;;
            192.168.1.*/*)
                dev="${dev%%@*}"
                src="${addr%%/*}"
                ip link set "$dev" up 2>/dev/null || true
                ip route replace 192.168.1.0/24 dev "$dev" src "$src" metric 5 2>/dev/null || true
                ;;
            10.0.6.5/*)
                dev="${dev%%@*}"
                ip link set "$dev" up 2>/dev/null || true
                ip route replace 10.0.6.0/24 dev "$dev" src 10.0.6.5 metric 10 2>/dev/null || true
                ;;
        esac
    done
}

apply_routes
sleep 2
apply_routes
