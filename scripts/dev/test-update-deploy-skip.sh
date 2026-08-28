#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped code in a sandbox (files written, shim invocations, exit codes), so a commented-out line changes the measured behaviour instead of hiding behind a needle grep; its source-text greps are extraction/retarget sanity guards on its own scratch copy, which abort the run when the shipped block moves.
# test-update-deploy-skip.sh — regression for the deploy-skip guard in
# etc/sa02m-update-runner.sh apply_deploy_items() (+ its is_unchanged predicate).
# Quality row `update-deploy-skip`.
#
# Why: a web update re-installed ALL ~344 deploy files every run — cp -a backup +
# install-to-tmp + 2x fsync + a python3 journal spawn PER FILE — even when a patch
# release changed only a handful (Operator: "обновление ставит все 300+ файлов
# заново, даже если изменились единицы"). The fix skips a file already installed
# IDENTICALLY on all three axes (content AND mode AND owner) with no backup, no
# journal line, no install — while a right-content wrong-mode file (0644-vs-0755)
# still deploys, and any doubt fails safe to deploying.
#
# Method: extract the SHIPPED apply_deploy_items / is_unchanged / atomic_install_file
# / journal_append / rollback_from_journal (single-function slices, like
# test-update-recover-rollback.sh), stub only the txn/log/manifest helpers, and run
# a mixed deploy against a sandbox manifest whose owner axis is set to the test
# user's own id (the harness runs NON-root). Nothing touches the real filesystem,
# no root, no device. Requires python3 (the runner's own manifest/journal parsing
# uses it, and so do the extracted journal_append / atomic_install_file).
#
# txn_patch/txn_get are FILE-backed here: the deploy loop runs in a `( subshell )`
# under production shell options, so a shell-variable store would not survive to
# the files_done/files_total assertion — the file does.
#
# Drive-to-failure: UPDATE_RUNNER_SRC=<(git show main:etc/sa02m-update-runner.sh) \
#   bash scripts/dev/test-update-deploy-skip.sh   → the skip assertion goes RED
#   (the pre-fix runner has no is_unchanged, so it journals `replace`, backs up,
#   and rewrites the unchanged file). The changed-content and wrong-mode
#   assertions are the OVER-skip tripwires: a fix that skipped a file it should
#   deploy turns them RED. The pre-fix runner defines no is_unchanged, so this
#   harness extracts it only when present (see HAS_GUARD) — the drive-to-failure
#   run must reach and FAIL the assertions, not abort on a missing marker.
#
# Run: bash scripts/dev/test-update-deploy-skip.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC="${UPDATE_RUNNER_SRC:-etc/sa02m-update-runner.sh}"
command -v python3 >/dev/null 2>&1 || { echo "SKIP  python3 unavailable (runner requires it)"; exit 0; }
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

# Materialise SRC to a regular file ONCE, then read only the copy. UPDATE_RUNNER_SRC
# is a process substitution (<(git show main:...)) in the drive-to-failure run — a
# non-seekable pipe that yields data on the FIRST read only. This harness reads the
# source several times (the guard-presence grep + one awk per extracted function),
# so it must snapshot the pipe up front or every read after the first sees nothing.
cat "$SRC" > "$T/runner.sh" 2>/dev/null || { echo "FAIL  cannot read runner source: $SRC"; exit 1; }
SRC="$T/runner.sh"

# POSIX-faithfulness probe. This is a FUNCTIONAL test of the Linux deploy path: the
# skip decision and the assertions turn on real file mode/owner semantics. Some
# sandboxes cannot represent them — Git Bash on Windows collapses the mode model
# (`install -m 0755` yields 644, a .sh chmod 644 reports 755), so the mode axis
# (assertion 3) and the journal-restore rollback (assertion 5) cannot be exercised
# faithfully. On such a host the test SKIPs (exit 0) rather than emit false
# failures — the same posture as the python3 guard above. Run it under WSL/Linux
# (or in CI on the device toolchain) for the full RED->GREEN coverage. The probe is
# `install -m 0755` -> mode 755, the exact operation assertion 3 verifies.
_probe="$T/.mode-probe"; printf 'x' > "$_probe.src"
install -m 0755 "$_probe.src" "$_probe.dst" 2>/dev/null
if [ "$(stat -c '%a' "$_probe.dst" 2>/dev/null)" != "755" ]; then
    echo "SKIP  sandbox filesystem cannot represent POSIX modes (install -m 0755 != 755, e.g. Git Bash on Windows) — run under WSL/Linux for full coverage"
    exit 0
fi

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── Extract each shipped function (start marker → first column-0 close) ──────
# LITERAL prefix match (index==1), not a dynamic `$0 ~ "^"fn"\\(\\)..."` regex:
# a string-built regex makes the awk escape `\(` implementation-defined (mawk vs
# gawk disagree — mawk reads it as a group and the extraction silently yields
# zero lines), so the fn-name must be matched as a plain literal to stay portable.
# The sibling test-update-recover-rollback.sh sidesteps the same trap with a
# static regex literal; a parameterised extractor cannot, so it matches literally.
extract() {
    awk -v start="$1() {" 'index($0,start)==1{f=1} f{print} f&&/^\}/{exit}' "$SRC"
}

# is_unchanged is the guard THIS change adds; the pre-fix runner (main) has none.
# Extract it only when the source defines it, so the drive-to-failure run still
# RUNS the assertions against the pre-fix apply loop (which deploys the unchanged
# file → the skip assertion goes RED) instead of aborting on a missing marker.
HAS_GUARD=0
grep -q '^is_unchanged() {' "$SRC" && HAS_GUARD=1

funcs="apply_deploy_items journal_append atomic_install_file rollback_from_journal"
[ "$HAS_GUARD" = "1" ] && funcs="is_unchanged $funcs"
for fn in $funcs; do
    extract "$fn" >> "$T/fn.sh"
    grep -q "^$fn() {" "$T/fn.sh" \
        || { echo "FAIL  could not extract $fn() from $SRC — the marker moved; fix this harness, do not delete it"; exit 1; }
done
# When the source HAS the guard, the extracted apply loop MUST call it — else the
# extraction range silently dropped it and every skip assertion is vacuous.
if [ "$HAS_GUARD" = "1" ]; then
    grep -q 'is_unchanged "' "$T/fn.sh" \
        || { echo "FAIL  extracted apply_deploy_items has no is_unchanged guard — extraction range broke"; exit 1; }
fi

# ── Stubs for the helpers the extracted functions call ──────────────────────
STATEDIR="$T/state"
TXN="TXN"
JOURNAL="$STATEDIR/staging/$TXN/journal.jsonl"
TXNVARS_F="$T/txnvars"; : > "$TXNVARS_F"
log()                  { :; }
utc_now()              { echo 1970-01-01T00:00:00Z; }
cleanup_imaging_lock() { :; }
manifest_path()        { printf '%s\n' "$STATEDIR/staging/$1/meta/manifest.json"; }
# FILE-backed key=value store: the deploy loop runs in a `( subshell )`, so the
# counters it patches must land in a file to be readable by the assertion below.
txn_patch() { local kv; for kv in "$@"; do printf '%s\n' "$kv" >> "$TXNVARS_F"; done; }
txn_get()   {
    local k=${1:-}; [ -n "$k" ] || return 0
    local v; v=$(grep "^$k=" "$TXNVARS_F" 2>/dev/null | tail -n1)
    printf '%s\n' "${v#*=}"
}

# shellcheck disable=SC1090
. "$T/fn.sh"

# ── Fixture: an overlay (staged new files) + live dst tree + a manifest ─────
OV="$STATEDIR/staging/$TXN/overlay"
LIVE="$T/live"
mkdir -p "$OV" "$LIVE" "$STATEDIR/staging/$TXN/meta" "$STATEDIR/staging/$TXN/backups"

# item A — UNCHANGED: identical content, mode 0644, owner matches  → must be SKIPPED
printf 'alpha content\n' > "$OV/a.conf";            chmod 644 "$OV/a.conf"
printf 'alpha content\n' > "$LIVE/a.conf";          chmod 644 "$LIVE/a.conf"
# item B — CHANGED CONTENT: dst differs → must DEPLOY (replace journal + backup)
printf 'beta NEW content\n'  > "$OV/b.conf";        chmod 644 "$OV/b.conf"
printf 'beta OLD content\n'  > "$LIVE/b.conf";      chmod 644 "$LIVE/b.conf"
# item C — WRONG MODE: identical content, dst 0644 vs manifest 0755 → must DEPLOY
printf '#!/bin/sh\nexit 0\n' > "$OV/c.sh";          chmod 755 "$OV/c.sh"
printf '#!/bin/sh\nexit 0\n' > "$LIVE/c.sh";        chmod 644 "$LIVE/c.sh"
# item D — EMPTY-OWNER UNCHANGED: identical content+mode, manifest owner "" → SKIP
printf 'delta content\n' > "$OV/d.conf";            chmod 644 "$OV/d.conf"
printf 'delta content\n' > "$LIVE/d.conf";          chmod 644 "$LIVE/d.conf"

# Owner axis: use what `stat -c '%U:%G'` ACTUALLY reports for the live file, not
# `id -un:id -gn`. The predicate compares its own `stat -c '%U:%G' "$dst"` to the
# manifest owner, so a stat round-trip is the only value guaranteed to MATCH on
# every platform (a Windows/Git-Bash sandbox where `id -gn` fails to resolve the
# group, or any FS that reports a synthetic owner, would otherwise wrongly force
# item A to deploy). This still exercises the "owner matches ⇒ skip permitted"
# direction faithfully; the non-root sandbox cannot exercise a real chown mismatch
# (named residual in the plan's adversary pass), so no mismatch item is asserted.
OWNER="$(stat -c '%U:%G' "$LIVE/a.conf")"

cat > "$STATEDIR/staging/$TXN/meta/manifest.json" <<JSON
{
  "schema_version": 1,
  "version": "9.9.9.9",
  "deploy": [
    {"src": "a.conf", "dst": "$LIVE/a.conf", "mode": "0644", "owner": "$OWNER"},
    {"src": "b.conf", "dst": "$LIVE/b.conf", "mode": "0644", "owner": "$OWNER"},
    {"src": "c.sh",   "dst": "$LIVE/c.sh",   "mode": "0755", "owner": "$OWNER"},
    {"src": "d.conf", "dst": "$LIVE/d.conf", "mode": "0644", "owner": ""}
  ]
}
JSON

# Snapshot the two SKIP candidates' inode+mtime so we can prove they were untouched.
a_before=$(stat -c '%i %Y' "$LIVE/a.conf")
d_before=$(stat -c '%i %Y' "$LIVE/d.conf")
sleep 1   # ensure any rewrite would move mtime (1s stat granularity)

# ── Run the shipped deploy loop under production shell options ──────────────
( set -euo pipefail; apply_deploy_items "$TXN" ) >/dev/null 2>&1
rc=$?
[ "$rc" -eq 0 ] && ok "apply_deploy_items returned 0" \
                 || bad "apply_deploy_items FAILED (rc=$rc)"

# Count journal lines naming a given dst. `grep -c` prints "0" AND exits 1 on no
# match, so a `... || echo 0` fallback would emit a SECOND "0" ("0\n0") that never
# equals "0" — silently failing every skip assertion. Capture the count and swallow
# grep's zero-match exit instead.
jrl() {
    local n=0
    if [ -f "$JOURNAL" ]; then
        n=$(grep -c "\"dst\": \"$1\"" "$JOURNAL" 2>/dev/null) || :
    fi
    printf '%s\n' "$n"
}

# 1. SKIP IS REAL — unchanged item A: no journal line, no backup, inode+mtime same.
a_after=$(stat -c '%i %Y' "$LIVE/a.conf")
skip_ok=1
[ "$(jrl "$LIVE/a.conf")" = "0" ] || { skip_ok=0; }
[ "$a_before" = "$a_after" ]      || { skip_ok=0; }
# no backup taken for A (backups dir empty or without A's content)
if [ "$skip_ok" = "1" ] && ! grep -rql 'alpha content' "$STATEDIR/staging/$TXN/backups" 2>/dev/null; then
    ok "unchanged file SKIPPED (no journal line, no backup, dst untouched)"
else
    bad "unchanged file was NOT skipped (journalled/backed-up/rewritten) — the whole regression"
fi
# empty-owner unchanged item D also skipped
d_after=$(stat -c '%i %Y' "$LIVE/d.conf")
if [ "$(jrl "$LIVE/d.conf")" = "0" ] && [ "$d_before" = "$d_after" ]; then
    ok "empty-owner unchanged file SKIPPED (content+mode axis only)"
else
    bad "empty-owner unchanged file was NOT skipped"
fi

# 2. CHANGED CONTENT STILL DEPLOYS — item B updated AND a replace journal line.
if [ "$(cat "$LIVE/b.conf")" = "beta NEW content" ] && [ "$(jrl "$LIVE/b.conf")" -ge 1 ]; then
    ok "changed-content file DEPLOYED (new content + replace journal line)"
else
    bad "changed-content file was over-skipped (content or journal missing) — over-skip"
fi

# 3. WRONG-MODE NOT SKIPPED — item C re-deployed, mode corrected to 0755.
c_mode=$(stat -c '%a' "$LIVE/c.sh")
if [ "$c_mode" = "755" ] && [ "$(jrl "$LIVE/c.sh")" -ge 1 ]; then
    ok "wrong-mode file RE-DEPLOYED, mode corrected to 0755 (0644-vs-0755 trap)"
else
    bad "wrong-mode file was over-skipped (mode still $c_mode) — the 0644-vs-0755 class"
fi

# 4. files_done == files_total after the mixed run (recover-verify invariant).
fd=$(txn_get files_done); ft=$(txn_get files_total)
if [ -n "$ft" ] && [ "$ft" = "4" ] && [ "$fd" = "$ft" ]; then
    ok "files_done == files_total ($fd/$ft) with skips counted"
else
    bad "files_done/files_total wrong (done=$fd total=$ft) — skip broke progress accounting"
fi

# 5. ROLLBACK SURVIVES A SKIPPED FILE — drive rollback over the produced journal:
#    the CHANGED file (B) restores to its pre-update content; the SKIPPED file (A)
#    is untouched (no phantom replace entry restoring a non-existent backup over it).
( set -euo pipefail; rollback_from_journal "$TXN" ) >/dev/null 2>&1
rb_ok=1
[ "$(cat "$LIVE/b.conf")" = "beta OLD content" ] || rb_ok=0   # changed file rolled back
[ "$(cat "$LIVE/a.conf")" = "alpha content" ]    || rb_ok=0   # skipped file intact
if [ "$rb_ok" = "1" ]; then
    ok "rollback restores the changed file and leaves the skipped file intact (trap 5)"
else
    bad "rollback mishandled a skipped file (changed not restored, or skipped clobbered)"
fi

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
