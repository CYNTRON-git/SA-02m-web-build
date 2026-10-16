#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# test-audio-cgi.sh — behavioural harness for the «Запись звука» card
# endpoint www/network_config/cgi-bin/sa02m_audio_api.cgi (quality row
# `audio-cgi`; contract docs/contracts/audio-recorder.md §15, CSRF policy
# docs/decisions/selective-csrf-policy.md).
#
# Method (the Home Connect harness's): the SHIPPED CGI is copied into a
# sandbox tree and RUN. Its lib_web_auth.sh is a stub whose session / CSRF
# verdicts come from STUB_AUTH_RC / STUB_CSRF_RC; `sudo`, `mosquitto_pub` and
# `logger` are PATH stubs that RECORD their argv and run nothing; `python3` is
# a PATH shim to the first working interpreter (Windows ships a dead python3
# stub); the dispatch is a scripted `sa02m_audio.api` package reached through
# SA02M_AUDIO_ROOT (the real two-line protocol). Nothing here needs root,
# nginx, a broker or a device.
#
# Cases:
#   1  no session              → unauthorized; dispatch, sudo, mosquitto_pub
#                                NEVER run — for a POST and for a download
#   2  PUT / DELETE            → method_not_allowed; nothing runs
#   3  POST, bad CSRF          → E_CSRF body; nothing runs (even `status`)
#   4  CONTENT_LENGTH over 16 KiB / 7 digits → payload_too_large; a
#      non-numeric length reads as an empty body
#   5  GET                     → dispatch(GET), empty stdin; a follow-up the
#                                dispatch names is NOT executed on GET
#   6  POST, every follow-up of the grammar → mosquitto_pub argv is exactly
#      `-h 127.0.0.1 -p 1883 -q 1 -t <command topic> -m <payload>` (never
#      `-r`), sudo argv exactly `-n /usr/local/sbin/sa02m-audio-web-trigger.sh
#      <verb>`; ONE JSON object + "done":"ok"; one audit line `source=panel`
#   7  follow-ups outside the grammar (injection shapes, a retain flag, a
#      third topic, the Home Connect verb, an extra argument) → nothing runs,
#      no "done" field
#   8  helper fails → "done":"failed" + its [a-z_] code; other text NOT echoed
#   9  helper hangs → "done":"timeout" within the 11 s budget
#   10 mosquitto_pub fails → "done":"failed","done_error":"publish_failed"
#   11 dispatch garbage / nothing / crash → audio_api_failed, nothing runs
#   12 package absent → GET not_installed JSON, POST not_installed error
#   13 download: the dispatch runs in DOWNLOAD mode with the query and its
#      bytes (a NUL included) reach the client unchanged; the CGI printed no
#      header of its own; a query not starting `action=download&` is a status GET
#   K  the CGI's own error codes (`"error":"…"` literals + `csrf`) EQUAL the
#      contract's «Код CGI» table — an empty table or < 4 literals FAILS
#   R  the REAL package: GET is one JSON object with a known state; a
#      mutation without the install footprint is not_installed and runs
#      nothing; an unknown action is not_found
# Non-vacuous: a missing CGI, a stub never invoked where it must be, or a body
# that is not exactly one JSON object FAILS.
#
# Run: bash scripts/dev/test-audio-cgi.sh   (bash, python3/python/py, coreutils)
#      AUDIO_CGI_SRC=<file> judges another copy of the CGI (the RED run).
# ═══════════════════════════════════════════════════════════════════════════
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CGI_SRC="${AUDIO_CGI_SRC:-$HERE/www/network_config/cgi-bin/sa02m_audio_api.cgi}"
fails=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

REAL_PY=""
for p in python3 python py; do
    if "$p" -c "import sys" >/dev/null 2>&1; then REAL_PY=$(command -v "$p"); break; fi
done
[ -r "$CGI_SRC" ] || { echo "audio-cgi: FAIL — cannot read $CGI_SRC"; exit 1; }
[ -n "$REAL_PY" ] || { echo "audio-cgi: FAIL — no working python interpreter"; exit 1; }
command -v timeout >/dev/null || { echo "audio-cgi: FAIL — coreutils timeout missing"; exit 1; }

BOX="$(mktemp -d "${TMPDIR:-/tmp}/audio-cgi.XXXXXX")" || { echo "audio-cgi: FAIL — mktemp"; exit 1; }
trap 'rm -rf -- "$BOX"' EXIT
CGI_DIR="$BOX/www/network_config/cgi-bin"
mkdir -p "$CGI_DIR" "$BOX/bin" "$BOX/pkg/sa02m_audio"
cp "$CGI_SRC" "$CGI_DIR/sa02m_audio_api.cgi"

cat > "$CGI_DIR/lib_web_auth.sh" <<'SH'
web_session_check_cookie() { echo auth >> "$STUB_CALLS"; [ "${STUB_AUTH_RC:-0}" -eq 0 ]; }
web_csrf_validate() { echo csrf >> "$STUB_CALLS"; [ "${STUB_CSRF_RC:-0}" -eq 0 ]; }
web_csrf_error_body() { printf '{"ok":false,"error":"csrf","error_code":"E_CSRF","reason":"stub"}\n'; }
SH

cat > "$BOX/bin/sudo" <<'SH'
#!/usr/bin/env bash
printf 'sudo %s\n' "$*" >> "$STUB_CALLS"
case "${STUB_SUDO:-ok}" in
    ok)       printf '{"ok":true,"action":"%s"}\n' "${3:-}" ;;
    fail)     printf '{"ok":false,"error":"busy"}\n'; exit 1 ;;
    badcode)  printf '{"ok":false,"error":"x\\"y<script>"}\n'; exit 1 ;;
    hang)     exec sleep 30 ;;
esac
SH
cat > "$BOX/bin/mosquitto_pub" <<'SH'
#!/usr/bin/env bash
printf 'mosquitto_pub %s\n' "$*" >> "$STUB_CALLS"
[ "${STUB_PUB:-ok}" = ok ]
SH
cat > "$BOX/bin/logger" <<'SH'
#!/usr/bin/env bash
printf 'logger %s\n' "$*" >> "$STUB_CALLS"
SH
# Windows-box accommodations, inert on Linux: UTF-8 stdio (the console default
# is a legacy code page, and the Cyrillic in a response would crash it; the
# board's C locale is UTF-8-coerced, PEP 538), and text-mode CRLF folded back
# to LF for the two-line protocol (a DOWNLOAD writes bytes and is passed raw).
cat > "$BOX/bin/python3" <<SH
#!/usr/bin/env bash
case " \$* " in
    *" DOWNLOAD "*) PYTHONIOENCODING=utf-8 exec "$REAL_PY" "\$@" ;;
esac
set -o pipefail
PYTHONIOENCODING=utf-8 "$REAL_PY" "\$@" | tr -d '\r'
SH
chmod +x "$BOX/bin/"*

: > "$BOX/pkg/sa02m_audio/__init__.py"
cat > "$BOX/pkg/sa02m_audio/api.py" <<'PY'
import json, os, sys
mode = os.environ.get("STUB_API", "status")
if sys.argv[1:] == ["DOWNLOAD"]:
    with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as f:
        f.write("api DOWNLOAD query=%s\n" % os.environ.get("QUERY_STRING", ""))
    out = sys.stdout.buffer
    out.write(b"Content-Type: audio/wav\r\nContent-Length: 8\r\n\r\nRIFF\x00\x01\x02\xff")
    out.flush()
    sys.exit(0)
body = sys.stdin.read()
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as f:
    f.write("api %s len=%d\n" % (" ".join(sys.argv[1:]), len(body)))
with open(os.environ["STUB_BODY"], "w", encoding="utf-8") as f:
    f.write(body)
if mode == "garbage":
    print("Traceback (most recent call last):")
    sys.exit(0)
if mode == "silent":
    sys.exit(0)
if mode == "crash":
    raise RuntimeError("boom")
if mode == "save":
    print(json.dumps({"ok": True, "action": "save", "status": {"state": "idle"}}))
elif mode == "set_token":
    print(json.dumps({"ok": True, "action": "set_token", "token_set": True}))
else:
    print(json.dumps({"ok": True, "state": "idle", "name": "Кухня \"A\""}, ensure_ascii=False))
print(os.environ.get("STUB_FOLLOW", ""))
PY

STUB_CALLS="$BOX/calls"
STUB_BODY="$BOX/body"

# run_cgi METHOD BODY [VAR=value …] → body after the headers, CR-free, in $OUT;
# the raw response in $BOX/raw.
run_cgi() {
    local method="$1" body="$2" nbytes; shift 2
    : > "$STUB_CALLS"; : > "$STUB_BODY"
    nbytes=$(printf '%s' "$body" | wc -c | tr -d ' ')
    (
        cd "$CGI_DIR" || exit 1
        printf '%s' "$body" | env PATH="$BOX/bin:$PATH" STUB_CALLS="$STUB_CALLS" STUB_BODY="$STUB_BODY" \
            REQUEST_METHOD="$method" CONTENT_LENGTH="$nbytes" \
            SA02M_AUDIO_ROOT="$BOX/pkg" "$@" bash ./sa02m_audio_api.cgi 2>/dev/null
    ) > "$BOX/raw"
    OUT=$(tr -d '\r\000' < "$BOX/raw" | awk 'b{print; next} /^$/{b=1}')
}
ran()       { grep -c "^$1" "$STUB_CALLS" 2>/dev/null; }
argv_of()   { sed -n "s/^$1 //p" "$STUB_CALLS"; }
nothing_ran() { [ "$(ran api)" -eq 0 ] && [ "$(ran sudo)" -eq 0 ] && [ "$(ran mosquitto_pub)" -eq 0 ]; }

one_doc() {
    local label="$1" expr="$2" lines
    lines=$(printf '%s\n' "$OUT" | grep -c .)
    if [ "$lines" -ne 1 ]; then
        bad "$label — expected one line, got $lines: $(printf '%s' "$OUT" | head -c 200 | tr '\n' '|')"
        return 1
    fi
    if printf '%s' "$OUT" | PYTHONIOENCODING=utf-8 "$REAL_PY" -c '
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

echo "audio-cgi: $CGI_SRC"

echo "1. auth first"
run_cgi POST '{"action":"enable"}' STUB_AUTH_RC=1 STUB_CSRF_RC=1 STUB_FOLLOW="sudo enable"
one_doc "1a no session → unauthorized" 'd == {"ok": False, "error": "unauthorized"}'
if nothing_ran && [ "$(ran csrf)" -eq 0 ] && [ "$(ran auth)" -eq 1 ]; then
    ok "1b no dispatch, no sudo, no publish, no CSRF read before the session check"
else bad "1b work before auth: $(tr '\n' ' ' < "$STUB_CALLS")"; fi
run_cgi GET '' STUB_AUTH_RC=1 QUERY_STRING="action=download&id=20261015T120000Z-0a1b2c"
one_doc "1c a download without a session → unauthorized JSON" 'd.get("error") == "unauthorized"'
nothing_ran && ok "1d no dispatch runs for an unauthenticated download" || bad "1d download before auth ran: $(tr '\n' ' ' < "$STUB_CALLS")"

echo "2. method gate"
for m in PUT DELETE; do
    run_cgi "$m" '{"action":"enable"}' STUB_FOLLOW="sudo enable"
    one_doc "2 $m → method_not_allowed" 'd.get("error") == "method_not_allowed"'
    nothing_ran || bad "2 $m reached the dispatch, sudo or the broker"
done

echo "3. CSRF before any mutation"
run_cgi POST '{"action":"record","seconds":5}' STUB_CSRF_RC=1 STUB_FOLLOW="pub record 5"
one_doc "3a bad token → E_CSRF" 'd.get("error_code") == "E_CSRF"'
nothing_ran && ok "3b no dispatch, no publish, no sudo on a bad token" || bad "3b bad token still reached: $(tr '\n' ' ' < "$STUB_CALLS")"
run_cgi POST '{"action":"status"}' STUB_CSRF_RC=1
one_doc "3c even a status POST is token-checked" 'd.get("error_code") == "E_CSRF"'

echo "4. bounded body"
for cl in 16385 9999999; do
    : > "$STUB_CALLS"
    OUT=$(cd "$CGI_DIR" && printf '{}' | env PATH="$BOX/bin:$PATH" STUB_CALLS="$STUB_CALLS" STUB_BODY="$STUB_BODY" \
        REQUEST_METHOD=POST CONTENT_LENGTH="$cl" SA02M_AUDIO_ROOT="$BOX/pkg" bash ./sa02m_audio_api.cgi 2>/dev/null \
        | tr -d '\r' | awk 'b{print; next} /^$/{b=1}')
    one_doc "4 CONTENT_LENGTH=$cl → payload_too_large" 'd.get("error") == "payload_too_large"'
    [ "$(ran api)" -eq 0 ] || bad "4 CONTENT_LENGTH=$cl reached the dispatch"
done
: > "$STUB_CALLS"
OUT=$(cd "$CGI_DIR" && printf '{"action":"enable"}' | env PATH="$BOX/bin:$PATH" STUB_CALLS="$STUB_CALLS" STUB_BODY="$STUB_BODY" \
    REQUEST_METHOD=POST CONTENT_LENGTH=abc SA02M_AUDIO_ROOT="$BOX/pkg" bash ./sa02m_audio_api.cgi 2>/dev/null \
    | tr -d '\r' | awk 'b{print; next} /^$/{b=1}')
grep -q '^api POST len=0$' "$STUB_CALLS" && ok "4 a non-numeric CONTENT_LENGTH reads as an empty body" \
    || bad "4 non-numeric CONTENT_LENGTH: $(tr '\n' ' ' < "$STUB_CALLS")"

echo "5. GET never acts"
for follow in "sudo enable" "pub record 10"; do
    run_cgi GET '' STUB_FOLLOW="$follow"
    one_doc "5a GET [$follow] → the dispatch answer, verbatim" 'd.get("state") == "idle" and "done" not in d'
    if grep -q '^api GET len=0$' "$STUB_CALLS" && [ "$(ran sudo)" -eq 0 ] && [ "$(ran mosquitto_pub)" -eq 0 ]; then
        ok "5b dispatch(GET) with an empty stdin; [$follow] NOT executed on GET"
    else bad "5b GET [$follow]: $(tr '\n' ' ' < "$STUB_CALLS")"; fi
done

echo "6. every follow-up of the grammar"
check_follow() {  # $1 follow, $2 stub tool, $3 exact argv wanted
    local body='{"action":"x","pad":"Кухня — «тест»"}'
    run_cgi POST "$body" STUB_FOLLOW="$1"
    one_doc "6 [$1] → one JSON object, done ok, dispatch fields kept" \
        'd.get("done") == "ok" and d.get("state") == "idle" and d.get("name") == "Кухня \"A\""'
    if [ "$(ran "$2")" -eq 1 ] && [ "$(argv_of "$2")" = "$3" ]; then
        ok "6 [$1] → $2 argv exactly [$3]"
    else bad "6 [$1] → $2 calls: $(argv_of "$2" | tr '\n' '|') (sudo $(ran sudo), pub $(ran mosquitto_pub))"; fi
    [ "$(cat "$STUB_BODY")" = "$body" ] || bad "6 [$1] the body did not reach the dispatch intact"
    if grep -q '^logger -t sa02m-audio-panel -- source=panel ' "$STUB_CALLS"; then
        ok "6 [$1] → one audit line tagged source=panel"
    else bad "6 [$1] → no audit line: $(tr '\n' ' ' < "$STUB_CALLS")"; fi
}
REC=/devices/sa02m-audio/controls/record/on
DEL=/devices/sa02m-audio/controls/delete/on
check_follow "pub record 10" mosquitto_pub "-h 127.0.0.1 -p 1883 -q 1 -t $REC -m 10"
check_follow "pub record 2" mosquitto_pub "-h 127.0.0.1 -p 1883 -q 1 -t $REC -m 2"
check_follow "pub record stop" mosquitto_pub "-h 127.0.0.1 -p 1883 -q 1 -t $REC -m stop"
check_follow "pub delete 20261015T120000Z-0a1b2c" mosquitto_pub "-h 127.0.0.1 -p 1883 -q 1 -t $DEL -m 20261015T120000Z-0a1b2c"
check_follow "pub delete all" mosquitto_pub "-h 127.0.0.1 -p 1883 -q 1 -t $DEL -m all"
for verb in enable disable restart; do
    check_follow "sudo $verb" sudo "-n /usr/local/sbin/sa02m-audio-web-trigger.sh $verb"
done
if grep -q -- ' -r' "$STUB_CALLS"; then bad "6 a publish carried -r"; else ok "6 no publish ever carries -r (retain)"; fi
run_cgi POST '{"action":"save"}' STUB_API=save STUB_FOLLOW=""
if [ "$(ran sudo)" -eq 0 ] && [ "$(ran mosquitto_pub)" -eq 0 ] \
   && grep -q '^logger -t sa02m-audio-panel -- source=panel save$' "$STUB_CALLS"; then
    ok "6 a save runs no publish and no sudo, and leaves its audit line"
else bad "6 save: $(tr '\n' ' ' < "$STUB_CALLS")"; fi
run_cgi POST '{"action":"set_token","token":"SECRET-tok.123"}' STUB_API=set_token STUB_FOLLOW=""
if [ "$(ran sudo)" -eq 0 ] && [ "$(ran mosquitto_pub)" -eq 0 ] \
   && grep -q '^logger -t sa02m-audio-panel -- source=panel set_token$' "$STUB_CALLS" \
   && ! grep -q 'SECRET-tok' "$STUB_CALLS"; then
    ok "6 set_token runs nothing privileged and audits the action name only, never the token"
else bad "6 set_token: $(tr '\n' ' ' < "$STUB_CALLS")"; fi

echo "7. follow-ups outside the grammar run nothing"
for follow in 'pub record 10; reboot' 'pub record 100' 'pub record 0' 'pub record -r' 'pub record 10 -r' \
              'pub record $(id)' 'pub delete ../x' 'pub delete ALL' 'pub delete 20261015T120000Z-0a1b2c x' \
              'pub foo 1' 'pub /devices/x/controls/y/on 1' 'sudo unlink' 'sudo enable extra' 'sudo  enable' \
              'sudo -u root enable' 'enable' 'pub record' ' pub record 10'; do
    run_cgi POST '{"action":"x"}' STUB_FOLLOW="$follow"
    if [ "$(ran sudo)" -eq 0 ] && [ "$(ran mosquitto_pub)" -eq 0 ] && [ -n "$OUT" ] && [[ $OUT != *'"done"'* ]]; then
        ok "7 [$follow] → nothing run, no done field"
    else bad "7 [$follow] → $(tr '\n' ' ' < "$STUB_CALLS") out=$OUT"; fi
done

echo "8. helper failure is reported, never echoed"
run_cgi POST '{"action":"enable"}' STUB_FOLLOW="sudo enable" STUB_SUDO=fail
one_doc "8a helper rc≠0 → done failed + done_error busy" 'd.get("done") == "failed" and d.get("done_error") == "busy"'
run_cgi POST '{"action":"enable"}' STUB_FOLLOW="sudo enable" STUB_SUDO=badcode
one_doc "8b an error code outside [a-z_] is not echoed" \
    'd.get("done") == "failed" and "done_error" not in d and "script" not in json.dumps(d)'

echo "9. helper hang is bounded"
t0=$(date +%s)
run_cgi POST '{"action":"enable"}' STUB_FOLLOW="sudo enable" STUB_SUDO=hang
t1=$(date +%s)
one_doc "9a hung helper → done timeout" 'd.get("done") == "timeout"'
[ $((t1 - t0)) -le 15 ] && ok "9b answered in $((t1 - t0)) s (budget 11 s + slack, under nginx's 20 s)" || bad "9b took $((t1 - t0)) s"

echo "10. a failed publish is reported"
run_cgi POST '{"action":"record","seconds":5}' STUB_FOLLOW="pub record 5" STUB_PUB=fail
one_doc "10 broker refused → done failed, publish_failed" 'd.get("done") == "failed" and d.get("done_error") == "publish_failed"'

echo "11. dispatch failure"
for mode in garbage silent crash; do
    run_cgi POST '{"action":"enable"}' STUB_API="$mode" STUB_FOLLOW="sudo enable"
    one_doc "11 dispatch $mode → audio_api_failed" 'd.get("error") == "audio_api_failed"'
    [ "$(ran sudo)" -eq 0 ] && [ "$(ran mosquitto_pub)" -eq 0 ] || bad "11 dispatch $mode still acted"
done

echo "12. package absent"
run_cgi GET '' SA02M_AUDIO_ROOT="$BOX/nonexistent"
one_doc "12a GET → not_installed state" 'd.get("ok") is True and d.get("state") == "not_installed" and d.get("installed") is False'
[ "$(ran api)" -eq 0 ] || bad "12a the dispatch ran without a package"
run_cgi POST '{"action":"enable"}' SA02M_AUDIO_ROOT="$BOX/nonexistent"
one_doc "12b POST → not_installed error" 'd.get("ok") is False and d.get("error") == "not_installed"'
nothing_ran || bad "12b work ran without a package"

echo "13. download"
run_cgi GET '' QUERY_STRING="action=download&id=20261015T120000Z-0a1b2c&inline=1"
want=$(printf 'Content-Type: audio/wav\r\nContent-Length: 8\r\n\r\nRIFF\000\001\002\377' | od -An -tx1 | tr -d ' \n')
got=$(od -An -tx1 < "$BOX/raw" | tr -d ' \n')
if [ "$got" = "$want" ]; then
    ok "13a the dispatch's own response reaches the client byte for byte (NUL and 0xff included), no CGI header before it"
else bad "13a download bytes differ: got $got"; fi
if grep -qx 'api DOWNLOAD query=action=download&id=20261015T120000Z-0a1b2c&inline=1' "$STUB_CALLS" \
   && [ "$(ran sudo)" -eq 0 ] && [ "$(ran mosquitto_pub)" -eq 0 ] && [ "$(ran csrf)" -eq 0 ]; then
    ok "13b DOWNLOAD mode with the query; no token needed (read-only), nothing else runs"
else bad "13b download calls: $(tr '\n' ' ' < "$STUB_CALLS")"; fi
run_cgi GET '' QUERY_STRING="id=x&action=download"
one_doc "13c a query not starting action=download& is a status GET" 'd.get("state") == "idle"'

echo "K. the CGI's own error codes = the contract's «Код CGI» table"
CONTRACT="$HERE/docs/contracts/audio-recorder.md"
k_src=$(sed 's/^[[:space:]]*#.*$//' "$CGI_SRC" | grep -oE '"error":"[a-z_]+"' | sed 's/.*:"//; s/"$//' | sort -u)
k_doc=$(awk '/^\| Код CGI /{f=1; next} f && /^\|/{print; next} f{exit}' "$CONTRACT" \
        | grep -v '^|[-: |]*$' | sed -n 's/^| *`\([a-z_]*\)`.*/\1/p' | sort -u)
k_want=$(printf '%s\ncsrf\n' "$k_src" | grep . | sort -u)
if [ "$(printf '%s\n' "$k_src" | grep -c .)" -lt 4 ] || [ -z "$k_doc" ]; then
    bad "K non-vacuity: $(printf '%s\n' "$k_src" | grep -c .) error literal(s) in the CGI, contract table rows: '$(printf '%s' "$k_doc" | tr '\n' ' ')'"
elif [ "$k_doc" = "$k_want" ]; then
    ok "K CGI error codes == contract «Код CGI» ($(printf '%s' "$k_doc" | tr '\n' ' '))"
else
    bad "K CGI error codes differ from the contract «Код CGI» — CGI (+csrf): $(printf '%s' "$k_want" | tr '\n' ' ') | contract: $(printf '%s' "$k_doc" | tr '\n' ' ')"
fi

echo "R. the real package"
REAL_PKG="$HERE/opt/sa02m-audio"
RS="$BOX/real"
mkdir -p "$RS/etc" "$RS/var" "$RS/run"
printf '[recorder]\nenabled = true\n' > "$RS/etc/sa02m-audio.conf"
renv=(SA02M_AUDIO_ROOT="$REAL_PKG" SA02M_AUDIO_ETC="$RS/etc" SA02M_AUDIO_VAR="$RS/var" SA02M_AUDIO_RUN="$RS/run")
run_cgi GET '' "${renv[@]}"
one_doc "R1 real GET → one JSON object with a known state" \
    'd.get("ok") is True and d.get("state") in ("not_installed","disabled","conf_unreadable","missing_deps","idle","recording","stopped","error","not_running")'
run_cgi POST '{"action":"enable"}' "${renv[@]}"
one_doc "R2 real mutation without the install footprint → not_installed" 'd.get("error") == "not_installed"'
[ "$(ran sudo)" -eq 0 ] && [ "$(ran mosquitto_pub)" -eq 0 ] && ok "R3 not_installed runs nothing" || bad "R3 not_installed acted: $(tr '\n' ' ' < "$STUB_CALLS")"
run_cgi POST '{"action":"no_such_action"}' "${renv[@]}"
one_doc "R4 real unknown action → not_found" 'd.get("error") == "not_found"'

if [ "$fails" -eq 0 ]; then
    echo "audio-cgi: all checks passed"
    exit 0
fi
echo "audio-cgi: $fails FAILURE(S)"
exit 1
