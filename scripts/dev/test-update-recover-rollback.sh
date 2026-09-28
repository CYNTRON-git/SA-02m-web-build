#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped code in a sandbox (files written, shim invocations, exit codes), so a commented-out line changes the measured behaviour instead of hiding behind a needle grep; its source-text greps are extraction/retarget sanity guards on its own scratch copy, which abort the run when the shipped block moves.
# test-update-recover-rollback.sh — regression for the power-loss recovery path
# in etc/sa02m-update-runner.sh rollback_from_journal() (the archive fallback).
# Quality row `update-recover-rollback`.
#
# Why: this is exactly the path a freshly-flashed board runs on first boot when
# the golden image carries an interrupted update (stage=rolling_back). TWO bugs
# made it fail every boot ("plata pingaetsya posle zalivki, no umiraet posle
# perezagruzki", golden board 1.136, 2026-08-21):
#   (a) mktemp -d "$STATEDIR/staging/$txn/rollback-extract.XXXXXX" failed with
#       "No such file or directory" because staging/$txn is gone after the power
#       loss (tmpfs staging) — so recover exited 1 in the very case it exists for;
#   (b) `install -m 644` hardcoded mode 644 for EVERY restored file, stripping
#       exec bits (systemd 203/EXEC on ~37 scripts) and loosening 640/600 perms.
#
# Method: extract the SHIPPED rollback_from_journal() (single-function slice,
# like test-port-lease-gate.sh extracts its dispatch prefix), stub its helpers,
# build a real rollback archive of absolute sandbox paths (same tar recipe as
# build_rollback_archive), and run the archive fallback against a STATEDIR whose
# staging/$txn is ABSENT. Asserts (a) it succeeds and (b) a restored executable
# keeps its +x bit. The archive's leading-slash-stripped members restore back
# INTO the sandbox — nothing touches the real filesystem, no root, no device.
#
# Drive-to-failure: UPDATE_RUNNER_SRC=<(git show main:etc/sa02m-update-runner.sh) \
#   bash scripts/dev/test-update-recover-rollback.sh   → both cases go RED
#   (pre-fix mktemp aborts under set -e; pre-fix install -m 644 drops +x).
#
# Cases (c)-(g), 1.0.6.60: the archive restore is ATOMIC. Until then the
# fallback wrote each member with a bare `install -m … "$f" "$rel"` — a
# truncate-then-fill of a LIVE path (/usr/local/**, /etc/systemd/system/** are
# exactly what the archive holds) on the one path that runs when the board is
# already mid-failure — while the journal replay above it had been atomic since
# 1.0.6.54, and while the very same file defines atomic_install_file(). Now each
# member goes through that helper (tmp beside the target → fdatasync →
# rename-over → dir fsync), a failed member is counted and the outcome is
# «rollback incomplete» (stage=error), never rolled_back over a partly restored
# tree — the contract the function's own header states. Failure injection and
# the fsync-order trace use test-update-deploy-skip.sh's idiom (same-named
# shell functions over `install` / `mv` / `sync`); `install` is TORN (8 bytes of
# the source land in the target, then it dies — a copy killed mid-body, the
# incident's own shape), `mv` refuses, `sync -d` refuses.
# RED on the pre-1.0.6.60 runner, observed 2026-09-28 (Windows git-bash), 5
# FAILURES: (c) torn copy — rc=1 (the errexit death in the `find | while`
# subshell), the live plain.conf left as the 8-byte prefix "plain pa" (BROKEN),
# and no stage recorded at all (stage=rolling_back;result=pending was the last
# patch); (d) refused rename and (g) refused fdatasync — a bare `install` has
# no mv and no sync -d, so neither injection fired: rc=0, the member restored
# by truncate-then-fill, stage=rolled_back; (f) order — an EMPTY trace. (e)
# passed on both. GREEN after the conversion, same host.
#
# Run: bash scripts/dev/test-update-recover-rollback.sh   (bash + tar + stat +
#   install + find; no flock/systemctl/python — the runner's lock/systemd path
#   is not exercised by this function).
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC="${UPDATE_RUNNER_SRC:-etc/sa02m-update-runner.sh}"
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── Extract the shipped function (start marker → first column-0 close) ──────
awk '/^rollback_from_journal\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$SRC" > "$T/fn.sh"
grep -q '^rollback_from_journal() {' "$T/fn.sh" \
    || { echo "FAIL  could not extract rollback_from_journal() from $SRC — the marker moved; fix this harness, do not delete it"; exit 1; }
grep -q 'rollback-extract' "$T/fn.sh" \
    || { echo "FAIL  extracted body has no archive-fallback (rollback-extract) — extraction range broke"; exit 1; }
[ "$(tail -n1 "$T/fn.sh")" = "}" ] \
    || { echo "FAIL  extraction did not stop at the function's closing brace"; exit 1; }
# The archive fallback restores through atomic_install_file() (cases (c)-(g)),
# defined elsewhere in the same file: extract it too. Its absence is a FAIL,
# not a skip — the fallback would then call an undefined helper on the board.
awk '/^atomic_install_file\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$SRC" > "$T/aif.sh"
grep -q '^atomic_install_file() {' "$T/aif.sh" \
    || bad "$SRC defines no atomic_install_file() — the archive fallback has no atomic helper to restore through"

# ── Stubs for the helpers rollback_from_journal calls ──────────────────────
STATEDIR="$T/state"
ARCHIVE="$STATEDIR/rollback/pre-update-TXN.tar.gz"
TXNPATCH="$T/txnpatch"; : > "$TXNPATCH"
log()                  { :; }
utc_now()              { echo 1970-01-01T00:00:00Z; }
txn_patch()            { printf '%s\n' "$@" >> "$TXNPATCH"; }
cleanup_imaging_lock() { :; }
txn_get()              { case "${1:-}" in rollback_archive) printf '%s\n' "$ARCHIVE" ;; *) : ;; esac; }

# ── Failure injection + fsync-order trace (test-update-deploy-skip.sh idiom) ─
# `install`, `mv` and `sync` are resolved by name inside the extracted
# functions, so a same-named shell function sees every call with its argv.
# `install` TEARS one destination (8 bytes of the source, then rc=1); `mv`
# refuses one destination; `sync` records argv plus the first bytes the probed
# live file holds at call time (the ORDER proof) and may refuse a `-d` on one
# path. The real `sync` is forwarded but its status is not read: on Cygwin/MSYS
# fdatasync fails with EACCES on the read-only fd coreutils opens, which would
# turn every case RED on a Windows dev box for a host reason; durability is not
# observable from a harness anyway — the order and the refusal path are.
INSTALL_TORN_ON=""; MV_FAIL_ON=""; SYNC_FAIL_D_ON=""; LIVE_PROBE=""
TRACE="$T/fsync.trace"; : > "$TRACE"
install() {
    if [ -n "$INSTALL_TORN_ON" ] && [ "$#" -ge 2 ] && [[ "${*: -1}" == *"$INSTALL_TORN_ON"* ]]; then
        head -c 8 "${*: -2:1}" > "${*: -1}" 2>/dev/null; return 1
    fi
    command install "$@"
}
mv() {
    printf 'mv %s\n' "$*" >> "$TRACE"
    if [ -n "$MV_FAIL_ON" ] && [ "$#" -ge 1 ] && [[ "${*: -1}" == *"$MV_FAIL_ON"* ]]; then return 1; fi
    command mv "$@"
}
sync() {
    printf 'sync %s | live=%s\n' "$*" "$(head -c 3 "$LIVE_PROBE" 2>/dev/null)" >> "$TRACE"
    if [ -n "$SYNC_FAIL_D_ON" ] && [ "${1:-}" = -d ] && [[ "${*: -1}" == *"$SYNC_FAIL_D_ON"* ]]; then return 1; fi
    command sync "$@" >/dev/null 2>&1 || :
}

# shellcheck disable=SC1090
. "$T/aif.sh"
# shellcheck disable=SC1090
. "$T/fn.sh"

# ── Fixture: files to back up, restored back into the sandbox on recover ────
# Absolute paths under $T; build_rollback_archive stores them with the leading
# '/' stripped, so rollback_from_journal restores them to the same $T paths.
RESTORE="$T/restore"
mkdir -p "$RESTORE"
printf '#!/bin/sh\nexit 0\n' > "$RESTORE/exec.sh";  chmod 755 "$RESTORE/exec.sh"
printf 'plain payload\n'      > "$RESTORE/plain.conf"; chmod 644 "$RESTORE/plain.conf"

mkdir -p "$STATEDIR/rollback"
printf '%s\n' "$RESTORE/exec.sh" "$RESTORE/plain.conf" > "$T/list"
tar -czf "$ARCHIVE" --verbatim-files-from -T "$T/list" 2>/dev/null

# The update "replaced" the live files: remove them so recover must restore.
rm -f "$RESTORE/exec.sh" "$RESTORE/plain.conf"

# ensure_dirs (called by cmd_recover before rollback) creates staging; the
# power loss lost staging/$txn. Reproduce exactly that: parent present, txn gone.
mkdir -p "$STATEDIR/staging"
[ ! -e "$STATEDIR/staging/TXN" ] || bad "precondition: staging/TXN should be absent"
[ -f "$ARCHIVE" ] || bad "precondition: rollback archive was not built"

# ── Run the shipped fallback with production shell options in a subshell ────
( set -euo pipefail; rollback_from_journal "TXN" ) 2>/dev/null
rc=$?

# (a) recover succeeds even though staging/$txn is absent (Fix 1)
[ "$rc" -eq 0 ] && ok "recover succeeds with staging/\$txn absent (rc=0)" \
                 || bad "recover FAILED with staging/\$txn absent (rc=$rc) — mktemp parent regression"

# (b) a restored executable keeps its +x bit (Fix 2)
if [ -x "$RESTORE/exec.sh" ]; then
    ok "restored executable keeps +x (mode $(stat -c '%a' "$RESTORE/exec.sh"))"
else
    bad "restored executable lost +x — install -m 644 mode-strip regression"
fi
[ -f "$RESTORE/plain.conf" ] && ok "restored non-executable present" \
                              || bad "restored non-executable missing"

# The extraction temp dir must be cleaned up (rm -rf "$tmp").
if find "$STATEDIR/staging" -maxdepth 1 -name 'rollback-extract.*' 2>/dev/null | grep -q .; then
    bad "rollback-extract temp dir left behind under staging/"
else
    ok "rollback-extract temp dir cleaned up"
fi

# ── (c)-(g): the archive restore is atomic — old-or-new, never a torn live file ──
# The update REPLACED the live files (post-update bytes on disk); recover must
# bring the archived pre-update bytes back. A torn or refused restore of ONE
# member leaves that member's live bytes exactly as they were, still restores
# the other, leaves no tmp behind, and ends in «rollback incomplete»
# (stage=error) — never rolled_back over a partly restored tree.
ARCHIVED_PLAIN='plain payload'
seed_live() {
    printf 'LIVE post-update exec\n'    > "$RESTORE/exec.sh"
    printf 'LIVE post-update payload\n' > "$RESTORE/plain.conf"
    : > "$TRACE"; : > "$TXNPATCH"
}
live_plain()   { cat "$RESTORE/plain.conf" 2>/dev/null; }
tmp_left()     { ls "$RESTORE"/*.tmp.* >/dev/null 2>&1; }
stage_is()     { grep -qx "stage=$1" "$TXNPATCH" 2>/dev/null; }
incomplete_recorded() { grep -q '^error_message=rollback incomplete (' "$TXNPATCH" 2>/dev/null; }
run_fallback() { ( set -euo pipefail; rollback_from_journal "TXN" ) 2>/dev/null; }
LIVE_PROBE="$RESTORE/plain.conf"

# (c) torn copy of plain.conf
seed_live; INSTALL_TORN_ON="plain.conf"
run_fallback; rc=$?
INSTALL_TORN_ON=""
if [ "$rc" -eq 0 ] && [ "$(live_plain)" = "LIVE post-update payload" ] && ! tmp_left; then
    ok "(c) torn copy: the live plain.conf is untouched (never the 8-byte prefix), rc=$rc, no tmp left"
else
    bad "(c) torn copy: rc=$rc live='$(live_plain)' dir='$(ls "$RESTORE" | tr '\n' ' ')'"
fi
[ "$(cat "$RESTORE/exec.sh" 2>/dev/null)" = "$(printf '#!/bin/sh\nexit 0')" ] \
    && ok "(c) …and the other member (exec.sh) was still restored" \
    || bad "(c) the other member was not restored — one failed member aborted the whole restore"
if stage_is error && incomplete_recorded && ! stage_is rolled_back; then
    ok "(c) …and the outcome is stage=error «rollback incomplete», not rolled_back"
else
    bad "(c) outcome recorded: $(tr '\n' ';' < "$TXNPATCH") — expected stage=error + 'rollback incomplete (…)'"
fi

# (d) refused rename of plain.conf
seed_live; MV_FAIL_ON="plain.conf"
run_fallback; rc=$?
MV_FAIL_ON=""
if [ "$rc" -eq 0 ] && [ "$(live_plain)" = "LIVE post-update payload" ] && ! tmp_left && stage_is error && incomplete_recorded; then
    ok "(d) refused rename: live plain.conf untouched, rc=$rc, no tmp, stage=error «rollback incomplete»"
else
    bad "(d) refused rename: rc=$rc live='$(live_plain)' dir='$(ls "$RESTORE" | tr '\n' ' ')' patched='$(tr '\n' ';' < "$TXNPATCH")'"
fi

# (e)/(f) success: both members restored, rolled_back, and the ORDER from the
# trace — fdatasync of the tmp while the live file still reads the post-update
# bytes ("LIV"), the rename, then the directory fsync once it reads the archived
# bytes ("pla").
seed_live
run_fallback; rc=$?
if [ "$rc" -eq 0 ] && [ "$(live_plain)" = "$ARCHIVED_PLAIN" ] && [ -x "$RESTORE/exec.sh" ] && ! tmp_left && stage_is rolled_back; then
    ok "(e) success: both members restored from the archive, rc=0, no tmp, stage=rolled_back"
else
    bad "(e) success: rc=$rc live='$(live_plain)' exec-x=$([ -x "$RESTORE/exec.sh" ] && echo yes || echo no) dir='$(ls "$RESTORE" | tr '\n' ' ')' patched='$(tr '\n' ';' < "$TXNPATCH")'"
fi
l_tmp=$(grep -n -E "^sync -d -- $RESTORE/plain\.conf\.tmp\.[0-9]+ \| live=LIV\$" "$TRACE" 2>/dev/null | head -1 | cut -d: -f1)
l_mv=$(grep -n -E "^mv -f $RESTORE/plain\.conf\.tmp\.[0-9]+ $RESTORE/plain\.conf\$" "$TRACE" 2>/dev/null | head -1 | cut -d: -f1)
l_dir=$(grep -n -E "^sync -- $RESTORE \| live=pla\$" "$TRACE" 2>/dev/null | head -1 | cut -d: -f1)
if [ -n "$l_tmp" ] && [ -n "$l_mv" ] && [ -n "$l_dir" ] && [ "$l_tmp" -lt "$l_mv" ] && [ "$l_mv" -lt "$l_dir" ]; then
    ok "(f) order: 'sync -d -- <tmp>' with the live file still post-update (line $l_tmp) → 'mv -f <tmp> <live>' ($l_mv) → 'sync -- <dir>' with it archived ($l_dir)"
else
    bad "(f) order: expected sync -d <tmp> (live=LIV) → mv -f → sync <dir> (live=pla); trace: $(tr '\n' ';' < "$TRACE")"
fi

# (g) refused fdatasync of plain.conf's tmp — no rename over a tmp that may not
# be on disk.
seed_live; SYNC_FAIL_D_ON="plain.conf.tmp."
run_fallback; rc=$?
SYNC_FAIL_D_ON=""
if [ "$rc" -eq 0 ] && [ "$(live_plain)" = "LIVE post-update payload" ] && ! tmp_left && stage_is error && incomplete_recorded; then
    ok "(g) refused fdatasync: live plain.conf untouched, rc=$rc, no tmp, stage=error «rollback incomplete»"
else
    bad "(g) refused fdatasync: rc=$rc live='$(live_plain)' dir='$(ls "$RESTORE" | tr '\n' ' ')' patched='$(tr '\n' ';' < "$TXNPATCH")'"
fi

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
