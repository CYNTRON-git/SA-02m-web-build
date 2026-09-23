#!/bin/bash
# Functional RED/GREEN harness for the fail-closed «is there anything to apply»
# guard in www/network_config/cgi-bin/web_update_apply.cgi (audit 2026-09-08
# C10; contract table: docs/contracts/web-update.md).
#
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped CGI in a sandbox (a minted session + CSRF from the real lib_web_auth.sh, a PATH-shimmed sudo whose invocation is the observable, check.json fixtures in an env-directed state dir) and reading the JSON body it prints; it pins no source line a comment token could satisfy, and its RED is recorded below against the pre-fix CGI, not a comment mutation.
#
# What it runs: the REAL CGI end to end — session cookie + X-SA02M-CSRF minted
# through the shipped lib (SA02M_SESSION_DIR sandboxed), REQUEST_METHOD=POST
# with no body (the legacy internet-Apply branch), the state dir redirected via
# SA02M_WEB_BUILD_STATEDIR, and `sudo` replaced by a PATH shim that records its
# call and sleeps so the CGI's own `kill -0` sees it running. The observable is
# TWO-fold on every case: the body's error_code AND whether sudo was called —
# the root launch is the thing the guard exists to withhold.
#
# Cases (the contract table, one row each) — see the numbered blocks below.
# Non-vacuity: the launch cases (5, 7) FAIL unless the sudo shim really was
# invoked, so a broken shim, a wrong CGI path or an auth failure cannot read as
# "guard held". The lib's function shape is asserted before use.
#
# RED, observed 2026-09-08 against the pre-fix CGI (1f6f1a1, run with
# WEB_UPDATE_APPLY_CGI=<git show copy> and its own lib beside it): 9 FAILED —
# cases 1, 2, 3, 4, 4b, 6, 8, 9, 9b all LAUNCHED (sudo called, no error code)
# instead of refusing. The pre-fix CGI ignores SA02M_WEB_BUILD_STATEDIR and
# reads its hard-coded /var/lib path, absent on the dev host, so on it every
# fixture degrades to «no check.json» — which the old guard answered by
# launching: the whole fail-open class in one run. Cases 5, 7, 10, 11 passed
# there too (they hold on both trees). GREEN on the fixed CGI: 13/13.
#
# Windows/git-bash: python3 here is CPython writing CRLF to pipes — the CGI's
# probe prints nothing to stdout (exit code only), and every body read below is
# stripped of \r (.ai-dev/notes/quality-gate-environment.md).
#
# Section G (1.0.6.52, field incident: a runner SIGKILLed at the health gate left
# the panel polling «Проверка сервисов…» 85 % forever): the GET status answer
# carries `runner_alive` / `stale` and turns a running-stage transaction whose
# runner is gone into status «error» + E_RUNNER_LOST after WEB_UPD_STALE_AFTER_S
# (120 s) — contract: docs/contracts/web-update.md «GET — состояние». The state
# dir is SA02M_UPDATE_STATEDIR (the runner's own seam); liveness is the pid in
# $STATEDIR/update.lock (alive AND its cmdline names the runner) or an active
# sa02m-update / sa02m-update-verify / sa02m-update-apply-* unit (systemctl
# shimmed here: everything inactive). The live-runner fixture is a real process
# whose argv[0] is `sa02m-update-runner` (exec -a); the dead one is pid 2^22-1,
# above every pid_max this project meets. RED recorded in the section header.
#
# Run: bash scripts/dev/test-web-update-apply-guard.sh
#      WEB_UPDATE_APPLY_CGI=<path> to drive another copy (the RED run).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

CGI="${WEB_UPDATE_APPLY_CGI:-www/network_config/cgi-bin/web_update_apply.cgi}"
LIB="$(dirname "$CGI")/lib_web_auth.sh"
[ -f "$CGI" ] || { echo "FAIL  CGI not found: $CGI"; exit 1; }
[ -f "$LIB" ] || { echo "FAIL  lib_web_auth.sh not found beside the CGI: $LIB"; exit 1; }

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; mkdir -p "$BIN"
STATE="$T/state"; mkdir -p "$STATE"
export SA02M_WEB_BUILD_STATEDIR="$STATE"
export SA02M_SESSION_DIR="$T/sessions"

# ── sudo shim: records the call, stays alive so the CGI reports «running» ──
cat > "$BIN/sudo" <<SHIM
#!/bin/bash
printf '%s\n' "\$*" >> "$T/sudo.calls"
sleep 3
exit 0
SHIM
chmod +x "$BIN/sudo"
PATH="$BIN:$PATH"; export PATH

# ── a session + CSRF token from the shipped lib ────────────────────────────
# shellcheck source=www/network_config/cgi-bin/lib_web_auth.sh
. "$LIB"
for fn in web_session_create web_csrf_token_for_session web_csrf_validate web_session_check_cookie; do
  declare -F "$fn" >/dev/null || { echo "FAIL  $LIB does not define $fn (did the file shape change?)"; exit 1; }
done
TOK=$(web_session_create admin)
[[ "$TOK" =~ ^[a-f0-9]{64}$ ]] || { echo "FAIL  could not mint a session token"; exit 1; }
CSRF=$(web_csrf_token_for_session "$TOK")
[ -n "$CSRF" ] || { echo "FAIL  could not mint a CSRF token"; exit 1; }

# run_cgi [cookie] [csrf] → body on stdout (CR stripped); resets the sudo ledger.
run_cgi() {
  rm -f "$T/sudo.calls"
  REQUEST_METHOD=POST CONTENT_LENGTH=0 QUERY_STRING='' \
  HTTP_COOKIE="session_token=${1:-$TOK}" HTTP_X_SA02M_CSRF="${2:-$CSRF}" \
    bash "$CGI" </dev/null 2>/dev/null | tr -d '\r'
}
sudo_called() { [ -f "$T/sudo.calls" ]; }
code_of() { printf '%s' "$1" | sed -n 's/.*"error_code":"\([A-Z_]*\)".*/\1/p' | head -1; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
write_check() { printf '%s\n' "$1" > "$STATE/check.json"; }

# Expect a refusal: code $2 and NO sudo. $1 = case label.
expect_refused() {
  local label="$1" want="$2" body code
  body=$(run_cgi)
  code=$(code_of "$body")
  if [ "$code" = "$want" ] && ! sudo_called; then ok "$label → $want, no launch"
  else bad "$label → got code '${code:-none}', sudo called: $(sudo_called && echo yes || echo no) (want $want, no launch) — body: ${body##*$'\n\n'}"; fi
}
# Expect the launch: sudo called with the apply helper, «running» in the body.
expect_launch() {
  local label="$1" body code
  body=$(run_cgi)
  code=$(code_of "$body")
  if sudo_called && grep -q 'sa02m-web-update-apply' "$T/sudo.calls" && [ -z "$code" ] && printf '%s' "$body" | grep -q '"status":"running"'; then
    ok "$label → launched (sudo sa02m-web-update-apply), status running"
  else
    bad "$label → sudo called: $(sudo_called && echo yes || echo no), code '${code:-none}' (want a launch) — body: ${body##*$'\n\n'}"
  fi
}

# ═══ 1. no check.json at all ═══════════════════════════════════════════════
rm -f "$STATE/check.json"
expect_refused "1 no check.json" E_CHECK_STALE

# ═══ 2. unparseable check.json ═════════════════════════════════════════════
write_check '{not json'
expect_refused "2 unparseable check.json" E_CHECK_STALE

# ═══ 3. stale (2 days old), remote newer ═══════════════════════════════════
write_check "{\"checked_at\":\"$(date -u -d '-2 days' +%Y-%m-%dT%H:%M:%SZ)\",\"deployed_version\":\"1.0.6.29\",\"remote_version\":\"1.0.6.38\",\"update_available\":true}"
expect_refused "3 stale checked_at, remote newer" E_CHECK_STALE

# ═══ 4. fresh, deployed ≥ remote ═══════════════════════════════════════════
write_check "{\"checked_at\":\"$(now_utc)\",\"deployed_version\":\"1.0.6.38\",\"remote_version\":\"1.0.6.29\",\"update_available\":true}"
expect_refused "4 fresh, current newer than available" E_NO_UPDATE
write_check "{\"checked_at\":\"$(now_utc)\",\"deployed_version\":\"1.0.6.38\",\"remote_version\":\"1.0.6.38\",\"update_available\":false}"
expect_refused "4b fresh, equal versions" E_NO_UPDATE

# ═══ 5. fresh, remote newer → the ONE path to the launch ═══════════════════
write_check "{\"checked_at\":\"$(now_utc)\",\"deployed_version\":\"1.0.6.29\",\"remote_version\":\"1.0.6.38\",\"update_available\":false}"
expect_launch "5 fresh, remote newer (version compare wins over the flag)"

# ═══ 6. the probe interpreter is broken (python3 exits 127) ════════════════
cat > "$BIN/python3" <<'SHIM'
#!/bin/bash
exit 127
SHIM
chmod +x "$BIN/python3"
expect_refused "6 python3 unusable (exit 127) with a launch-worthy check.json" E_CHECK_STALE
rm -f "$BIN/python3"

# ═══ 7. fresh, no versions, checker flag true → launch (frontend parity) ═══
write_check "{\"checked_at\":\"$(now_utc)\",\"deployed_version\":null,\"remote_version\":null,\"update_available\":true}"
expect_launch "7 fresh, versions absent, update_available true"

# ═══ 8. fresh, nothing decidable ═══════════════════════════════════════════
write_check "{\"checked_at\":\"$(now_utc)\",\"deployed_version\":null,\"remote_version\":null,\"update_available\":null,\"error\":\"network_or_git_failed\"}"
expect_refused "8 fresh but undecidable (null flag, null versions)" E_CHECK_STALE

# ═══ 9. checked_at from the future (clock went backwards) ══════════════════
write_check "{\"checked_at\":\"$(date -u -d '+2 hours' +%Y-%m-%dT%H:%M:%SZ)\",\"deployed_version\":\"1.0.6.29\",\"remote_version\":\"1.0.6.38\",\"update_available\":true}"
expect_refused "9 checked_at 2 h in the future" E_CHECK_STALE
write_check "{\"deployed_version\":\"1.0.6.29\",\"remote_version\":\"1.0.6.38\",\"update_available\":true}"
expect_refused "9b no checked_at at all" E_CHECK_STALE

# ═══ 10. ordering: CSRF is checked BEFORE the guard, and both before sudo ══
write_check "{\"checked_at\":\"$(now_utc)\",\"deployed_version\":\"1.0.6.29\",\"remote_version\":\"1.0.6.38\",\"update_available\":true}"
rm -f "$T/sudo.calls"
body=$(run_cgi "$TOK" "wrong-token")
if [ "$(code_of "$body")" = "E_CSRF" ] && ! sudo_called; then ok "10 wrong CSRF → E_CSRF before the guard, no launch"
else bad "10 wrong CSRF → code '$(code_of "$body")', sudo called: $(sudo_called && echo yes || echo no)"; fi

# ═══ 11. no session → unauthorized, nothing else runs ══════════════════════
rm -f "$T/sudo.calls"
body=$(run_cgi "0000000000000000000000000000000000000000000000000000000000000000" "$CSRF")
if printf '%s' "$body" | grep -q '"error":"unauthorized"' && ! sudo_called; then ok "11 unknown session → unauthorized, no launch"
else bad "11 unknown session → body: ${body##*$'\n\n'}, sudo called: $(sudo_called && echo yes || echo no)"; fi

# ═══ G. GET status: a dead runner is reported as stale (1.0.6.52) ═══════════
# RED, observed 2026-09-23 against the 1.0.6.50 CGI (6ba943d): 9 FAILED (G1–G6 incl. both runner_alive reads) —
# the CGI reads a hard-coded /var/lib/sa02m-update (absent on the host), so the
# sandbox transaction is invisible (status «idle»), and it prints no
# runner_alive / stale field at all; on the board the same code answered
# «running» for a transaction whose runner had been SIGKILLed 40 min earlier.
# G6 was RED on the first fixed lib as well («stale=True … want stale=False»:
# its `is-active --quiet` clause cannot fire for a oneshot unit — review
# 1.0.6.52, finding 3).
echo
echo "── G. GET status: runner liveness + stale transaction ──"
UPD="$T/upd"; mkdir -p "$UPD"
export SA02M_UPDATE_STATEDIR="$UPD"
# systemctl shim: no update unit is active, no transient apply unit is loaded.
cat > "$BIN/systemctl" <<'SHIM'
#!/bin/bash
case "${1:-}" in
  is-active) exit 3 ;;
  list-units) exit 0 ;;
  *) exit 0 ;;
esac
SHIM
chmod +x "$BIN/systemctl"
# A live runner: a process whose argv[0] names the runner (what the lock pid's
# /proc/<pid>/cmdline shows on the board). Killed on exit.
bash -c 'exec -a sa02m-update-runner sleep 30' &
LIVE_PID=$!
# A live process that is NOT the runner: a reused pid must not read as alive.
sleep 30 &
OTHER_PID=$!
trap 'kill "$LIVE_PID" "$OTHER_PID" 2>/dev/null; rm -rf "$T"' EXIT
sleep 0.3
DEAD_PID=4194303   # 2^22-1: above every pid_max this project meets

run_get() {
  REQUEST_METHOD=GET QUERY_STRING='' HTTP_COOKIE="session_token=$TOK" \
    bash "$CGI" </dev/null 2>/dev/null | tr -d '\r'
}
# json_field BODY KEY → the value as python prints it (True/False/None/str).
json_field() {
  printf '%s' "${1##*$'\n\n'}" | python3 -c 'import json,sys
d=json.load(sys.stdin); v=d.get(sys.argv[1], "<absent>"); print(v)' "$2" 2>/dev/null
}
write_txn() {  # $1=stage $2=updated_at
  printf '{"schema_version":1,"id":"abcdef12-0000-4000-8000-000000000001","operation":"update","source":"github","stage":"%s","progress_pct":85,"files_total":3,"files_done":3,"result":"pending","error_code":null,"error_message":null,"target_version":"9.9.9.9","updated_at":"%s"}\n' "$1" "$2" > "$UPD/transaction.json"
}
expect_get() {  # label stale status code
  local label="$1" want_stale="$2" want_status="$3" want_code="$4" body stale status code
  body=$(run_get)
  stale=$(json_field "$body" stale); status=$(json_field "$body" status); code=$(json_field "$body" error_code)
  if [ "$stale" = "$want_stale" ] && [ "$status" = "$want_status" ] && [ "$code" = "$want_code" ]; then
    ok "$label → stale=$stale status=$status error_code=$code"
  else
    bad "$label → stale=$stale status=$status error_code=$code (want stale=$want_stale status=$want_status code=$want_code) — body: ${body##*$'\n\n'}"
  fi
}
OLD_TS=$(date -u -d '-600 seconds' +%Y-%m-%dT%H:%M:%SZ)

# G1 verifying, last update 600 s ago, lock pid dead → stale, error, E_RUNNER_LOST
write_txn verifying "$OLD_TS"; printf '%s\n' "$DEAD_PID" > "$UPD/update.lock"
expect_get "G1 verifying, 600 s old, lock pid dead" True error E_RUNNER_LOST
body=$(run_get)
[ "$(json_field "$body" runner_alive)" = "False" ] && ok "G1 runner_alive=false reported" \
  || bad "G1 runner_alive: $(json_field "$body" runner_alive) (want False)"
# G2 same transaction, lock pid = a live runner → not stale, running
printf '%s\n' "$LIVE_PID" > "$UPD/update.lock"
expect_get "G2 verifying, 600 s old, lock pid alive (runner cmdline)" False running None
body=$(run_get)
[ "$(json_field "$body" runner_alive)" = "True" ] && ok "G2 runner_alive=true reported" \
  || bad "G2 runner_alive: $(json_field "$body" runner_alive) (want True)"
# G2b lock pid alive but its cmdline is not the runner (pid reuse) → stale
printf '%s\n' "$OTHER_PID" > "$UPD/update.lock"
expect_get "G2b verifying, lock pid alive but not a runner (pid reuse)" True error E_RUNNER_LOST
# G3 dead pid but updated_at = now → inside the grace window, not stale
write_txn verifying "$(now_utc)"; printf '%s\n' "$DEAD_PID" > "$UPD/update.lock"
expect_get "G3 verifying, updated just now, lock pid dead (grace)" False running None
# G4 terminal stage → never stale, status done
write_txn done "$OLD_TS"
expect_get "G4 stage=done, 600 s old, lock pid dead" False done None
# G5 a transaction that already carries a code keeps it when it goes stale
printf '{"schema_version":1,"id":"abcdef12-0000-4000-8000-000000000001","stage":"rolling_back","progress_pct":90,"result":"pending","error_code":"E_HEALTH","error_message":"unit not active: nginx (inactive)","updated_at":"%s"}\n' "$OLD_TS" > "$UPD/transaction.json"
expect_get "G5 rolling_back, 600 s old, dead pid, own code" True error E_HEALTH
# G6 the unit half of the liveness test must FIRE for a oneshot unit while its
# ExecStart runs: systemd reports ActiveState=activating (is-active rc 3) for
# the whole run of sa02m-update-verify.service, so an `is-active --quiet`
# clause never returns 0 (review 1.0.6.52, finding 3). Dead lock pid, old
# updated_at, the verify unit activating → alive, not stale.
cat > "$BIN/systemctl" <<'SHIM'
#!/bin/bash
case "${1:-}" in
  is-active) exit 3 ;;
  show)
    u=""; for a in "$@"; do u=$a; done
    case "$*" in *ActiveState*) if [ "$u" = sa02m-update-verify.service ]; then echo activating; else echo inactive; fi ;; esac
    exit 0 ;;
  *) exit 0 ;;
esac
SHIM
chmod +x "$BIN/systemctl"
write_txn verifying "$OLD_TS"; printf '%s\n' "$DEAD_PID" > "$UPD/update.lock"
expect_get "G6 verifying, 600 s old, dead pid, sa02m-update-verify.service activating (oneshot mid-run)" False running None
# ═══ R. reboot.cgi refuses while a LIVE runner works, reboots a stale one ═══
# (1.0.6.52, F5a) A hard reset mid-apply was one click away: reboot.cgi ran
# `reboot -f` with no look at the transaction. Now: applying / verifying /
# committing / rolling_back AND a live runner → E_UPDATE_RUNNING, no sudo; a
# STALE transaction (runner gone) stays rebootable — the reboot IS its recovery
# path (recover → sa02m-update-verify at boot). The sudo chain of reboot.cgi
# runs inside a nohup'd `sh -c` whose stdout is redirected into
# /var/log/sa02m_install.log — absent on a dev host, so the shell never starts
# it there and «sudo called» is observable only as an ABSENCE; the ok:true body
# is the positive observable for the rebootable cases.
# RED, observed 2026-09-23 on the 1.0.6.50 reboot.cgi: R1 answers ok:true (it
# refuses nothing); R2–R4 hold on both trees.
echo
echo "── R. reboot.cgi vs a running update ──"
REBOOT_CGI="$(dirname "$CGI")/reboot.cgi"
[ -f "$REBOOT_CGI" ] || { bad "R reboot.cgi not found beside the CGI: $REBOOT_CGI"; }
export SA02M_UPDATE_STATEDIR="$UPD"
cat > "$BIN/systemctl" <<'SHIM'
#!/bin/bash
case "${1:-}" in
  is-active) exit 3 ;;
  *) exit 0 ;;
esac
SHIM
chmod +x "$BIN/systemctl"
run_reboot() {  # [csrf]
  rm -f "$T/sudo.calls"
  printf '{}' | REQUEST_METHOD=POST CONTENT_LENGTH=2 QUERY_STRING='' \
    HTTP_COOKIE="session_token=$TOK" HTTP_X_SA02M_CSRF="${1:-$CSRF}" \
    bash "$REBOOT_CGI" 2>/dev/null | tr -d '\r'
}
# R1 live runner at applying → refused, no sudo
write_txn applying "$(now_utc)"; printf '%s\n' "$LIVE_PID" > "$UPD/update.lock"
body=$(run_reboot); sleep 1.5
if printf '%s' "$body" | grep -q '"error_code":"E_UPDATE_RUNNING"' && printf '%s' "$body" | grep -q '"ok":false' && ! sudo_called; then
  ok "R1 live runner at applying → E_UPDATE_RUNNING, no reboot"
else
  bad "R1 live runner at applying → body: ${body##*$'\n\n'}, sudo called: $(sudo_called && echo yes || echo no) (want E_UPDATE_RUNNING, no sudo)"
fi
# R2 stale transaction (runner gone) → rebootable
write_txn verifying "$OLD_TS"; printf '%s\n' "$DEAD_PID" > "$UPD/update.lock"
body=$(run_reboot)
printf '%s' "$body" | grep -q '"ok":true' && ok "R2 stale transaction (dead runner) → reboot allowed (the recovery path)" \
  || bad "R2 stale transaction → body: ${body##*$'\n\n'} (want ok:true)"
# R3 terminal stage with a live pid in the lock → rebootable
write_txn done "$(now_utc)"; printf '%s\n' "$LIVE_PID" > "$UPD/update.lock"
body=$(run_reboot)
printf '%s' "$body" | grep -q '"ok":true' && ok "R3 stage=done → reboot allowed" \
  || bad "R3 stage=done → body: ${body##*$'\n\n'} (want ok:true)"
# R4 CSRF still first: wrong token → E_CSRF, no sudo, even with a live runner
write_txn applying "$(now_utc)"
body=$(run_reboot "wrong-token"); sleep 1.5
if printf '%s' "$body" | grep -q '"error_code":"E_CSRF"' && ! sudo_called; then ok "R4 wrong CSRF → E_CSRF before the update guard, no reboot"
else bad "R4 wrong CSRF → body: ${body##*$'\n\n'}, sudo called: $(sudo_called && echo yes || echo no)"; fi
rm -f "$BIN/systemctl" "$UPD/transaction.json" "$UPD/update.lock"
unset SA02M_UPDATE_STATEDIR

echo
if [ "$fails" -eq 0 ]; then
  echo "test-web-update-apply-guard: ALL OK — the guard refuses everything it cannot prove and launches only on a fresh, newer check"
  exit 0
fi
echo "test-web-update-apply-guard: $fails FAILED"
exit 1
