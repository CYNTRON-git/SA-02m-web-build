#!/usr/bin/env bash
# mqtt-tcp-probe-contract — the add-device dialog's «Проверить связь» endpoint
# (www/network_config/cgi-bin/mqtt_tcp_probe.cgi) reaches the network only for
# an authenticated POST carrying the CSRF token, only for an entry the save
# would accept, one probe at a time, within a shell budget — and relays the
# probe module's verdict verbatim.
#
# WHY THIS EXISTS. 1.0.6.67 lets the operator ask the board, before «Сохранить
# и применить», whether the typed host:tcp_port answers Modbus TCP at the typed
# unit id (docs/contracts/bridge-modbus-tcp.md §10). The endpoint makes the
# board an outbound TCP client on behalf of a browser session — the bound rule
# the threat model records is: the same address rules as a save (bridge_bus),
# POST + token before any socket work, one probe in flight per board, a body
# cap, a shell timeout, and a fail-closed answer when the probe module is
# missing. A unit test cannot see that half: it is bash + a python heredoc
# behind auth and CSRF.
#
# METHOD — behavioural, the shipped endpoint in a sandbox (the
# mqtt-config-transport-contract idiom). The SHIPPED CGI is copied into a temp
# dir with the SHIPPED lib_web_auth.sh and driven as a real CGI
# (REQUEST_METHOD / HTTP_COOKIE / HTTP_X_SA02M_CSRF / CONTENT_LENGTH, JSON body
# on stdin). The session and CSRF token are REAL, minted through the shipped
# auth lib. SA02M_BRIDGE_LIB_DIR points the endpoint at a temp lib dir holding
# the REAL bridge_bus.py plus a `bridge_probe.py` stand-in that loads the REAL
# opt/sa02m-modbus-mqtt/bridge_probe.py (so validation order and the
# single-flight lock are the shipped code) and replaces ONLY its socket `run()`
# with a recorder: it writes a marker file with the (host, port, unit) it was
# handed and returns a canned reply. SA02M_PROBE_LOCK and SA02M_PROBE_BUDGET_S
# are the endpoint's harness redirects (process env only — a client cannot set
# them). SA02M_PROBE_FAKE_SLEEP makes the stand-in hang for the timeout case.
# `\r` is stripped from bodies (Windows CPython writes text mode as CRLF).
#
# NON-VACUOUS: a missing endpoint / lib / bridge_bus / bridge_probe, a session
# the shipped lib refuses to mint, or fewer than 3 probes reaching the recorder
# over the run FAILS rather than passing on an empty sweep; the valid-entry
# case FAILS when no marker was written.
#
# PROVEN RED (1.0.6.67, recorded in the branch's commit body): with the
# endpoint's `web_csrf_validate` line commented out, cases 2-3 FAIL (a
# token-less POST reaches the recorder) — the registered comment-mutation-proof
# case; with bridge_probe.probe() calling run() before device_bus(), case 5
# FAILS (a loopback entry reaches the recorder). Before the endpoint existed the
# harness exited 1 on «not found».
#
# Requires python3 (stdlib only). The timeout case is SKIP-labelled — never a
# pass — where coreutils `timeout` is absent.
#
# Run: bash scripts/dev/test-mqtt-tcp-probe.sh
#      MQTT_TCP_PROBE_CGI_SRC=<file>  judges another copy of the endpoint
#      MQTT_TCP_PROBE_PY_SRC=<file>   judges another copy of bridge_probe.py
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

CGI_SRC=${MQTT_TCP_PROBE_CGI_SRC:-www/network_config/cgi-bin/mqtt_tcp_probe.cgi}
PROBE_PY_SRC=${MQTT_TCP_PROBE_PY_SRC:-opt/sa02m-modbus-mqtt/bridge_probe.py}
AUTH_LIB=www/network_config/cgi-bin/lib_web_auth.sh
BRIDGE_LIB=opt/sa02m-modbus-mqtt

fails=0
passes=0
ok()   { printf 'mqtt-tcp-probe: ok    %s\n' "$1"; passes=$((passes + 1)); }
bad()  { printf 'mqtt-tcp-probe: FAIL  %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf 'mqtt-tcp-probe: skip  %s\n' "$1"; }

for f in "$CGI_SRC" "$AUTH_LIB" "$BRIDGE_LIB/bridge_bus.py" "$PROBE_PY_SRC"; do
    [ -f "$f" ] || { echo "mqtt-tcp-probe: FAIL - not found: $f"; exit 1; }
done

PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import json' >/dev/null 2>&1; then PY=$p; break; fi
done
[ -n "$PY" ] || { echo "mqtt-tcp-probe: FAIL - no working python3; the endpoint needs it too"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
# Paths python sees are in a form both shells understand (git-bash on Windows
# hands python.exe a C:/… path); paths only bash sees stay POSIX. No-op on Linux.
TW="$T"
command -v cygpath >/dev/null 2>&1 && TW=$(cygpath -m "$T")
case "$PROBE_PY_SRC" in /*) PROBE_REALW="$PROBE_PY_SRC" ;; *) PROBE_REALW="$ROOT/$PROBE_PY_SRC" ;; esac
command -v cygpath >/dev/null 2>&1 && PROBE_REALW=$(cygpath -m "$PROBE_REALW")
CGIDIR="$T/cgi-bin"; LIBDIR="$T/lib"
mkdir -p "$CGIDIR" "$LIBDIR" "$T/sessions" "$T/nolib"
LOCK="$T/probe.lock"; LOCKW="$TW/probe.lock"
MARKER="$T/marker.json"; MARKERW="$TW/marker.json"
# Every recorder call appends a line here: `call` runs inside $(...), so a shell
# counter would die with the subshell and the non-vacuity floor would read 0.
PROBE_LOG="$T/probes.log"; PROBE_LOGW="$TW/probes.log"
CGI_ERR="$T/cgi-stderr.log"

# ── sandbox the endpoint ───────────────────────────────────────────────────
cp "$CGI_SRC" "$CGIDIR/mqtt_tcp_probe.cgi"
cp "$AUTH_LIB" "$CGIDIR/lib_web_auth.sh"
chmod +x "$CGIDIR/mqtt_tcp_probe.cgi"
cp "$BRIDGE_LIB/bridge_bus.py" "$LIBDIR/bridge_bus.py"
# The stand-in: the REAL module with its socket call replaced by a recorder.
cat > "$LIBDIR/bridge_probe.py" <<'PYFAKE'
"""Harness stand-in: the REAL bridge_probe (validation + lock) with run()
replaced by a recorder — the network call is the only thing faked."""
import importlib.util
import json
import os
import time

_spec = importlib.util.spec_from_file_location("bridge_probe_real", os.environ["PROBE_REAL"])
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)


def _fake_run(host, port, unit, timeout_s=None, refuse_self=True):
    delay = float(os.environ.get("SA02M_PROBE_FAKE_SLEEP", "0") or 0)
    if delay:
        time.sleep(delay)
    with open(os.environ["PROBE_MARKER"], "w", encoding="utf-8") as f:
        json.dump({"host": host, "tcp_port": port, "address": unit}, f)
    with open(os.environ["PROBE_LOG"], "a", encoding="utf-8") as f:
        f.write("%s:%s/%s\n" % (host, port, unit))
    return {"ok": True, "verdict": "device_ok", "host": host, "tcp_port": port,
            "address": unit, "exception": None, "elapsed_ms": 4242}


_real.run = _fake_run
probe = _real.probe
PYFAKE

# ── a REAL session + CSRF token, minted by the shipped auth lib ────────────
export SA02M_SESSION_DIR="$T/sessions"
# shellcheck source=www/network_config/cgi-bin/lib_web_auth.sh
. "$CGIDIR/lib_web_auth.sh"
TOKEN=$(web_session_create admin) || TOKEN=""
[ -n "$TOKEN" ] || { echo "mqtt-tcp-probe: FAIL - could not mint a session through the shipped lib"; exit 1; }
CSRF=$(web_csrf_token_for_session "$TOKEN") || CSRF=""
[ -n "$CSRF" ] || { echo "mqtt-tcp-probe: FAIL - could not mint a CSRF token through the shipped lib"; exit 1; }
GOOD_COOKIE="session_token=$TOKEN"

call() {  # <method> <cookie> <csrf> <body> [lib dir] [budget s] [fake sleep s]
    rm -f "$MARKER"
    local len
    len=$(printf '%s' "$4" | wc -c | tr -d ' ')
    printf '%s' "$4" | env \
        REQUEST_METHOD="$1" \
        HTTP_COOKIE="$2" \
        HTTP_X_SA02M_CSRF="$3" \
        CONTENT_LENGTH="$len" \
        SA02M_SESSION_DIR="$SA02M_SESSION_DIR" \
        SA02M_BRIDGE_LIB_DIR="${5:-$TW/lib}" \
        SA02M_PROBE_LOCK="$LOCKW" \
        SA02M_PROBE_BUDGET_S="${6:-8}" \
        SA02M_PROBE_FAKE_SLEEP="${7:-0}" \
        PROBE_REAL="$PROBE_REALW" \
        PROBE_MARKER="$MARKERW" \
        PROBE_LOG="$PROBE_LOGW" \
        PYTHONUTF8=1 \
        PATH="$PATH" \
        bash "$CGIDIR/mqtt_tcp_probe.cgi" 2>"$CGI_ERR" | sed '1,/^\r\{0,1\}$/d' | tr -d '\r'
    return 0
}

# One value out of a JSON reply (python expression over `j`).
jget() {  # <json> <expr>
    printf '%s' "$1" | "$PY" -c 'import json,sys
try:
    j = json.loads(sys.stdin.read())
except Exception as e:
    print("<<not json: %s>>" % e); sys.exit(0)
print(eval(sys.argv[1], {"j": j}))' "$2"
}
marker() {  # the recorder's (host, tcp_port, address), or <<no probe>>
    [ -f "$MARKER" ] || { printf '<<no probe>>'; return 0; }
    "$PY" -c 'import json,sys; m=json.load(open(sys.argv[1])); print((m["host"], m["tcp_port"], m["address"]))' "$MARKERW"
}

ENTRY='{"type":"template","transport":"tcp","host":"192.168.1.20","tcp_port":502,"address":1}'
LOOPBACK='{"type":"template","transport":"tcp","host":"127.0.0.1","tcp_port":502,"address":1}'
STRINGS='{"type":"carel","family":"crst","transport":"tcp","host":"192.168.1.50","tcp_port":"502","address":"7"}'
CANNED='{"ok": true, "verdict": "device_ok", "host": "192.168.1.20", "tcp_port": 502, "address": 1, "exception": null, "elapsed_ms": 4242}'

# ── 1-4: method, token and session come before any socket work ─────────────
out=$(call GET "$GOOD_COOKIE" "$CSRF" "$ENTRY")
if [ "$(jget "$out" '(j.get("ok"), j.get("error"))')" = "(False, 'method_not_allowed')" ] && [ ! -f "$MARKER" ]; then
    ok "1 a GET is refused (method_not_allowed), no probe"
else
    bad "1 a GET was not refused cleanly: $out / $(marker)"
fi
out=$(call POST "$GOOD_COOKIE" "" "$ENTRY")
if [ "$(jget "$out" '(j.get("ok"), j.get("error"), j.get("error_code"), j.get("reason"))')" = "(False, 'csrf', 'E_CSRF', 'no_header')" ] && [ ! -f "$MARKER" ]; then
    ok "2 a POST without the token is refused (E_CSRF no_header), no probe"
else
    bad "2 a token-less POST was not refused cleanly: $out / $(marker)"
fi
out=$(call POST "$GOOD_COOKIE" "not-the-token" "$ENTRY")
if [ "$(jget "$out" '(j.get("error"), j.get("reason"))')" = "('csrf', 'mismatch')" ] && [ ! -f "$MARKER" ]; then
    ok "3 a POST with a wrong token is refused (mismatch), no probe"
else
    bad "3 a wrong-token POST was not refused cleanly: $out / $(marker)"
fi
out=$(call POST "session_token=$(printf 'd%.0s' $(seq 1 64))" "$CSRF" "$ENTRY")
if [ "$(jget "$out" '(j.get("ok"), j.get("error"))')" = "(False, 'unauthorized')" ] && [ ! -f "$MARKER" ]; then
    ok "4 an unknown session is refused (unauthorized), no probe"
else
    bad "4 an unauthenticated POST was not refused cleanly: $out / $(marker)"
fi

# ── 5: the save's own address rules run BEFORE the probe ───────────────────
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$LOOPBACK")
if [ "$(jget "$out" '(j.get("ok"), j.get("error"), j.get("reason"))')" = "(False, 'invalid_device', 'host_forbidden')" ] && [ ! -f "$MARKER" ]; then
    ok "5 a loopback entry is refused with the bridge code (host_forbidden) and never probed"
else
    bad "5 a loopback entry was not refused before the probe: $out / $(marker)"
fi

# ── 6: a valid entry is probed with its canonical fields, the verdict relayed verbatim ──
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$ENTRY")
if [ "$(marker)" = "('192.168.1.20', 502, 1)" ] \
   && [ "$(printf '%s' "$out" | "$PY" -c 'import json,sys; print(json.loads(sys.stdin.read()) == json.loads(sys.argv[1]))' "$CANNED")" = True ]; then
    ok "6 a valid entry reaches the probe as (host, port, unit) and the reply is the probe's, verbatim"
else
    bad "6 a valid entry was not probed/relayed as expected: reply=$out marker=$(marker)"
fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$STRINGS")
if [ "$(marker)" = "('192.168.1.50', 502, 7)" ] && [ "$(jget "$out" 'j.get("verdict")')" = "device_ok" ]; then
    ok "6b digit strings for port/address are accepted as a save accepts them, canonicalised as ints"
else
    bad "6b digit-string port/address were not canonicalised: reply=$out marker=$(marker)"
fi
[ -f "$LOCK" ] && bad "6c the lock file survived a finished probe (would wedge the button)"

# ── 7-9: the untrusted-input floors ────────────────────────────────────────
big=$("$PY" -c 'print("{\"pad\":\"" + "x" * 5000 + "\"}")')
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$big")
if [ "$(jget "$out" '(j.get("ok"), j.get("error"))')" = "(False, 'body_too_large')" ] && [ ! -f "$MARKER" ]; then
    ok "7 a 5 KB body is refused (body_too_large), no probe"
else
    bad "7 an oversized body was not refused: $out / $(marker)"
fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$ENTRY" "$TW/nolib")
err=$(cat "$CGI_ERR" 2>/dev/null)
if [ "$(jget "$out" '(j.get("ok"), j.get("error"))')" = "(False, 'probe_unavailable')" ] && [ ! -f "$MARKER" ] \
   && [[ "$err" == *bridge_probe* ]]; then
    ok "8 probe module missing: fail-closed (probe_unavailable) and named on stderr (nginx error log)"
else
    bad "8 a missing probe module was not fail-closed/logged: $out / stderr='$err'"
fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" '{')
if [ "$(jget "$out" '(j.get("ok"), j.get("error"))')" = "(False, 'invalid_json')" ] && [ ! -f "$MARKER" ]; then
    ok "9 a malformed body is refused (invalid_json), no probe"
else
    bad "9 a malformed body was not refused: $out / $(marker)"
fi

# ── 10: one probe in flight per board — the lock file ──────────────────────
: > "$LOCK"
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$ENTRY")
if [ "$(jget "$out" '(j.get("ok"), j.get("error"))')" = "(False, 'probe_busy')" ] && [ ! -f "$MARKER" ] && [ -f "$LOCK" ]; then
    ok "10 a fresh lock held by another probe: probe_busy, no probe, the lock is left alone"
else
    bad "10 a held lock did not refuse the probe: $out / $(marker) lock=$([ -f "$LOCK" ] && echo present || echo gone)"
fi
touch -d '60 seconds ago' "$LOCK" 2>/dev/null || touch -t "$(date -d '-60 seconds' +%Y%m%d%H%M.%S 2>/dev/null)" "$LOCK"
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$ENTRY")
if [ "$(jget "$out" 'j.get("verdict")')" = "device_ok" ] && [ "$(marker)" = "('192.168.1.20', 502, 1)" ] && [ ! -f "$LOCK" ]; then
    ok "10b a 60 s-old lock (a crashed probe) is broken: the probe runs and releases the lock"
else
    bad "10b a stale lock was not broken: $out / $(marker) lock=$([ -f "$LOCK" ] && echo present || echo gone)"
fi
rm -f "$LOCK"

# ── 11: the shell budget bounds a hung probe ───────────────────────────────
if command -v timeout >/dev/null 2>&1; then
    t0=$(date +%s)
    out=$(call POST "$GOOD_COOKIE" "$CSRF" "$ENTRY" "$TW/lib" 2 5)
    dt=$(( $(date +%s) - t0 ))
    if [ "$(jget "$out" '(j.get("ok"), j.get("error"))')" = "(False, 'probe_timeout')" ] && [ ! -f "$MARKER" ] && [ "$dt" -lt 5 ]; then
        ok "11 a probe hanging past the budget (2 s) answers probe_timeout after ${dt}s, no verdict"
    else
        bad "11 a hung probe was not bounded by the budget: $out / $(marker) after ${dt}s"
    fi
    rm -f "$LOCK"   # the killed probe's lock: the stale rule (15 s) is the live reaper
else
    skip "11 coreutils timeout absent on this host — the shell budget is NOT verified here"
fi

# ── non-vacuity ────────────────────────────────────────────────────────────
probes_seen=0
[ -f "$PROBE_LOG" ] && probes_seen=$(wc -l < "$PROBE_LOG" | tr -d ' ')
if [ "$probes_seen" -ge 3 ]; then
    ok "12 the recorder saw $probes_seen probe(s) - the run drove the real probe path"
else
    bad "12 only $probes_seen probe(s) reached the recorder - the harness is not exercising the endpoint"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "mqtt-tcp-probe: ALL OK - $passes case(s) green"
    exit 0
fi
echo "mqtt-tcp-probe: $fails FAILURE(S)"
exit 1
