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
# / journal_append / rollback_from_journal / stamp_runner_version_after_deploy
# (single-function slices, like test-update-recover-rollback.sh), stub only the
# txn/log/manifest helpers, and run a mixed deploy against a sandbox manifest
# whose owner axis is set to the test user's own id (the harness runs NON-root).
# Nothing touches the real filesystem, no root, no device. Requires python3 (the
# runner's manifest emit and journal replay use it; the harness's own JSON
# checks too). Case 4b/5b (1.0.6.40, item 6) rides
# the same fixture: the runner stamp is written through the journal when the
# manifest deploys the runner binary, left alone when it does not, and
# restored by the rollback — a pre-1.0.6.40 runner (no stamp function) FAILS 4b.
#
# txn_patch/txn_get are FILE-backed here: the deploy loop runs in a `( subshell )`
# under production shell options, so a shell-variable store would not survive to
# the files_done/files_total assertion — the file does.
#
# 1.0.6.54 (fast OTA — CHANGELOG 1.0.6.54) — the 35-minute deploy was ~2,100
# python3 starts, four per manifest item to read four JSON fields plus three per
# changed file. Cases added on the same extraction:
#   4c/4d  progress cadence is TIME-based (SA02M_UPDATE_PROGRESS_S, exported 0
#          here so every item patches) and the final files_done==files_total
#          patch is issued exactly once, after the loop;
#   6      fsync ORDER per changed file, from `sync`/`mv` function shims that
#          trace their argv: sync -d <journal> → sync -d <tmp> → mv → sync <dir>
#          — the journal line is durable BEFORE the rename it describes;
#   7      a dst carrying `"` and `\` gets a journal line that PARSES (json.loads)
#          and rolls back; the pre-1.0.6.54 journal_append fed the raw string to
#          json.loads and the whole apply failed (E_APPLY) on such a name;
#   8      a 100-item manifest (90 unchanged, 10 changed) under a counting
#          python3 PATH shim starts python3 at most twice in apply_deploy_items
#          (the loop starts none per item) — the info line prints the count and
#          the wall time on this host for the record;
#   9a/9b  a failed `install` / `mv -f` inside atomic_install_file FAILS the apply
#          (rc=1, `ERROR: atomic install failed: <dst>` logged, files_done stays
#          below files_total, no tmp left behind) and the journal written before
#          the failure rolls the earlier items back. Pre-existing on main (review
#          1.0.6.54 F1): the function's status was that of its last `sync … ||
#          sync`, always 0, so a disk-full/EACCES install was counted done and the
#          update committed over a mixed tree;
#   10     a NUL inside a manifest field is refused by the NUL-list emitter before
#          any file is touched (rc≠0, nothing deployed, the reason on stderr) —
#          otherwise the field list shifts and the last item lands with a garbage
#          dst/mode (review 1.0.6.54 F2).
#
# Drive-to-failure: UPDATE_RUNNER_SRC=<(git show main:etc/sa02m-update-runner.sh) \
#   bash scripts/dev/test-update-deploy-skip.sh   → the skip assertion goes RED
#   (the pre-fix runner has no is_unchanged, so it journals `replace`, backs up,
#   and rewrites the unchanged file). The changed-content and wrong-mode
#   assertions are the OVER-skip tripwires: a fix that skipped a file it should
#   deploy turns them RED. The pre-fix runner defines no is_unchanged, so this
#   harness extracts it only when present (see HAS_GUARD) — the drive-to-failure
#   run must reach and FAIL the assertions, not abort on a missing marker.
#   RED for the 1.0.6.54 cases: UPDATE_RUNNER_SRC=<(git show 3c98af0:etc/sa02m-update-runner.sh)
#   (main = 1.0.6.52) — observed 2026-09-24: 6 FAIL — 4c/4d (files_done sequence
#   `0 4 4`), 6 (no `sync -d` of the tmp file: only mv lines in the trace), 7
#   (quote-dst apply rc=1, no journal line, rollback unproven), 8 (432 python3
#   starts for 100 items: 4 per item + 3 per changed + 2 up front); every
#   original assertion stays GREEN.
#   This box (Windows git-bash) SKIPs on the mode probe below — the runs were
#   under WSL Ubuntu-24.04, where mktemp's sandbox is a native ext4.
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
# Cases 9a/9b — failure injection for atomic_install_file. `install` and `mv` are
# resolved by name inside the extracted function, so a same-named shell function
# can refuse ONE destination (the gate variables name a substring of the LAST
# argument) and forward everything else to the real binary. Defined here, above
# the first `install` call of this file (the mode probe below), so every later
# call — the probe included — goes through the forwarder. `mv` also feeds the
# case-6 trace.
INSTALL_FAIL_ON=""; MV_FAIL_ON=""
install() {
    if [ -n "$INSTALL_FAIL_ON" ] && [[ "${*: -1}" == *"$INSTALL_FAIL_ON"* ]]; then return 1; fi
    command install "$@"
}
mv() {
    printf 'mv %s\n' "$*" >> "${TRACE:-/dev/null}"
    if [ -n "$MV_FAIL_ON" ] && [[ "${*: -1}" == *"$MV_FAIL_ON"* ]]; then return 1; fi
    command mv "$@"
}

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

# stamp_runner_version_after_deploy is the 1.0.6.40 runner-stamp step (item 6);
# same extract-if-present rule: a pre-1.0.6.40 runner has none, and the
# drive-to-failure run must FAIL the stamp assertions below, not abort here.
HAS_STAMP=0
grep -q '^stamp_runner_version_after_deploy() {' "$SRC" && HAS_STAMP=1

funcs="apply_deploy_items journal_append atomic_install_file rollback_from_journal"
[ "$HAS_GUARD" = "1" ] && funcs="is_unchanged $funcs"
[ "$HAS_STAMP" = "1" ] && funcs="$funcs stamp_runner_version_after_deploy"
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
LOG="$T/runner.log"; : > "$LOG"
log()                  { printf '%s\n' "$*" >> "$LOG"; }
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
# Case 6 — fsync-order trace. The shipped code fsyncs through coreutils `sync`
# (-d FILE = fdatasync, DIR = fsync) and renames with `mv`; both are resolved by
# name inside the extracted functions, so a same-named shell function sees every
# call with its argv, records it, and forwards to the real binary. A runner that
# fsyncs through python3 (pre-1.0.6.54) leaves this trace EMPTY — the RED.
TRACE="$T/fsync.trace"; : > "$TRACE"
sync() { printf 'sync %s\n' "$*" >> "$TRACE"; command sync "$@"; }
# (`mv` is the forwarder defined above the mode probe; it traces into $TRACE.)
# Case 4c/4d — time-based progress cadence: 0 s ⇒ every item patches, so the
# patch sequence is deterministic here whatever the host's speed.
export SA02M_UPDATE_PROGRESS_S=0

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
# 4c/4d. PROGRESS CADENCE (1.0.6.54). With SA02M_UPDATE_PROGRESS_S=0 every item
#     patches, so the recorded files_done sequence is 0 (the opening patch), 1, 2,
#     3 (in-loop), 4 (the single closing patch). The every-10th-item runner
#     records `0 4 4`: no intermediate patch, and the final count twice.
fd_seq=$(grep '^files_done=' "$TXNVARS_F" | cut -d= -f2 | tr '\n' ' ')
fd_final_n=$(grep -c '^files_done=4$' "$TXNVARS_F") || :
if [ "$fd_final_n" = "1" ]; then
    ok "final files_done == files_total patch issued exactly once (sequence: $fd_seq)"
else
    bad "final files_done == files_total patch issued $fd_final_n times, want 1 (sequence: $fd_seq)"
fi
if [ "$fd_seq" = "0 1 2 3 4 " ]; then
    ok "time-based progress cadence: at SA02M_UPDATE_PROGRESS_S=0 every item patches (0 1 2 3 4)"
else
    bad "progress cadence is not time-based (sequence: ${fd_seq}want: 0 1 2 3 4 ) — the bar freezes ~40 s between moves on a 505-item tree"
fi

# 4b. RUNNER STAMP RIDES THE JOURNAL (1.0.6.40, item 6). When the manifest
#     deploys the runner binary, stamp_runner_version_after_deploy writes the
#     manifest version to $RUNNER_VERSION_FILE THROUGH a journal record, so the
#     rollback in 5 restores the pre-update stamp with the pre-update binary and
#     a board never reports a runner it rolled back from. Item C stands in for
#     the runner binary (RUNNER_BIN_DST names it); the two constants are the
#     runner's top-level ones, defined here because only functions are extracted.
RUNNER_BIN_DST="$LIVE/c.sh"
RUNNER_VERSION_FILE="$STATEDIR/runner.version"
printf '1.0.0.1\n' > "$RUNNER_VERSION_FILE"
if [ "$HAS_STAMP" = "1" ]; then
    ( set -euo pipefail; stamp_runner_version_after_deploy "$TXN" ) >/dev/null 2>&1 || :
fi
stamp_now=$(cat "$RUNNER_VERSION_FILE" 2>/dev/null)
if [ "$stamp_now" = "9.9.9.9" ] && [ "$(jrl "$RUNNER_VERSION_FILE")" -ge 1 ]; then
    ok "runner stamp written from the manifest version and journalled"
else
    bad "runner stamp not written/journalled (stamp='$stamp_now') — a board keeps reporting the old runner after a self-deploy"
fi
#     A manifest that does NOT deploy the runner leaves the stamp alone — the
#     www-only case the stamp exists for: point RUNNER_BIN_DST at a dst the
#     manifest never names, run again, expect no change and no new journal line.
stamp_lines=$(jrl "$RUNNER_VERSION_FILE")
if [ "$HAS_STAMP" = "1" ]; then
    ( set -euo pipefail; RUNNER_BIN_DST="$LIVE/not-deployed"; stamp_runner_version_after_deploy "$TXN" ) >/dev/null 2>&1 || :
fi
if [ "$(cat "$RUNNER_VERSION_FILE" 2>/dev/null)" = "$stamp_now" ] && [ "$(jrl "$RUNNER_VERSION_FILE")" = "$stamp_lines" ]; then
    ok "manifest without the runner binary leaves the stamp untouched (www-only case)"
else
    bad "stamp changed/journalled by a manifest that did not deploy the runner"
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
# 5b. ...and the pre-update runner stamp comes back with the pre-update binary.
if [ "$(cat "$RUNNER_VERSION_FILE" 2>/dev/null)" = "1.0.0.1" ]; then
    ok "rollback restores the pre-update runner stamp (4b's journal record)"
else
    bad "rollback left the NEW runner stamp behind (stamp='$(cat "$RUNNER_VERSION_FILE" 2>/dev/null)') — board over-reports a runner it rolled back from"
fi

# 6. FSYNC ORDER PER CHANGED FILE (1.0.6.54, G3). For item B the trace must read,
#    on four CONSECUTIVE lines: `sync -d -- <journal>` (the line describing B is
#    durable first), `sync -d -- <B tmp>`, `mv -f <B tmp> <B>`, `sync -- <live dir>`.
#    A power cut between any two leaves old-or-new B and a journal that already
#    names it — never a truncated B, never a NEW B the rollback does not know.
#    Capture-then-match (no producer into an early-exit pipe).
trace_all=$(cat "$TRACE" 2>/dev/null)
t_tmp=$(grep -n -- "^sync -d -- $LIVE/b.conf.tmp." "$TRACE" 2>/dev/null) || :
t_tmp=${t_tmp%%$'\n'*}; t_tmp=${t_tmp%%:*}
if [ -z "$trace_all" ]; then
    bad "no sync/mv calls traced at all — fsync goes through python3 (or nowhere), journal never synced"
elif [ -z "$t_tmp" ]; then
    bad "no 'sync -d' of B's tmp file in the trace — fdatasync before rename is not through coreutils sync"
else
    l_j=$(sed -n "$((t_tmp - 1))p" "$TRACE"); l_mv=$(sed -n "$((t_tmp + 1))p" "$TRACE"); l_d=$(sed -n "$((t_tmp + 2))p" "$TRACE")
    case "$l_j" in "sync -d -- $JOURNAL") j_ok=1 ;; *) j_ok=0 ;; esac
    case "$l_mv" in "mv -f $LIVE/b.conf.tmp."*" $LIVE/b.conf") mv_ok=1 ;; *) mv_ok=0 ;; esac
    case "$l_d" in "sync -- $LIVE") d_ok=1 ;; *) d_ok=0 ;; esac
    if [ "$j_ok$mv_ok$d_ok" = "111" ]; then
        ok "fsync order per changed file: sync -d journal → sync -d tmp → mv → sync dir (trace lines $((t_tmp - 1))-$((t_tmp + 2)))"
    else
        bad "fsync order broken around B (journal-before-tmp=$j_ok mv-after-tmp=$mv_ok dir-after-mv=$d_ok): [$l_j] [$l_mv] [$l_d]"
    fi
fi

# 7. A dst WITH `"` AND `\` (1.0.6.54, R-a2). Own fixture (TXN2): the item is a
#    replace whose journal line must parse and must roll back. Before 1.0.6.54
#    journal_append built the line by string interpolation and fed it to
#    json.loads, which raised on the unescaped quote — journal_append returned 1
#    and, under set -e, the whole apply failed (E_APPLY rollback) on such a
#    name. The manifest is written by python (json.dump) so the quoting under
#    test is the runner's, not this heredoc's.
TXN2="TXN2"; OV2="$STATEDIR/staging/$TXN2/overlay"; LIVE2="$T/live2"
mkdir -p "$OV2" "$LIVE2" "$STATEDIR/staging/$TXN2/meta" "$STATEDIR/staging/$TXN2/backups"
QNAME='q"uo\te.conf'
printf 'quoted NEW\n' > "$OV2/e.conf";        chmod 644 "$OV2/e.conf"
printf 'quoted OLD\n' > "$LIVE2/$QNAME";      chmod 644 "$LIVE2/$QNAME"
python3 - "$LIVE2/$QNAME" "$STATEDIR/staging/$TXN2/meta/manifest.json" "$OWNER" <<'PY'
import json, sys
dst, mf, owner = sys.argv[1], sys.argv[2], sys.argv[3]
m = {"schema_version": 1, "version": "9.9.9.9",
     "deploy": [{"src": "e.conf", "dst": dst, "mode": "0644", "owner": owner}]}
open(mf, "w", encoding="utf-8").write(json.dumps(m) + "\n")
PY
( set -euo pipefail; apply_deploy_items "$TXN2" ) >/dev/null 2>&1
rc2=$?
J2="$STATEDIR/staging/$TXN2/journal.jsonl"
# Parse every journal line; print the op of the record whose dst is the quoted path.
q_op=$(python3 - "$J2" "$LIVE2/$QNAME" <<'PY' 2>/dev/null
import json, sys
path, want = sys.argv[1], sys.argv[2]
try:
    lines = [ln for ln in open(path, encoding="utf-8") if ln.strip()]
except OSError:
    sys.exit(0)
for ln in lines:
    rec = json.loads(ln)          # a line that does not parse is the RED
    if rec.get("dst") == want:
        print(rec.get("op", ""), sorted(rec.keys()))
PY
) || q_op=""
if [ "$rc2" -eq 0 ] && [ "$(cat "$LIVE2/$QNAME")" = "quoted NEW" ] && [ "$q_op" = "replace ['backup', 'dst', 'mode', 'op', 'owner']" ]; then
    ok "dst with \" and \\ deploys and its journal line parses with the record keys (op,dst,backup,mode,owner)"
else
    bad "dst with \" and \\: apply rc=$rc2, content='$(cat "$LIVE2/$QNAME" 2>/dev/null)', parsed record='$q_op' — journal line missing/unparseable"
fi
( set -euo pipefail; rollback_from_journal "$TXN2" ) >/dev/null 2>&1
# Guarded by rc2: a failed apply leaves the OLD content untouched, which must not
# read as "rollback worked" (the assertion would pass for the wrong reason).
if [ "$rc2" -eq 0 ] && [ "$(cat "$LIVE2/$QNAME" 2>/dev/null)" = "quoted OLD" ]; then
    ok "rollback restores the quoted-name file from its journal record"
else
    bad "rollback of the quoted-name file not proven (apply rc=$rc2, content='$(cat "$LIVE2/$QNAME" 2>/dev/null)')"
fi

# 8. INTERPRETER STARTS PER ITEM (1.0.6.54, R-a1/G2). 100 items, 90 unchanged
#    and 10 changed, under a python3 PATH shim that counts its own starts and
#    execs the real interpreter. The deploy loop must start none per item: the
#    whole apply_deploy_items is allowed 2 (one manifest emit + slack). The
#    pre-1.0.6.54 loop starts 4 per item + 3 per changed file (+2 up front) = 432
#    here, and on the Cortex-A7 that was the 33 minutes. The wall time printed is
#    this host's, for the record — not the board's.
TXN3="TXN3"; OV3="$STATEDIR/staging/$TXN3/overlay"; LIVE3="$T/live3"
mkdir -p "$OV3" "$LIVE3" "$STATEDIR/staging/$TXN3/meta" "$STATEDIR/staging/$TXN3/backups" "$T/bin"
for i in $(seq 1 100); do
    printf 'item %s content\n' "$i" > "$OV3/f$i.conf"; chmod 644 "$OV3/f$i.conf"
    if [ "$i" -le 90 ]; then printf 'item %s content\n' "$i" > "$LIVE3/f$i.conf"
    else printf 'item %s OLD\n' "$i" > "$LIVE3/f$i.conf"; fi
    chmod 644 "$LIVE3/f$i.conf"
done
python3 - "$LIVE3" "$STATEDIR/staging/$TXN3/meta/manifest.json" "$OWNER" <<'PY'
import json, sys
live, mf, owner = sys.argv[1], sys.argv[2], sys.argv[3]
deploy = [{"src": "f%d.conf" % i, "dst": "%s/f%d.conf" % (live, i), "mode": "0644", "owner": owner} for i in range(1, 101)]
open(mf, "w", encoding="utf-8").write(json.dumps({"schema_version": 1, "version": "9.9.9.9", "deploy": deploy}) + "\n")
PY
REAL_PY=$(command -v python3); PYCOUNT="$T/py.count"; : > "$PYCOUNT"
printf '#!/bin/bash\nprintf . >> "%s"\nexec "%s" "$@"\n' "$PYCOUNT" "$REAL_PY" > "$T/bin/python3"
chmod 755 "$T/bin/python3"
t0=$EPOCHREALTIME
( set -euo pipefail; PATH="$T/bin:$PATH"; apply_deploy_items "$TXN3" ) >/dev/null 2>&1
rc3=$?
t1=$EPOCHREALTIME
py_n=$(wc -c < "$PYCOUNT" | tr -d ' ')
wall=$(python3 -c 'import sys; print("%.2f" % (float(sys.argv[2]) - float(sys.argv[1])))' "$t0" "$t1")
echo "info  apply_deploy_items over 100 items (90 unchanged / 10 changed): python3 starts=$py_n wall=${wall}s (this host, not the board)"
if [ "$rc3" -eq 0 ] && [ "$(cat "$LIVE3/f100.conf")" = "item 100 content" ] && [ "$(txn_get files_done)" = "100" ]; then
    ok "100-item mixed deploy applied (rc=0, changed file landed, files_done=100)"
else
    bad "100-item mixed deploy broken (rc=$rc3, f100='$(cat "$LIVE3/f100.conf" 2>/dev/null)', files_done=$(txn_get files_done))"
fi
if [ "$py_n" -le 2 ]; then
    ok "deploy loop starts no python3 per item ($py_n start(s) for 100 items, limit 2)"
else
    bad "deploy loop starts python3 per item: $py_n starts for 100 items (limit 2) — the 33-minute class"
fi

# 9. A FAILED install / mv FAILS THE APPLY (review 1.0.6.54 F1). Three changed
#    items; the forwarder refuses the SECOND one's destination. Expected: rc=1,
#    the `ERROR: atomic install failed: <dst>` line, files_done < files_total (the
#    first item's patch only, at SA02M_UPDATE_PROGRESS_S=0), no `*.tmp.*` left in
#    the live dir, and rollback_from_journal restoring item 1 (its journal line
#    was written before the failure) while items 2 and 3 are still OLD.
#    On main / the first 1.0.6.54 build the function's last command was
#    `sync … || sync` (always 0) — the failure was swallowed: rc=0, files_done=3.
run_fail_case() {   # <txn> <live dir> <install-gate> <mv-gate> <label>
    local txn=$1 live=$2 label=$5 ov="$STATEDIR/staging/$1/overlay" i
    mkdir -p "$ov" "$live" "$STATEDIR/staging/$txn/meta" "$STATEDIR/staging/$txn/backups"
    for i in 1 2 3; do
        printf 'g%s NEW\n' "$i" > "$ov/g$i.conf"; chmod 644 "$ov/g$i.conf"
        printf 'g%s OLD\n' "$i" > "$live/g$i.conf"; chmod 644 "$live/g$i.conf"
    done
    python3 - "$live" "$STATEDIR/staging/$txn/meta/manifest.json" "$OWNER" <<'PY'
import json, sys
live, mf, owner = sys.argv[1], sys.argv[2], sys.argv[3]
deploy = [{"src": "g%d.conf" % i, "dst": "%s/g%d.conf" % (live, i), "mode": "0644", "owner": owner} for i in (1, 2, 3)]
open(mf, "w", encoding="utf-8").write(json.dumps({"schema_version": 1, "version": "9.9.9.9", "deploy": deploy}) + "\n")
PY
    : > "$TXNVARS_F"; : > "$LOG"
    ( set -euo pipefail; INSTALL_FAIL_ON=$3; MV_FAIL_ON=$4; apply_deploy_items "$txn" ) >/dev/null 2>&1
    local rc=$? fd tmps
    fd=$(txn_get files_done)
    tmps=$(find "$live" -name '*.tmp.*' 2>/dev/null | wc -l | tr -d ' ')
    if [ "$rc" -ne 0 ] && grep -qF "ERROR: atomic install failed: $live/g2.conf" "$LOG" \
       && [ -n "$fd" ] && [ "$fd" -lt 3 ] && [ "$tmps" = "0" ] \
       && [ "$(cat "$live/g1.conf")" = "g1 NEW" ] && [ "$(cat "$live/g2.conf")" = "g2 OLD" ] && [ "$(cat "$live/g3.conf")" = "g3 OLD" ]; then
        ok "$label: apply FAILS (rc=$rc, error logged, files_done=$fd<3, no tmp left, item 3 never touched)"
    else
        bad "$label: failure swallowed or mishandled (rc=$rc, files_done='$fd', tmp-left=$tmps, g1='$(cat "$live/g1.conf")', g2='$(cat "$live/g2.conf")', g3='$(cat "$live/g3.conf")', log-line=$(grep -cF 'atomic install failed' "$LOG"))"
    fi
    ( set -euo pipefail; rollback_from_journal "$txn" ) >/dev/null 2>&1
    if [ "$(cat "$live/g1.conf")" = "g1 OLD" ] && [ "$(cat "$live/g2.conf")" = "g2 OLD" ] && [ "$(cat "$live/g3.conf")" = "g3 OLD" ]; then
        ok "$label: rollback over the partial journal restores item 1, items 2-3 intact"
    else
        bad "$label: rollback after the failed item left g1='$(cat "$live/g1.conf")' g2='$(cat "$live/g2.conf")' g3='$(cat "$live/g3.conf")'"
    fi
}
run_fail_case TXN4 "$T/live4" "g2.conf.tmp." "" "9a install fails on item 2"
run_fail_case TXN5 "$T/live5" "" "g2.conf" "9b mv fails on item 2"

# 10. NUL INSIDE A MANIFEST FIELD IS REFUSED UP FRONT (review 1.0.6.54 F2). The
#     emitter writes NUL-separated fields, so a `\u0000` inside the LAST item's dst
#     would shift the list and land that item with a garbage dst/mode. Expected:
#     apply rc≠0 before ANY file is touched (item 1 still OLD) and the reason on
#     stderr. Without the guard: item 1 deployed, then the shifted last item.
TXN6="TXN6"; OV6="$STATEDIR/staging/$TXN6/overlay"; LIVE6="$T/live6"
mkdir -p "$OV6" "$LIVE6" "$STATEDIR/staging/$TXN6/meta" "$STATEDIR/staging/$TXN6/backups"
printf 'n1 NEW\n' > "$OV6/n1.conf"; printf 'n1 OLD\n' > "$LIVE6/n1.conf"; chmod 644 "$OV6/n1.conf" "$LIVE6/n1.conf"
printf 'n2 NEW\n' > "$OV6/n2.conf"; chmod 644 "$OV6/n2.conf"
python3 - "$LIVE6" "$STATEDIR/staging/$TXN6/meta/manifest.json" "$OWNER" <<'PY'
import json, sys
live, mf, owner = sys.argv[1], sys.argv[2], sys.argv[3]
deploy = [{"src": "n1.conf", "dst": live + "/n1.conf", "mode": "0644", "owner": owner},
          {"src": "n2.conf", "dst": live + "/n2\u0000.conf", "mode": "0644", "owner": owner}]
open(mf, "w", encoding="utf-8").write(json.dumps({"schema_version": 1, "version": "9.9.9.9", "deploy": deploy}) + "\n")
PY
: > "$TXNVARS_F"
( set -euo pipefail; apply_deploy_items "$TXN6" ) >/dev/null 2>"$T/nul.err"
rc6=$?
if [ "$rc6" -ne 0 ] && [ "$(cat "$LIVE6/n1.conf")" = "n1 OLD" ] && grep -qi 'NUL' "$T/nul.err"; then
    ok "NUL in a manifest field refused before any file is touched (rc=$rc6, reason on stderr)"
else
    bad "NUL in a manifest field not refused up front (rc=$rc6, n1='$(cat "$LIVE6/n1.conf")', stderr='$(tr -d '\n' < "$T/nul.err" | cut -c1-120)')"
fi

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
