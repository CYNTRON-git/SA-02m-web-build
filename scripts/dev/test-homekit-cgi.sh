#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# test-homekit-cgi.sh — behavioural harness for the HomeKit card endpoint
# www/network_config/cgi-bin/sa02m_homekit_api.cgi (planned quality row
# `homekit-cgi`; contract docs/contracts/homekit-bridge.md, CSRF policy
# docs/decisions/selective-csrf-policy.md).
#
# Method: the SHIPPED CGI is copied into a sandbox tree and RUN. Its
# lib_web_auth.sh is a stub whose session / CSRF verdicts come from
# STUB_AUTH_RC / STUB_CSRF_RC; `sudo` is a PATH stub that RECORDS its argv and
# never executes anything; the dispatch is a scripted `sa02m_homekit.api`
# package reached through SA02M_HOMEKIT_ROOT (the same two-line protocol as
# the real opt/sa02m-homekit/sa02m_homekit/api.py). Nothing here needs root,
# nginx or a device; nothing is ever run through the real sudo.
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
#                                `-n /usr/local/sbin/sa02m-homekit-web-trigger.sh <verb>`;
#                                the body reaches the dispatch stdin intact;
#                                the answer is ONE JSON object + "trigger":"ok"
#   7  POST, a verb outside the pin (injection shapes, the action spelling
#      `reset_pairing`, a trailing space) → sudo never runs, no "trigger" field
#   8  helper fails            → "trigger":"failed" + its [a-z_] error code;
#                                an error string outside [a-z_] is NOT echoed
#   9  helper hangs            → "trigger":"timeout" within the 11 s budget
#   10 dispatch prints garbage / nothing / crashes → homekit_api_failed
#   11 package absent          → GET not_installed JSON, POST not_installed
#                                error; the dispatch never runs
#   R  the REAL package (when api.py exists): GET answers one JSON object with
#      a known state and never the setup code; an unknown action is not_found
#      and reaches no sudo. Reported as SKIP — never as pass — while api.py is
#      absent.
# Non-vacuous: a missing CGI, a stub that was never invoked where it must be,
# or a body that is not exactly one JSON object FAILS.
#
# Run: bash scripts/dev/test-homekit-cgi.sh     (bash, python3, coreutils)
#      HK_CGI_SRC=<file> judges another copy of the CGI (the RED run).
# ═══════════════════════════════════════════════════════════════════════════
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CGI_SRC="${HK_CGI_SRC:-$HERE/www/network_config/cgi-bin/sa02m_homekit_api.cgi}"
REAL_PY="$(command -v python3 || true)"
fails=0
skips=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf '  SKIP  %s\n' "$1"; skips=$((skips + 1)); }

[ -r "$CGI_SRC" ] || { echo "homekit-cgi: FAIL — cannot read $CGI_SRC"; exit 1; }
[ -n "$REAL_PY" ] || { echo "homekit-cgi: FAIL — no python3 on PATH"; exit 1; }
command -v timeout >/dev/null || { echo "homekit-cgi: FAIL — coreutils timeout missing"; exit 1; }

BOX="$(mktemp -d "${TMPDIR:-/tmp}/hk-cgi.XXXXXX")" || { echo "homekit-cgi: FAIL — mktemp"; exit 1; }
trap 'rm -rf -- "$BOX"' EXIT
CGI_DIR="$BOX/www/network_config/cgi-bin"
mkdir -p "$CGI_DIR" "$BOX/bin" "$BOX/pkg/sa02m_homekit"
cp "$CGI_SRC" "$CGI_DIR/sa02m_homekit_api.cgi"

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
: > "$BOX/pkg/sa02m_homekit/__init__.py"
cat > "$BOX/pkg/sa02m_homekit/api.py" <<'PY'
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
print(json.dumps({"ok": True, "state": "running", "name": "Кухня \"A\""}, ensure_ascii=False))
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
            SA02M_HOMEKIT_ROOT="$BOX/pkg" "$@" bash ./sa02m_homekit_api.cgi 2>/dev/null \
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

echo "homekit-cgi: $CGI_SRC"

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
run_cgi POST '{"action":"reset_pairing"}' STUB_CSRF_RC=1 STUB_VERB=reset-pairing
one_doc "3a bad token → E_CSRF" 'd.get("error_code") == "E_CSRF"'
[ "$(ran api)" -eq 0 ] && [ "$(ran sudo)" -eq 0 ] && ok "3b no dispatch, no sudo on a bad token" \
    || bad "3b bad token still reached: $(tr '\n' ' ' < "$STUB_CALLS")"
run_cgi POST '{"action":"setup"}' STUB_CSRF_RC=1
one_doc "3c the setup-code action is token-checked too" 'd.get("error_code") == "E_CSRF"'

echo "4. bounded body"
for cl in 16385 9999999; do
    : > "$STUB_CALLS"
    OUT=$(cd "$CGI_DIR" && printf '{}' | env PATH="$BOX/bin:$PATH" STUB_CALLS="$STUB_CALLS" STUB_BODY="$STUB_BODY" \
        REQUEST_METHOD=POST CONTENT_LENGTH="$cl" SA02M_HOMEKIT_ROOT="$BOX/pkg" bash ./sa02m_homekit_api.cgi 2>/dev/null \
        | tr -d '\r' | awk 'b{print; next} /^$/{b=1}')
    one_doc "4 CONTENT_LENGTH=$cl → payload_too_large" 'd.get("error") == "payload_too_large"'
    [ "$(ran api)" -eq 0 ] || bad "4 CONTENT_LENGTH=$cl reached the dispatch"
done
: > "$STUB_CALLS"
OUT=$(cd "$CGI_DIR" && printf '{"action":"enable"}' | env PATH="$BOX/bin:$PATH" STUB_CALLS="$STUB_CALLS" STUB_BODY="$STUB_BODY" \
    REQUEST_METHOD=POST CONTENT_LENGTH=abc SA02M_HOMEKIT_ROOT="$BOX/pkg" bash ./sa02m_homekit_api.cgi 2>/dev/null \
    | tr -d '\r' | awk 'b{print; next} /^$/{b=1}')
grep -q '^api POST len=0$' "$STUB_CALLS" && ok "4 a non-numeric CONTENT_LENGTH reads as an empty body" \
    || bad "4 non-numeric CONTENT_LENGTH: $(tr '\n' ' ' < "$STUB_CALLS")"

echo "5. GET never nudges"
run_cgi GET '' STUB_VERB=enable
one_doc "5a GET → the dispatch answer, verbatim" 'd.get("state") == "running" and "trigger" not in d'
if grep -q '^api GET len=0$' "$STUB_CALLS" && [ "$(ran sudo)" -eq 0 ]; then
    ok "5b dispatch(GET) with an empty stdin; a returned verb is NOT nudged on GET"
else
    bad "5b GET: $(tr '\n' ' ' < "$STUB_CALLS")"
fi

echo "6. the four pinned verbs"
for verb in enable disable restart reset-pairing; do
    body='{"action":"x","pad":"Кухня — «тест»"}'
    run_cgi POST "$body" STUB_VERB="$verb"
    one_doc "6 verb $verb → one JSON object, trigger ok, dispatch fields kept" \
        'd.get("trigger") == "ok" and d.get("state") == "running" and d.get("name") == "Кухня \"A\""'
    want="-n /usr/local/sbin/sa02m-homekit-web-trigger.sh $verb"
    if [ "$(ran sudo)" -eq 1 ] && [ "$(sudo_argv)" = "$want" ]; then
        ok "6 verb $verb → sudo argv exactly [$want]"
    else
        bad "6 verb $verb → sudo calls: $(sudo_argv | tr '\n' '|')"
    fi
    [ "$(cat "$STUB_BODY")" = "$body" ] || bad "6 verb $verb → the body did not reach the dispatch intact"
done

echo "7. verbs outside the pin never reach sudo"
for verb in 'reset_pairing' 'enable ' 'enable;reboot' '$(reboot)' '-u root' 'status' 'restart extra'; do
    run_cgi POST '{"action":"x"}' STUB_VERB="$verb"
    if [ "$(ran sudo)" -eq 0 ] && [ -n "$OUT" ] && [[ $OUT != *'"trigger"'* ]]; then
        ok "7 verb [$verb] → no sudo, no trigger field"
    else
        bad "7 verb [$verb] → sudo calls: $(sudo_argv | tr '\n' '|') out=$OUT"
    fi
done

echo "8. helper failure is reported, never echoed"
run_cgi POST '{"action":"reset_pairing"}' STUB_VERB=reset-pairing STUB_SUDO=fail
one_doc "8a helper rc≠0 → trigger failed + trigger_error still_running" \
    'd.get("trigger") == "failed" and d.get("trigger_error") == "still_running"'
run_cgi POST '{"action":"reset_pairing"}' STUB_VERB=reset-pairing STUB_SUDO=badcode
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
    one_doc "10 dispatch $mode → homekit_api_failed" 'd.get("error") == "homekit_api_failed"'
    [ "$(ran sudo)" -eq 0 ] || bad "10 dispatch $mode still nudged sudo"
done

echo "11. package absent"
run_cgi GET '' SA02M_HOMEKIT_ROOT="$BOX/nonexistent"
one_doc "11a GET → not_installed state" 'd.get("ok") is True and d.get("state") == "not_installed" and d.get("setup_available") is False'
[ "$(ran api)" -eq 0 ] || bad "11a the dispatch ran without a package"
run_cgi POST '{"action":"enable"}' SA02M_HOMEKIT_ROOT="$BOX/nonexistent"
one_doc "11b POST → not_installed error" 'd.get("ok") is False and d.get("error") == "not_installed"'
[ "$(ran api)" -eq 0 ] && [ "$(ran sudo)" -eq 0 ] || bad "11b work ran without a package"

echo "R. the real package"
REAL_PKG="$HERE/opt/sa02m-homekit"
if [ -f "$REAL_PKG/sa02m_homekit/api.py" ]; then
    RS="$BOX/real"
    mkdir -p "$RS/etc" "$RS/var" "$RS/run"
    printf '[bridge]\nenabled = true\ninterface = eth0\nport = 21064\n' > "$RS/etc/sa02m-homekit.conf"
    printf '{"state":"running","ts":%s,"paired":false}\n' "$(date +%s)" > "$RS/run/status.json"
    printf '{"code":"314-15-926","setup_uri":"X-HM://0023ISYWYABCD","setup_id":"ABCD","qr":[],"ts":%s}\n' "$(date +%s)" > "$RS/run/setup.json"
    renv=(SA02M_HOMEKIT_ROOT="$REAL_PKG" SA02M_HOMEKIT_ETC="$RS/etc" SA02M_HOMEKIT_VAR="$RS/var" SA02M_HOMEKIT_RUN="$RS/run")
    run_cgi GET '' "${renv[@]}"
    one_doc "R1 real GET → one JSON object with a known state" \
        'd.get("state") in ("not_installed","disabled","starting","running","missing_deps","no_interface","port_in_use","error")'
    case "$OUT" in *314-15-926*) bad "R2 the setup code leaked into a GET answer" ;; *) ok "R2 the setup code is absent from the GET answer" ;; esac
    run_cgi POST '{"action":"no_such_action"}' "${renv[@]}"
    one_doc "R3 real unknown action → not_found" 'd.get("error") == "not_found"'
    [ "$(ran sudo)" -eq 0 ] && ok "R4 unknown action reaches no sudo" || bad "R4 unknown action nudged: $(sudo_argv)"
else
    skip "R real-package cases: $REAL_PKG/sa02m_homekit/api.py absent (not a pass)"
fi

if [ "$fails" -eq 0 ]; then
    if [ "$skips" -gt 0 ]; then
        echo "homekit-cgi: all checks passed, $skips SKIPPED (reported as skip, not pass)"
    else
        echo "homekit-cgi: all checks passed"
    fi
    exit 0
fi
echo "homekit-cgi: $fails FAILURE(S)"
exit 1
