# Shared LAN iface name resolution: eth0/eth1 (predictable) vs end0/end1 (legacy).
# JSON / form field names stay eth0_*/eth1_*; on-disk conf + ifupdown use the
# name that actually exists on the board.
# shellcheck shell=bash

# Prefer a live /sys/class/net name, else a present interfaces.d conf, else $1.
resolve_lan_iface() {
    local c
    for c in "$@"; do
        [ -n "$c" ] || continue
        [ -d "/sys/class/net/$c" ] && { printf '%s' "$c"; return 0; }
    done
    for c in "$@"; do
        [ -n "$c" ] || continue
        [ -f "/etc/network/interfaces.d/${c}.conf" ] && { printf '%s' "$c"; return 0; }
    done
    printf '%s' "${1:-}"
}

# Conf path for a resolved iface name (may not exist yet).
lan_iface_conf() {
    printf '/etc/network/interfaces.d/%s.conf' "$1"
}

# Sibling name of the ethN/endN pair (for stale-conf cleanup after write).
lan_iface_sibling() {
    case "$1" in
        eth0) printf 'end0' ;;
        end0) printf 'eth0' ;;
        eth1) printf 'end1' ;;
        end1) printf 'eth1' ;;
        *)    printf '' ;;
    esac
}
