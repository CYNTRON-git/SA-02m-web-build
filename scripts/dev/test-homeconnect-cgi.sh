#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# test-homeconnect-cgi.sh — behavioural harness for the Home Connect card
# endpoint www/network_config/cgi-bin/sa02m_homeconnect_api.cgi (quality row
# `homeconnect-cgi`; contract docs/contracts/home-connect.md §9, CSRF policy
# docs/decisions/selective-csrf-policy.md).
#
# Method: the SHIPPED CGI is copied into a sandbox tree and RUN. Its
# lib_web_auth.sh is a stub whose session / CSRF verdicts come from
# STUB_AUTH_RC / STUB_CSRF_RC; `sudo` is a PATH stub that RECORDS its argv and
# never executes anything; the dispatch is a scripted `sa02m_homeconnect.api`
# package reached through SA02M_HOMECONNECT_ROOT (the same two-line protocol
# as the real opt/sa02m-homeconnect/sa02m_homeconnect/api.py). Nothing here
# needs root, nginx or a device; nothing is ever run through the real sudo.
#
# Cases:
#   1  no session              → unauthorized; dispatch and sudo NEVER run
#                                (auth first — even with a failing CSRF token)
#   2  PUT / DELETE            → method_not_allowed; nothing runs
#   3  POST, bad CSRF          → E_CSRF body; dispatch and sudo never run
#   4  POST, CONTENT_LENGTH over 16 KiB / 7 digits → payload_too_large; nothing
#      runs. A non-numeric length reads as an empty body
#   5  GET                     → dispatch(GET) with an EMPTY stdin; a verb the
#                                dispatch returns is NOT nudged on GET
#   6  POST, each pinned verb  → sudo argv is exactly
#                                `-n /usr/local/sbin/sa02m-homeconnect-web-trigger.sh <verb>`;
#                                the body reaches the dispatch stdin intact;
#                                the answer is ONE JSON object + "trigger":"ok"
#   7  POST, a verb outside the pin (injection shapes, the HomeKit verb
#      `reset-pairing`, an action spelling, a trailing space) → sudo never
#      runs, no "trigger" field
#   8  helper fails            → "trigger":"failed" + its [a-z_] error code;
#                                an error string outside [a-z_] is NOT echoed
#   9  helper hangs            → "trigger":"timeout" within the 11 s budget
#   10 dispatch prints garbage / nothing / crashes → homeconnect_api_failed
#   11 package absent          → GET not_installed JSON, POST not_installed
#                                error; the dispatch never runs
#   K  the CGI's own error codes (the `"error":"…"` literals of the shipped
#      file, plus `csrf` from the shared body) EQUAL the contract's §9
#      «Код CGI» table — non-vacuous: an empty table or < 4 literals FAILS
#   R  the REAL package (when api.py exists): GET answers one JSON object
#      with a known state and never a token or the device code (both seeded
#      where the daemon keeps them); a mutation on a board without the
#      install footprint is not_installed and reaches no sudo; an unknown
#      action is not_found. Reported as SKIP — never as pass — while api.py
#      is absent.
# Non-vacuous: a missing CGI, a stub that was never invoked where it must be,
# or a body that is not exactly one JSON object FAILS.
#
# Run: bash scripts/dev/test-homeconnect-cgi.sh   (bash, python3, coreutils)
#      HC_CGI_SRC=<file> judges another copy of the CGI (the RED run).
# ═══════════════════════════════════════════════════════════════════════════
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CGI_SRC="${HC_CGI_SRC:-$HERE/www/network_config/cgi-bin/sa02m_homeconnect_api.cgi}"
REAL_PY="$(command -v python3 || true)"
fails=0
skips=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf '  SKIP  %s\n' "$1"; skips=$((skips + 1)); }

[ -r "$CGI_SRC" ] || { echo "homeconnect-cgi: FAIL — cannot read $CGI_SRC"; exit 1; }
[ -n "$REAL_PY" ] || { echo "homeconnect-cgi: FAIL — no python3 on PATH"; exit 1; }
command -v timeout >/dev/null || { echo "homeconnect-cgi: FAIL — coreutils timeout missing"; exit 1; }

BOX="$(mktemp -d "${TMPDIR:-/tmp}/hc-cgi.XXXXXX")" || { echo "homeconnect-cgi: FAIL — mktemp"; exit 1; }
trap 'rm -rf -- "$BOX"' EXIT
CGI_DIR="$BOX/www/network_config/cgi-bin"
mkdir -p "$CGI_DIR" "$BOX/bin" "$BOX/pkg/sa02m_homeconnect"
cp "$CGI_SRC" "$CGI_DIR/sa02m_homeconnect_api.cgi"

cat > "$CGI_DIR/lib_web_auth.sh" <<'SH'
web_session_check_cookie() { echo auth >> "$STUB_CALLS"; [ "${STUB_AUTH_RC:-0}" -eq 0 ]; }
web_csrf_validate() { echo csrf >> "$STUB_CALLS"; [ "${STUB_CSRF_RC:-0}" -eq 0 ]; }
web_csrf_error_body() { printf '{"ok":false,"error":"csrf","error_code":"E_CSRF","reason":"stub"}\n'; }
SH

# sudo: record argv, answer per STUB_SUDO. `exec sleep` so `timeout` kills
# the process itself, never an orphan.
cat > "$BOX/bin/sudo" <<'SH'
#!/usr/bin/env bash
printf 'sudo %s\n' "$*" >> "$STUB_CALLS"
case "${STUB_SUDO:-ok}" in
    ok)       printf '{"ok":true,"action":"%s"}\n' "${3:-}" ;;
    fail)     printf '{"ok":false,"error":"still_running"}\n'; exit 1 ;;
    badcode)  printf '{"ok":false,"error":"x\\"y<script>"}\n'; exit 1 ;;
    hang)     exec sleep 30 ;;
esac
SH
chmod +x "$BOX/bin/sudo"

# Scripted dispatch: the real protocol — METHOD in argv, body on stdin, two
# lines out (response JSON, verb).
: > "$BOX/pkg/sa02m_homeconnect/__init__.py"
cat > "$BOX/pkg/sa02m_homeconnect/api.py" <<'PY'
import json, os, sys
body = sys.stdin.read()
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as f:
    f.write("api %s len=%d\n" % (" ".join(sys.argv[1:]), len(body)))
with open(os.environ["STUB_BODY"], "w", encoding="utf-8") as f:
    f.write(body)
mode = os.environ.get("STUB_API", "status")
if mode == "garbage":
    print("Traceback (most recent call last):")
    sys.exit(0)
if mode == "silent":
    sys.exit(0)
if mode == "crash":
    raise RuntimeError("boom")
print(json.dumps({"ok": True, "state": "connected", "name": "Посудомойка \"A\""}, ensure_ascii=False))
print(os.environ.get("STUB_VERB", ""))
PY

STUB_CALLS="$BOX/calls"
STUB_BODY="$BOX/body"

# run_cgi METHOD BODY [VAR=value …] → body after the headers, CR-free, in $OUT
run_cgi() {
    local method="$1" body="$2" nbytes; shift 2
    : > "$STUB_CALLS"; : > "$STUB_BODY"
    nbytes=$(printf '%s' "$body" | wc -c | tr -d ' ')
    OUT=$(
        cd "$CGI_DIR" || exit 1
        printf '%s' "$body" | env PATH="$BOX/bin:$PATH" STUB_CALLS="$STUB_CALLS" STUB_BODY="$STUB_BODY" \
            REQUEST_METHOD="$method" CONTENT_LENGTH="$nbytes" \
            SA02M_HOMECONNECT_ROOT="$BOX/pkg" "$@" bash ./sa02m_homeconnect_api.cgi 2>/dev/null \
            | tr -d '\r' | awk 'b{print; next} /^$/{b=1}'
    )
}
ran()    { grep -c "^$1" "$STUB_CALLS" 2>/dev/null; }
sudo_argv() { sed -n 's/^sudo //p' "$STUB_CALLS"; }

# one_doc LABEL PY-EXPR — $OUT must be exactly one JSON object with EXPR true.
one_doc() {
    local label="$1" expr="$2" lines
    lines=$(printf '%s\n' "$OUT" | grep -c .)
    if [ "$lines" -ne 1 ]; then
        bad "$label — expected one line, got $lines: $(printf '%s' "$OUT" | head -c 200 | tr '\n' '|')"
        return 1
    fi
    if printf '%s' "$OUT" | "$REAL_PY" -c '
import json, sys
d = json.loads(sys.stdin.read())
if not isinstance(d, dict):
    raise SystemExit(2)
raise SystemExit(0 if eval(sys.argv[1]) else 3)' "$expr" 2>/dev/null; then
        ok "$label"
    else
        bad "$label — not one JSON object matching [$expr]: $(printf '%s' "$OUT" | head -c 200)"
        return 1
    fi
}

echo "homeconnect-cgi: $CGI_SRC"

echo "1. auth first"
run_cgi POST '{"action":"enable"}' STUB_AUTH_RC=1 STUB_CSRF_RC=1 STUB_VERB=enable
one_doc "1a no session → unauthorized" 'd == {"ok": False, "error": "unauthorized"}'
if [ "$(ran api)" -eq 0 ] && [ "$(ran sudo)" -eq 0 ] && [ "$(ran csrf)" -eq 0 ] && [ "$(ran auth)" -eq 1 ]; then
    ok "1b no dispatch, no sudo, no CSRF read before the session check"
else
    bad "1b work before auth: $(tr '\n' ' ' < "$STUB_CALLS")"
fi

echo "2. method gate"
for m in PUT DELETE; do
    run_cgi "$m" '{"action":"enable"}' STUB_VERB=enable
    one_doc "2 $m → method_not_allowed" 'd.get("error") == "method_not_allowed"'
    [ "$(ran api)" -eq 0 ] && [ "$(ran sudo)" -eq 0 ] || bad "2 $m reached the dispatch or sudo"
done

echo "3. CSRF before any mutation"
run_cgi POST '{"action":"unlink"}' STUB_CSRF_RC=1 STUB_VERB=unlink
one_doc "3a bad token → E_CSRF" 'd.get("error_code") == "E_CSRF"'
[ "$(ran api)" -eq 0 ] && [ "$(ran sudo)" -eq 0 ] && ok "3b no dispatch, no sudo on a bad token" \
    || bad "3b bad token still reached: $(tr '\n' ' ' < "$STUB_CALLS")"
run_cgi POST '{"action":"status"}' STUB_CSRF_RC=1
one_doc "3c even a status POST is token-checked" 'd.get("error_code") == "E_CSRF"'

echo "4. bounded body"
for cl in 16385 9999999; do
    : > "$STUB_CALLS"
    OUT=$(cd "$CGI_DIR" && printf '{}' | env PATH="$BOX/bin:$PATH" STUB_CALLS="$STUB_CALLS" STUB_BODY="$STUB_BODY" \
        REQUEST_METHOD=POST CONTENT_LENGTH="$cl" SA02M_HOMECONNECT_ROOT="$BOX/pkg" bash ./sa02m_homeconnect_api.cgi 2>/dev/null \
        | tr -d '\r' | awk 'b{print; next} /^$/{b=1}')
    one_doc "4 CONTENT_LENGTH=$cl → payload_too_large" 'd.get("error") == "payload_too_large"'
    [ "$(ran api)" -eq 0 ] || bad "4 CONTENT_LENGTH=$cl reached the dispatch"
done
: > "$STUB_CALLS"
OUT=$(cd "$CGI_DIR" && printf '{"action":"enable"}' | env PATH="$BOX/bin:$PATH" STUB_CALLS="$STUB_CALLS" STUB_BODY="$STUB_BODY" \
    REQUEST_METHOD=POST CONTENT_LENGTH=abc SA02M_HOMECONNECT_ROOT="$BOX/pkg" bash ./sa02m_homeconnect_api.cgi 2>/dev/null \
    | tr -d '\r' | awk 'b{print; next} /^$/{b=1}')
grep -q '^api POST len=0$' "$STUB_CALLS" && ok "4 a non-numeric CONTENT_LENGTH reads as an empty body" \
    || bad "4 non-numeric CONTENT_LENGTH: $(tr '\n' ' ' < "$STUB_CALLS")"

echo "5. GET never nudges"
run_cgi GET '' STUB_VERB=enable
one_doc "5a GET → the dispatch answer, verbatim" 'd.get("state") == "connected" and "trigger" not in d'
if grep -q '^api GET len=0$' "$STUB_CALLS" && [ "$(ran sudo)" -eq 0 ]; then
    ok "5b dispatch(GET) with an empty stdin; a returned verb is NOT nudged on GET"
else
    bad "5b GET: $(tr '\n' ' ' < "$STUB_CALLS")"
fi

echo "6. the four pinned verbs"
for verb in enable disable restart unlink; do
    body='{"action":"x","pad":"Кухня — «тест»"}'
    run_cgi POST "$body" STUB_VERB="$verb"
    one_doc "6 verb $verb → one JSON object, trigger ok, dispatch fields kept" \
        'd.get("trigger") == "ok" and d.get("state") == "connected" and d.get("name") == "Посудомойка \"A\""'
    want="-n /usr/local/sbin/sa02m-homeconnect-web-trigger.sh $verb"
    if [ "$(ran sudo)" -eq 1 ] && [ "$(sudo_argv)" = "$want" ]; then
        ok "6 verb $verb → sudo argv exactly [$want]"
    else
        bad "6 verb $verb → sudo calls: $(sudo_argv | tr '\n' '|')"
    fi
    [ "$(cat "$STUB_BODY")" = "$body" ] || bad "6 verb $verb → the body did not reach the dispatch intact"
done

echo "7. verbs outside the pin never reach sudo"
for verb in 'reset-pairing' 'set_client_id' 'unlink ' 'enable;reboot' '$(reboot)' '-u root' 'status' 'restart extra' 'link'; do
    run_cgi POST '{"action":"x"}' STUB_VERB="$verb"
    if [ "$(ran sudo)" -eq 0 ] && [ -n "$OUT" ] && [[ $OUT != *'"trigger"'* ]]; then
        ok "7 verb [$verb] → no sudo, no trigger field"
    else
        bad "7 verb [$verb] → sudo calls: $(sudo_argv | tr '\n' '|') out=$OUT"
    fi
done

echo "8. helper failure is reported, never echoed"
run_cgi POST '{"action":"unlink"}' STUB_VERB=unlink STUB_SUDO=fail
one_doc "8a helper rc≠0 → trigger failed + trigger_error still_running" \
    'd.get("trigger") == "failed" and d.get("trigger_error") == "still_running"'
run_cgi POST '{"action":"unlink"}' STUB_VERB=unlink STUB_SUDO=badcode
one_doc "8b an error code outside [a-z_] is not echoed" \
    'd.get("trigger") == "failed" and "trigger_error" not in d and "script" not in json.dumps(d)'

echo "9. helper hang is bounded"
t0=$(date +%s)
run_cgi POST '{"action":"enable"}' STUB_VERB=enable STUB_SUDO=hang
t1=$(date +%s)
one_doc "9a hung helper → trigger timeout" 'd.get("trigger") == "timeout"'
[ $((t1 - t0)) -le 15 ] && ok "9b answered in $((t1 - t0)) s (budget 11 s + slack, under nginx's 20 s)" \
    || bad "9b took $((t1 - t0)) s"

echo "10. dispatch failure"
for mode in garbage silent crash; do
    run_cgi POST '{"action":"enable"}' STUB_API="$mode" STUB_VERB=enable
    one_doc "10 dispatch $mode → homeconnect_api_failed" 'd.get("error") == "homeconnect_api_failed"'
    [ "$(ran sudo)" -eq 0 ] || bad "10 dispatch $mode still nudged sudo"
done

echo "11. package absent"
run_cgi GET '' SA02M_HOMECONNECT_ROOT="$BOX/nonexistent"
one_doc "11a GET → not_installed state" 'd.get("ok") is True and d.get("state") == "not_installed" and d.get("linked") is False and d.get("read_only") is True'
[ "$(ran api)" -eq 0 ] || bad "11a the dispatch ran without a package"
run_cgi POST '{"action":"enable"}' SA02M_HOMECONNECT_ROOT="$BOX/nonexistent"
one_doc "11b POST → not_installed error" 'd.get("ok") is False and d.get("error") == "not_installed"'
[ "$(ran api)" -eq 0 ] && [ "$(ran sudo)" -eq 0 ] || bad "11b work ran without a package"

echo "K. the CGI's own error codes = the contract's «Код CGI» table"
CONTRACT="$HERE/docs/contracts/home-connect.md"
k_src=$(sed 's/^[[:space:]]*#.*$//' "$CGI_SRC" | grep -oE '"error":"[a-z_]+"' | sed 's/.*:"//; s/"$//' | sort -u)
k_doc=$(awk '/^\| Код CGI /{f=1; next} f && /^\|/{print; next} f{exit}' "$CONTRACT" \
        | grep -v '^|[-: |]*$' | sed -n 's/^| *`\([a-z_]*\)`.*/\1/p' | sort -u)
k_want=$(printf '%s\ncsrf\n' "$k_src" | grep . | sort -u)
if [ "$(printf '%s\n' "$k_src" | grep -c .)" -lt 4 ] || [ -z "$k_doc" ]; then
    bad "K non-vacuity: $(printf '%s\n' "$k_src" | grep -c .) error literal(s) in the CGI, contract table rows: '$(printf '%s' "$k_doc" | tr '\n' ' ')'"
elif [ "$k_doc" = "$k_want" ]; then
    ok "K CGI error codes == contract §9 «Код CGI» ($(printf '%s' "$k_doc" | tr '\n' ' '))"
else
    bad "K CGI error codes differ from contract §9 «Код CGI» — CGI (+csrf): $(printf '%s' "$k_want" | tr '\n' ' ') | contract: $(printf '%s' "$k_doc" | tr '\n' ' ')"
fi

echo "R. the real package"
REAL_PKG="$HERE/opt/sa02m-homeconnect"
if [ -f "$REAL_PKG/sa02m_homeconnect/api.py" ]; then
    RS="$BOX/real"
    mkdir -p "$RS/etc" "$RS/var" "$RS/run"
    printf '[account]\nenabled = true\nclient_id = ABCDEFGH12345678\nhost = api\nlink_requested_at = 0\n' > "$RS/etc/sa02m-homeconnect.conf"
    now=$(date +%s)
    printf '{"access_token":"AT-LEAK-MARKER-1","refresh_token":"RT-LEAK-MARKER-2","device_code":"DC-LEAK-MARKER-3"}\n' > "$RS/var/tokens.json"
    chmod 0600 "$RS/var/tokens.json"
    printf '{"state":"awaiting_user","ts":%s,"linked":false}\n' "$now" > "$RS/run/status.json"
    printf '{"user_code":"WXYZ-1234","verification_uri":"https://verify.home-connect.com/","verification_uri_complete":"https://verify.home-connect.com/?code=WXYZ-1234","expires_at":%s,"ts":%s,"device_code":"DC-LEAK-MARKER-3"}\n' "$((now + 300))" "$now" > "$RS/run/link.json"
    renv=(SA02M_HOMECONNECT_ROOT="$REAL_PKG" SA02M_HOMECONNECT_ETC="$RS/etc" SA02M_HOMECONNECT_VAR="$RS/var" SA02M_HOMECONNECT_RUN="$RS/run")
    run_cgi GET '' "${renv[@]}"
    one_doc "R1 real GET → one JSON object with a known state" \
        'd.get("state") in ("not_installed","disabled","missing_deps","missing_client_id","unlinked","awaiting_user","link_expired","connecting","connected","rate_limited","offline","token_revoked","error") and d.get("read_only") is True'
    case "$OUT" in
        *LEAK-MARKER*) bad "R2 a token or the device code leaked into the GET answer" ;;
        *) ok "R2 no token and no device code in the GET answer (both seeded where the daemon keeps them)" ;;
    esac
    run_cgi POST '{"action":"enable"}' "${renv[@]}"
    one_doc "R3 real mutation without the install footprint → not_installed" 'd.get("error") == "not_installed"'
    [ "$(ran sudo)" -eq 0 ] && ok "R4 not_installed reaches no sudo" || bad "R4 not_installed nudged: $(sudo_argv)"
    run_cgi POST '{"action":"no_such_action"}' "${renv[@]}"
    one_doc "R5 real unknown action → not_found" 'd.get("error") == "not_found"'
    [ "$(ran sudo)" -eq 0 ] && ok "R6 unknown action reaches no sudo" || bad "R6 unknown action nudged: $(sudo_argv)"
else
    skip "R real-package cases: $REAL_PKG/sa02m_homeconnect/api.py absent (not a pass)"
fi

if [ "$fails" -eq 0 ]; then
    if [ "$skips" -gt 0 ]; then
        echo "homeconnect-cgi: all checks passed, $skips SKIPPED (reported as skip, not pass)"
    else
        echo "homeconnect-cgi: all checks passed"
    fi
    exit 0
fi
echo "homeconnect-cgi: $fails FAILURE(S)"
exit 1
