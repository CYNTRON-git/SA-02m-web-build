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

echo
if [ "$fails" -eq 0 ]; then
  echo "test-web-update-apply-guard: ALL OK — the guard refuses everything it cannot prove and launches only on a fresh, newer check"
  exit 0
fi
echo "test-web-update-apply-guard: $fails FAILED"
exit 1
