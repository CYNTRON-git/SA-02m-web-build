#!/bin/bash
# sa02m-ensure-eth1-dhcp-hook.sh — install the eth1/end1 DHCP default-route
# dhclient exit-hook. Called from apply.cgi when the panel enables DHCP on
# Ethernet № 2 (OTA boards may never have run scripts/02-network.sh).
#
# No arguments, no stdin. Fixed content only — www-data cannot choose the
# path or the script body (audit B1). Idempotent: rewrite only when missing
# or content differs.
#
# Contract: docs/contracts/ethernet-iface-naming.md (eth1 DHCP + metric 100).
set -euo pipefail

LOG=/var/log/sa02m_install.log
DST=/etc/dhcp/dhclient-exit-hooks.d/eth1-default-route

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-ensure-eth1-dhcp-hook: $*" >>"$LOG" 2>&1 || true
}

mkdir -p /etc/dhcp/dhclient-exit-hooks.d

tmp=$(mktemp) || { log "REFUSED (mktemp failed)"; exit 2; }
chmod 600 "$tmp" 2>/dev/null || true
trap 'rm -f "$tmp"' EXIT

cat >"$tmp" <<'HOOK'
#!/bin/sh
# Default route for LAN2 DHCP (eth1/end1). Metric 100 keeps eth0 preferred.
# Some DHCP servers send RFC3442 option 121, which makes dhclient-script ignore
# option 3 (routers); this hook forces the default via the DHCP gateway.
case "$interface" in
    eth1|end1)
        if [ -n "$new_routers" ]; then
            case "$reason" in
                BOUND|RENEW|REBIND|REBOOT)
                    ip route replace default via $new_routers dev "$interface" metric 100 2>/dev/null || true
                    ;;
            esac
        fi
        ;;
esac
HOOK

if [ -f "$DST" ] && cmp -s "$tmp" "$DST"; then
    chmod 755 "$DST" 2>/dev/null || true
    exit 0
fi

if cat "$tmp" >"$DST"; then
    chmod 755 "$DST"
    log "installed $DST"
    exit 0
fi
log "REFUSED (write failed): $DST"
exit 2
