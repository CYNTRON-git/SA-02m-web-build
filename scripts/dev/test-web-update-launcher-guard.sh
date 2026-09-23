#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped launcher in a sandbox (a PATH-shimmed git whose clone call is the observable, the status file it writes, its exit code, its log), so a commented-out line changes the measured behaviour instead of hiding behind a needle grep; it pins no source line of its own.
# test-web-update-launcher-guard.sh — the GitHub OTA launcher
# (etc/sa02m-web-update-apply.sh, `sudo -n sa02m-web-update-apply` from the
# panel) refuses to clobber a RUNNING transaction. Quality row
# `web-update-launcher-guard`. Field incident 2026-09-23 (plan 1.0.6.52, F5b /
# premise P2).
#
# Why: the offline POST path of web_update_apply.cgi answers E_LOCK busy on a
# running stage, the GitHub POST path had no such check — a second «Применить»
# cloned the repo again and its handoff OVERWROTE transaction.json under the
# live runner (sa02m-web-update-apply.sh handoff_to_shared_runner). The guard
# runs BEFORE the clone: a transaction at applying / verifying / committing /
# rolling_back whose runner is alive → log, update_status=error, exit 1. A
# transaction whose runner is gone (stale) is NOT protected — re-applying is a
# legitimate recovery, and recover/verify at boot is the other one. Liveness
# is the same test as the CGI side (cgi-bin/lib_web_update.sh) — a second copy
# by necessity: a root helper must not source a www-data-writable file.
#
# Method: run the REAL launcher end to end with both state dirs sandboxed
# (SA02M_WEB_BUILD_STATEDIR — the legacy lock/status/log dir, the same seam the
# CGI honours — and SA02M_UPDATE_STATEDIR), a PATH-first `git` shim that
# records its argv and FAILS the clone (so every run stops right after the
# guard, whichever way it went), a `systemctl` shim answering inactive, and a
# live-runner fixture (a process whose argv[0] is sa02m-update-runner). The
# observable is whether `git … clone` was attempted, plus update_status and the
# exit code. Nothing touches the real filesystem, no root, no network.
#
# Drive-to-failure: WEB_UPDATE_LAUNCHER=<(git show 6ba943d:etc/sa02m-web-update-apply.sh) \
#   bash scripts/dev/test-web-update-launcher-guard.sh   → L1 RED (the
#   pre-fix launcher clones over the live transaction; it also ignores
#   SA02M_WEB_BUILD_STATEDIR, so its status file lands outside the sandbox).
#   PINNED ref. RED observed 2026-09-23 on that tree: 3 FAIL — L1 «rc=1,
#   cloned: yes, status=''» (it clones over the live transaction; status lands
#   outside the sandbox), L1 no log line, and L5 (the seam is missing, so the
#   pre-fix launcher's legacy lock is /var/lib/…, not the sandbox one — an
#   artefact of the seam, not a behaviour difference). L2–L4 hold on both trees.
#
# Run: bash scripts/dev/test-web-update-launcher-guard.sh   (bash + python3 + coreutils)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC="${WEB_UPDATE_LAUNCHER:-etc/sa02m-web-update-apply.sh}"
command -v python3 >/dev/null 2>&1 || { echo "SKIP  python3 unavailable (launcher requires it)"; exit 0; }
T=$(mktemp -d) || exit 1
cat "$SRC" > "$T/launcher.sh" 2>/dev/null || { echo "FAIL  cannot read launcher source: $SRC"; exit 1; }
LAUNCHER="$T/launcher.sh"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

LEGACY="$T/legacy"; UPD="$T/upd"; BIN="$T/bin"
mkdir -p "$LEGACY" "$UPD" "$BIN"
export SA02M_WEB_BUILD_STATEDIR="$LEGACY" SA02M_UPDATE_STATEDIR="$UPD"
export SA02M_UPDATE_RUNNER="$T/no-such-runner"   # never reached: the clone fails first

cat > "$BIN/git" <<SHIM
#!/bin/bash
printf 'git %s\n' "\$*" >> "$T/git.calls"
for a in "\$@"; do [ "\$a" = clone ] && { echo "shim: clone refused" >&2; exit 1; }; done
exit 0
SHIM
cat > "$BIN/systemctl" <<'SHIM'
#!/bin/bash
case "${1:-}" in is-active) exit 3 ;; *) exit 0 ;; esac
SHIM
chmod +x "$BIN/git" "$BIN/systemctl"
PATH="$BIN:$PATH"; export PATH

bash -c 'exec -a sa02m-update-runner sleep 30' &
LIVE_PID=$!
trap 'kill "$LIVE_PID" 2>/dev/null; rm -rf "$T"' EXIT
sleep 0.3
DEAD_PID=4194303

write_txn() {  # $1=stage
  printf '{"schema_version":1,"id":"abcdef12-0000-4000-8000-000000000001","operation":"update","source":"github","stage":"%s","progress_pct":40,"result":"pending","error_code":null,"error_message":null,"updated_at":"2026-09-23T10:00:00Z"}\n' "$1" > "$UPD/transaction.json"
}
run_launcher() {
  rm -f "$T/git.calls" "$LEGACY/update_status" "$LEGACY/update.lock"
  bash "$LAUNCHER" >/dev/null 2>&1; rc=$?
}
cloned() { grep -q ' clone ' "$T/git.calls" 2>/dev/null; }
status_is() { [ "$(cat "$LEGACY/update_status" 2>/dev/null)" = "$1" ]; }
rc=0

# ── L1: live runner at applying → refused before the clone ──────────────────
write_txn applying; printf '%s\n' "$LIVE_PID" > "$UPD/update.lock"
run_launcher
if [ "$rc" -ne 0 ] && ! cloned && status_is error; then
  ok "L1 live runner at applying → exit $rc, NO clone, update_status=error"
else
  bad "L1 live runner at applying → rc=$rc, cloned: $(cloned && echo yes || echo no), status='$(cat "$LEGACY/update_status" 2>/dev/null)' (want exit 1, no clone, error) — the clobber class"
fi
grep -q 'уже выполняется' "$LEGACY/update.log" 2>/dev/null && ok "L1 the refusal is logged (панель показывает причину)" \
  || bad "L1 no 'уже выполняется' line in the update log"
# ── L2: stale transaction (runner gone) → the launch proceeds to the clone ──
write_txn verifying; printf '%s\n' "$DEAD_PID" > "$UPD/update.lock"
run_launcher
cloned && ok "L2 dead runner (stale transaction) → the launch proceeds (clone attempted)" \
  || bad "L2 dead runner → clone NOT attempted (rc=$rc) — a stale transaction blocks re-apply"
# ── L3: no transaction at all → proceeds ────────────────────────────────────
rm -f "$UPD/transaction.json" "$UPD/update.lock"
run_launcher
cloned && ok "L3 no transaction → the launch proceeds" || bad "L3 no transaction → clone NOT attempted (rc=$rc)"
# ── L4: terminal stage with a live pid in the lock → proceeds ───────────────
write_txn done; printf '%s\n' "$LIVE_PID" > "$UPD/update.lock"
run_launcher
cloned && ok "L4 stage=done (live pid in a stale lock) → the launch proceeds" || bad "L4 stage=done → clone NOT attempted (rc=$rc)"
# ── L5: legacy launcher lock of a live launcher → refused (pre-existing guard) ─
write_txn done
run_launcher_locked() {
  rm -f "$T/git.calls" "$LEGACY/update_status"
  printf '%s\n' "$LIVE_PID" > "$LEGACY/update.lock"
  bash "$LAUNCHER" >/dev/null 2>&1; rc=$?
}
run_launcher_locked
[ "$rc" -ne 0 ] && ! cloned && ok "L5 the legacy launcher lock (live pid) still refuses a second launcher" \
  || bad "L5 legacy lock held by a live pid → rc=$rc, cloned: $(cloned && echo yes || echo no)"

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
