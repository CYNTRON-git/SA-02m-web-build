#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped code in a sandbox (files written, shim invocations, exit codes), so a commented-out line changes the measured behaviour instead of hiding behind a needle grep; its source-text greps are extraction/retarget sanity guards on its own scratch copy, which abort the run when the shipped block moves.
# test-iface-migration.sh — regression test for the installer's on-disk
# migration to canonical interface names
# (scripts/02-network.sh migrate_iface_conf; contract §3).
#
# Why this exists: the failure mode of this code is "the board comes up with no
# address" — the 1.0.3.38 `Cannot find device` class — and it only runs on a
# device, as root, during an install. This test exercises the §3 gate branches
# against a sandboxed interfaces.d so the branch that ships is the branch that
# was checked. It already caught one real defect: on a second installer run the
# DUAL-CONF path copied the legacy file's (by then downgraded) `allow-hotplug`
# header onto the CANONICAL conf, which `ifup -a` does not bring up.
#
# Method: extract the SHIPPED functions and retarget only the two absolute roots
# (/etc/network/interfaces.d, /sys/class/net) into a scratch tree.
#
# Run: bash scripts/dev/test-iface-migration.sh   (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=scripts/02-network.sh
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
ND="$T/net"; CD="$T/ifd"; mkdir -p "$ND" "$CD"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

sed -n '/^canonical_conf_from_legacy() {/,/^migrate_iface_conf end1 eth1 allow-hotplug$/p' "$SRC" \
  | sed "s#/etc/network/interfaces.d#$CD#g; s#/sys/class/net/#$ND/#g" > "$T/fn.sh"
if ! grep -q '^migrate_iface_conf() {' "$T/fn.sh"; then
    echo "FAIL  could not extract migrate_iface_conf from $SRC (did the section markers move?)"
    exit 1
fi

log() { :; }
LAN0_CONF_WRITTEN=0; IP_EXPLICIT=0; CANONICAL_RENAME_OK=0
# shellcheck disable=SC1090
. "$T/fn.sh"   # defines the functions; the trailing calls run against an empty dir (no-op)

legacy_conf() {
    printf 'auto end0\niface end0 inet static\n    address 192.168.1.77\n    netmask 255.255.255.0\n    post-up /home/klogic/adjust-eth0 || /bin/true\n' > "$CD/end0.conf"
}

echo "── gate: canonical device already live -> migrate and drop the legacy conf ──"
legacy_conf; mkdir -p "$ND/eth0"
migrate_iface_conf end0 eth0 auto 0
[ ! -f "$CD/end0.conf" ] && ok "legacy conf removed" || bad "legacy conf survived"
grep -q '^auto eth0$' "$CD/eth0.conf" && ok "canonical conf carries the renamed stanza" || bad "canonical stanza wrong"
grep -q 'address 192.168.1.77' "$CD/eth0.conf" && ok "operator address carried over" || bad "operator address lost"
grep -q 'post-up /home/klogic/adjust-eth0' "$CD/eth0.conf" && ok "vendor post-up carried over" || bad "vendor post-up lost"
migrate_iface_conf end0 eth0 auto 0 && ok "re-run is a no-op" || bad "re-run errored"
rmdir "$ND/eth0"; rm -f "$CD"/*.conf

echo "── gate: explicit --ip -> the address the operator asked for wins ──"
legacy_conf; mkdir -p "$ND/eth0"
printf 'auto eth0\niface eth0 inet static\n    address 10.9.9.9\n    netmask 255.255.255.0\n' > "$CD/eth0.conf"
migrate_iface_conf end0 eth0 auto 1
grep -q 'address 10.9.9.9' "$CD/eth0.conf" && ok "explicit --ip not overwritten by the legacy address" || bad "explicit --ip clobbered by the migration"
[ ! -f "$CD/end0.conf" ] && ok "legacy conf still dropped" || bad "legacy conf survived"
rmdir "$ND/eth0"; rm -f "$CD"/*.conf

echo "── gate: rename deferred to the next boot -> DUAL-CONF ──"
legacy_conf; CANONICAL_RENAME_OK=0          # no canonical device, not a rootfs build
migrate_iface_conf end0 eth0 auto 0
grep -q '^auto eth0$' "$CD/eth0.conf" && ok "canonical conf written ahead of the reboot" || bad "canonical conf missing"
grep -q '^allow-hotplug end0$' "$CD/end0.conf" && ok "legacy conf downgraded to allow-hotplug" || bad "legacy conf not downgraded"
if grep -q '^auto end0$' "$CD/end0.conf"; then bad "legacy conf kept auto (ifup -a would fight the rename)"; else ok "legacy auto replaced"; fi
grep -q 'address 192.168.1.77' "$CD/end0.conf" && ok "legacy conf keeps the same address (board stays reachable if the rename fails)" || bad "legacy address changed"
cp "$CD/end0.conf" "$T/d1"; cp "$CD/eth0.conf" "$T/c1"
migrate_iface_conf end0 eth0 auto 0
if diff -q "$T/d1" "$CD/end0.conf" >/dev/null && diff -q "$T/c1" "$CD/eth0.conf" >/dev/null; then
    ok "DUAL-CONF re-run is byte-identical (the canonical header is NOT demoted)"
else
    bad "DUAL-CONF re-run churned the confs"
    diff "$T/c1" "$CD/eth0.conf"; diff "$T/d1" "$CD/end0.conf"
fi
mkdir -p "$ND/eth0"
migrate_iface_conf end0 eth0 auto 0
[ ! -f "$CD/end0.conf" ] && ok "duplication window closes on the next installer run" || bad "legacy conf lingered past the rename"
rmdir "$ND/eth0"; rm -f "$CD"/*.conf

echo "── a stanza-free legacy conf is dropped, never copied over the real one ──"
# The web panel cannot delete a conf (no `rm` in the pinned sudoers), so it
# retires one to comments. The installer must not then copy those comments onto
# the canonical conf and wipe the live configuration.
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.5\n    netmask 255.255.255.0\n' > "$CD/eth0.conf"
printf '# Retired by the SA-02m web panel: this interface is now eth0.\n# Previous content: end0.conf.sa02m-bak\n' > "$CD/end0.conf"
mkdir -p "$ND/eth0"
migrate_iface_conf end0 eth0 auto 0
grep -q 'address 192.168.1.5' "$CD/eth0.conf" && ok "live canonical conf untouched by a comment-only legacy file" || bad "canonical conf wiped by a retired legacy conf"
[ ! -f "$CD/end0.conf" ] && ok "retired legacy conf removed" || bad "retired legacy conf survived"
rmdir "$ND/eth0"; rm -f "$CD"/*.conf

echo "── eth1 keeps its allow-hotplug header convention ──"
printf 'allow-hotplug end1\niface end1 inet dhcp\n    metric 100\n' > "$CD/end1.conf"
mkdir -p "$ND/eth1"
migrate_iface_conf end1 eth1 allow-hotplug 0
grep -q '^allow-hotplug eth1$' "$CD/eth1.conf" && ok "eth1 migrates as allow-hotplug, not auto" || bad "eth1 header wrong"
grep -q 'metric 100' "$CD/eth1.conf" && ok "eth1 options carried over" || bad "eth1 options lost"

echo "---"
if [ "$fails" = 0 ]; then echo "iface-migration: all checks passed"; else echo "iface-migration: $fails check(s) FAILED"; fi
exit "$fails"
