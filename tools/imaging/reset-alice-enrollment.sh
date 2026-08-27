#!/bin/bash
# Reset SA-02m Alice controller enrollment to factory (unlinked, no bindings).
# Twin of reset-cloud-enrollment.sh — same shape, same seam, read them together.
#
# Use it to un-link a donor by hand (docs/deployment.md §2), or to un-clone a
# board already flashed from a pre-fix image that carries a donor's identity.
#
#   sudo bash tools/imaging/reset-alice-enrollment.sh
#
# Idempotent. Clear-list home: docs/contracts/image-identity-reset.md.
# NOT the factory-reset path: factory reset deliberately PRESERVES a board's
# own certs (etc/sa02m-factory-defaults/lists/preserve.list). Opposite policy.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: reset-alice-enrollment.sh must run as root" >&2
    exit 1
fi

wipe_alice_enrollment() {
    # ca.crt.pem is the SHARED gateway CA, not board identity — it stays, or
    # every clone's client loses its trust anchor. server.conf (gateway URLs)
    # and the rest of client.conf (mqtt_host/port, log_level) are configuration
    # and stay too. Only identity and bindings go.
    timeout 10 systemctl stop sa02m-alice-client.service 2>/dev/null || true
    timeout 10 systemctl disable sa02m-alice-client.service 2>/dev/null || true
    # The globs cover the atomic-write sidecars by shape: a crash mid-link
    # strands device.key.pem.tmp (api.py writes <path>.tmp then os.replace) or
    # a mkstemp .alice-XXXXXX holding the donor's bindings. `*.tmp` cannot
    # match ca.crt.pem, which must survive.
    rm -f /var/lib/sa02m-alice/device.crt.pem \
          /var/lib/sa02m-alice/device.key.pem \
          /var/lib/sa02m-alice/pending_claim.json \
          /var/lib/sa02m-alice/*.tmp \
          /etc/sa02m-alice/.alice-* \
          /run/sa02m-alice/status.json \
          /run/sa02m-alice/*.tmp
    # Each file guarded — an absent one is a no-op, never an abort under
    # `set -euo pipefail` (a board that never linked has no client.conf, and
    # the legacy flat layout is absent on any modern board).
    local f
    for f in /etc/sa02m-alice/sa02m-alice-devices.conf \
             /etc/sa02m-alice-devices.conf; do
        [ -f "$f" ] || continue
        printf '%s\n' '{' '  "rooms": [],' '  "devices": []' '}' > "$f"
    done
    for f in /etc/sa02m-alice/sa02m-alice-client.conf \
             /etc/sa02m-alice-client.conf; do
        [ -f "$f" ] || continue
        grep -q 'client_enabled' "$f" 2>/dev/null || continue
        sed -i 's/^[[:space:]]*client_enabled[[:space:]]*=.*/client_enabled = false/' "$f"
    done
}

wipe_alice_enrollment

echo "=== VERIFY ==="
ls -la /var/lib/sa02m-alice 2>/dev/null || echo "var dir absent (never linked)"
for f in device.crt.pem device.key.pem pending_claim.json; do
    if [ -e "/var/lib/sa02m-alice/$f" ]; then
        echo "$f=STILL_PRESENT_FAIL"
    else
        echo "$f=ABSENT_OK"
    fi
done
if [ -f /var/lib/sa02m-alice/ca.crt.pem ]; then
    echo "ca.crt.pem=PRESENT_OK (shared gateway CA — must stay)"
else
    echo "ca.crt.pem=absent (board never linked, or CA over-wiped)"
fi
for f in /etc/sa02m-alice/sa02m-alice-devices.conf /etc/sa02m-alice-devices.conf; do
    if [ -f "$f" ]; then
        echo "--- $f"
        cat "$f"
    fi
done
for f in /etc/sa02m-alice/sa02m-alice-client.conf /etc/sa02m-alice-client.conf; do
    if [ -f "$f" ]; then
        printf '%s: ' "$f"
        grep -E '^[[:space:]]*client_enabled' "$f" || true
    fi
done
timeout 10 systemctl is-enabled sa02m-alice-client 2>&1 || true
timeout 10 systemctl is-active sa02m-alice-client 2>&1 || true
