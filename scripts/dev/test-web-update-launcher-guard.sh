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
#   PINNED ref. RED observed 2026-09-23 on that tree: 4 FAIL — L1 «rc=1,
#   cloned: yes, status=''» (it clones over the live transaction; status lands
#   outside the sandbox), L1 no log line, and L5 (the seam is missing, so the
#   pre-fix launcher's legacy lock is /var/lib/…, not the sandbox one — an
#   artefact of the seam, not a behaviour difference), L6 (no unit clause at
#   all). L2–L4 hold on both trees, and so does L7 — it has NO RED tree by
#   construction: it pins that the guard does not over-refuse the dead-runner
#   rolling_back residue (a non-regression pin, added when the residue was
#   found on bench 1.135), never a behaviour the pre-fix launcher lacked.
#   L6 was RED on the first fixed tree too
#   («cloned: yes» — its is-active clause never fires for a oneshot unit;
#   review 1.0.6.52, finding 3).
#
# Run: bash scripts/dev/test-web-update-launcher-guard.sh   (bash + python3 + coreutils)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC="${WEB_UPDATE_LAUNCHER:-etc/sa02m-web-update-apply.sh}"
command -v python3 >/dev/null 2>&1 || { echo "SKIP  python3 unavailable (launcher requires it)"; exit 77; }  # run.mjs SKIP_EXIT
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

# ── L6: no lock pid, but the verify unit is mid-run (oneshot: ActiveState=activating) → refused ─
# `is-active --quiet` answers rc 3 for a oneshot unit for the whole run of its
# ExecStart, so a clause built on it never fires (review 1.0.6.52, finding 3);
# the unit half of the liveness test must read ActiveState.
cat > "$BIN/systemctl" <<'SHIM'
#!/bin/bash
case "${1:-}" in
  is-active) exit 3 ;;
  show) case "$*" in *ActiveState*sa02m-update-verify.service*) echo activating ;; *ActiveState*) echo inactive ;; esac; exit 0 ;;
  *) exit 0 ;;
esac
SHIM
write_txn verifying; rm -f "$UPD/update.lock"
run_launcher
if [ "$rc" -ne 0 ] && ! cloned && status_is error; then
  ok "L6 verify unit activating (oneshot mid-run), no lock pid → refused, no clone"
else
  bad "L6 verify unit activating → rc=$rc, cloned: $(cloned && echo yes || echo no) — the unit half of the liveness test does not fire for a oneshot unit"
fi

# ── L7: the field residue — rolling_back, dead runner (stale lock) → re-apply allowed ─
cat > "$BIN/systemctl" <<'SHIM'
#!/bin/bash
case "${1:-}" in is-active) exit 3 ;; *) exit 0 ;; esac
SHIM
write_txn rolling_back; printf '%s\n' "$DEAD_PID" > "$UPD/update.lock"
run_launcher
cloned && ok "L7 rolling_back with a dead runner (the bench residue) → «Применить» proceeds (clone attempted)" \
  || bad "L7 rolling_back + dead runner → clone NOT attempted (rc=$rc) — a stuck board could never re-apply"

# ── L8–L11: the clobber PAST the pre-clone guard (1.0.6.62, audit 2026-09-28 M1) ─
# Until 1.0.6.62 the guard above pinned FOUR stages and ran only BEFORE the
# clone, while the write that clobbers (handoff_to_shared_runner: tmp.replace
# over transaction.json) ran unconditionally 30–60 s later — a runner at
# validating / backing_up (10–30 s on the A7) was not protected at all, and one
# that became alive DURING the clone was protected by nothing; runner 2 then
# died on E_LOCK with runner 1's transaction already replaced, and a power cut
# in that window rolled nothing back (recover reads the new `validating`, wipes
# staging, E_POWER — the half-deployed tree stays). Now: every non-terminal
# stage is guarded before the clone (L8); the launcher takes the runner's OWN
# flock on $SA02M_UPDATE_STATEDIR/update.lock (fd 9, opened for append — the
# runner's try_lock idiom) before it writes transaction.json, refuses when it is
# held (L9), re-checks liveness under it (L10 — the cgroup handover releases the
# lock for a moment while the runner is alive), and holds it through `exec` so
# the runner inherits it and re-takes it on the same descriptor without a
# deadlock (L11 — also the non-vacuity anchor: the clone + handoff really run).
# Method: a git shim that lets the clone SUCCEED (a repo skeleton with a
# VERSION, the allowlisted remote, a pinned HEAD) and an optional on-clone hook
# that plants the racing runner «while the clone runs»; a fake runner at
# SA02M_UPDATE_RUNNER that records what it inherited (fd 9's target, whether
# the lock was held at exec, whether the runner's own re-take succeeds) and
# removes the clone it was handed. A lock HOLDER is a real process holding
# flock(2) on the sandbox lock with argv[0] = sa02m-update-runner.
# flock(1) is util-linux — absent on git-bash, so the section is SKIPPED there
# (printed as a skip, never counted as a pass); run it under WSL/Linux.
# Drive-to-failure: WEB_UPDATE_LAUNCHER=<(git show d66d7b6:etc/sa02m-web-update-apply.sh)
#   bash scripts/dev/test-web-update-launcher-guard.sh   (under WSL/Linux; on a
#   Windows worktree WSL cannot resolve the worktree's git dir — `git show` the
#   file to disk from the Windows side and point WEB_UPDATE_LAUNCHER at it).
# RED observed 2026-09-28 on that pre-fix launcher (WSL): 6 FAIL — L8 ×3
# «clone attempted (rc=0, txn id now <new uuid>, was …0001, runner exec'd:
# yes)» (uploaded / validating / backing_up: the clone ran, the handoff
# replaced the live transaction, the fake runner was exec'd on top of the
# holder); L9 and L10 the same id change (rc=0, status=running, runner
# exec'd); L11 «fd9=none held_at_exec=no» (nothing inherited — a contender's
# flock -n succeeded against the just-exec'd runner). L11's handoff line and
# its re-take line hold on both trees.
echo
echo "── L8–L11: clobber past the pre-clone guard (runner lock inheritance) ──"
skipped=""
if ! command -v flock >/dev/null 2>&1; then
  echo "SKIP  L8–L11 need flock(1) (util-linux) — the launcher's lock idiom; absent here, run this harness under WSL/Linux"
  skipped="L8–L11 (no flock)"
else
  SHA=0123456789abcdef0123456789abcdef01234567
  REPO=https://github.com/CYNTRON-git/SA-02m-web-build.git
  export SA02M_WEB_BUILD_REPO_URL="$REPO" SA02M_WEB_BUILD_REPO_ALLOWLIST="$REPO" SA02M_WEB_BUILD_COMMIT_PIN="$SHA"
  # git shim: the clone SUCCEEDS and materialises a repo skeleton; the optional
  # on-clone hook runs while «the clone is in progress» (its children must not
  # keep the launcher's `tee` pipe open — every fixture below detaches stdio).
  cat > "$BIN/git" <<SHIM
#!/bin/bash
printf 'git %s\n' "\$*" >> "$T/git.calls"
sub=""; for a in "\$@"; do case "\$a" in clone|remote|rev-parse|ls-remote) [ -n "\$sub" ] || sub=\$a ;; esac; done
case "\$sub" in
  clone) dest=""; for a in "\$@"; do dest=\$a; done
         mkdir -p "\$dest/www/network_config" && printf '9.9.9.9\n' > "\$dest/www/network_config/VERSION" || exit 1
         [ -x "$T/on-clone" ] && "$T/on-clone"
         exit 0 ;;
  remote) printf '%s\n' "$REPO" ;;
  rev-parse) printf '%s\n' "$SHA" ;;
  ls-remote) printf '%s\trefs/heads/main\n' "$SHA" ;;
esac
exit 0
SHIM
  chmod +x "$BIN/git"
  # The fake runner: carries the launcher's handoff-capability tokens in its
  # header (the launcher greps for them), records what it inherited, and owns
  # the clone it was handed (the real runner cleans the overlay up too).
  FAKE_RUNNER="$T/fake-runner"
  cat > "$FAKE_RUNNER" <<SHIM
#!/bin/bash
# fake sa02m-update-runner — capability tokens for the launcher's grep: overlay_path SA02M_UPDATE_GITHUB
lock="\${SA02M_UPDATE_STATEDIR:?}/update.lock"
{
  printf 'args=%s\n' "\$*"
  printf 'overlay=%s\n' "\${SA02M_UPDATE_GITHUB_OVERLAY:-}"
  printf 'fd9=%s\n' "\$([ -e /proc/self/fd/9 ] && readlink -f /proc/self/fd/9 || echo none)"
  if flock -n "\$lock" true 2>/dev/null; then printf 'held_at_exec=no\n'; else printf 'held_at_exec=yes\n'; fi
  # the runner's own try_lock over the inherited descriptor: must not deadlock
  if exec 9>>"\$lock" && flock -n 9; then printf 'reacquire=ok\n'; else printf 'reacquire=FAIL\n'; fi
} >> "$T/runner.calls"
case "\${SA02M_UPDATE_GITHUB_OVERLAY:-}" in /tmp/sa02m-web-update-*/repo) rm -rf "\$(dirname "\$SA02M_UPDATE_GITHUB_OVERLAY")" ;; esac
exit 0
SHIM
  chmod +x "$FAKE_RUNNER"
  export SA02M_UPDATE_RUNNER="$FAKE_RUNNER"
  # Fixtures: a runner that is alive but holds NO lock (the cgroup-handover
  # moment), and — per case — a HOLDER that keeps flock(2) on the sandbox lock.
  bash -c 'exec -a sa02m-update-runner sleep 900' </dev/null >/dev/null 2>&1 &
  LIVE2_PID=$!
  HOLD_PIDS=""
  trap 'kill "$LIVE_PID" "$LIVE2_PID" $HOLD_PIDS 2>/dev/null; rm -rf "$T"' EXIT
  sleep 0.3
  spawn_holder() {  # → HOLD_PID; the lock file names it
    bash -c 'exec 9>>"$0" && flock -n 9 || exit 99; exec -a sa02m-update-runner sleep 900' "$UPD/update.lock" </dev/null >/dev/null 2>&1 &
    HOLD_PID=$!; HOLD_PIDS="$HOLD_PIDS $HOLD_PID"
    local i=0
    while flock -n "$UPD/update.lock" true 2>/dev/null; do i=$((i + 1)); [ "$i" -ge 40 ] && break; sleep 0.1; done
    printf '%s\n' "$HOLD_PID" > "$UPD/update.lock"
  }
  txn_id() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("id",""))' "$UPD/transaction.json" 2>/dev/null | tr -d '\r'; }
  txn_same() { cmp -s "$UPD/transaction.json" "$T/txn.before"; }
  runner_execd() { [ -f "$T/runner.calls" ]; }
  snapshot_txn() { cp "$UPD/transaction.json" "$T/txn.before"; BEFORE_ID=$(txn_id); }
  run_launcher_ho() { rm -f "$T/runner.calls"; run_launcher; }
  refused_intact() {  # $1=label — the shared shape of a refusal that left the live transaction alone
    if [ "$rc" -ne 0 ] && txn_same && ! runner_execd && status_is error && grep -q 'уже выполняется' "$LEGACY/update.log" 2>/dev/null; then
      ok "$1 → exit $rc, transaction.json byte-identical, runner NOT exec'd, update_status=error, refusal logged"
    else
      bad "$1 → rc=$rc, cloned: $(cloned && echo yes || echo no), txn id now '$(txn_id)' (was '$BEFORE_ID'), runner exec'd: $(runner_execd && echo yes || echo no), status='$(cat "$LEGACY/update_status" 2>/dev/null)' — the clobber class"
    fi
  }

  # ── L8: a live runner (holding the lock) at uploaded / validating / backing_up → refused BEFORE the clone ─
  spawn_holder
  flock -n "$UPD/update.lock" true 2>/dev/null && bad "L8 fixture: the holder does not hold flock(2) on the sandbox lock"
  rm -f "$T/on-clone"
  for st in uploaded validating backing_up; do
    write_txn "$st"; printf '%s\n' "$HOLD_PID" > "$UPD/update.lock"; snapshot_txn
    run_launcher_ho
    if ! cloned; then refused_intact "L8 live runner at $st"
    else bad "L8 live runner at $st → clone attempted (rc=$rc, txn id now '$(txn_id)', was '$BEFORE_ID', runner exec'd: $(runner_execd && echo yes || echo no)) — the stage is not guarded before the clone"; fi
  done
  kill "$HOLD_PID" 2>/dev/null

  # ── L9: the race — nothing running at the guard, a runner takes the lock DURING the clone → refused at the lock ─
  rm -f "$UPD/transaction.json" "$UPD/update.lock" "$T/hold9.pid" "$T/txn.before"
  cat > "$T/on-clone" <<HOOK
#!/bin/bash
printf '{"schema_version":1,"id":"abcdef12-0000-4000-8000-000000000009","operation":"update","source":"file","stage":"validating","progress_pct":5,"result":"pending","error_code":null,"error_message":null,"updated_at":"%s"}\n' "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$UPD/transaction.json"
bash -c 'exec 9>>"\$0" && flock -n 9 || exit 99; exec -a sa02m-update-runner sleep 900' "$UPD/update.lock" </dev/null >/dev/null 2>&1 &
echo \$! > "$T/hold9.pid"
i=0; while flock -n "$UPD/update.lock" true 2>/dev/null; do i=\$((i + 1)); [ "\$i" -ge 40 ] && exit 1; sleep 0.1; done
printf '%s\n' "\$(cat "$T/hold9.pid")" > "$UPD/update.lock"
cp "$UPD/transaction.json" "$T/txn.before"
exit 0
HOOK
  chmod +x "$T/on-clone"
  run_launcher_ho
  [ -s "$T/hold9.pid" ] && HOLD_PIDS="$HOLD_PIDS $(cat "$T/hold9.pid")"
  if ! cloned || [ ! -s "$T/txn.before" ]; then
    bad "L9 fixture: the clone/hook did not run (cloned: $(cloned && echo yes || echo no), rc=$rc) — nothing was raced"
  else
    BEFORE_ID=abcdef12-0000-4000-8000-000000000009
    refused_intact "L9 a runner took the lock during the clone (validating)"
  fi
  [ -s "$T/hold9.pid" ] && kill "$(cat "$T/hold9.pid")" 2>/dev/null

  # ── L10: under the lock — the lock is FREE but a runner is alive at validating (cgroup handover) → refused before the write ─
  rm -f "$UPD/transaction.json" "$UPD/update.lock" "$T/txn.before"
  cat > "$T/on-clone" <<HOOK
#!/bin/bash
printf '{"schema_version":1,"id":"abcdef12-0000-4000-8000-000000000010","operation":"update","source":"github","stage":"validating","progress_pct":5,"result":"pending","error_code":null,"error_message":null,"updated_at":"%s"}\n' "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$UPD/transaction.json"
printf '%s\n' "$LIVE2_PID" > "$UPD/update.lock"
cp "$UPD/transaction.json" "$T/txn.before"
exit 0
HOOK
  chmod +x "$T/on-clone"
  run_launcher_ho
  if ! cloned || [ ! -s "$T/txn.before" ]; then
    bad "L10 fixture: the clone/hook did not run (cloned: $(cloned && echo yes || echo no), rc=$rc)"
  else
    BEFORE_ID=abcdef12-0000-4000-8000-000000000010
    refused_intact "L10 lock free, runner alive at validating (planted during the clone)"
  fi

  # ── L11: the positive handoff — idle, no runner: transaction written, runner exec'd WITH the lock held ─
  rm -f "$UPD/transaction.json" "$UPD/update.lock" "$T/on-clone"
  run_launcher_ho
  txn_shape_ok() { python3 -c 'import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8")); raise SystemExit(0 if d.get("stage")=="validating" and d.get("source")=="github" and d.get("id") else 1)' "$UPD/transaction.json" 2>/dev/null; }
  if [ "$rc" -eq 0 ] && runner_execd && grep -qx 'args=apply' "$T/runner.calls" && txn_shape_ok && status_is running; then
    ok "L11 idle → clone, transaction.json written (stage=validating, source=github), runner exec'd with 'apply', update_status=running"
  else
    bad "L11 idle → rc=$rc, cloned: $(cloned && echo yes || echo no), runner exec'd: $(runner_execd && echo yes || echo no), txn id '$(txn_id)', status='$(cat "$LEGACY/update_status" 2>/dev/null)' — the handoff itself broke (the L8–L10 refusals would be vacuous)"
  fi
  lockpath=$(readlink -f "$UPD/update.lock")
  if grep -qxF "fd9=$lockpath" "$T/runner.calls" 2>/dev/null && grep -qx 'held_at_exec=yes' "$T/runner.calls" 2>/dev/null; then
    ok "L11 the runner inherited fd 9 → update.lock with flock(2) HELD at exec (a contender's flock -n fails)"
  else
    bad "L11 lock not inherited: $(grep -E '^(fd9|held_at_exec)=' "$T/runner.calls" 2>/dev/null | tr '\n' ' ')(want fd9=$lockpath held_at_exec=yes) — between the write and the runner's own lock a second launcher can still clobber"
  fi
  grep -qx 'reacquire=ok' "$T/runner.calls" 2>/dev/null && ok "L11 the runner's own try_lock idiom (exec 9>>lock; flock -n 9) re-takes the inherited lock — no deadlock" \
    || bad "L11 re-take over the inherited descriptor FAILED ($(grep '^reacquire=' "$T/runner.calls" 2>/dev/null)) — the real runner would die on E_LOCK against its own launcher"
fi

echo "-----"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks${skipped:+; SKIPPED here: $skipped})"; exit 0
else echo "FAIL ($fails check(s)${skipped:+; SKIPPED here: $skipped})"; exit 1; fi
