#!/bin/bash
# Install after the matching web root; no service other than this collector is touched.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib.sh"
check_root
sa02m_svc_capture sa02m-status-cache.service
source_root="$SCRIPT_DIR/.."
for source in etc/sa02m-status-part.sh etc/sa02m-status-cache.py etc/systemd/system/sa02m-status-cache.service; do
    [ -s "$source_root/$source" ] || { log ERR "Dashboard cache payload is incomplete"; exit 1; }
done
install -d -m 0755 /usr/local/libexec /etc/systemd/system
sa02m_atomic_install -m 0755 -o root -g root "$source_root/etc/sa02m-status-part.sh" /usr/local/libexec/sa02m-status-part
sa02m_atomic_install -m 0644 -o root -g root "$source_root/etc/sa02m-status-cache.py" /usr/local/libexec/sa02m-status-cache.py
unit=/etc/systemd/system/sa02m-status-cache.service
if [[ -L "$unit" && "$(readlink "$unit")" == /dev/null ]]; then
    log INFO "Dashboard cache mask preserved; after unmask, run this installer again to install the unit"
else
    sa02m_atomic_install -m 0644 -o root -g root "$source_root/etc/systemd/system/sa02m-status-cache.service" "$unit"
fi
sa02m_systemctl daemon-reload
# Preserve an operator's disabled/masked policy using the shared installer policy.
sa02m_svc_apply sa02m-status-cache.service app on
