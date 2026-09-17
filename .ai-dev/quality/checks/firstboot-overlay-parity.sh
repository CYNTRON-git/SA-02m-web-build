#!/usr/bin/env bash
# firstboot-overlay-parity — every file under tools/imaging/firstboot-overlay/
# is BYTE-IDENTICAL to its repo home. tools/imaging/autorun.sh (and
# autorun-fel.sh) `cp -f` the overlay over a freshly written rootfs, so a clone
# flashed that way boots the overlay's scripts, not the installer's: audit
# 2026-09-16 (M1) found sa02m-eth-coldboot.sh 76 lines behind its home (no
# resolver belt, no OTA bootstrap shim) and fix-eth.sh without dns_ensure —
# every such clone silently lost docs/contracts/boot-network-dns.md. Nothing
# pinned the pairs except the expand script (firstboot-sb-csum layer 1 — kept
# there because that harness's contract includes it; this row is the set-wide
# home).
#
# WHAT IT PINS: the PAIRS table below (overlay path | repo home), `cmp` each.
# FAIL on any byte difference (the pair + the first diff lines), on a missing
# home, on a missing overlay file. OPEN-WORLD: every regular file under the
# overlay must be in PAIRS («overlay file without a declared home» FAILS) —
# a new overlay file is declared, not discovered. NON-VACUOUS: fewer than
# PAIR_FLOOR pairs, or an empty overlay sweep, FAILS.
#
# Known gap, recorded not fixed (backlog, 1.0.6.49): the overlay carries the
# belt CALLERS (dns_ensure in both scripts) but not usr/local/sbin/
# sa02m-dns-ensure.sh; dns_ensure guards on -x and degrades to the pre-belt
# behaviour when the helper is absent, so a clone from a pre-1.0.6.6 image gets
# the guard, not the belt.
#
# PROVEN RED (2026-09-17, 1.0.6.49): on the pre-sync tree exactly the two
# stale pairs FAIL (sa02m-eth-coldboot.sh, fix-eth.sh); GREEN after the cp.
# The comment-mutation case (a `#` on the overlay fix-eth.sh `dns_ensure`
# line changes the bytes) is registered in comment-mutation-proof.
#
# Run: bash .ai-dev/quality/checks/firstboot-overlay-parity.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

OVL=tools/imaging/firstboot-overlay
PAIR_FLOOR=15
fails=0
ok()  { printf 'firstboot-overlay-parity: ok    %s\n' "$*"; }
bad() { printf 'firstboot-overlay-parity: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

# overlay-relative path | repo home
PAIRS='
usr/local/sbin/sa02m-eth-coldboot.sh|usr/local/sbin/sa02m-eth-coldboot.sh
usr/local/bin/fix-eth.sh|etc/fix-eth.sh
usr/local/sbin/sa02m-rootfs-expand.sh|etc/sa02m-rootfs-expand.sh
usr/local/sbin/sa02m-failure-monitor|etc/sa02m-failure-monitor.sh
usr/local/sbin/sa02m-userspace-watchdog|etc/sa02m-userspace-watchdog.sh
usr/local/bin/net-watchdog.sh|etc/net-watchdog.sh
etc/sa02m_failure_monitor.conf|etc/sa02m_failure_monitor.conf
etc/sa02m_userspace_watchdog.conf|etc/sa02m_userspace_watchdog.conf
etc/systemd/system.conf.d/sa02m-watchdog.conf|etc/systemd/sa02m-watchdog.conf
etc/systemd/system/fix-eth@.service|etc/fix-eth@.service
etc/systemd/system/net-watchdog.service|etc/net-watchdog.service
etc/systemd/system/sa02m-eth-coldboot.service|etc/systemd/sa02m-eth-coldboot.service
etc/systemd/system/sa02m-failure-monitor.service|etc/sa02m-failure-monitor.service
etc/systemd/system/sa02m-rootfs-expand.service|etc/systemd/sa02m-rootfs-expand.service
etc/systemd/system/sa02m-userspace-watchdog.service|etc/systemd/sa02m-userspace-watchdog.service
'

[ -d "$OVL" ] || { echo "firstboot-overlay-parity: FAIL — $OVL absent"; exit 1; }
rows=$(printf '%s\n' "$PAIRS" | awk -F'|' 'NF == 2 && $1 != "" && $2 != "" { print }')
n_pairs=$(printf '%s\n' "$rows" | grep -c .)
if [ "$n_pairs" -ge "$PAIR_FLOOR" ]; then ok "$n_pairs declared pairs (floor $PAIR_FLOOR)"
else bad "$n_pairs declared pairs < floor $PAIR_FLOOR — a pair was dropped"; fi

declared=""
while IFS='|' read -r rel home; do
    [ -n "$rel" ] || continue
    declared="$declared"$'\n'"$rel"
    o="$OVL/$rel"
    if [ ! -f "$o" ]; then bad "overlay file missing: $o (home $home)"; continue; fi
    if [ ! -f "$home" ]; then bad "repo home missing for $rel: $home"; continue; fi
    if cmp -s "$o" "$home"; then
        ok "$rel == $home"
    else
        n=$(diff "$o" "$home" | grep -c '^[<>]')
        bad "$rel differs from its home $home ($n changed lines) — cp the home over the overlay copy: $(diff "$o" "$home" | head -3 | tr '\n' ' ')"
    fi
done <<<"$rows"

# Open-world: every regular file in the overlay is declared.
sweep=$(find "$OVL" -type f | sed "s#^$OVL/##" | sort)
n_sweep=$(printf '%s\n' "$sweep" | grep -c .)
[ "$n_sweep" -gt 0 ] || bad "empty overlay sweep — find saw no files under $OVL"
while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    case "$declared"$'\n' in *$'\n'"$rel"$'\n'*) ;; *) bad "overlay file without a declared home: $rel — add it to PAIRS with its repo home" ;; esac
done <<<"$sweep"

echo
if [ "$fails" -eq 0 ]; then
    echo "firstboot-overlay-parity: ALL OK — $n_pairs overlay files byte-identical to their homes, $n_sweep files swept"
    exit 0
fi
echo "firstboot-overlay-parity: $fails FAILURE(S)"
exit 1
