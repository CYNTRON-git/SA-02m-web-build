#!/usr/bin/env bash
# mqtt-config-transport-contract — the MQTT tab's save endpoint refuses a Modbus
# TCP device entry the bridge would refuse, and changes nothing else.
#
# WHY THIS EXISTS. 1.0.6.56 gave the Modbus->MQTT bridge a Modbus TCP transport
# (`transport: tcp`, docs/contracts/bridge-modbus-tcp.md). The rules for a TCP
# entry (strict IPv4 literal, no loopback/self/multicast, template|carel only,
# Carel family required, unit 1..255, the endpoint cap) live in ONE home,
# opt/sa02m-modbus-mqtt/bridge_bus.py; the bridge loader is the authority
# (the YAML is www-data-writable, so a save-time check can be bypassed), and
# mqtt_config.cgi imports the same module so the operator gets the refusal at
# «Сохранить», not as a silently skipped device after the restart. A unit test
# cannot see that half: the endpoint is bash + a python heredoc behind auth and
# CSRF, and it writes through a sudo helper.
#
# It also pins the NON-change: an RS-485-only save produces the byte-identical
# YAML the endpoint produced before 1.0.6.56 (the golden below was captured from
# the pre-change endpoint on 1.0.6.55 d976451), and a missing validator lib
# refuses only bodies that carry a TCP entry — no existing config gains a
# refusal. A bundle that echoes its whole config back (every mqtt.js before
# 1.0.6.56) must not persist the GET-only `capabilities` into the YAML.
#
# METHOD — behavioural, the shipped endpoint in a sandbox (the
# gateway-acl-contract idiom). The SHIPPED mqtt_config.cgi is copied into a temp
# dir with the SHIPPED lib_web_auth.sh + lib_web_json.sh, its absolute config
# path (/etc/sa02m-modbus-mqtt.yaml) retargeted into the sandbox, and driven as
# a real CGI (REQUEST_METHOD / HTTP_COOKIE / HTTP_X_SA02M_CSRF / CONTENT_LENGTH,
# JSON body on stdin). The session and CSRF token are REAL, minted through the
# shipped auth lib. `sudo` is a recording PATH shim that captures the YAML the
# endpoint hands the privileged helper — "what would have landed in /etc" is
# asserted, and no privileged helper is ever run. SA02M_BRIDGE_LIB_DIR points
# the endpoint at the repo's opt/sa02m-modbus-mqtt (the validator under test)
# or at an empty dir (the missing-lib cases).
#
# NON-VACUOUS: an un-retargeted config path, a session the shipped lib refuses
# to mint, a missing endpoint/lib/validator, or a helper shim never invoked on
# the save cases FAILS rather than passing on an empty sweep.
#
# Proven RED on the pre-change endpoint (1.0.6.55): it saved a loopback TCP
# entry and every other refusal class (ok:true, helper invoked), and its GET
# carried no `capabilities`. The comment-out of the endpoint's
# `validate_devices(` line is this gate's registered case in
# `comment-mutation-proof`.
#
# Requires python3 with PyYAML (the endpoint itself does); skips nothing.
#
# Run: bash scripts/dev/test-mqtt-config-transport.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

CGI_SRC=${MQTT_CONFIG_CGI_SRC:-www/network_config/cgi-bin/mqtt_config.cgi}
AUTH_LIB=www/network_config/cgi-bin/lib_web_auth.sh
JSON_LIB=www/network_config/cgi-bin/lib_web_json.sh
BRIDGE_LIB=opt/sa02m-modbus-mqtt

fails=0
ok()  { printf 'mqtt-config-transport: ok    %s\n' "$1"; }
bad() { printf 'mqtt-config-transport: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

for f in "$CGI_SRC" "$AUTH_LIB" "$JSON_LIB" "$BRIDGE_LIB/bridge_bus.py"; do
    [ -f "$f" ] || { echo "mqtt-config-transport: FAIL - not found: $f"; exit 1; }
done

PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import yaml, json' >/dev/null 2>&1; then PY=$p; break; fi
done
[ -n "$PY" ] || { echo "mqtt-config-transport: FAIL - no python3 with PyYAML; the endpoint needs it too"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
# Paths python sees are in a form both shells understand (git-bash on Windows
# hands python.exe a C:/… path); paths only bash sees stay POSIX. No-op on Linux.
TW="$T"
command -v cygpath >/dev/null 2>&1 && TW=$(cygpath -m "$T")
LIBW="$ROOT/$BRIDGE_LIB"
command -v cygpath >/dev/null 2>&1 && LIBW=$(cygpath -m "$LIBW")
export T_DIR="$TW"
BIN="$T/bin"; CGIDIR="$T/cgi-bin"
mkdir -p "$BIN" "$CGIDIR" "$T/sessions" "$T/nolib"
YAML_FILE="$TW/sa02m-modbus-mqtt.yaml"
APPLIED="$TW/applied.yaml"
SUDOALL="$TW/sudo-all.log"
: > "$SUDOALL"

# PYTHONUTF8=1 on every endpoint run: Windows python otherwise decodes the
# UTF-8 body as cp1251 (mojibake in a Cyrillic device name); Linux, the board
# and CI already read UTF-8, so this only makes the dev box agree with them.

# ── sandbox the endpoint ───────────────────────────────────────────────────
sed "s|/etc/sa02m-modbus-mqtt.yaml|$YAML_FILE|g" "$CGI_SRC" > "$CGIDIR/mqtt_config.cgi"
cp "$AUTH_LIB" "$CGIDIR/lib_web_auth.sh"
cp "$JSON_LIB" "$CGIDIR/lib_web_json.sh"
chmod +x "$CGIDIR/mqtt_config.cgi"
if grep -q '/etc/sa02m-modbus-mqtt.yaml' "$CGIDIR/mqtt_config.cgi"; then
    echo "mqtt-config-transport: FAIL - config path not retargeted; the run would touch the host /etc"; exit 1
fi
grep -q "$YAML_FILE" "$CGIDIR/mqtt_config.cgi" \
    || { echo "mqtt-config-transport: FAIL - retarget produced no sandbox config path"; exit 1; }

cat > "$BIN/sudo" <<'SHIM'
#!/bin/bash
printf '%s\n' "$*" >> "$T_DIR/sudo-all.log"
cp "${!#}" "$T_DIR/applied.yaml" 2>/dev/null
exit 0
SHIM
chmod +x "$BIN/sudo"
# Windows git-bash only (no-op on Linux/CI): the endpoint embeds its /tmp body
# path INSIDE `python3 -c "…open('/tmp/…')…"`, and MSYS converts only an
# argument that IS a path, never one inside code — so python.exe could not open
# the body and every save read as invalid_json. The shim rewrites that prefix
# in the harness only; the shipped endpoint runs on Linux, where /tmp is /tmp.
if command -v cygpath >/dev/null 2>&1; then
    REAL_PY=$(command -v "$PY")
    TMPW=$(cygpath -m /tmp)
    cat > "$BIN/python3" <<SHIM
#!/bin/bash
args=()
for a in "\$@"; do args+=("\${a//\/tmp\//$TMPW/}"); done
exec "$REAL_PY" "\${args[@]}"
SHIM
    chmod +x "$BIN/python3"
fi
PATH="$BIN:$PATH"; export PATH

# ── a REAL session + CSRF token, minted by the shipped auth lib ────────────
export SA02M_SESSION_DIR="$T/sessions"
# shellcheck source=www/network_config/cgi-bin/lib_web_auth.sh
. "$CGIDIR/lib_web_auth.sh"
TOKEN=$(web_session_create admin) || TOKEN=""
[ -n "$TOKEN" ] || { echo "mqtt-config-transport: FAIL - could not mint a session through the shipped lib"; exit 1; }
CSRF=$(web_csrf_token_for_session "$TOKEN") || CSRF=""
[ -n "$CSRF" ] || { echo "mqtt-config-transport: FAIL - could not mint a CSRF token through the shipped lib"; exit 1; }
GOOD_COOKIE="session_token=$TOKEN"

call() {  # <method> <cookie> <csrf> <body> [lib dir]
    rm -f "$APPLIED"
    local len
    len=$(printf '%s' "$4" | wc -c | tr -d ' ')
    printf '%s' "$4" | env \
        REQUEST_METHOD="$1" \
        HTTP_COOKIE="$2" \
        HTTP_X_SA02M_CSRF="$3" \
        CONTENT_LENGTH="$len" \
        SA02M_SESSION_DIR="$SA02M_SESSION_DIR" \
        SA02M_BRIDGE_LIB_DIR="${5:-$LIBW}" \
        PYTHONUTF8=1 \
        PATH="$PATH" \
        bash "$CGIDIR/mqtt_config.cgi" 2>/dev/null | sed '1,/^\r\{0,1\}$/d' | tr -d '\r'
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

# One value out of the YAML handed to the helper.
applied() {  # <python expression over `d`>
    [ -f "$APPLIED" ] || { printf '<<no yaml applied>>\n'; return 0; }
    APPLIED_FILE="$APPLIED" "$PY" - "$1" <<'PY'
import os, sys, yaml
d = yaml.safe_load(open(os.environ["APPLIED_FILE"], encoding="utf-8")) or {}
print(eval(sys.argv[1], {"d": d}))
PY
}

MQTT='"mqtt":{"broker":"127.0.0.1","port":1883,"qos":1,"retain":true}'
RTU_BODY='{'"$MQTT"',"devices":[{"id":"mr02m-COM1-5","type":"mr02m","port":"/dev/COM1","baudrate":115200,"address":5,"module_type":1,"name":"Модуль МР-02м","restart":true},{"id":"carel-COM3-1","type":"carel","port":"/dev/COM3","baudrate":19200,"address":1,"family":"crst","poll_s":2},{"id":"tmpl-COM5-30","type":"template","template":"example","port":"/dev/COM5","baudrate":9600,"address":30,"ai":[{"ch":1,"sensor":5}]}],"restart":false}'
TCP_TMPL='{"id":"mp02-ahu-tcp-192_168_1_20-1","type":"template","template":"mp02-ahu","transport":"tcp","host":"192.168.1.20","tcp_port":502,"address":1,"poll_s":2}'
TCP_CAREL='{"id":"carel-tcp-192_168_1_50-1","type":"carel","family":"crst","app_version":"2.03.00.46","transport":"tcp","host":"192.168.1.50","tcp_port":502,"address":1,"poll_s":2,"tcp_timeout_s":1.5,"name":"Carel c.pCOmini (192.168.1.50:502 addr=1)"}'
tcp_body() {  # <device json …>
    local IFS=,
    printf '{%s,"devices":[{"id":"mr02m-COM1-5","type":"mr02m","port":"/dev/COM1","address":5},%s]}' "$MQTT" "$*"
}

# The YAML the pre-1.0.6.56 endpoint wrote for RTU_BODY (captured on 1.0.6.55).
GOLDEN="$T/golden.yaml"
cat > "$GOLDEN" <<'YML'
# SA-02m Modbus→MQTT bridge configuration
# Managed by web UI. Edit manually or via MQTT tab.

mqtt:
  broker: 127.0.0.1
  port: 1883
  qos: 1
  retain: true
devices:
- id: mr02m-COM1-5
  type: mr02m
  port: /dev/COM1
  baudrate: 115200
  address: 5
  module_type: 1
  name: Модуль МР-02м
- id: carel-COM3-1
  type: carel
  port: /dev/COM3
  baudrate: 19200
  address: 1
  family: crst
  poll_s: 2
- id: tmpl-COM5-30
  type: template
  template: example
  port: /dev/COM5
  baudrate: 9600
  address: 30
  ai:
  - ch: 1
    sensor: 5
YML

# Byte comparison against the golden. Windows python writes text mode as CRLF,
# so on a git-bash host (only) the CRs are dropped first; on Linux — the board
# and CI — the comparison is exact.
is_golden() {  # <file>
    if command -v cygpath >/dev/null 2>&1; then
        tr -d '\r' < "$1" | cmp -s - "$GOLDEN"
    else
        cmp -s "$1" "$GOLDEN"
    fi
}

if [ "${MQCFG_CAPTURE_GOLDEN:-0}" = 1 ]; then
    call POST "$GOOD_COOKIE" "$CSRF" "$RTU_BODY" >&2
    cp "$APPLIED" "$MQCFG_GOLDEN_OUT"
    exit 0
fi

# ── 1-2: auth and CSRF come before anything ────────────────────────────────
n0=$(grep -c . "$SUDOALL")
out=$(call POST "session_token=$(printf 'd%.0s' $(seq 1 64))" "$CSRF" "$(tcp_body "$TCP_TMPL")")
if [[ "$out" == *unauthorized* ]] && [ ! -f "$APPLIED" ]; then
    ok "1 an unauthenticated save is refused before any write"
else
    bad "1 an unauthenticated save was not refused cleanly: $out"
fi
out=$(call POST "$GOOD_COOKIE" "not-the-token" "$(tcp_body "$TCP_TMPL")")
if [[ "$out" == *csrf* ]] && [ ! -f "$APPLIED" ]; then
    ok "2 a save without the CSRF token is refused before any write"
else
    bad "2 a CSRF-less save was not refused cleanly: $out"
fi
[ "$(grep -c . "$SUDOALL")" = "$n0" ] || bad "1-2 the helper shim ran for a refused request"

# ── 3: RS-485-only save = the pre-change bytes ─────────────────────────────
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$RTU_BODY")
if [ "$(jget "$out" 'j.get("ok")')" = True ] && [ -f "$APPLIED" ] && is_golden "$APPLIED"; then
    ok "3 an RS-485-only save writes the byte-identical pre-1.0.6.56 YAML"
else
    bad "3 an RS-485-only save changed: reply=$out; diff: $(diff "$GOLDEN" "$APPLIED" 2>&1 | head -5)"
fi

# ── 4-5: valid TCP entries save, and survive a GET→POST round trip ─────────
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(tcp_body "$TCP_TMPL" "$TCP_CAREL")")
got=$(applied '[(x["id"], x.get("transport"), x.get("host"), x.get("tcp_port"), x.get("family"), "port" in x) for x in d["devices"][1:]]')
if [ "$(jget "$out" 'j.get("ok")')" = True ] \
   && [ "$got" = "[('mp02-ahu-tcp-192_168_1_20-1', 'tcp', '192.168.1.20', 502, None, False), ('carel-tcp-192_168_1_50-1', 'tcp', '192.168.1.50', 502, 'crst', False)]" ]; then
    ok "4 valid TCP template + Carel entries save as sent"
else
    bad "4 valid TCP entries did not save as sent: reply=$out applied=$got"
fi
if [ -f "$APPLIED" ]; then
    cp "$APPLIED" "$YAML_FILE"
    cp "$APPLIED" "$T/first.yaml"
    got_json=$(call GET "$GOOD_COOKIE" "" "")
    # What a cached pre-1.0.6.56 mqtt.js does: send back the WHOLE document it
    # loaded (Object.assign({}, _config, {restart})) — unknown device keys AND
    # the new top-level `capabilities`, which must not land in the YAML.
    # (restart:false here only so no background restart job is spawned.)
    again=$(printf '%s' "$got_json" | "$PY" -c 'import json,sys
j = json.loads(sys.stdin.read())
assert "capabilities" in j, "GET carried no capabilities to echo back"
j["restart"] = False
print(json.dumps(j, ensure_ascii=False))')
    out=$(call POST "$GOOD_COOKIE" "$CSRF" "$again")
    if [ "$(jget "$out" 'j.get("ok")')" = True ] && cmp -s "$APPLIED" "$T/first.yaml"; then
        ok "5 GET→POST round-trips the TCP entries unchanged, capabilities not persisted (a cached old bundle keeps them)"
    else
        bad "5 the GET→POST round trip changed the TCP entries: reply=$out; diff: $(diff "$T/first.yaml" "$APPLIED" 2>&1 | head -5)"
    fi
else
    bad "5 no applied YAML to round-trip (case 4 wrote nothing)"
fi

# ── 6-12: every refusal class refuses the WHOLE save, names the reason ─────
refuse() {  # <case> <reason> <device json>
    local before out
    before=$(grep -c . "$SUDOALL")
    out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(tcp_body "$TCP_TMPL" "$3")")
    if [ "$(jget "$out" '(j.get("ok"), j.get("error"), j.get("reason"))')" = "(False, 'invalid_device', '$2')" ] \
       && [ ! -f "$APPLIED" ] && [ "$(grep -c . "$SUDOALL")" = "$before" ]; then
        ok "$1 refused with $2, nothing written, helper not run"
    else
        bad "$1 was not refused as $2 (or something was written): $out"
    fi
}
refuse "6 loopback host"            host_forbidden        '{"id":"x-tcp","type":"template","template":"mp02-ahu","transport":"tcp","host":"127.0.0.1","address":1}'
refuse "7 leading-zero octet"       host_not_ipv4_literal '{"id":"x-tcp","type":"template","template":"mp02-ahu","transport":"tcp","host":"0177.0.0.1","address":1}'
refuse "8 hostname"                 host_not_ipv4_literal '{"id":"x-tcp","type":"template","template":"mp02-ahu","transport":"tcp","host":"mp02.local","address":1}'
refuse "9 MR-02m over TCP"          type_not_tcp_capable  '{"id":"x-tcp","type":"mr02m","transport":"tcp","host":"192.168.1.30","address":1}'
refuse "10 Carel over TCP, no family" carel_family_required '{"id":"carel-tcp-x","type":"carel","transport":"tcp","host":"192.168.1.50","address":1}'
refuse "11 unknown transport"       transport_unknown     '{"id":"x-udp","type":"template","template":"mp02-ahu","transport":"udp","host":"192.168.1.20","address":1}'
refuse "12 COM port on a TCP entry" serial_keys_on_tcp    '{"id":"x-tcp","type":"template","template":"mp02-ahu","transport":"tcp","host":"192.168.1.20","port":"/dev/COM3","address":1}'

# ── 13-14: validator lib missing — fail closed on TCP, unchanged on RS-485 ─
before=$(grep -c . "$SUDOALL")
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(tcp_body "$TCP_TMPL")" "$TW/nolib")
if [ "$(jget "$out" '(j.get("ok"), j.get("error"))')" = "(False, 'transport_validator_unavailable')" ] \
   && [ ! -f "$APPLIED" ] && [ "$(grep -c . "$SUDOALL")" = "$before" ]; then
    ok "13 lib missing + a TCP entry: refused (transport_validator_unavailable), nothing written"
else
    bad "13 lib missing + a TCP entry was not refused fail-closed: $out"
fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$RTU_BODY" "$TW/nolib")
if [ "$(jget "$out" 'j.get("ok")')" = True ] && [ -f "$APPLIED" ] && is_golden "$APPLIED"; then
    ok "14 lib missing + RS-485 only: saves exactly as before"
else
    bad "14 lib missing broke an RS-485-only save: $out"
fi

# ── 15-16: GET advertises what the modal may offer — only with the lib ─────
cp "$GOLDEN" "$YAML_FILE"
out=$(call GET "$GOOD_COOKIE" "" "")
if [ "$(jget "$out" 'j.get("capabilities")')" = "{'tcp_types': ['template', 'carel'], 'carel_families': ['crst', 'uaria'], 'tcp_default_port': 502}" ] \
   && [ "$(jget "$out" 'len(j.get("devices") or [])')" = 3 ]; then
    ok "15 GET carries capabilities (tcp_types template+carel) beside the unchanged config"
else
    bad "15 GET has no/wrong capabilities: $out"
fi
out=$(call GET "$GOOD_COOKIE" "" "" "$TW/nolib")
if [ "$(jget "$out" '"capabilities" in j')" = False ] && [ "$(jget "$out" 'len(j.get("devices") or [])')" = 3 ]; then
    ok "16 lib missing: GET omits capabilities (the UI hides Ethernet) and still serves the config"
else
    bad "16 lib missing: GET is not fail-closed: $out"
fi
rm -f "$YAML_FILE"
out=$(call GET "$GOOD_COOKIE" "" "")
if [ "$(jget "$out" '(j.get("devices"), j["mqtt"].get("port"), "capabilities" in j)')" = "([], 1883, True)" ]; then
    ok "17 no config yet: GET serves the defaults and the capabilities"
else
    bad "17 no config yet: GET is wrong: $out"
fi

# ── non-vacuity ────────────────────────────────────────────────────────────
sudo_calls=$(grep -c . "$SUDOALL")
if [ "$sudo_calls" -ge 4 ]; then
    ok "18 the privileged-helper shim was exercised $sudo_calls time(s) - the run drove the real write path"
else
    bad "18 only $sudo_calls save(s) reached the helper shim - the harness is not exercising the endpoint"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "mqtt-config-transport: ALL OK - 18 case(s) green"
    exit 0
fi
echo "mqtt-config-transport: $fails FAILURE(S)"
exit 1
