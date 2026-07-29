#!/bin/bash
# sa02m-conf-rm.sh — the ONLY rm capability granted to www-data via sudoers.
#
# Deletes exactly one LAN conf from /etc/network/interfaces.d, backing it up
# first. Exists because the panel's apply.cgi must be able to remove a legacy
# or disabled conf, and granting raw rm to www-data is an unbounded surface:
# this helper pins the whole capability to a four-name allow-list.
# Contract: docs/contracts/ethernet-iface-naming.md §5.4.
#
# Exit: 0 deleted or already absent (idempotent); 2 validation refused.
set -o pipefail

LOG="/var/log/sa02m_install.log"
conf="${1:-}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-conf-rm: $*" >> "$LOG" 2>&1 || true
}

# Allow-list: the four LAN conf names, absolute, nothing else. case (not regex)
# so no interpreter surprises; the path is compared as one literal word.
case "$conf" in
    /etc/network/interfaces.d/eth0.conf|\
    /etc/network/interfaces.d/eth1.conf|\
    /etc/network/interfaces.d/end0.conf|\
    /etc/network/interfaces.d/end1.conf)
        ;;
    *)
        log "REFUSED (not allow-listed): $conf"
        exit 2
        ;;
esac

# A symlink under a root-written path is an escalation vector — refuse.
if [ -L "$conf" ]; then
    log "REFUSED (symlink): $conf"
    exit 2
fi

if [ ! -f "$conf" ]; then
    log "already absent: $conf"
    exit 0
fi

# One-generation backup, same convention as apply.cgi's .sa02m-bak.
cp -f "$conf" "${conf}.sa02m-bak" 2>/dev/null || true
chmod 644 "${conf}.sa02m-bak" 2>/dev/null || true

rm -f "$conf"
log "deleted: $conf (backup: ${conf}.sa02m-bak)"
exit 0
