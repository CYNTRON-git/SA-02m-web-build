#!/bin/bash
# Reset SA-02m Alice controller enrollment to factory (unlinked, no bindings),
# and the HomeKit bridge identity (pairing store, bridge off) when installed.
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
        # The stand-down marker is IDENTITY, not configuration (contract §2,
        # mirroring the cloud half's §6): left in place, a clone taken from a
        # donor that was ever unlinked boots with its card reading «отвязано в
        # облаке» instead of «нет сертификата». Dropped BEFORE the
        # client_enabled guard below, so a conf carrying only the marker is
        # still cleaned.
        sed -i '/^[[:space:]]*unlinked_at[[:space:]]*=/d;/^[[:space:]]*unlinked_reason[[:space:]]*=/d;/^[[:space:]]*unlinked_reason_text[[:space:]]*=/d' "$f"
        grep -q 'client_enabled' "$f" 2>/dev/null || continue
        sed -i 's/^[[:space:]]*client_enabled[[:space:]]*=.*/client_enabled = false/' "$f"
    done
}

wipe_homekit_identity() {
    # HomeKit bridge identity (docs/contracts/image-identity-reset.md §7): the
    # pairing store holds the accessory's long-term key and the paired iPhones,
    # so a board carrying it IS the donor's accessory to the donor's family.
    # Stop the daemon FIRST — it holds the keys in memory and persists them
    # again. The unit, the dirs (tmpfiles.d owns them), interface/port and the
    # software stay: a clone boots with the bridge off, as on a first install.
    timeout 10 systemctl stop sa02m-homekit.service 2>/dev/null || true
    timeout 10 systemctl disable sa02m-homekit.service 2>/dev/null || true
    # Contents, never the dir. `.hk-*` is the atomic-write sidecar shape
    # (fsutil.atomic_write) — a torn write of the pairing store is the same key
    # under another name, and `*` does not expand to dot-files. /run holds the
    # live setup code (setup.json) and the status.
    # A symlink AT the store is dropped, never descended (the glob would empty
    # whatever it points at); tmpfiles.d re-creates the real dir at boot.
    if [ -L /var/lib/sa02m-homekit ]; then
        rm -f /var/lib/sa02m-homekit
    else
        rm -f /var/lib/sa02m-homekit/* \
              /var/lib/sa02m-homekit/.hk-*
    fi
    rm -f /run/sa02m-homekit/*
    # /etc/sa02m-homekit is www-data-writable: a symlink at the conf is never
    # the installer's, and sed -i would read through it as root — drop it
    # instead (an absent conf reads as disabled). Absent file: nothing to do.
    if [ -L /etc/sa02m-homekit/sa02m-homekit.conf ]; then
        rm -f /etc/sa02m-homekit/sa02m-homekit.conf
    elif [ -f /etc/sa02m-homekit/sa02m-homekit.conf ]; then
        # configparser reads the key case-insensitively and accepts `:` too.
        sed -i 's/^[[:space:]]*[Ee][Nn][Aa][Bb][Ll][Ee][Dd][[:space:]]*[=:].*/enabled = false/' \
            /etc/sa02m-homekit/sa02m-homekit.conf
    fi
}

wipe_homeconnect_identity() {
    # Home Connect sign-in (docs/contracts/image-identity-reset.md §8): the
    # state dir holds the OAuth refresh token of the owner's BSH account, so a
    # board carrying it reads the donor household's appliances. Order: the conf
    # to `enabled = false` FIRST — the daemon, stopping with its conf disabled
    # (or seeing it within 2 s), removes its retained appliance topics instead
    # of leaving them on the broker — then stop + disable (the daemon holds the
    # token in memory and a refresh would write it back), then the contents.
    # The unit, the dirs (tmpfiles.d owns them), the Client ID and the software
    # stay: a clone boots with the client off, as on a first install.
    # /etc/sa02m-homeconnect is www-data-writable: a symlink at the conf is
    # never the installer's, and sed -i would read through it as root — drop it
    # instead (an absent conf reads as disabled). Absent file: nothing to do.
    if [ -L /etc/sa02m-homeconnect/sa02m-homeconnect.conf ]; then
        rm -f /etc/sa02m-homeconnect/sa02m-homeconnect.conf
    elif [ -f /etc/sa02m-homeconnect/sa02m-homeconnect.conf ]; then
        # configparser reads the key case-insensitively and accepts `:` too.
        sed -i 's/^[[:space:]]*[Ee][Nn][Aa][Bb][Ll][Ee][Dd][[:space:]]*[=:].*/enabled = false/' \
            /etc/sa02m-homeconnect/sa02m-homeconnect.conf
    fi
    timeout 10 systemctl stop sa02m-homeconnect.service 2>/dev/null || true
    timeout 10 systemctl disable sa02m-homeconnect.service 2>/dev/null || true
    # Contents, never the dir. `.hc-*` is the atomic-write sidecar shape
    # (fsutil.atomic_write) — a torn write of the token file is the same secret
    # under another name, and `*` does not expand to dot-files. /run holds the
    # live sign-in code (link.json), the status and the appliance inventory.
    # A symlink AT the state dir is dropped, never descended (the glob would
    # empty whatever it points at); tmpfiles.d re-creates the real dir at boot.
    if [ -L /var/lib/sa02m-homeconnect ]; then
        rm -f /var/lib/sa02m-homeconnect
    else
        rm -f /var/lib/sa02m-homeconnect/* \
              /var/lib/sa02m-homeconnect/.hc-*
    fi
    rm -f /run/sa02m-homeconnect/*
}

wipe_alice_enrollment
wipe_homekit_identity
wipe_homeconnect_identity

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
if [ -d /var/lib/sa02m-homekit ]; then
    if [ -z "$(ls -A /var/lib/sa02m-homekit 2>/dev/null)" ]; then
        echo "homekit state=EMPTY_OK"
    else
        echo "homekit state=STILL_PRESENT_FAIL"
    fi
    grep -E '^[[:space:]]*enabled' /etc/sa02m-homekit/sa02m-homekit.conf 2>/dev/null || true
    timeout 10 systemctl is-enabled sa02m-homekit 2>&1 || true
fi
if [ -d /var/lib/sa02m-homeconnect ]; then
    if [ -z "$(ls -A /var/lib/sa02m-homeconnect 2>/dev/null)" ]; then
        echo "homeconnect state=EMPTY_OK"
    else
        echo "homeconnect state=STILL_PRESENT_FAIL"
    fi
    grep -E '^[[:space:]]*enabled' /etc/sa02m-homeconnect/sa02m-homeconnect.conf 2>/dev/null || true
    timeout 10 systemctl is-enabled sa02m-homeconnect 2>&1 || true
fi
