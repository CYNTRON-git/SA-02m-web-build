#!/usr/bin/env bash
# Gate for sa02m_alice_topics.cgi — ONE JSON document, whatever python does (1.0.6.39).
# comment-mutation-proof-exempt: behavioural harness - the SHIPPED CGI is copied into a sandbox and RUN against scripted python3 / auth stubs, and its stdout is parsed as JSON; a commented-out line changes the emitted body instead of hiding from a needle grep. The only source-text pin this CGI carries (the timeout literal <-> constants.TOPICS_CGI_TIMEOUT_S) is asserted by tests/test_sio_connection.py TestTopicsCgiTimeout, a unittest runner row.
#
# The defect it pins (bench 1.135, fixed in 1.0.6.38 with NO test): the
# picker's inventory answer was streamed straight out with `|| echo` on the
# end, so when python exited non-zero AFTER printing a complete body the
# fallback object was appended to it — `{...,"count":12}{"ok":false,...}` —
# JSON.parse threw and the «Умный дом» tab fell back to manual entry with every
# channel on the board in hand. A fallback must REPLACE the answer, never trail
# it. TOPICS_CGI_SRC=<(git show 1f6f1a1~1:www/network_config/cgi-bin/sa02m_alice_topics.cgi)
# drives the pre-fix CGI RED on case 1.
#
# Cases (each asserts EXACTLY one JSON document on stdout after the headers):
#   1. python prints a full body then exits 1  -> that body, verbatim, alone
#   2. python ok, no query                     -> the body; SA02M_TOPICS_FORMAT=flat
#   3. python ok, ?format=inventory            -> SA02M_TOPICS_FORMAT=inventory
#   4. python prints nothing, exits 0          -> the topics_failed fallback
#   5. python hangs, SA02M_ALICE_TOPICS_TIMEOUT=1 -> fallback within the budget
#   6. session check fails                     -> unauthorized, python NEVER runs
#   7. the REAL interpreter + the real package (an empty live cache) -> one doc
# Non-vacuous: a missing CGI or interpreter, a stub that was never invoked, or a
# body that is not exactly one JSON object FAILS. No root, no device, no nginx.
#
# Windows note: the real CPython here writes CRLF to a pipe, so every read site
# strips `\r` before parsing (the CGI itself runs on Linux).
set -u
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
CGI_SRC="${TOPICS_CGI_SRC:-$HERE/www/network_config/cgi-bin/sa02m_alice_topics.cgi}"
REAL_PY="$(command -v python3 || true)"
fails=0
ok(){ printf '  ok    %s\n' "$1"; }
bad(){ printf '  FAIL  %s\n' "$1"; fails=$((fails+1)); }

[ -r "$CGI_SRC" ] || { echo "alice-topics-cgi: cannot read $CGI_SRC"; exit 1; }
[ -n "$REAL_PY" ] || { echo "alice-topics-cgi: no python3 on PATH (needed to parse the answers)"; exit 1; }
command -v timeout >/dev/null || { echo "alice-topics-cgi: coreutils timeout missing"; exit 1; }

BOX="$(mktemp -d)"
trap 'rm -rf "$BOX"' EXIT
mkdir -p "$BOX/cgi-bin" "$BOX/bin" "$BOX/empty-cache"
cp "$CGI_SRC" "$BOX/cgi-bin/sa02m_alice_topics.cgi"

# The CGI sources lib_web_auth.sh from its own directory: a stub decides the
# session verdict from STUB_AUTH_RC and nothing else.
cat > "$BOX/cgi-bin/lib_web_auth.sh" <<'SH'
web_session_check_cookie() { [ "${STUB_AUTH_RC:-0}" -eq 0 ]; }
SH

# A scripted python3 ahead of the real one on PATH. It drains the heredoc it
# is handed (so the CGI never SIGPIPEs), records the call, and answers per
# STUB_MODE. `exec sleep` in the hang case: `timeout` must be able to kill the
# process holding the stdout pipe, not a shell that leaves it orphaned.
cat > "$BOX/bin/python3" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
printf '%s\n' "${STUB_MODE:-ok}" >> "$STUB_CALLS"
case "${STUB_MODE:-ok}" in
  ok)             printf '{"ok":true,"source":"stub","format":"%s","count":12}\n' "${SA02M_TOPICS_FORMAT:-}" ;;
  body-then-fail) printf '{"ok":true,"source":"stub","count":12}\n'; exit 1 ;;
  empty-ok)       exit 0 ;;
  hang)           exec sleep 30 ;;
esac
SH
chmod +x "$BOX/bin/python3"

# run_cgi <mode> [query] [extra env assignments...] -> body (headers stripped, CR-free)
run_cgi() {
    local mode="$1" query="${2:-}"; shift; shift || true
    : > "$STUB_CALLS"
    (
        cd "$BOX/cgi-bin" || exit 1
        env PATH="$BOX/bin:$PATH" STUB_MODE="$mode" STUB_CALLS="$STUB_CALLS" \
            QUERY_STRING="$query" SA02M_ALICE_ROOT="$HERE/opt/sa02m-alice" \
            "$@" bash ./sa02m_alice_topics.cgi 2>/dev/null
    ) | tr -d '\r' | awk 'body{print; next} /^$/{body=1}'
}
STUB_CALLS="$BOX/calls"

# one_doc <label> <body> <python expression over `d` that must be truthy>
one_doc() {
    local label="$1" body="$2" expr="$3" lines
    lines="$(printf '%s\n' "$body" | grep -c .)"
    printf '%s' "$body" > "$BOX/out.json"
    if [ "$lines" -ne 1 ]; then
        bad "$label — expected one line of output, got $lines: $(printf '%s' "$body" | head -c 200 | tr '\n' '|')"
        return
    fi
    if "$REAL_PY" - "$BOX/out.json" "$expr" <<'PY' 2>/dev/null
import json, sys
text = open(sys.argv[1], encoding="utf-8").read().strip()
d = json.loads(text)          # two concatenated objects raise here
if not isinstance(d, dict):
    raise SystemExit(2)
raise SystemExit(0 if eval(sys.argv[2]) else 3)
PY
    then ok "$label"
    else bad "$label — not exactly one JSON object matching [$expr]: $(printf '%s' "$body" | head -c 200)"
    fi
}

echo "alice-topics-cgi: $CGI_SRC"

# The shipped CGI answers a non-zero exit with the fallback in PLACE of the
# body (collected, then emitted once); the pre-fix one emitted both. The
# invariant is one object — whichever of the two it is.
body="$(run_cgi body-then-fail)"
one_doc "(1) full body then a non-zero exit -> ONE object (the body or the fallback), never both" \
    "$body" '(d.get("ok") is True and d.get("count") == 12) or d.get("error") == "topics_failed"'
grep -q body-then-fail "$STUB_CALLS" || bad "(1) the python stub was never invoked — the harness measured nothing"

body="$(run_cgi ok)"
one_doc "(2) plain call -> flat format reaches python" "$body" 'd.get("format") == "flat"'

body="$(run_cgi ok 'format=inventory')"
one_doc "(3) ?format=inventory -> inventory format reaches python" "$body" 'd.get("format") == "inventory"'

body="$(run_cgi empty-ok)"
one_doc "(4) empty stdout on exit 0 -> the topics_failed fallback" "$body" 'd.get("ok") is False and d.get("error") == "topics_failed"'

t0=$(date +%s)
body="$(run_cgi hang '' SA02M_ALICE_TOPICS_TIMEOUT=1)"
t1=$(date +%s)
one_doc "(5) a hung interpreter -> fallback, not a hung tab" "$body" 'd.get("ok") is False and d.get("error") == "topics_failed"'
if [ $((t1 - t0)) -le 8 ]; then
    ok "(5) answered in $((t1 - t0)) s under SA02M_ALICE_TOPICS_TIMEOUT=1 (nginx cuts at 20)"
else
    bad "(5) took $((t1 - t0)) s — the timeout override is not honoured (or timeout is not wrapping python)"
fi

body="$(run_cgi ok '' STUB_AUTH_RC=1)"
one_doc "(6) a refused session -> unauthorized" "$body" 'd.get("ok") is False and d.get("error") == "unauthorized"'
if [ -s "$STUB_CALLS" ]; then
    bad "(6) python ran for a refused session — auth must come first"
else
    ok "(6) python never ran for a refused session"
fi

# (7) the real interpreter and the real package, against an empty live cache:
# whatever the module answers (topics or topics_failed), it is ONE object.
# PYTHONIOENCODING: a Windows CPython writes the Russian channel titles to a
# pipe in the console codepage (cp1251) and the UTF-8 parse below dies at the
# first Cyrillic byte; on the board python runs under the C.UTF-8 coercion and
# is UTF-8 already — the pin is on the ONE-object shape, not the codepage.
: > "$STUB_CALLS"
body="$(
    cd "$BOX/cgi-bin" || exit 1
    env QUERY_STRING="format=inventory" SA02M_ALICE_ROOT="$HERE/opt/sa02m-alice" \
        SA02M_MQTT_LIVE_CACHE="$BOX/empty-cache" STUB_AUTH_RC=0 PYTHONIOENCODING=utf-8 \
        bash ./sa02m_alice_topics.cgi 2>/dev/null | tr -d '\r' | awk 'body{print; next} /^$/{body=1}'
)"
one_doc "(7) the real interpreter + package answer one object" "$body" '"ok" in d'

echo
[ "$fails" -eq 0 ] && { echo "alice-topics-cgi: ALL OK"; exit 0; }
echo "alice-topics-cgi: $fails FAILURE(S)"; exit 1
