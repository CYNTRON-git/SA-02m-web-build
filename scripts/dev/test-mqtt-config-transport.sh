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
# Requires python3 with PyYAML and paho-mqtt (the live cache writer); skips nothing.
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
# Simulate an unavailable optional TCP validator with a working MQTT core.
cp "$BRIDGE_LIB/bridge_meta.py" "$T/nolib/"
YAML_FILE="$TW/sa02m-modbus-mqtt.yaml"
APPLIED="$TW/applied.yaml"
SUDOALL="$TW/sudo-all.log"
: > "$SUDOALL"
# The endpoint's stderr of the LAST call — what fcgiwrap hands to the nginx
# error log on the board.
CGI_ERR="$T/cgi-stderr.log"

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
        bash "$CGIDIR/mqtt_config.cgi" 2>"$CGI_ERR" | sed '1,/^\r\{0,1\}$/d' | tr -d '\r'
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
import json, os, sys, yaml
d = yaml.safe_load(open(os.environ["APPLIED_FILE"], encoding="utf-8")) or {}
print(eval(sys.argv[1], {"d": d, "json": json}))
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
# Review 1.0.6.56 A5: the missing validator is never silent — it is named in
# the log the operator's integrator can read, not only in the refusal code.
err=$(cat "$CGI_ERR" 2>/dev/null)
if [[ "$err" == *"validator unavailable"* && "$err" == *bridge_bus* ]]; then
    ok "13b lib missing on save: the import failure is logged to stderr (nginx error log)"
else
    bad "13b lib missing on save left no trace on stderr: '${err}'"
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
if [ "$(jget "$out" 'j.get("capabilities")')" = "{'tcp_types': ['template', 'carel'], 'carel_families': ['crst', 'uaria'], 'tcp_default_port': 502, 'rtu_tcp_types': ['ce02m3', 'dtv', 'led', 'mr02m', 'template']}" ] \
   && [ "$(jget "$out" 'len(j.get("devices") or [])')" = 3 ]; then
    ok "15 GET carries capabilities (tcp_types template+carel) beside the unchanged config"
else
    bad "15 GET has no/wrong capabilities: $out"
fi
if [[ "$(cat "$CGI_ERR" 2>/dev/null)" != *"validator unavailable"* ]]; then
    ok "15b lib present: no import-failure line on stderr"
else
    bad "15b lib present but the endpoint logged an import failure: $(cat "$CGI_ERR")"
fi
out=$(call GET "$GOOD_COOKIE" "" "" "$TW/nolib")
if [ "$(jget "$out" '"capabilities" in j')" = False ] && [ "$(jget "$out" 'len(j.get("devices") or [])')" = 3 ]; then
    ok "16 lib missing: GET omits capabilities (the UI hides Ethernet) and still serves the config"
else
    bad "16 lib missing: GET is not fail-closed: $out"
fi
err=$(cat "$CGI_ERR" 2>/dev/null)
if [[ "$err" == *"validator unavailable"* && "$err" == *bridge_bus* ]]; then
    ok "16b lib missing on GET: the Ethernet option does not vanish silently - the import failure is logged to stderr"
else
    bad "16b lib missing on GET left no trace on stderr (the Ethernet option would just vanish): '${err}'"
fi
rm -f "$YAML_FILE"
out=$(call GET "$GOOD_COOKIE" "" "")
if [ "$(jget "$out" '(j.get("devices"), j["mqtt"].get("port"), "capabilities" in j)')" = "([], 1883, True)" ]; then
    ok "17 no config yet: GET serves the defaults and the capabilities"
else
    bad "17 no config yet: GET is wrong: $out"
fi

# Exercise the catalog and the authenticated write endpoint on one template.
export SA02M_WB_TEMPLATES_DIR="$TW/event-templates"
mkdir -p "$SA02M_WB_TEMPLATES_DIR"
cat > "$SA02M_WB_TEMPLATES_DIR/config-events.json" <<'JSON'
{"device":{"channels":[
  {"name":"group","channels":[{"name":"voltage","reg_type":"input","address":"0x10d9","scale":0.01}]},
  {"name":"disabled","reg_type":"input","address":4,"enabled":false},
  {"name":"relay/on","reg_type":"holding","address":7,"readonly":false},
  {"name":"relay/meta/type","reg_type":"holding","address":8,"readonly":false},
  {"name":"+","reg_type":"holding","address":9,"readonly":false},
  {"name":"unwritable_from_web","reg_type":"holding","address":5,"readonly":false},
  {"name":"trigger_sensitivity","reg_type":"holding","address":6,"readonly":false}
]}}
JSON
catalog() {
    env REQUEST_METHOD=GET HTTP_COOKIE="$1" SA02M_BRIDGE_LIB_DIR="$LIBW" \
        bash www/network_config/cgi-bin/mqtt_templates.cgi | sed '1,/^\r\{0,1\}$/d'
}
body_for_selection() {
    printf '{"devices":[{"type":"template","id":"fixture","port":"/dev/COM1","address":5,"template":"events","fast_modbus":true,"fmb_event_channels":%s}]}' "$1"
}
if [ "$(jget "$(catalog '')" "j.get('error')")" = unauthorized ]; then ok 'catalog requires authentication'; else bad 'anonymous catalog'; fi
missing=$(SA02M_WB_TEMPLATES_DIR="$TW/missing-templates" catalog "$GOOD_COOKIE")
if [ "$(jget "$missing" "j.get('error')")" = template_catalog_unavailable ]; then
    ok 'unavailable directory does not replace a previously loaded catalog with an empty one'
else bad "missing directory presented as a usable catalog: $missing"; fi
out=$(catalog "$GOOD_COOKIE")
if [ "$(jget "$out" "[(c['name'],c['fast_eligible'],c['web_write']) for c in j['templates'][0]['channels']]")" = "[('voltage', True, False), ('unwritable_from_web', True, False), ('trigger_sensitivity', True, True)]" ]; then
    ok 'catalog uses actual nested supported channels and the web write allowlist'
else
    bad "catalog disagrees with the runtime: $out"
fi
body=$(body_for_selection '["voltage"]')
out=$(call POST "$GOOD_COOKIE" "$CSRF" "${body/fixture/room\/strip}")
if [ "$(jget "$out" "j.get('ok')")" = True ] && [ "$(applied "d['devices'][0]['id']")" = room/strip ] &&
        [ "$(applied "d['devices'][0]['fmb_event_channels']")" = "['voltage']" ]; then
    ok 'existing nested RTU device ID still accepts a channel selection'
else bad "nested RTU device refused or changed: $out"; fi

cache="$TW/live-cache"
# Git Bash needs the harness's python3 shim in PATH in place of the board path.
sed 's|/usr/bin/python3|python3|g' www/network_config/cgi-bin/mqtt_live.cgi > "$CGIDIR/mqtt_live.cgi"
"$PY" - "$LIBW" "$cache" <<'PY'
import json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
os.environ["SA02M_MQTT_LIVE_CACHE"] = sys.argv[2]
from bridge_mqtt import DeviceLiveCache
DeviceLiveCache.set_online("room/errors", True)
DeviceLiveCache.set_error("room/errors", "temperature", "r")
DeviceLiveCache.flush_file("room/errors")
for device, value in [("room", "17"), ("room/strip", "223"), ("room strip", "92"), ("room/offline", "1"),
                      ("room.events", "93"), ("room.profile", "94"), (42, "95")]:
    DeviceLiveCache.set_online(device, device != "room/offline")
    DeviceLiveCache.set_control(device, "voltage", value)
    DeviceLiveCache.flush_file(device)
for reserved in ('room.events', 'room.profile'):
    # A sidecar belonging to room may itself contain the other ID in its data.
    # The identity guard must never accept a nested field as the owner of the file.
    (pathlib.Path(sys.argv[2]) / (reserved + ".json")).write_text(json.dumps({
        "device": "room", "ok": True, "controls": {"device": reserved, "voltage": "wrong"}}))
PY
if "$PY" - "$LIBW" <<'PY'
import sys
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, sys.argv[1])
import mqtt_live_snapshot as live
import mqtt_monitor_stream as stream
client = Mock()
prefix = '/devices/room/strip/controls/'
def send(filters):
    assert filters == [(prefix + '+', 0), (prefix + '+/meta/units', 0), (prefix + '+/meta/error', 0)]
    for topic, value in [(prefix+'voltage','223'), (prefix+'voltage/meta/units','V'),
                         (prefix+'energy/meta/error','r'), ('/devices/room/controls/voltage','17'),
                         (prefix+'voltage/on','0')]:
        client.on_message(client, None, SimpleNamespace(topic=topic, payload=value.encode()))
client.subscribe.side_effect = send
with patch.object(live.mqtt, 'Client', return_value=client) as reader:
    data = live.snapshot_paho('room/strip', idle_s=0)
    live.snapshot_paho('room/strip', idle_s=0)
    ids = [call.kwargs['client_id'] for call in reader.call_args_list]
    assert len(ids) == len(set(ids)) == 2
assert data['controls'] == {'voltage':'223'} and data['units'] == {'voltage':'V'}
assert data['errors'] == {'energy':'r'}
with patch.object(sys, 'argv', ['mqtt_monitor_stream.py', 'device=room%2Fstrip']), \
     patch.object(stream, 'mqtt_client_v5', side_effect=StopIteration) as reader, redirect_stdout(StringIO()):
    for _ in range(2):
        try:
            stream.main()
        except StopIteration:
            pass
    ids = [call.args[0] for call in reader.call_args_list]
    assert len(ids) == len(set(ids)) == 2
PY
then ok 'snapshot and SSE use independent client IDs; fallback selects exact nested controls and discards a neighbor and /on'
else bad 'nested fallback subscription, topic selection or client ID isolation failed'; fi
live() {
    env REQUEST_METHOD=GET HTTP_COOKIE="$GOOD_COOKIE" QUERY_STRING="$1" SA02M_BRIDGE_LIB_DIR="$LIBW" \
        SA02M_MQTT_LIVE_CACHE="$cache" bash "$CGIDIR/mqtt_live.cgi" | sed '1,/^\r\{0,1\}$/d'
}
out=$(live 'device=room%2Ferrors')
if [ "$(jget "$out" "(j.get('source'), j.get('controls'), j.get('errors'))")" = "('cache', {}, {'temperature': 'r'})" ]; then
    ok 'channel read failure is visible even before its first good sample'
else bad "lost the errors-only live snapshot: $out"; fi
for pair in 'room:17' 'room%2Fstrip:223' 'room%20strip:92' 'room.events:93' 'room.profile:94' '42:95'; do
    out=$(live "device=${pair%:*}")
    if [ "$(jget "$out" "j.get('controls', {}).get('voltage')")" = "${pair#*:}" ]; then
        ok "live CGI reads the intended device: ${pair%:*}"
    else bad "cross-device/absent live value for $pair: $out"; fi
done
out=$(live 'device=room%2Foffline')
if [ "$(jget "$out" "(j.get('source'), j.get('ok'))")" = "('cache', False)" ]; then
    ok 'an offline nested device stays offline when its last values are cached'
else bad "offline cache read reported the wrong status: $out"; fi
for url_id in 'room+strip' 'room%2Bstrip' 'room%00' 'room%2F..%2Fstrip' 'room%2Fcontrols%2Frelay'; do
    out=$(live "device=$url_id")
    if [ "$(jget "$out" "(j.get('ok'), j.get('controls'))")" = "(False, {})" ]; then
        ok "live CGI refuses unsafe/ambiguous query: $url_id"
    else bad "unsafe live query: $url_id ($out)"; fi
done
for unsafe_id in '+' '#' 'another/controls/relay'; do
    out=$(call POST "$GOOD_COOKIE" "$CSRF" "${body/fixture/$unsafe_id}")
    if [ "$(jget "$out" "j.get('reason')")" = invalid_device_id ] && [ ! -f "$APPLIED" ]; then
        ok "device ID refused before YAML: $unsafe_id"
    else
        bad "unsafe MQTT device ID was not refused: $out"
    fi
done
# An older/incomplete cache refresh may provide bridge_meta without the new guard.
mkdir -p "$T/old-core"
touch "$T/old-core/bridge_meta.py"
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$body" "$TW/old-core")
if [ "$(jget "$out" "j.get('error')")" = mqtt_id_validator_unavailable ] && [ ! -f "$APPLIED" ]; then
    ok 'a mismatched MQTT core cannot silently accept unchecked IDs'
else
    bad "an unchecked device ID reached YAML: $out"
fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$body")
if [ "$(jget "$out" "j.get('ok')")" = True ] && [ "$(applied "d['devices'][0]['fmb_event_channels']")" = "['voltage']" ]; then
    ok 'selected channel reaches YAML'
    cp "$APPLIED" "$YAML_FILE"
else
    bad "valid event selection was not applied: $out"
fi
for bad_choice in '["disabled"]' '["missing"]' '["relay/on"]' '["relay/meta/type"]' '["+"]' '["voltage","voltage"]' '[5]' '"voltage"'; do
    out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(body_for_selection "$bad_choice")")
    if [ "$(jget "$out" "j.get('reason')")" = invalid_template_event_channels ] && [ ! -f "$APPLIED" ]; then
        ok "invalid event choice refused: $bad_choice"
    else
        bad "invalid event choice accepted: $bad_choice ($out)"
    fi
done
out=$(call POST "$GOOD_COOKIE" "$CSRF" "${body/\"port\":\"\/dev\/COM1\"/\"transport\":\"tcp\",\"host\":\"192.0.2.1\"}")
if [ "$(jget "$out" "j.get('reason')")" = invalid_template_event_channels ] && [ ! -f "$APPLIED" ]; then
    ok 'Modbus TCP event list refused'
else
    bad "Modbus TCP event list reached YAML: $out"
fi
mv "$SA02M_WB_TEMPLATES_DIR/config-events.json" "$T/offline-template.json"
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$body")
if [ "$(jget "$out" "j.get('ok')")" = True ]; then ok 'saved selection survives unavailable catalog'; else bad "saved selection lost: $out"; fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" "${body/COM1/COM4}")
if [ "$(jget "$out" "j.get('reason')")" = template_event_catalog_unavailable ] && [ ! -f "$APPLIED" ]; then
    ok 'moving a subscription to another bus requires a valid catalog'
else
    bad "moved subscription bypassed validation: $out"
fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(body_for_selection '["new"]')")
if [ "$(jget "$out" "j.get('reason')")" = template_event_catalog_unavailable ] && [ ! -f "$APPLIED" ]; then
    ok 'new subscription refused when catalog unavailable'
else
    bad "unchecked subscription reached YAML: $out"
fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(body_for_selection '[]')")
if [ "$(jget "$out" "j.get('ok')")" = True ]; then ok 'selection clears without the catalog'; else bad "clear failed: $out"; fi

# Old YAML can carry numeric IDs. An offline catalog must preserve the ID's
# type while an edit to a different device is applied; bool is never an ID.
for typed_id in '42' '"42"' 'true'; do
    typed_body=$("$PY" - "$YAML_FILE" "$typed_id" "$body" <<'PY'
import json, pathlib, sys, yaml
data = json.loads(sys.argv[3])
data["devices"][0]["id"] = json.loads(sys.argv[2])
peer = {"type": "dtv", "id": "peer", "poll_s": 1}
data["devices"].append(peer)
pathlib.Path(sys.argv[1]).write_text(yaml.safe_dump(data))
peer["poll_s"] = 3
print(json.dumps(data))
PY
)
    out=$(call POST "$GOOD_COOKIE" "$CSRF" "$typed_body")
    if [ "$typed_id" = 'true' ]; then
        if [ "$(jget "$out" "j.get('reason')")" = invalid_device_id ] && [ ! -f "$APPLIED" ]; then
            ok 'prior YAML boolean ID cannot bypass admission'
        else bad "previous bool ID was allowed: $out"; fi
    elif [ "$(jget "$out" "j.get('ok')")" = True ] &&
         [ "$(applied "json.dumps(d['devices'][0]['id'])")" = "$typed_id" ] &&
         [ "$(applied "d['devices'][1]['poll_s']")" = 3 ]; then
        ok "unrelated edit preserved offline selection and ID type: $typed_id"
    else bad "unrelated edit blocked or ID changed: $typed_id ($out)"; fi
    # Python considers True == 1. A historical bool cannot exempt a numeric
    # ID, and changing a stored number to a string is a fresh device config.
    if [ "$typed_id" != '"42"' ]; then
        changed_id='"42"'
        [ "$typed_id" = 'true' ] && changed_id=1
        changed_body=$(printf '%s' "$typed_body" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); d["devices"][0]["id"]=json.loads(sys.argv[1]); print(json.dumps(d))' "$changed_id")
        out=$(call POST "$GOOD_COOKIE" "$CSRF" "$changed_body")
        if [ "$(jget "$out" "j.get('reason')")" = template_event_catalog_unavailable ] && [ ! -f "$APPLIED" ]; then
            ok "changed ID does not inherit offline selection: $typed_id -> $changed_id"
        else bad "ID conversion bypassed catalog validation: $out"; fi
    fi
done

cp "$BRIDGE_LIB/templates/config-mtdx62-mb.json" "$SA02M_WB_TEMPLATES_DIR/"
"$PY" - "$SA02M_WB_TEMPLATES_DIR" <<'PY'
import json, pathlib, sys
base = pathlib.Path(sys.argv[1])
(base / "config-deep.json").write_text('{"device":{"channels":[' + '{"channels":[' * 1500
        + '{"name":"nested","reg_type":"input","address":0}' + ']}' * 1500 + ']}}')
(base / "config-overflow.json").write_text(json.dumps({"device": {"channels": [
    {"name": "bad_scale", "reg_type": "input", "address": 1, "scale": 10**309},
    {"name": "survivor", "reg_type": "input", "address": 2}]}}))
PY
out=$(catalog "$GOOD_COOKIE")
if [ "$(jget "$out" "{t['name']: [c['name'] for c in t['channels']] for t in j['templates'] if t['name'] in ('deep', 'overflow')}")" = "{'overflow': ['survivor']}" ]; then
    ok 'deep template isolated; valid neighbor survives a scale overflow in its own template'
else
    bad 'catalog with malformed templates lost valid files/channels or emitted invalid JSON'
fi
for template in deep overflow; do
    probe=$(body_for_selection '["bad_scale"]')
    out=$(call POST "$GOOD_COOKIE" "$CSRF" "${probe/\"template\":\"events\"/\"template\":\"$template\"}")
    expected=invalid_template_event_channels
    [ "$template" = deep ] && expected=template_event_catalog_unavailable
    if [ "$(jget "$out" "j.get('reason')")" = "$expected" ] && [ ! -f "$APPLIED" ]; then
        ok "unusable template/channel selection returns a named JSON refusal: $template"
    else bad "unusable $template yielded invalid refusal or a write: $out"; fi
done
out=$(catalog "$GOOD_COOKIE")
if [ "$(jget "$out" "any(c['name'] == 'device_status' and c['fast_eligible'] for t in j['templates'] if t['name'] == 'mtdx62-mb' for c in t['channels'])")" = True ]; then
    ok 'MTD scan fingerprint is an eligible serial event'
else
    bad "shipped MTD fingerprint missing from event catalog: $out"
fi
body=$(body_for_selection '["device_status","presence_status"]')
out=$(call POST "$GOOD_COOKIE" "$CSRF" "${body/\"template\":\"events\"/\"template\":\"mtdx62-mb\"}")
if [ "$(jget "$out" "j.get('ok')")" = True ] && [ "$(applied "d['devices'][0]['fmb_event_channels']")" = "['device_status', 'presence_status']" ]; then
    ok 'MTD fingerprint and neighboring selection reach YAML together'
else
    bad "MTD selection changed: $out"
fi

# The same legal name can fit under a short ID, but not a longer UTF-8 ID.
"$PY" - "$TW" "$SA02M_WB_TEMPLATES_DIR" <<'PY'
import json, pathlib, sys
tmp, templates = map(pathlib.Path, sys.argv[1:])
name = "x" * 65490
(templates / "config-limit.json").write_text(json.dumps({"device": {"channels": [
    {"name": name, "reg_type": "input", "address": 0}]}}))
for suffix, device_id in (("short", "x"), ("long", "я" * 13)):
    (tmp / (suffix + ".json")).write_text(json.dumps({"devices": [{"type": "template",
        "template": "limit", "id": device_id, "port": "/dev/COM1", "address": 10,
        "fmb_event_channels": [name]}]}))
PY
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(cat "$T/short.json")")
if [ "$(jget "$out" "j.get('ok')")" = True ]; then ok 'complete topic fits under short ID'; else bad "legal topic refused: $out"; fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(cat "$T/long.json")")
if [ "$(jget "$out" "j.get('reason')")" = invalid_template_event_channels ] && [ ! -f "$APPLIED" ]; then
    ok 'selection cannot exceed MQTT topic limit with actual device ID'
else
    bad "oversized topic reached YAML: $out"
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
    echo "mqtt-config-transport: ALL OK"
    exit 0
fi
echo "mqtt-config-transport: $fails FAILURE(S)"
exit 1
