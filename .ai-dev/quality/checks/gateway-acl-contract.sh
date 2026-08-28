#!/usr/bin/env bash
# gateway-acl-contract — the RS-485 gateway's network-access settings survive
# the panel, and a malformed one is refused before anything is written.
#
# WHY THIS EXISTS. 1.0.6.24 gave every gateway port a bind address and an IP
# allow-list (2026-08-28 audit, H3: an enabled port put Modbus process control
# on every interface with no authentication and no device-side way to narrow
# it). The daemon half is covered by `py-unit-gateway`. This row covers the
# half a unit test cannot see: `gateway_config.cgi` REWRITES the whole per-port
# map from an enumerated list of keys, so a key it does not carry is ERASED on
# the next save from the panel. An allow-list that silently disappears the next
# time the operator presses «Сохранить» is worse than none — the operator
# believes the port is restricted and it is not.
#
# It also pins the failure DIRECTION at this layer: a malformed entry refuses
# the whole save (nothing is written, the helper is never invoked) rather than
# being dropped while the rest is written. Dropping the bad entry is the
# fail-open shape — the config would then look narrower than it is.
#
# METHOD — behavioural, the shipped endpoint in a sandbox.
#   The SHIPPED gateway_config.cgi is copied into a temp dir with the SHIPPED
#   lib_web_auth.sh, its one absolute config path (/etc/sa02m-gateway.yaml) is
#   retargeted into the sandbox, and it is driven as a real CGI: REQUEST_METHOD
#   / HTTP_COOKIE / HTTP_X_SA02M_CSRF / CONTENT_LENGTH in the environment, the
#   JSON body on stdin. The session and the CSRF token are REAL, minted through
#   the shipped auth lib (SA02M_SESSION_DIR), not stubbed. `sudo` is a recording
#   PATH shim that captures the YAML the endpoint hands the privileged helper —
#   so "what would have landed in /etc" is asserted, never assumed, and no
#   privileged helper is ever invoked.
#
#   Case 14 is the source floor a happy path cannot observe: the two keys are
#   named in the endpoint's per-port output map, read through lib_check.sh so a
#   commented-out line cannot satisfy the pin (the comment-blindness class,
#   docs/agent-rules/quality-gate-rigor.md (a)). That line is also this gate's
#   registered case in `comment-mutation-proof`.
#
#   Cases 16-23 cover the PANEL half — the warning shown at the moment a port is
#   enabled is the only notice the operator gets, so "it stopped appearing" must
#   fail here and not in the field. `_accessWarnNeeded`, `_parseAllowInput` and
#   the two helpers deciding what counts as restricted (`_allowListRestricts`,
#   `_allowEntryIsOpen`) are EXTRACTED from the shipped gateway.js and executed
#   in a vm (the rs485-roster-consumer idiom: run the shipped function, never
#   re-implement it), so the gate and the panel cannot drift apart. A failed
#   extraction of any of the four FAILS.
#   Cases 22* pin the direction that shipped wrong in 1.0.6.24: the warning was
#   silenced by allow_from.length alone, so a wide-open list (`0.0.0.0/0`) read
#   as "restricted" and the operator was told nothing about a fully open port.
#   Case 23 pins the opposite direction so the fix cannot become "anything
#   containing 0.0.0.0" — a prefix-less address is a single host.
#
# NON-VACUOUS: an un-retargeted config path, a `sudo` shim that was never
# invoked across the whole run, a session the shipped lib refuses to mint, or a
# missing endpoint FAILS rather than passing on an empty sweep.
#
# Proven RED (1.0.6.24), five mutations of a scratch copy driven through
# GATEWAY_CGI_SRC so the real endpoint is never touched:
#   * dropping "allow_from" from the output map      -> cases 2,6,7,8,9,14 FAIL
#   * dropping "bind" from the output map            -> cases 3,5,10,11,14 FAIL
#   * accepting a malformed allow_from entry         -> case 9 FAIL
#   * skipping the bad entry and writing the rest    -> case 9 FAIL
#   * accepting a malformed bind                     -> cases 10, 11 FAIL
# and eight of a scratch gateway.js through GATEWAY_JS_SRC:
#   * the warning never shows                        -> cases 16, 20b FAIL
#   * an allow-list no longer silences it            -> case 19 FAIL
#   * any 1x.x.x bind counted as loopback            -> case 20b FAIL
#   * the allow-list field split on commas only      -> case 21 FAIL
#   * the decision function renamed away             -> case 16 FAIL (non-vacuity)
#   * `_allowEntryIsOpen` always answering "not open"-> cases 22,22b,22c,22d,22e FAIL
#   * any non-empty list counted as restricting
#     (the defect as shipped in 1.0.6.24)            -> cases 22,22b,22c,22d,22e FAIL
#   * `_allowEntryIsOpen` renamed away               -> case 16 FAIL (non-vacuity)
# and by the standing `comment-mutation-proof` row, which comments the
# `norm_allow_from(name, pcfg, all_errors)` line out.
#
# Requires python3 with PyYAML (the endpoint itself does) and node (the quality
# runner is node, so it is always present); skips nothing.
#
# Run: bash .ai-dev/quality/checks/gateway-acl-contract.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1

# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. "$ROOT/.ai-dev/quality/checks/lib_check.sh"

CGI_SRC=${GATEWAY_CGI_SRC:-www/network_config/cgi-bin/gateway_config.cgi}
AUTH_LIB=www/network_config/cgi-bin/lib_web_auth.sh

fails=0
ok()  { printf 'gateway-acl-contract: ok    %s\n' "$1"; }
bad() { printf 'gateway-acl-contract: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -f "$CGI_SRC" ]  || { echo "gateway-acl-contract: FAIL - endpoint not found: $CGI_SRC"; exit 1; }
[ -f "$AUTH_LIB" ] || { echo "gateway-acl-contract: FAIL - auth lib not found: $AUTH_LIB"; exit 1; }

PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import yaml, json' >/dev/null 2>&1; then PY=$p; break; fi
done
[ -n "$PY" ] || { echo "gateway-acl-contract: FAIL - no python3 with PyYAML; the endpoint needs it too"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
# The endpoint hands its config path to python3. On Windows git-bash a /tmp/...
# path is a shell-only path the Windows interpreter cannot open, so the paths
# PYTHON sees are named in a form both understand ($TW). The paths only BASH
# sees stay POSIX — a drive-letter entry in PATH would split on its own colon
# and the shims below would never be found. No-op on Linux/CI.
TW="$T"
command -v cygpath >/dev/null 2>&1 && TW=$(cygpath -m "$T")
export T_DIR="$TW"
BIN="$T/bin"; CGIDIR="$T/cgi-bin"
mkdir -p "$BIN" "$CGIDIR" "$T/sessions"
YAML_FILE="$TW/sa02m-gateway.yaml"
APPLIED="$TW/applied.yaml"
SUDOLOG="$TW/sudo.log"

# ── sandbox the endpoint ───────────────────────────────────────────────────
sed "s|/etc/sa02m-gateway.yaml|$YAML_FILE|g" "$CGI_SRC" > "$CGIDIR/gateway_config.cgi"
cp "$AUTH_LIB" "$CGIDIR/lib_web_auth.sh"
chmod +x "$CGIDIR/gateway_config.cgi"
if grep -q '/etc/sa02m-gateway.yaml' "$CGIDIR/gateway_config.cgi"; then
    echo "gateway-acl-contract: FAIL - config path not retargeted; the run would touch the host /etc"; exit 1
fi
grep -q "$YAML_FILE" "$CGIDIR/gateway_config.cgi" \
    || { echo "gateway-acl-contract: FAIL - retarget produced no sandbox config path"; exit 1; }

# ── PATH shim: capture what the privileged helper would have installed ─────
cat > "$BIN/sudo" <<'SHIM'
#!/bin/bash
printf '%s\n' "$*" >> "$T_DIR/sudo.log"
printf '%s\n' "$*" >> "$T_DIR/sudo-all.log"
# argv: <helper> <tmp-yaml>. Keep the payload for inspection.
cp "${!#}" "$T_DIR/applied.yaml" 2>/dev/null
exit 0
SHIM
chmod +x "$BIN/sudo"

# The node harness that runs the shipped decision function.
cat > "$T/warn.mjs" <<'NODEEOF'
import { readFileSync, writeFileSync } from 'node:fs';
import vm from 'node:vm';

const src = readFileSync(process.env.GW_JS_PATH, 'utf8');
const grab = (name) => {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) return null;
  let i = src.indexOf('{', start), depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  return null;
};

let fails = 0;
const ok  = m => console.log('gateway-acl-contract: ok    ' + m);
const bad = m => { console.log('gateway-acl-contract: FAIL  ' + m); fails++; };

// Every function the decision depends on is extracted from the shipped file —
// a helper re-implemented here could pass while the panel's own copy is wrong.
const NEEDED = ['_accessWarnNeeded', '_parseAllowInput',
                '_allowListRestricts', '_allowEntryIsOpen'];
const parts = NEEDED.map(grab);
const missing = NEEDED.filter((n, i) => !parts[i]);
if (missing.length) {
  bad('16 could not extract ' + missing.join(', ') + ' from the shipped '
      + 'gateway.js — the extraction is broken, not the panel');
} else {
  const ctx = vm.createContext({});
  vm.runInContext(parts.join('\n') + '\nglobalThis.W = _accessWarnNeeded;'
                  + '\nglobalThis.P = _parseAllowInput;', ctx);
  const W = ctx.W, P = ctx.P;

  const base = { enabled: true, mode: 'modbus_tcp', bind: '0.0.0.0', allow_from: [] };
  const t = (label, cfg, want) => {
    const got = W(cfg);
    if (got === want) ok(label);
    else bad(label + ` — expected ${want}, got ${got}`);
  };

  t('16 an enabled, unrestricted port warns', base, true);
  t('17 a disabled port does not warn', { ...base, enabled: false }, false);
  t('18 mode=disabled does not warn', { ...base, mode: 'disabled' }, false);
  t('19 an allow-list silences the warning',
    { ...base, allow_from: ['192.168.1.0/24'] }, false);
  t('20 a loopback bind silences the warning (unreachable from the network)',
    { ...base, bind: '127.0.0.1' }, false);
  t('20b a LAN bind does NOT silence it (an interface is not an allow-list)',
    { ...base, bind: '192.168.1.5' }, true);

  // 22*: an allow-list that narrows NOTHING must not read as "restricted".
  // The daemon parses each entry with ipaddress.ip_network(entry,
  // strict=False), so every /0 spelling below covers the whole address family
  // and the port stays open to everyone — a hidden warning there is the same
  // lie the warning exists to prevent.
  t('22 an all-addresses allow-list (0.0.0.0/0) still warns',
    { ...base, allow_from: ['0.0.0.0/0'] }, true);
  t('22b an all-addresses IPv6 allow-list (::/0) still warns',
    { ...base, allow_from: ['::/0'] }, true);
  t('22c a host form the daemon normalises to /0 (192.168.1.10/0) still warns',
    { ...base, allow_from: ['192.168.1.10/0'] }, true);
  t('22d a wide-open entry mixed into a real list still warns',
    { ...base, allow_from: ['192.168.1.0/24', '0.0.0.0/0'] }, true);
  t('22e the netmask spelling of /0 (0.0.0.0/0.0.0.0) still warns',
    { ...base, allow_from: ['0.0.0.0/0.0.0.0'] }, true);
  // The opposite direction, so the fix cannot be "anything containing 0.0.0.0":
  // a bare address carries no prefix, so the daemon reads it as a single host.
  t('23 a bare address without a prefix is a single host, not a wildcard',
    { ...base, allow_from: ['0.0.0.0'] }, false);

  const parsed = P(' 192.168.1.10, 10.0.0.0/8  ');
  if (JSON.stringify(parsed) === JSON.stringify(['192.168.1.10', '10.0.0.0/8'])) {
    ok('21 the allow-list field splits on commas and whitespace');
  } else {
    bad('21 allow-list field parsing is wrong: ' + JSON.stringify(parsed));
  }
  if (JSON.stringify(P('')) !== '[]') bad('21b an empty field must parse to []');
}
writeFileSync(process.env.WARN_RC, fails === 0 ? '0' : String(fails));
NODEEOF

PATH="$BIN:$PATH"; export PATH

# ── a REAL session + CSRF token, minted by the shipped auth lib ────────────
export SA02M_SESSION_DIR="$T/sessions"
# shellcheck source=www/network_config/cgi-bin/lib_web_auth.sh
. "$CGIDIR/lib_web_auth.sh"
TOKEN=$(web_session_create admin) || TOKEN=""
[ -n "$TOKEN" ] || { echo "gateway-acl-contract: FAIL - could not mint a session through the shipped lib"; exit 1; }
CSRF=$(web_csrf_token_for_session "$TOKEN") || CSRF=""
[ -n "$CSRF" ] || { echo "gateway-acl-contract: FAIL - could not mint a CSRF token through the shipped lib"; exit 1; }
GOOD_COOKIE="session_token=$TOKEN"

# call() runs inside a command substitution, so a counter incremented there is
# lost with the subshell — the shim keeps its own cumulative log instead.
SUDOALL="$TW/sudo-all.log"
: > "$SUDOALL"

call() {  # <method> <cookie> <csrf> <body>
    : > "$SUDOLOG"; rm -f "$APPLIED"
    printf '%s' "$4" | env \
        REQUEST_METHOD="$1" \
        HTTP_COOKIE="$2" \
        HTTP_X_SA02M_CSRF="$3" \
        CONTENT_LENGTH="${#4}" \
        SA02M_SESSION_DIR="$SA02M_SESSION_DIR" \
        PATH="$PATH" \
        bash "$CGIDIR/gateway_config.cgi" 2>/dev/null | tr -d ' '
    return 0
}

# Read one value out of the YAML the endpoint handed the helper.
applied() {  # <python expression over `d` (the parsed doc)>
    [ -f "$APPLIED" ] || { printf '<<no yaml applied>>\n'; return 0; }
    APPLIED_FILE="$APPLIED" "$PY" - "$1" <<'PY'
import os, sys, yaml
d = yaml.safe_load(open(os.environ["APPLIED_FILE"], encoding="utf-8")) or {}
print(eval(sys.argv[1], {"d": d}))
PY
}

body() {  # <extra per-port json fields>
    printf '{"ports":{"COM1":{"enabled":true,"mode":"modbus_tcp","tcp_port":502,"baudrate":19200,"parity":"none","stopbits":1,"databits":8,"fast_modbus_probe":true%s}}}' "$1"
}

# ── 1-3: a save carrying the access settings lands them ────────────────────
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(body ',"bind":"192.168.1.5","allow_from":["192.168.1.0/24","10.0.0.7"]')")
if [[ "$out" == *'"ok":true'* ]]; then
    ok "1 a save carrying bind + allow_from is accepted"
else
    bad "1 a valid save was rejected: $out"
fi
got=$(applied "d['ports']['COM1'].get('allow_from')")
if [ "$got" = "['192.168.1.0/24', '10.0.0.7']" ]; then
    ok "2 allow_from reaches the config the daemon reads"
else
    bad "2 allow_from did not survive the save (the panel erases it): $got"
fi
got=$(applied "d['ports']['COM1'].get('bind')")
if [ "$got" = "192.168.1.5" ]; then
    ok "3 bind reaches the config the daemon reads"
else
    bad "3 bind did not survive the save: $got"
fi

# ── 4-6: a save that omits them keeps today's open default, and keeps the keys
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(body '')")
if [[ "$out" == *'"ok":true'* ]]; then
    ok "4 a save with no access settings is accepted (the pre-1.0.6.24 body)"
else
    bad "4 a save without the new keys was rejected - deployed panels would break: $out"
fi
got=$(applied "d['ports']['COM1'].get('bind')")
if [ "$got" = "0.0.0.0" ]; then
    ok "5 the default bind is all interfaces (the behaviour that must not move)"
else
    bad "5 default bind is not 0.0.0.0: $got"
fi
got=$(applied "repr(d['ports']['COM1'].get('allow_from'))")
if [ "$got" = "[]" ]; then
    ok "6 the default allow_from is empty = no filtering"
else
    bad "6 default allow_from is not an empty list: $got"
fi

# ── 7-8: values are normalised, not mangled ────────────────────────────────
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(body ',"allow_from":["  192.168.2.30  ","fd00::/8"]')")
got=$(applied "d['ports']['COM1'].get('allow_from')")
if [ "$got" = "['192.168.2.30', 'fd00::/8']" ]; then
    ok "7 entries are trimmed and IPv6 ranges are kept"
else
    bad "7 entry normalisation is wrong: $got"
fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(body ',"allow_from":"192.168.3.1, 10.1.0.0/16"')")
got=$(applied "d['ports']['COM1'].get('allow_from')")
if [ "$got" = "['192.168.3.1', '10.1.0.0/16']" ]; then
    ok "8 a comma-separated string is accepted (the hand-edited form)"
else
    bad "8 comma-separated form is not accepted: $got"
fi

# ── 9-11: fail closed - a malformed value refuses the WHOLE save ───────────
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(body ',"allow_from":["192.168.1.0/24","not-an-ip"]')")
if [[ "$out" == *'"ok":false'* && "$out" == *allow_from* ]]; then
    ok "9 a malformed allow_from entry is refused and named"
else
    bad "9 a malformed allow_from entry was accepted: $out"
fi
out=$(call POST "$GOOD_COOKIE" "$CSRF" "$(body ',"bind":"localhost"')")
if [[ "$out" == *'"ok":false'* && "$out" == *bind* ]]; then
    ok "10 a non-literal bind address is refused and named"
else
    bad "10 a non-literal bind address was accepted: $out"
fi
if [ ! -f "$APPLIED" ]; then
    ok "11 a refused save writes nothing - the helper is never invoked"
else
    bad "11 a refused save still handed a config to the privileged helper: $(cat "$APPLIED")"
fi

# ── 12: the settings round-trip back to the panel ──────────────────────────
cat > "$YAML_FILE" <<'YML'
ports:
  COM1:
    enabled: true
    mode: modbus_tcp
    tcp_port: 502
    bind: 127.0.0.1
    allow_from:
      - 192.168.9.0/24
YML
out=$(call GET "$GOOD_COOKIE" "" "")
if [[ "$out" == *'192.168.9.0/24'* && "$out" == *'127.0.0.1'* ]]; then
    ok "12 GET returns the access settings, so the panel re-saves them instead of erasing them"
else
    bad "12 GET drops the access settings - the next save from the panel would erase them: $out"
fi

# ── 13: auth still comes first ─────────────────────────────────────────────
out=$(call POST "session_token=$(printf 'd%.0s' $(seq 1 64))" "$CSRF" "$(body ',"allow_from":["10.0.0.1"]')")
if [[ "$out" == *unauthorized* ]] && [ ! -f "$APPLIED" ]; then
    ok "13 an unauthenticated save is refused before any write"
else
    bad "13 an unauthenticated save was not refused cleanly: $out"
fi

# ── 14: source floor - both keys are BUILT by the per-port output map ──────
# Pinned on the call sites, not on the key names: `"allow_from":` also appears
# in the GET default block, so a key-name pin would stay green while the POST
# map that actually writes the config had lost it. Read through lib_check.sh,
# so a commented-out line cannot satisfy the pin either.
if stripped_has "$CGI_SRC" 'norm_allow_from(name, pcfg, all_errors)' \
   && stripped_has "$CGI_SRC" 'norm_bind(name, pcfg, all_errors)'; then
    ok "14 the per-port output map builds both access keys"
else
    bad "14 the per-port output map no longer builds bind/allow_from - a save would erase them"
fi


# ── 16-21: the panel WARNING, driven through the SHIPPED gateway.js ────────
# The warning is the only notice the operator gets at the moment of enabling a
# port, so "it stopped appearing" must fail here rather than in the field. The
# decision function is EXTRACTED from the shipped file and executed — not
# re-implemented (the rs485-roster-consumer idiom), so a divergence between the
# gate and the panel is impossible. Extraction failure FAILS (non-vacuity).
GW_JS=${GATEWAY_JS_SRC:-www/network_config/static/js/gateway.js}
if [ ! -f "$GW_JS" ]; then
    bad "16 the panel bundle is missing: $GW_JS"
elif ! command -v node >/dev/null 2>&1; then
    bad "16 node is required to run the shipped warning logic"
else
    GW_JS_PATH="$GW_JS" WARN_RC="$T/warn.rc" node "$T/warn.mjs" 2>&1 | while IFS= read -r line; do
        printf '%s\n' "$line"
    done
    # the node script writes a verdict file the shell can read (a pipe would
    # lose its exit status through the loop above)
    if [ -f "$T/warn.rc" ] && [ "$(cat "$T/warn.rc")" = "0" ]; then
        :
    else
        fails=$((fails + 1))
    fi
fi

# ── non-vacuity ────────────────────────────────────────────────────────────
sudo_calls=0
[ -s "$SUDOALL" ] && sudo_calls=$(grep -c . "$SUDOALL")
if [ "$sudo_calls" -ge 4 ]; then
    ok "15 the privileged-helper shim was exercised $sudo_calls time(s) - the run drove the real write path"
else
    bad "15 only $sudo_calls save(s) reached the helper shim - the harness is not exercising the endpoint"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "gateway-acl-contract: ALL OK - 15 endpoint case(s) + the panel warning cases green"
    exit 0
fi
echo "gateway-acl-contract: $fails FAILURE(S)"
exit 1
