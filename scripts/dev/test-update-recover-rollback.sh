#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
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

# ── Stubs for the helpers rollback_from_journal calls ──────────────────────
STATEDIR="$T/state"
ARCHIVE="$STATEDIR/rollback/pre-update-TXN.tar.gz"
log()                  { :; }
utc_now()              { echo 1970-01-01T00:00:00Z; }
txn_patch()            { :; }
cleanup_imaging_lock() { :; }
txn_get()              { case "${1:-}" in rollback_archive) printf '%s\n' "$ARCHIVE" ;; *) : ;; esac; }

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

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
