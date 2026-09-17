#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-firstboot-sb-csum.sh — regression harness for the first-boot primary
# superblock checksum guard in etc/sa02m-rootfs-expand.sh (and its
# byte-identical firstboot-overlay copy). Quality row `firstboot-sb-csum`.
#
# Why this exists: two boards flashed from the 20260915 golden image booted
# once, were power-cut a few minutes later, and never came back — the root
# primary superblock carried a STALE crc32c after the first-boot online
# resize2fs (board kernel 6.1.0-rc6 predates upstream "ext4: fix bad checksum
# after online resize"), so a cold boot before the first clean shutdown hit
# "Superblock checksum does not match" and panicked before networking
# (docs/bugs/BUGLOG.md 2026-09-16). The guard freezes/unfreezes / so the
# kernel rewrites the superblock, then verifies the bytes ON THE MEDIUM.
#
# Method — four layers, none needing root, a device or a real ext4:
#   1. the two shipped copies are byte-identical (patch-firstboot ships the
#      overlay one, the installer ships the etc one — a fix in one is not a fix);
#   2. wiring pins read COMMENT-STRIPPED through lib_check.sh: the freeze
#      cycle under `timeout`, the O_DIRECT read, the guard called in BOTH
#      `start` paths BEFORE finish_firstboot, the status reaching systemd
#      (`exit "$csum_rc"`) in both, the marker path, and NO live `systemctl`
#      (the script's hot-path constraint, kept true rather than asserted);
#      since 1.0.6.48 also the durable-evidence wiring: the verdict path
#      /var/lib/sa02m-rootfs-expand.result, the fsync of verdict + DONE + log
#      as the LAST line of finish_firstboot (after its own log line), and the
#      «root may still be frozen» branch writing to stderr, not through log()
#      (a tee onto a root that may be frozen — reviewer A2, 1.0.6.47);
#   3. BEHAVIOUR of the shipped crc32c verifier against two 4 KiB blocks
#      captured on the bench (scripts/dev/fixtures/): sb-bad.bin is the first
#      block of a bricked board's root partition — stored 0x8c9e9dcb, computed
#      0xe6961c00 — and MUST read BAD; sb-good.bin is the same block after
#      e2fsck repair (0xf8449046 both) and MUST read OK; plus a short read
#      (ERR), a block with metadata_csum off (NOCSUM) and a one-bit body
#      corruption of the good block (BAD — the crc covers the body);
#   4. BEHAVIOUR of the whole guard and of the `start` dispatcher on a
#      sandbox-retargeted copy, driven through PATH shims for dd / fsfreeze /
#      timeout / sync / blockdev / parted / resize2fs: freeze-verify-OK; the
#      one retry (BAD then OK); BAD twice -> ERROR + marker + rc 1 while DONE
#      is STILL set (the boot must not be delayed again); a missing fsfreeze
#      still verifies; a failed `-f` still issues `-u`; nothing writes the log
#      between -f and -u (a tee onto a frozen root would hang the boot); the
#      resize path freezes AFTER resize2fs and BEFORE DONE; and (1.0.6.48)
#      the durable verdict: after OK / BAD twice / BAD-then-OK the result file
#      holds exactly ONE line `<ISO time> <OK|BAD> stored=… computed=…
#      attempts=N`, staged through a temp file that is sync'd before the
#      rename (no .tmp left behind), rewritten not appended on a re-run, still
#      written when `sync FILE` is rejected (the bare-sync fallback), and in
#      every `start` path the LAST recorded call is the fsync of verdict +
#      DONE + log + their directory — on the failing path too; and (1.0.6.49)
#      a stale `$RESULT.tmp` (a cut between the temp write and the rename
#      commit) is removed at `start` BEFORE the DONE check, so the no-op
#      re-run path clears it too (6e — the only path where nothing else
#      would consume it; 6f shows the live paths overwrite it anyway).
#
# Why the durable layer exists: nothing not fsync'd survives a cut on this
# root (commit=600 + journal_data_writeback) — bench evidence and the operator
# reading: docs/bugs/BUGLOG.md 2026-09-16 (12:40, 16:40), docs/deployment.md §12.
#
# Non-vacuous: a missing/empty script copy, a fixture that is not exactly
# 4096 bytes, an extraction that lost a function, a retargeted copy that still
# names a real /etc /var /lib /dev /usr path (fail-closed: it is never run),
# or a pin matching no live line FAILS.
#
# Drive-to-failure (recipe): ROOTFS_EXPAND_SRC=<copy> runs layers 2-4 against
# any copy of the script (layer 1 always compares the two SHIPPED copies, so a
# mutated SRC is judged by the pins and the behaviour, not by the identity
# check); SB_FIXTURES=<dir> points at another fixture pair. Measured RED,
# 2026-09-16 (each mutation alone, the rest pristine): fixtures swapped ->
# 3a/3b/3e/4a/4b/5a-5f/6a/6b/6c RED (14); the compare inverted in a copy
# (`"OK" if crc != stored`) -> 3a/3b/3e/4a/4b/5a/5b/5c/5e/6a/6b RED (11);
# the `timeout 20 fsfreeze -f /` line commented out -> 2a RED and the copy no
# longer parses (extraction floor FAILS); the guard call dropped from the
# already-uses-eMMC path -> 2e/6a/6b RED (3); HEAD's pre-fix script ->
# 2a-2f + 2h RED (10 pins) then the extraction floor FAILS (no verifier).
# Measured RED, 2026-09-16 (1.0.6.48, the durable-evidence layer; each
# mutation alone, the rest pristine): the final `sync_files "$RESULT" "$DONE"
# "$LOG"` line deleted -> 2j/6a/6b/6c RED (4); the same line commented out ->
# the same 4 (comment-safe); both write_result calls dropped ->
# 5a/5f/5h/5i/5j/5k/6a/6b/6c RED (9); the failed-unfreeze branch back to
# log() -> 2k/5g RED (2); the verdict appended in place instead of
# staged+synced+renamed -> 5a/5f/5h/5j/5k RED (5); the bare-sync fallback
# removed -> 5k RED (1); the sync moved above the finish log line -> 2j RED
# (1); HEAD's 1.0.6.47 script -> 2d×2 + 2i/2j/2k RED (5 pins) then the
# extraction floor FAILS (no sync_files). Measured RED, 2026-09-17 (1.0.6.49,
# the stale-temp case): the 1.0.6.48 script (git archive origin/main as SRC)
# -> 6e RED (`tmp-left=yes`), 6f holds on both trees; the `rm -f
# "$RESULT.tmp"` line is registered in comment-mutation-proof.
#
# Comment-mutation: the `timeout 20 fsfreeze -f /` line and the
# `sync_files "$RESULT" "$DONE" "$LOG"` line are registered in
# comment-mutation-proof. That row mutates the etc copy alone, which layer 1
# (identity) also catches — so each pin's OWN proof is the SRC recipe above
# (the mutated copy judged by pins + behaviour, never by the identity check).
#
# Run: bash scripts/dev/test-firstboot-sb-csum.sh   (bash + coreutils + python3)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
ETC=etc/sa02m-rootfs-expand.sh
OVL=tools/imaging/firstboot-overlay/usr/local/sbin/sa02m-rootfs-expand.sh
SRC=${ROOTFS_EXPAND_SRC:-$ETC}
FIX=${SB_FIXTURES:-scripts/dev/fixtures}
BAD_FX=$FIX/sb-bad.bin
GOOD_FX=$FIX/sb-good.bin

# shellcheck disable=SC1091
source .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  cannot source lib_check.sh"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── Non-vacuity floor ───────────────────────────────────────────────────────
for f in "$ETC" "$OVL" "$SRC"; do
    [ -s "$f" ] || { echo "FAIL  $f missing or empty — nothing to test"; exit 1; }
done
for f in "$BAD_FX" "$GOOD_FX"; do
    [ -f "$f" ] || { echo "FAIL  fixture $f missing — the behavioural layer has nothing to run against"; exit 1; }
    n=$(wc -c < "$f")
    [ "$n" = 4096 ] || { echo "FAIL  fixture $f is $n bytes, expected exactly 4096 (one partition block)"; exit 1; }
done
PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys" >/dev/null 2>&1; then PY=$p; break; fi
done
[ -n "$PY" ] || { echo "FAIL  no python3/python/py on PATH — the shipped verifier is python3 and cannot be exercised"; exit 1; }
# Absolute: the `python3` shim below sits FIRST on PATH, so a bare name would
# resolve back to the shim and exec itself forever.
PY=$(command -v "$PY")

# ── 1. the two shipped copies are one file ──────────────────────────────────
echo "── 1. shipped copies byte-identical ──"
if cmp -s "$ETC" "$OVL"; then ok "1 $ETC == $OVL"
else bad "1 $ETC and $OVL differ — patch-firstboot ships the overlay copy, the installer the etc copy; a fix in one is not a fix"; fi

# ── 2. wiring pins (comment-stripped) ───────────────────────────────────────
echo "── 2. wiring pins in $SRC ──"
stripped_has_inline "$SRC" 'timeout 20 fsfreeze -f /' \
    && ok "2a freeze under timeout: 'timeout 20 fsfreeze -f /'" \
    || bad "2a 'timeout 20 fsfreeze -f /' not found live — the kernel is never made to rewrite the superblock (or the freeze can hang the boot)"
stripped_has_inline "$SRC" 'timeout 20 fsfreeze -u /' \
    && ok "2b unfreeze under timeout: 'timeout 20 fsfreeze -u /'" \
    || bad "2b 'timeout 20 fsfreeze -u /' not found live"
stripped_matches_inline "$SRC" 'dd if="\$1" bs=4096 count=1 iflag=direct' \
    && ok "2c on-disk read bypasses the page cache (dd … iflag=direct)" \
    || bad "2c no 'dd if=\"\$1\" bs=4096 count=1 iflag=direct' — a buffered read is answered by the page cache and cannot see the stale on-disk checksum"
for fn in sb_csum_of_block primary_sb_checksum force_primary_sb_rewrite ensure_primary_sb_checksum sync_files write_result; do
    stripped_matches "$SRC" "^${fn}\(\) \{" \
        && ok "2d ${fn}() defined" \
        || bad "2d ${fn}() not defined in $SRC"
done
# Call order in both `start` paths: guard(E) before finish_firstboot(F):
# E1 < F1 < E2 < F2 — the already-uses-eMMC path, then the resize path.
ERE_E='^[[:space:]]+ensure_primary_sb_checksum( |$)'
ERE_F='^[[:space:]]+finish_firstboot[[:space:]]*$'
n_e=$(stripped_count "$SRC" "$ERE_E")
n_f=$(stripped_count "$SRC" "$ERE_F")
e1=$(stripped_first_line "$SRC" "$ERE_E"); e2=$(stripped_last_line "$SRC" "$ERE_E")
f1=$(stripped_first_line "$SRC" "$ERE_F"); f2=$(stripped_last_line "$SRC" "$ERE_F")
if [ "$n_e" = 2 ] && [ "$n_f" = 2 ] && [ "$e1" -lt "$f1" ] && [ "$f1" -lt "$e2" ] && [ "$e2" -lt "$f2" ]; then
    ok "2e guard called in BOTH start paths before finish_firstboot (lines $e1<$f1, $e2<$f2)"
else
    bad "2e guard wiring: ensure calls=$n_e (lines '$e1','$e2'), finish calls=$n_f (lines '$f1','$f2') — expected 2 each, each ensure directly before its finish"
fi
n_x=$(stripped_count "$SRC" '^[[:space:]]+exit "\$csum_rc"$')
[ "$n_x" = 2 ] && ok "2f the guard's status reaches systemd in both paths (exit \"\$csum_rc\" ×2)" \
    || bad "2f 'exit \"\$csum_rc\"' appears $n_x times, expected 2 — a BAD checksum would leave the unit green"
n_s=$(stripped_count "$SRC" 'systemctl')
[ "$n_s" = 0 ] && ok "2g no live systemctl in the script (hot-path constraint holds)" \
    || bad "2g $n_s live systemctl line(s) — the script's own header forbids it (D-Bus seconds at boot, cancelled watchdog starts)"
stripped_has_inline "$SRC" '/var/lib/sa02m-rootfs-expand.csum-bad' \
    && ok "2h failure marker path present" \
    || bad "2h '/var/lib/sa02m-rootfs-expand.csum-bad' not found — the repair tool's evidence copy and the audit have no signal"
stripped_has_inline "$SRC" '/var/lib/sa02m-rootfs-expand.result' \
    && ok "2i durable verdict path present (/var/lib/sa02m-rootfs-expand.result)" \
    || bad "2i '/var/lib/sa02m-rootfs-expand.result' not found — the only first-boot evidence that survives a power cut is gone"
# The fsync is the LAST line of finish_firstboot: after its own log line, so
# every log line is covered, and nothing may follow it.
ERE_SYNC='^[[:space:]]+sync_files "\$RESULT" "\$DONE" "\$LOG"'
ERE_FIN_LOG='^[[:space:]]+log "firstboot finish: DONE set'
s_line=$(stripped_first_line "$SRC" "$ERE_SYNC"); l_line=$(stripped_first_line "$SRC" "$ERE_FIN_LOG")
n_sync=$(stripped_count "$SRC" "$ERE_SYNC")
if [ "$n_sync" = 1 ] && [ -n "$l_line" ] && [ "$l_line" -lt "$s_line" ]; then
    ok "2j verdict + DONE + log fsync'd once, after the finish log line (lines $l_line<$s_line)"
else
    bad "2j sync_files \"\$RESULT\" \"\$DONE\" \"\$LOG\": count=$n_sync line='$s_line', finish log line='$l_line' — expected exactly one, after the log line (the evidence is not made durable, or a later write is left unsynced)"
fi
if stripped_matches "$SRC" '^[[:space:]]+echo .*root may still be frozen" >&2$' \
   && ! stripped_matches "$SRC" 'log "ERROR: fsfreeze -u / failed'; then
    ok "2k the «root may still be frozen» branch writes to stderr, never through log() onto that root"
else
    bad "2k the failed-unfreeze branch must be an 'echo … >&2' and not a log() call — a tee onto a possibly frozen root blocks the boot"
fi

# ── Extraction: the shipped functions without the dispatcher ────────────────
sed '/^case "\${1:-start}" in/,$d' "$SRC" > "$T/lib.sh"
for fn in sb_csum_of_block primary_sb_checksum force_primary_sb_rewrite ensure_primary_sb_checksum log sync_files write_result; do
    bash -c "source '$T/lib.sh'; declare -F $fn >/dev/null" 2>/dev/null \
        || { echo "FAIL  extraction lost ${fn}() — the sourced copy defines nothing to test (non-vacuity)"; exit 1; }
done

# ── Shims ───────────────────────────────────────────────────────────────────
mkdir -p "$T/bin" "$T/dev" "$T/var/log" "$T/var/lib" "$T/etc/systemd/system/multi-user.target.wants" "$T/lib/systemd/system"
CALLS="$T/calls"
: > "$CALLS"
printf '#!/bin/bash\nexec "%s" "$@"\n' "$PY" > "$T/bin/python3"
# dd: ignores its arguments (records them), emits the fixture named by the
# N-th line of $T/dd.seq for the N-th call (last line repeats).
cat > "$T/bin/dd" <<SHIM
#!/bin/bash
T="$T"
SHIM
cat >> "$T/bin/dd" <<'SHIM'
printf 'dd %s\n' "$*" >> "$T/calls"
n=$(( $(cat "$T/dd.n" 2>/dev/null || echo 0) + 1 )); printf '%s\n' "$n" > "$T/dd.n"
src=$(sed -n "${n}p" "$T/dd.seq"); [ -n "$src" ] || src=$(tail -1 "$T/dd.seq")
cat "$src"
SHIM
# fsfreeze: records; on -f notes the log size and whether DONE already exists,
# on -u reports a log that grew while frozen. $T/fsfreeze.mode: ok|fail-f|fail-u
cat > "$T/bin/fsfreeze" <<SHIM
#!/bin/bash
T="$T"
SHIM
cat >> "$T/bin/fsfreeze" <<'SHIM'
printf 'fsfreeze %s\n' "$*" >> "$T/calls"
mode=$(cat "$T/fsfreeze.mode" 2>/dev/null || echo ok)
logsize() { if [ -f "$T/log" ]; then wc -c < "$T/log"; else echo 0; fi; }
case "${1:-}" in
    -f) [ "$mode" = fail-f ] && exit 1
        logsize > "$T/frozen-logsize"
        [ -e "$T/var/lib/sa02m-rootfs-expand.done" ] && : > "$T/done-before-freeze"
        ;;
    -u) [ "$mode" = fail-u ] && exit 1
        [ -f "$T/frozen-logsize" ] && [ "$(logsize)" != "$(cat "$T/frozen-logsize")" ] && : > "$T/wrote-while-frozen"
        ;;
esac
exit 0
SHIM
# timeout: records the bound and runs the command (the real one would too;
# the shim proves the bound is passed, not that the shim can kill).
cat > "$T/bin/timeout" <<SHIM
#!/bin/bash
T="$T"
SHIM
cat >> "$T/bin/timeout" <<'SHIM'
printf 'timeout %s\n' "$*" >> "$T/calls"
shift; exec "$@"
SHIM
for s in partprobe udevadm resize2fs; do
    printf '#!/bin/bash\nprintf "%s %%s\\n" "$*" >> "%s"\nexit 0\n' "$s" "$CALLS" > "$T/bin/$s"
done
# sync: records; $T/sync.mode=nofile plays a sync without FILE support (a lean
# busybox / pre-8.24 coreutils) — any argument is rejected, so the script's
# bare-sync fallback is what gets exercised.
cat > "$T/bin/sync" <<SHIM
#!/bin/bash
T="$T"
SHIM
cat >> "$T/bin/sync" <<'SHIM'
printf 'sync %s\n' "$*" >> "$T/calls"
[ "$(cat "$T/sync.mode" 2>/dev/null || echo ok)" = nofile ] && [ $# -gt 0 ] && exit 1
exit 0
SHIM
# blockdev: --getsize64 <part|disk> -> the numbers in $T/blockdev.part / .disk
cat > "$T/bin/blockdev" <<SHIM
#!/bin/bash
T="$T"
SHIM
cat >> "$T/bin/blockdev" <<'SHIM'
printf 'blockdev %s\n' "$*" >> "$T/calls"
case "${2:-}" in *p2) cat "$T/blockdev.part" ;; *) cat "$T/blockdev.disk" ;; esac
SHIM
# parted: `print` answers a capacity line; `resizepart` is recorded only.
cat > "$T/bin/parted" <<SHIM
#!/bin/bash
T="$T"
SHIM
cat >> "$T/bin/parted" <<'SHIM'
printf 'parted %s\n' "$*" >> "$T/calls"
case "$*" in *print*) printf 'BYT;\n/dev/mmcblk2:15269888s:sd/mmc:512:512:msdos::;\n' ;; esac
exit 0
SHIM
chmod +x "$T/bin"/*
export PATH="$T/bin:$PATH"

reset_shims() {  # $@ = dd sequence (fixture paths)
    : > "$CALLS"; rm -f "$T/dd.n" "$T/frozen-logsize" "$T/done-before-freeze" "$T/wrote-while-frozen" "$T/log" \
                     "$T/var/lib/sa02m-rootfs-expand.done" "$T/var/lib/sa02m-rootfs-expand.csum-bad" \
                     "$T/var/lib/sa02m-rootfs-expand.result" "$T/var/lib/sa02m-rootfs-expand.result.tmp"
    printf 'ok\n' > "$T/fsfreeze.mode"
    printf 'ok\n' > "$T/sync.mode"
    : > "$T/dd.seq"
    local a
    for a; do   # absolute, so the shim finds the fixture from any cwd
        case "$a" in /*|[A-Za-z]:/*) ;; *) a="$PWD/$a" ;; esac   # the drive form: git-bash
        printf '%s\n' "$a" >> "$T/dd.seq"
    done
}
count_calls() { grep -c -- "$1" "$CALLS"; }
has_file() { if [ -e "$1" ]; then echo yes; else echo no; fi; }
log_flat() { tr '\n' '|' < "$1" 2>/dev/null; }
# Run a snippet inside the sourced copy with the state paths pointed at the
# sandbox; stdin passes through.
run_fn() {  # $1 = shell snippet
    bash -c "source '$T/lib.sh'; LOG='$T/log'; DONE='$T/var/lib/sa02m-rootfs-expand.done'; CSUM_BAD='$T/var/lib/sa02m-rootfs-expand.csum-bad'; RESULT='$T/var/lib/sa02m-rootfs-expand.result'; ROOT_PART=/dev/sa02m-fake-p2; $1"
}
RES="$T/var/lib/sa02m-rootfs-expand.result"
# The verdict line: ISO time with offset, the verifier's own word and sums, the attempt count.
result_matches() {  # $1=ERE for the part after the timestamp
    [ -f "$RES" ] && [ "$(wc -l < "$RES")" = 1 ] && [ ! -e "$RES.tmp" ] \
        && grep -qE "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{4} $1\$" "$RES"
}
result_flat() { if [ -f "$RES" ]; then tr '\n' '|' < "$RES"; else echo '<absent>'; fi; }

# ── 3. the crc32c verifier against the bench blocks ─────────────────────────
echo "── 3. sb_csum_of_block: bench fixtures ──"
out=$(run_fn sb_csum_of_block < "$BAD_FX"); rc=$?
[ "$rc" = 1 ] && [ "$out" = "BAD stored=0x8c9e9dcb computed=0xe6961c00" ] \
    && ok "3a bricked board's block reads BAD (stored 0x8c9e9dcb ≠ computed 0xe6961c00), rc=1" \
    || bad "3a sb-bad.bin: rc=$rc out='$out' (expected rc=1, 'BAD stored=0x8c9e9dcb computed=0xe6961c00')"
out=$(run_fn sb_csum_of_block < "$GOOD_FX"); rc=$?
[ "$rc" = 0 ] && [ "$out" = "OK stored=0xf8449046 computed=0xf8449046" ] \
    && ok "3b repaired block reads OK (0xf8449046), rc=0" \
    || bad "3b sb-good.bin: rc=$rc out='$out' (expected rc=0, 'OK stored=0xf8449046 computed=0xf8449046')"
out=$(head -c 100 "$GOOD_FX" | run_fn sb_csum_of_block); rc=$?
if [ "$rc" = 2 ] && case "$out" in ERR*) true ;; *) false ;; esac; then
    ok "3c short read -> ERR, rc=2 (never a false OK)"
else
    bad "3c 100-byte input: rc=$rc out='$out' (expected rc=2, ERR…)"
fi
"$PY" - "$GOOD_FX" "$T/nocsum.bin" "$T/corrupt.bin" <<'PYEOF'
import struct, sys
blk = bytearray(open(sys.argv[1], "rb").read())
noc = bytearray(blk)
ro = struct.unpack_from("<I", noc, 1024 + 0x64)[0] & ~0x400
struct.pack_into("<I", noc, 1024 + 0x64, ro)
open(sys.argv[2], "wb").write(noc)
cor = bytearray(blk)
cor[1024 + 0x78] ^= 0x01          # one bit of s_volume_name; checksum untouched
open(sys.argv[3], "wb").write(cor)
PYEOF
out=$(run_fn sb_csum_of_block < "$T/nocsum.bin"); rc=$?
if [ "$rc" = 0 ] && case "$out" in NOCSUM*) true ;; *) false ;; esac; then
    ok "3d metadata_csum feature off -> NOCSUM, rc=0 (nothing the kernel would verify)"
else
    bad "3d feature bit cleared: rc=$rc out='$out' (expected rc=0, NOCSUM…)"
fi
out=$(run_fn sb_csum_of_block < "$T/corrupt.bin"); rc=$?
if [ "$rc" = 1 ] && case "$out" in "BAD stored=0xf8449046 computed="*) true ;; *) false ;; esac; then
    ok "3e one bit flipped in the body -> BAD (the crc covers bytes 0..1019, not just the tail)"
else
    bad "3e body corruption: rc=$rc out='$out' (expected rc=1, BAD stored=0xf8449046 computed=<other>)"
fi

# ── 4. primary_sb_checksum: the dd glue ─────────────────────────────────────
echo "── 4. primary_sb_checksum reads the device O_DIRECT ──"
reset_shims "$GOOD_FX"
out=$(run_fn 'primary_sb_checksum "$ROOT_PART"'); rc=$?
ddline=$(grep '^dd ' "$CALLS")
if [ "$rc" = 0 ] && case "$out" in OK*) true ;; *) false ;; esac \
   && case "$ddline" in *"if=/dev/sa02m-fake-p2"*) true ;; *) false ;; esac \
   && case "$ddline" in *"iflag=direct"*) true ;; *) false ;; esac \
   && case "$ddline" in *"bs=4096"*) true ;; *) false ;; esac \
   && case "$ddline" in *"count=1"*) true ;; *) false ;; esac; then
    ok "4a dd invoked as '$ddline', block reaches the verifier -> OK"
else
    bad "4a rc=$rc out='$out' dd='$ddline' (expected OK with if=<device> bs=4096 count=1 iflag=direct)"
fi
reset_shims "$BAD_FX"
out=$(run_fn 'primary_sb_checksum "$ROOT_PART"'); rc=$?
if [ "$rc" = 1 ] && case "$out" in BAD*) true ;; *) false ;; esac; then
    ok "4b the bad block through the same path -> BAD, rc=1"
else
    bad "4b rc=$rc out='$out' (expected BAD, rc=1)"
fi

# ── 5. ensure_primary_sb_checksum: freeze, verify, retry, mark ──────────────
echo "── 5. ensure_primary_sb_checksum ──"
seq_of() { grep -E '^(fsfreeze|timeout|dd|sync)' "$CALLS" | tr '\n' ';'; }
MARK="$T/var/lib/sa02m-rootfs-expand.csum-bad"

reset_shims "$GOOD_FX"
run_fn ensure_primary_sb_checksum >/dev/null 2>&1; rc=$?
want="sync ;timeout 20 fsfreeze -f /;fsfreeze -f /;timeout 20 fsfreeze -u /;fsfreeze -u /;sync ;dd if=/dev/sa02m-fake-p2 bs=4096 count=1 iflag=direct;sync $RES.tmp;"
if [ "$rc" = 0 ] && [ "$(seq_of)" = "$want" ] && grep -q 'primary superblock checksum OK stored=0xf8449046 computed=0xf8449046 (attempt 1)' "$T/log" \
   && [ ! -e "$MARK" ] && [ ! -e "$T/wrote-while-frozen" ]; then
    ok "5a OK first time: sync → freeze → unfreeze → sync → O_DIRECT verify → verdict staged+synced, rc=0, log line, no marker, nothing logged while frozen"
else
    bad "5a rc=$rc seq='$(seq_of)' log='$(log_flat "$T/log")' marker=$(has_file "$MARK") frozen-write=$(has_file "$T/wrote-while-frozen")"
fi

reset_shims "$BAD_FX"
run_fn ensure_primary_sb_checksum >/dev/null 2>&1; rc=$?
if [ "$rc" = 1 ] && [ "$(count_calls '^fsfreeze -f /')" = 2 ] && [ "$(count_calls '^dd ')" = 2 ] \
   && [ -e "$MARK" ] \
   && grep -q 'ERROR: primary superblock checksum still not OK after 2 freeze cycles' "$T/log" \
   && grep -q 'checksum BAD stored=0x8c9e9dcb computed=0xe6961c00 (attempt 2)' "$T/log"; then
    ok "5b BAD twice: two freeze cycles, two reads, loud ERROR, marker touched, rc=1"
else
    bad "5b rc=$rc freezes=$(count_calls '^fsfreeze -f /') reads=$(count_calls '^dd ') marker=$(has_file "$MARK") log='$(log_flat "$T/log")'"
fi

reset_shims "$BAD_FX" "$GOOD_FX"
run_fn ensure_primary_sb_checksum >/dev/null 2>&1; rc=$?
if [ "$rc" = 0 ] && [ "$(count_calls '^fsfreeze -f /')" = 2 ] && [ "$(count_calls '^dd ')" = 2 ] \
   && [ ! -e "$MARK" ] \
   && grep -q 'checksum BAD .* (attempt 1)' "$T/log" && grep -q 'checksum OK .* (attempt 2)' "$T/log"; then
    ok "5c BAD then OK: exactly one retry, rc=0, no marker"
else
    bad "5c rc=$rc freezes=$(count_calls '^fsfreeze -f /') reads=$(count_calls '^dd ') log='$(log_flat "$T/log")'"
fi

reset_shims "$GOOD_FX"
: > "$MARK"
run_fn ensure_primary_sb_checksum >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && [ ! -e "$MARK" ] \
    && ok "5d a stale marker is cleared on OK (re-run is idempotent)" \
    || bad "5d rc=$rc marker still present=$(has_file "$MARK")"

reset_shims "$GOOD_FX"
mv "$T/bin/fsfreeze" "$T/fsfreeze.away"
run_fn ensure_primary_sb_checksum >/dev/null 2>&1; rc=$?
mv "$T/fsfreeze.away" "$T/bin/fsfreeze"
if [ "$rc" = 0 ] && grep -q 'ERROR: fsfreeze not found' "$T/log" && [ "$(count_calls '^dd ')" = 1 ] \
   && grep -q 'checksum OK' "$T/log"; then
    ok "5e fsfreeze missing: ERROR logged, verification still runs (rc follows the on-disk state)"
else
    bad "5e rc=$rc reads=$(count_calls '^dd ') log='$(log_flat "$T/log")'"
fi

reset_shims "$GOOD_FX"
printf 'fail-f\n' > "$T/fsfreeze.mode"
run_fn ensure_primary_sb_checksum >/dev/null 2>&1; rc=$?
if [ "$rc" = 0 ] && grep -q 'ERROR: fsfreeze -f / failed' "$T/log" \
   && [ "$(seq_of)" = "sync ;timeout 20 fsfreeze -f /;fsfreeze -f /;timeout 20 fsfreeze -u /;fsfreeze -u /;dd if=/dev/sa02m-fake-p2 bs=4096 count=1 iflag=direct;sync $RES.tmp;" ]; then
    ok "5f a failed -f still issues -u (a half-applied freeze must not stay), then verifies"
else
    bad "5f rc=$rc seq='$(seq_of)' log='$(log_flat "$T/log")'"
fi

reset_shims "$GOOD_FX"
printf 'fail-u\n' > "$T/fsfreeze.mode"
err=$(run_fn ensure_primary_sb_checksum 2>&1 >/dev/null); rc=$?
if case "$err" in *"ERROR: fsfreeze -u / failed — root may still be frozen"*) true ;; *) false ;; esac \
   && ! grep -q 'fsfreeze -u / failed' "$T/log"; then
    ok "5g a failed -u is a loud ERROR on stderr and NOT in the log (that root may be frozen), rc=$rc from the on-disk state"
else
    bad "5g rc=$rc stderr='$err' log='$(log_flat "$T/log")' (expected the ERROR on stderr only)"
fi

# ── 5h–5k. the durable verdict file ─────────────────────────────────────────
reset_shims "$GOOD_FX"
run_fn ensure_primary_sb_checksum >/dev/null 2>&1; rc=$?
tail_seq=$(grep -E '^(dd|sync)' "$CALLS" | tail -n 2 | tr '\n' ';')
if [ "$rc" = 0 ] && result_matches 'OK stored=0xf8449046 computed=0xf8449046 attempts=1' \
   && [ "$tail_seq" = "dd if=/dev/sa02m-fake-p2 bs=4096 count=1 iflag=direct;sync $RES.tmp;" ]; then
    ok "5h OK: one verdict line '<ISO time> OK … attempts=1', staged .tmp synced after the read, no .tmp left"
else
    bad "5h rc=$rc result='$(result_flat)' tmp=$(has_file "$RES.tmp") tail='$tail_seq'"
fi

reset_shims "$BAD_FX"
run_fn ensure_primary_sb_checksum >/dev/null 2>&1; rc=$?
if [ "$rc" = 1 ] && result_matches 'BAD stored=0x8c9e9dcb computed=0xe6961c00 attempts=2' && [ -e "$MARK" ]; then
    ok "5i BAD twice: the verdict line says BAD … attempts=2 next to the marker (the failing outcome is durable too)"
else
    bad "5i rc=$rc result='$(result_flat)' marker=$(has_file "$MARK")"
fi

reset_shims "$BAD_FX" "$GOOD_FX"
printf 'STALE verdict from a previous run\n' > "$RES"
run_fn ensure_primary_sb_checksum >/dev/null 2>&1; rc=$?
if [ "$rc" = 0 ] && result_matches 'OK stored=0xf8449046 computed=0xf8449046 attempts=2' && ! grep -q STALE "$RES"; then
    ok "5j BAD then OK: verdict OK … attempts=2, and a stale file is REWRITTEN (one line, not appended)"
else
    bad "5j rc=$rc result='$(result_flat)'"
fi

reset_shims "$GOOD_FX"
printf 'nofile\n' > "$T/sync.mode"
run_fn ensure_primary_sb_checksum >/dev/null 2>&1; rc=$?
tail_seq=$(grep -E '^sync' "$CALLS" | tail -n 2 | tr '\n' ';')
if [ "$rc" = 0 ] && result_matches 'OK stored=0xf8449046 computed=0xf8449046 attempts=1' \
   && [ "$tail_seq" = "sync $RES.tmp;sync ;" ]; then
    ok "5k a sync without FILE support: the rejected 'sync FILE' falls back to a bare sync, verdict still written"
else
    bad "5k rc=$rc result='$(result_flat)' syncs='$(grep '^sync' "$CALLS" | tr '\n' ';')' (expected 'sync <tmp>' then a bare 'sync ')"
fi

# ── 6. the `start` dispatcher on a sandbox-retargeted copy ──────────────────
echo "── 6. start paths (retargeted copy, fail-closed) ──"
DONE_F="$T/var/lib/sa02m-rootfs-expand.done"
SLOG="$T/var/log/sa02m-rootfs-expand.log"
sed -e "s#/var/log/sa02m-rootfs-expand.log#$SLOG#" \
    -e "s#/var/lib/sa02m-rootfs-expand#$T/var/lib/sa02m-rootfs-expand#g" \
    -e "s#/etc/systemd#$T/etc/systemd#g" \
    -e "s#/lib/systemd#$T/lib/systemd#g" \
    -e "s#:-/dev/mmcblk2p2}#:-$T/dev/p2}#" \
    -e "s#:-/dev/mmcblk2}#:-$T/dev/disk}#" \
    -e 's#\[ -b "\$ROOT_PART" \] && \[ -b "\$ROOT_DISK" \]#[ -e "$ROOT_PART" ] \&\& [ -e "$ROOT_DISK" ]#' \
    "$SRC" > "$T/start.sh"
# Fail-closed: a real absolute path left in the copy means it is never run.
leftover=$(stripped_text_inline "$T/start.sh" | sed -e "s#$T#@T@#g" -e 's#/dev/null##g' | grep -nE '(^|[^A-Za-z0-9_@])/(etc|var|lib|dev|usr)/')
if [ -n "$leftover" ]; then
    bad "6 retargeting incomplete — NOT running the copy: $leftover"
else
    : > "$T/dev/p2"; : > "$T/dev/disk"
    run_start() { bash "$T/start.sh" start >"$T/start.out" 2>&1; }
    FINAL_SYNC="sync $RES $DONE_F $SLOG $T/var/lib"
    last_call() { tail -n 1 "$CALLS"; }

    # 6a already uses eMMC (no resize): guard runs, DONE set, rc 0
    reset_shims "$GOOD_FX"; printf '8000000000\n' > "$T/blockdev.part"; printf '8000000000\n' > "$T/blockdev.disk"
    run_start; rc=$?
    if [ "$rc" = 0 ] && [ -e "$DONE_F" ] && grep -q 'nothing to resize' "$SLOG" \
       && grep -q 'primary superblock checksum OK' "$SLOG" && [ "$(count_calls '^fsfreeze -f /')" = 1 ] \
       && [ ! -e "$T/done-before-freeze" ] && [ "$(count_calls '^resize2fs')" = 0 ] \
       && result_matches 'OK stored=0xf8449046 computed=0xf8449046 attempts=1' && [ "$(last_call)" = "$FINAL_SYNC" ]; then
        ok "6a already-uses-eMMC path: guard ran (freeze before DONE), DONE set, rc=0, verdict OK, LAST call = fsync of verdict+DONE+log+dir"
    else
        bad "6a rc=$rc done=$(has_file "$DONE_F") freezes=$(count_calls '^fsfreeze -f /') done-before-freeze=$(has_file "$T/done-before-freeze") result='$(result_flat)' last='$(last_call)' out='$(log_flat "$T/start.out")'"
    fi

    # 6b same path, checksum BAD twice: rc 1 AND DONE still set (no re-run next boot), marker set
    reset_shims "$BAD_FX"
    run_start; rc=$?
    if [ "$rc" = 1 ] && [ -e "$DONE_F" ] && [ -e "$MARK" ] && grep -q 'checksum BAD stored=0x8c9e9dcb .* (attempt 2)' "$SLOG" \
       && result_matches 'BAD stored=0x8c9e9dcb computed=0xe6961c00 attempts=2' && [ "$(last_call)" = "$FINAL_SYNC" ]; then
        ok "6b BAD twice: unit fails (rc=1) but DONE is still set, marker + BAD verdict on disk, and the final fsync still runs"
    else
        bad "6b rc=$rc done=$(has_file "$DONE_F") marker=$(has_file "$MARK") result='$(result_flat)' last='$(last_call)' out='$(log_flat "$T/start.out")'"
    fi

    # 6c resize path: resize2fs, THEN the freeze cycle, THEN DONE
    reset_shims "$GOOD_FX"; printf '1000000000\n' > "$T/blockdev.part"
    run_start; rc=$?
    order=$(grep -E '^(resize2fs|fsfreeze -f)' "$CALLS" | tr '\n' ';')
    if [ "$rc" = 0 ] && [ "$order" = "resize2fs $T/dev/p2;fsfreeze -f /;" ] && [ -e "$DONE_F" ] \
       && [ ! -e "$T/done-before-freeze" ] && grep -q 'primary superblock checksum OK' "$SLOG" \
       && result_matches 'OK stored=0xf8449046 computed=0xf8449046 attempts=1' && [ "$(last_call)" = "$FINAL_SYNC" ]; then
        ok "6c resize path: resize2fs → freeze cycle → verify OK → DONE → final fsync (last call), rc=0"
    else
        bad "6c rc=$rc order='$order' done=$(has_file "$DONE_F") result='$(result_flat)' last='$(last_call)' out='$(log_flat "$T/start.out")'"
    fi

    # 6d DONE present: nothing runs (idempotent re-run)
    reset_shims "$GOOD_FX"; : > "$DONE_F"
    run_start; rc=$?
    [ "$rc" = 0 ] && [ "$(count_calls '^fsfreeze')" = 0 ] && [ "$(count_calls '^dd ')" = 0 ] && [ ! -e "$RES" ] \
        && ok "6d DONE already set: no freeze, no read, no verdict written, rc=0 (re-run is a no-op)" \
        || bad "6d rc=$rc calls='$(tr '\n' ';' < "$CALLS")'"

    # 6e a stale verdict temp file (a cut between the temp write and the rename
    # commit) with DONE already set: removed at start, the rest still a no-op —
    # the ONLY path where nothing else would ever consume it (1.0.6.48 review A2)
    reset_shims "$GOOD_FX"; : > "$DONE_F"; printf 'stale\n' > "$RES.tmp"
    run_start; rc=$?
    [ "$rc" = 0 ] && [ ! -e "$RES.tmp" ] && [ "$(count_calls '^fsfreeze')" = 0 ] && [ ! -e "$RES" ] \
        && ok "6e stale .result.tmp + DONE set: the temp file is removed at start, no freeze, no verdict, rc=0" \
        || bad "6e rc=$rc tmp-left=$(has_file "$RES.tmp") freezes=$(count_calls '^fsfreeze') result='$(result_flat)'"

    # 6f the same stale temp file on the already-uses-eMMC path: gone afterwards,
    # the verdict still one line (holds on both trees — write_result overwrites
    # and renames it; kept so the rm cannot be moved past the DONE check)
    reset_shims "$GOOD_FX"; printf 'stale\n' > "$RES.tmp"; printf '8000000000\n' > "$T/blockdev.part"
    run_start; rc=$?
    [ "$rc" = 0 ] && [ ! -e "$RES.tmp" ] && result_matches 'OK stored=0xf8449046 computed=0xf8449046 attempts=1' \
        && ok "6f stale .result.tmp on the no-resize path: gone, verdict OK on one line, rc=0" \
        || bad "6f rc=$rc tmp-left=$(has_file "$RES.tmp") result='$(result_flat)'"
fi

echo
if [ "$fails" -eq 0 ]; then echo "firstboot-sb-csum: ALL OK"; exit 0; fi
echo "firstboot-sb-csum: $fails FAILURE(S)"; exit 1
