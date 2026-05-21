#!/bin/sh
# WSL2 mirrored: поднять NIC с LAN-адресами и задать src для 192.168.1.0/24.

apply_routes() {
    ip -4 -o addr show scope global 2>/dev/null | while read -r _ dev _ addr _; do
        dev="${dev%%@*}"
        ip link set "$dev" up 2>/dev/null || true
    done

    iface=""
    ip -4 -o addr show 2>/dev/null | while read -r _ dev _ addr _; do
        case "$addr" in
            192.168.1.5/*)
                dev="${dev%%@*}"
                ip link set "$dev" up 2>/dev/null || true
                ip route replace 192.168.1.0/24 dev "$dev" src 192.168.1.5 metric 10 2>/dev/null || true
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
