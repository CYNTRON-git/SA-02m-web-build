#!/bin/bash
# Functional RED/GREEN harness for the two CGIs the 2026-09-16 audit (M2) found
# outside docs/decisions/selective-csrf-policy.md — mqtt_scan.cgi and
# web_update_check.cgi — proving what the static row (cgi-csrf-policy) cannot:
# that the POST+token branch is the ONLY branch reaching the root helper.
# Quality row `cgi-csrf-behaviour`.
#
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped CGI in a sandbox (a minted session + CSRF from the real lib_web_auth.sh, a PATH-shimmed sudo whose invocation is the observable, fixtures in env-directed paths) and reading the JSON body it prints; it pins no source line a comment token could satisfy, and its RED is recorded below against the pre-fix CGIs, not a comment mutation.
#
# What it runs: the REAL CGIs end to end — session cookie + X-SA02M-CSRF minted
# through the shipped lib (SA02M_SESSION_DIR sandboxed), REQUEST_METHOD /
# QUERY_STRING / a POST body on stdin as fcgiwrap would pass them, `sudo`
# replaced by a PATH shim that records its argv, the scanner path redirected via
# SA02M_MQTT_SCAN_PY, the update-check state dir via SA02M_WEB_BUILD_STATEDIR
# and its install log via SA02M_INSTALL_LOG (nginx/fcgiwrap set none of the
# SA02M_* names — they are process environment, never a client input). The
# observable is TWO-fold on every case: the body AND whether sudo was called.
#
# Cases — see the numbered blocks below (1–5 scan, 6–9 check, 10 auth-first).
# Non-vacuity: the launch cases (4, 9) FAIL unless the sudo shim really was
# invoked with the expected helper, so a broken shim, a wrong CGI path or an
# auth failure cannot read as "gate held"; the lib's function shape is asserted
# before use; a missing CGI or lib exits 1.
#
# RED, observed 2026-09-17 against the 1.0.6.48 CGIs (git archive origin/main,
# CGI_DIR=<copy>, its own lib beside them): 8 FAILED. The policy half — 1 (the
# GET scan was parsed and validated, not refused: no method_not_allowed), 2 and
# 3 (POST without / with a wrong token went through to the scanner path:
# «scanner not installed», no E_CSRF), 7 and 8 (GET ?force=1 and a token-less
# POST both reached the helper branch: no refusal). The env-blind half — 4, 6,
# 9: the pre-fix CGIs ignore SA02M_MQTT_SCAN_PY / SA02M_WEB_BUILD_STATEDIR /
# SA02M_INSTALL_LOG, so on them the launch and the cache are unobservable in a
# sandbox (the redirect into /var/log fails before sudo is reached; the body is
# the hard-coded no_cache_yet default) — the same class the apply-guard harness
# recorded. Cases 5 and 10 hold on both trees. GREEN on the fixed CGIs: 10/10.
#
# Windows/git-bash: python3 here is CPython writing CRLF to pipes — every body
# read below is stripped of \r (.ai-dev/notes/quality-gate-environment.md).
#
# Run: bash scripts/dev/test-cgi-csrf-behaviour.sh
#      CGI_DIR=<dir> drives another copy of cgi-bin (the RED run).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

CGI_DIR="${CGI_DIR:-www/network_config/cgi-bin}"
SCAN="$CGI_DIR/mqtt_scan.cgi"
CHECK="$CGI_DIR/web_update_check.cgi"
TOKEN="$CGI_DIR/csrf_token.cgi"
LIB="$CGI_DIR/lib_web_auth.sh"
for f in "$SCAN" "$CHECK" "$TOKEN" "$LIB"; do
  [ -f "$f" ] || { echo "FAIL  not found: $f"; exit 1; }
done

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; mkdir -p "$BIN"
STATE="$T/state"; mkdir -p "$STATE"
export SA02M_WEB_BUILD_STATEDIR="$STATE"
export SA02M_INSTALL_LOG="$T/install.log"
export SA02M_SESSION_DIR="$T/sessions"
SCAN_FIXTURE="$T/mqtt_bus_scan.py"
printf '# fixture: never executed — the sudo shim records the argv and exits\n' > "$SCAN_FIXTURE"
export SA02M_MQTT_SCAN_PY="$SCAN_FIXTURE"

# ── sudo shim: records the call; answers like a scanner would ──────────────
cat > "$BIN/sudo" <<SHIM
#!/bin/bash
printf '%s\n' "\$*" >> "$T/sudo.calls"
printf '{"ok":true,"devices":[],"shim":true}\n'
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
NOSESSION="0000000000000000000000000000000000000000000000000000000000000000"

# run_cgi CGI METHOD QUERY BODY [cookie] [csrf] → body on stdout (CR stripped);
# resets the sudo ledger. An empty csrf argument sends NO header.
run_cgi() {
  local cgi="$1" method="$2" qs="$3" body="$4" cookie="${5:-$TOK}" csrf="${6-$CSRF}"
  rm -f "$T/sudo.calls"
  if [ -n "$csrf" ]; then
    printf '%s' "$body" | REQUEST_METHOD="$method" QUERY_STRING="$qs" CONTENT_LENGTH="${#body}" \
      HTTP_COOKIE="session_token=$cookie" HTTP_X_SA02M_CSRF="$csrf" \
      bash "$cgi" 2>/dev/null | tr -d '\r'
  else
    printf '%s' "$body" | REQUEST_METHOD="$method" QUERY_STRING="$qs" CONTENT_LENGTH="${#body}" \
      HTTP_COOKIE="session_token=$cookie" \
      bash "$cgi" 2>/dev/null | tr -d '\r'
  fi
}
sudo_called() { [ -f "$T/sudo.calls" ]; }
sudo_state() { if sudo_called; then echo "yes ($(tr '\n' ';' < "$T/sudo.calls"))"; else echo no; fi; }
body_tail() { printf '%s' "${1##*$'\n\n'}"; }   # the JSON after the header block
has() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }

# Expect a refusal: body carries $3, and NO sudo. $1 = label, $2 = body.
expect_refused() {
  local label="$1" body="$2" want="$3"
  if has "$(body_tail "$body")" "$want" && ! sudo_called; then ok "$label → refused ($want), no launch"
  else bad "$label → want '$want' + no launch; got body: $(body_tail "$body") — sudo called: $(sudo_state)"; fi
}

SCAN_BODY='{"port":"/dev/COM1","baudrate":9600,"max_addr":1}'

# ═══ 1. mqtt_scan.cgi — GET with valid params + session: method refused ═════
body=$(run_cgi "$SCAN" GET 'port=/dev/COM1&baudrate=9600&max_addr=1' '')
expect_refused "1 scan GET ?port=… (Lax top-level navigation)" "$body" '"error":"method_not_allowed"'

# ═══ 2. POST, no token header ══════════════════════════════════════════════
body=$(run_cgi "$SCAN" POST '' "$SCAN_BODY" "$TOK" '')
expect_refused "2 scan POST, no X-SA02M-CSRF" "$body" '"error_code":"E_CSRF"'

# ═══ 3. POST, wrong token ══════════════════════════════════════════════════
body=$(run_cgi "$SCAN" POST '' "$SCAN_BODY" "$TOK" 'wrong-token')
expect_refused "3 scan POST, wrong X-SA02M-CSRF" "$body" '"error_code":"E_CSRF"'

# ═══ 4. POST, valid token + params → the ONE path to the root scanner ══════
body=$(run_cgi "$SCAN" POST '' "$SCAN_BODY")
if sudo_called && grep -qF -- "$SCAN_FIXTURE" "$T/sudo.calls" && grep -q '^/usr/bin/python3 ' "$T/sudo.calls"; then
  ok "4 scan POST + token + valid params → launched (sudo /usr/bin/python3 \$SA02M_MQTT_SCAN_PY)"
else
  bad "4 scan POST + token → want sudo /usr/bin/python3 $SCAN_FIXTURE; sudo called: $(sudo_state); body: $(body_tail "$body")"
fi

# ═══ 5. POST, valid token, port outside the allow-list ═════════════════════
body=$(run_cgi "$SCAN" POST '' '{"port":"../etc","baudrate":9600,"max_addr":1}')
expect_refused "5 scan POST + token, port=../etc (allow-list survives the edit)" "$body" 'invalid scan parameters'

# ═══ 6. web_update_check.cgi — GET, no force: the cache, no helper ══════════
FIXTURE_CHECK='{"checked_at":"2026-09-17T00:00:00Z","remote_version":"9.9.9","deployed_version":"1.0.6.49","update_available":true,"fixture":"csrf-behaviour"}'
printf '%s\n' "$FIXTURE_CHECK" > "$STATE/check.json"
body=$(run_cgi "$CHECK" GET '' '')
if has "$(body_tail "$body")" '"fixture":"csrf-behaviour"' && ! sudo_called; then ok "6 check GET → cached check.json, no launch"
else bad "6 check GET → want the fixture body + no launch; body: $(body_tail "$body"); sudo called: $(sudo_state)"; fi

# ═══ 7. GET ?force=1 → still the cache only (the closed GET-mutation) ═══════
body=$(run_cgi "$CHECK" GET 'force=1' '')
if has "$(body_tail "$body")" '"fixture":"csrf-behaviour"' && ! sudo_called; then ok "7 check GET ?force=1 → cached body, no launch (GET never forces)"
else bad "7 check GET ?force=1 → want the fixture body + no launch; body: $(body_tail "$body"); sudo called: $(sudo_state)"; fi

# ═══ 8. POST, no token → E_CSRF from web_csrf_require, no helper ═══════════
body=$(run_cgi "$CHECK" POST 'force=1' '' "$TOK" '')
expect_refused "8 check POST, no X-SA02M-CSRF" "$body" '"error_code":"E_CSRF"'

# ═══ 9. POST + token → the helper runs, then the cache is served ═══════════
body=$(run_cgi "$CHECK" POST 'force=1' '')
if sudo_called && grep -q -- '-n /usr/local/sbin/sa02m-web-update-check --manual' "$T/sudo.calls" \
   && has "$(body_tail "$body")" '"fixture":"csrf-behaviour"'; then
  ok "9 check POST + token → launched (sudo -n sa02m-web-update-check --manual), then the cache body"
else
  bad "9 check POST + token → want the launch + fixture body; sudo called: $(sudo_state); body: $(body_tail "$body")"
fi

# ═══ 10. no session on POST → unauthorized before any gate, both CGIs ══════
body=$(run_cgi "$SCAN" POST '' "$SCAN_BODY" "$NOSESSION")
b1=$(body_tail "$body"); s1=$(sudo_state)
body=$(run_cgi "$CHECK" POST 'force=1' '' "$NOSESSION")
b2=$(body_tail "$body"); s2=$(sudo_state)
if has "$b1" '"error":"unauthorized"' && [ "$s1" = no ] && has "$b2" '"error":"unauthorized"' && [ "$s2" = no ]; then
  ok "10 unknown session → unauthorized on both, no launch (auth first)"
else
  bad "10 unknown session → scan: $b1 (sudo $s1); check: $b2 (sudo $s2)"
fi

# ═══ 11–14. csrf_token.cgi (1.0.6.53): the session's own token, GET-only ═════
# The panel refreshes its X-SA02M-CSRF value through this endpoint instead of
# logging the user out on E_CSRF (a session older than the sa02m_csrf cookie —
# the upgrade window — or a token file gone). It must hand the token ONLY to
# the session named by the cookie (web_session_check_cookie first), never on a
# POST, and mint the file when it is missing (the same self-scoped act login.cgi
# performs). RED on 91157d5 (1.0.6.52): the CGI does not exist — the file check
# above exits 1 («FAIL not found: …/csrf_token.cgi»).
# Capture, then take the first line in-shell (quality-gate-rigor.md shape f: no
# producer piped into an early-exit consumer under pipefail).
token_of() { local v; v=$(printf '%s' "${1##*$'\n\n'}" | sed -n 's/.*"csrf":"\([a-f0-9]*\)".*/\1/p'); printf '%s' "${v%%$'\n'*}"; }

# ═══ 11. GET with a live session → ok:true + the lib's token for THAT session ═
body=$(run_cgi "$TOKEN" GET '' '' "$TOK" '')
if has "$(body_tail "$body")" '"ok":true' && [ "$(token_of "$body")" = "$CSRF" ] && ! sudo_called; then
  ok "11 token GET + session → ok:true, csrf == the session's token, no sudo"
else
  bad "11 token GET + session → want ok:true + csrf=$CSRF; got: $(body_tail "$body"); sudo: $(sudo_state)"
fi
# The headers it promises: JSON, never cached, not sniffable (the token must not
# be script-embeddable from another origin).
hdrs=$(REQUEST_METHOD=GET QUERY_STRING='' CONTENT_LENGTH=0 HTTP_COOKIE="session_token=$TOK" bash "$TOKEN" </dev/null 2>/dev/null | tr -d '\r' | sed '/^$/q')
if has "$hdrs" 'Cache-Control: no-store' && has "$hdrs" 'X-Content-Type-Options: nosniff' && has "$hdrs" 'application/json'; then
  ok "11b token GET → Content-type JSON, Cache-Control no-store, nosniff"
else
  bad "11b token GET headers → got: $(printf '%s' "$hdrs" | tr '\n' '|')"
fi

# ═══ 12. no / unknown session → unauthorized, no token, nothing minted ═══════
body=$(run_cgi "$TOKEN" GET '' '' "$NOSESSION" '')
if has "$(body_tail "$body")" '"error":"unauthorized"' && [ -z "$(token_of "$body")" ] && ! has "$(body_tail "$body")" '"csrf"'; then
  ok "12 token GET, unknown session → unauthorized, no csrf field"
else
  bad "12 token GET, unknown session → got: $(body_tail "$body")"
fi

# ═══ 13. a session whose .csrf file is gone: the GET mints one, a POST with it validates ═
TOK3=$(web_session_create admin)
rm -f "$SA02M_SESSION_DIR/$(printf '%s' "$TOK3" | sha256sum | cut -d' ' -f1).csrf"
body=$(run_cgi "$CHECK" POST 'force=1' '' "$TOK3" 'anything')
expect_refused "13a POST with a token-less session (upgrade window) → E_CSRF, no launch" "$body" '"error_code":"E_CSRF"'
body=$(run_cgi "$TOKEN" GET '' '' "$TOK3" '')
NEWTOK=$(token_of "$body")
if [[ "$NEWTOK" =~ ^[a-f0-9]{64}$ ]] && [ "$NEWTOK" != "$CSRF" ]; then
  ok "13b token GET mints a fresh per-session token for the token-less session"
else
  bad "13b token GET for the token-less session → got: $(body_tail "$body")"
fi
printf '%s\n' "$FIXTURE_CHECK" > "$STATE/check.json"
body=$(run_cgi "$CHECK" POST 'force=1' '' "$TOK3" "$NEWTOK")
if sudo_called && has "$(body_tail "$body")" '"fixture":"csrf-behaviour"'; then
  ok "13c the minted token validates the next POST (the helper runs)"
else
  bad "13c POST with the minted token → want the launch; sudo: $(sudo_state); body: $(body_tail "$body")"
fi

# ═══ 14. POST to the token endpoint → method_not_allowed, no token ══════════
body=$(run_cgi "$TOKEN" POST '' '{}' "$TOK" '')
if has "$(body_tail "$body")" '"error":"method_not_allowed"' && ! has "$(body_tail "$body")" '"csrf"'; then
  ok "14 token POST → method_not_allowed, no token"
else
  bad "14 token POST → got: $(body_tail "$body")"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "test-cgi-csrf-behaviour: ALL OK — the root scanner and the root update check run only on POST with a valid X-SA02M-CSRF"
  exit 0
fi
echo "test-cgi-csrf-behaviour: $fails FAILED"
exit 1
