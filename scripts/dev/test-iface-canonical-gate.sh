#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped code in a sandbox (files written, shim invocations, exit codes), so a commented-out line changes the measured behaviour instead of hiding behind a needle grep; its source-text greps are extraction/retarget sanity guards on its own scratch copy, which abort the run when the shipped block moves.
# test-iface-canonical-gate.sh — regression test for the rename script's
# per-device udev-initialized gate
# (etc/sa02m-iface-canonical.sh canonicalize_pair; contract §1.0).
#
# Why this exists: the failure mode of this code is "the board comes up
# unreachable on an unconfigured end0" — the 8D 2026-07-29 boot-0 class — and
# the systemd/udev timing that provokes it cannot be enumerated by rebooting a
# bench board. This test drives the shipped wait/gate/fallback state machine
# against a sandboxed /sys/class/net + /run/udev/data pair, including the
# exact boot-0 sequence (kernel-named eth0 that udev later renames to end0)
# and the degraded orderings a healthy board cannot produce.
#
# Method: extract the SHIPPED functions (everything above the main marker) and
# retarget ONLY the two roots SYS_NET / UDEV_DB (the deliberate seam) into a
# scratch tree; `ip` is a PATH shim (rename = mv of the sysfs dir).
#
# Run: bash scripts/dev/test-iface-canonical-gate.sh  (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=etc/sa02m-iface-canonical.sh
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
ND="$T/net"; UD="$T/udev"; BIN="$T/bin"
mkdir -p "$ND" "$UD" "$BIN"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

sed '/^# ── main/,$d' "$SRC" > "$T/fn.sh"
if ! grep -q '^udev_initialized() {' "$T/fn.sh" \
   || ! grep -q '^canonicalize_pair() {' "$T/fn.sh"; then
    echo "FAIL  could not extract the gate functions from $SRC (did the main marker move?)"
    exit 1
fi
# shellcheck disable=SC1090
. "$T/fn.sh"

# The §5.1 seam — the ONLY retarget — plus test-local log/state homes.
SYS_NET="$ND"; UDEV_DB="$UD"
LOG_FILE="$T/gate.log"; RENAMED_STATE="$T/renamed"; LIVE_MODE=0

# `ip` PATH shim: rename moves the sysfs dir; link show reports no altnames;
# every call is journaled so ordering assertions (down before up-restore) hold.
cat > "$BIN/ip" <<SHIM
#!/bin/bash
printf '%s\n' "\$*" >> "$T/ip-calls.log"
case "\$*" in
    "-d link show"*)          exit 0 ;;
    "link property del"*)     exit 0 ;;
    "link set dev "*" down")  exit 0 ;;
    "link set dev "*" up")    exit 0 ;;
    "link set dev "*" name "*)
        if [ "\${IP_SHIM_FAIL_RENAME:-0}" = "1" ]; then
            echo "RTNETLINK answers: Device or resource busy" >&2
            exit 2
        fi
        mv "$ND/\$4" "$ND/\$6" ;;
    *) exit 0 ;;
esac
SHIM
chmod +x "$BIN/ip"
PATH="$BIN:$PATH"

mk_dev()  { mkdir -p "$ND/$1"; printf '%s\n' "$2" > "$ND/$1/ifindex"; printf '%s\n' "${3:-0x1002}" > "$ND/$1/flags"; }
mk_init() { : > "$UD/n$1"; }
reset_tree() { rm -rf "$ND" "$UD"; mkdir -p "$ND" "$UD"; rm -f "$T/ip-calls.log"; : > "$LOG_FILE"; }
run_pair() { OUT=$(canonicalize_pair "$1" "$2"); RC=$?; }

echo "── 1. canonical present + initialized -> immediate settled no-op ──"
reset_tree; mk_dev eth0 2; mk_init 2
WAIT_SECS=2; start=$SECONDS
run_pair end0 eth0
elapsed=$(( SECONDS - start ))
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
printf '%s\n' "$OUT" | grep -q "already canonical (settled)" && ok "logged settled no-op" || bad "no settled-no-op log line"
[ "$elapsed" -le 1 ] && ok "no wait budget burned (${elapsed}s)" || bad "burned ${elapsed}s waiting on a settled device"

echo "── 2. legacy present + initialized -> renamed without burning the budget ──"
reset_tree; mk_dev end0 3; mk_init 3
WAIT_SECS=2; start=$SECONDS
run_pair end0 eth0
elapsed=$(( SECONDS - start ))
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
printf '%s\n' "$OUT" | grep -q "end0 -> eth0: renamed" && ok "renamed" || bad "no rename logged"
[ -d "$ND/eth0" ] && [ ! -d "$ND/end0" ] && ok "device now canonical" || bad "device tree wrong after rename"
[ "$elapsed" -le 1 ] && ok "no wait budget burned (${elapsed}s)" || bad "burned ${elapsed}s on an initialized legacy device"

echo "── 3. the boot-0 sequence: kernel-named eth0, udev renames it away mid-wait ──"
# eth0 exists but is NOT initialized — the old name-existence check no-op'd
# here and the board ended on an unconfigured end0. The gate must keep
# waiting; a helper plays udev (rename eth0->end0 + initialize) after ~1 s.
reset_tree; mk_dev eth0 7
( sleep 1; mv "$ND/eth0" "$ND/end0" 2>/dev/null; : > "$UD/n7" ) &
helper=$!
WAIT_SECS=5
run_pair end0 eth0
wait "$helper" 2>/dev/null
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
if printf '%s\n' "$OUT" | grep -q "already canonical"; then
    bad "uninitialized kernel-named eth0 was counted as canonical (the boot-0 regression)"
else
    ok "uninitialized eth0 not trusted"
fi
printf '%s\n' "$OUT" | grep -q "end0 -> eth0: renamed" && ok "renamed after udev's pass" || bad "no rename after udev's pass"
[ -d "$ND/eth0" ] && [ ! -d "$ND/end0" ] && ok "device ends canonical" || bad "device tree wrong after the race"

echo "── 4. neither name present -> budget burned, nothing to do, exit 0 ──"
reset_tree
WAIT_SECS=1
run_pair end0 eth0
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
printf '%s\n' "$OUT" | grep -q "absent after 1s — nothing to do" && ok "absent-pair log line" || bad "no absent-pair log line"

echo "── 5. degraded: udev db never appears ──"
reset_tree; mk_dev end0 5           # legacy present, never initialized
WAIT_SECS=1
run_pair end0 eth0
[ "$RC" = 0 ] && ok "legacy fallback: exit 0 on a successful rename" || bad "legacy fallback: exit $RC"
printf '%s\n' "$OUT" | grep -q "udev db entry absent" && ok "legacy fallback: honest WARN logged" || bad "legacy fallback: no WARN"
[ -d "$ND/eth0" ] && ok "legacy fallback: rename still attempted" || bad "legacy fallback: rename not attempted"
reset_tree; mk_dev eth0 6           # canonical present, never initialized
WAIT_SECS=1
run_pair end0 eth0
[ "$RC" = 0 ] && ok "canonical fallback: exit 0" || bad "canonical fallback: exit $RC"
printf '%s\n' "$OUT" | grep -q "\[WARN\] eth0: udev db entry absent after 1s — treating current name as settled" \
    && ok "canonical fallback: WARN (not INFO) distinguishes a wedged-udev boot" \
    || bad "canonical fallback: WARN line missing"

echo "── 6. failed rename -> admin state restored, exit non-zero ──"
reset_tree; mk_dev end0 8 0x1003; mk_init 8   # admin-UP device, rename will fail
WAIT_SECS=2
export IP_SHIM_FAIL_RENAME=1
run_pair end0 eth0
unset IP_SHIM_FAIL_RENAME
[ "$RC" != 0 ] && ok "non-zero exit (visible in systemctl --failed)" || bad "exit 0 despite a failed rename"
printf '%s\n' "$OUT" | grep -q "rename FAILED" && ok "failure logged" || bad "no failure log line"
printf '%s\n' "$OUT" | grep -q "previous admin state (up) restored" && ok "admin state restored" || bad "admin state not restored"
grep -q "link set dev end0 down" "$T/ip-calls.log" && grep -q "link set dev end0 up" "$T/ip-calls.log" \
    && ok "down before rename, up after failure (call journal)" || bad "down/up call sequence missing"
[ -d "$ND/end0" ] && ok "board keeps its legacy name (still reachable)" || bad "legacy device gone after a failed rename"

echo "---"
if [ "$fails" = 0 ]; then echo "iface-canonical-gate: all checks passed"; else echo "iface-canonical-gate: $fails check(s) FAILED"; fi
exit "$fails"
