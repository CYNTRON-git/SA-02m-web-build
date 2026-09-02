#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-rs485-roster-consumer.sh — the CONSUMER half of
# docs/contracts/rs485-roster.md §2: the additive `modules` block that
# status.cgi?part=rs485 puts on each port object.
#
# Why this exists: `py-unit-roster` covers `opt/sa02m-rs485-roster/` — the
# PRODUCER — and nothing else. The contracted `modules` block is assembled in
# www/network_config/cgi-bin/status.cgi, a file no row covered for anything but
# bash syntax, so deleting the `modules_frag` line broke contract §2 with every
# gate green (2026-08-28 audit, finding C2 — the "covers names the origin, not
# the breakers" shape, docs/agent-rules/quality-gate-rigor.md (c)).
#
# This harness runs the real chain end to end, producer AND consumer:
#   fixture roster cache  ->  the SHIPPED rs485_roster_cgi.py helper
#                         ->  the SHIPPED rs485_load_roster / rs485_port_json
#                         ->  the port JSON the dashboard actually receives.
# The only stand-ins are the device nodes (/dev/RS-485-N) and the privileged
# stats helper; the JSON on the way out is parsed, not grepped.
#
# What it pins (contract §1/§2 as the CONSUMER must honour them):
#   * a port with roster data carries `modules`, and the block is VERBATIM what
#     the helper produced — status.cgi must not re-derive or reshape it
#     (the cloud contract is a subset of it, rs485-roster.md §3);
#   * port mapping RS-485-N <-> COM(N+1);
#   * an ABSENT or unreadable cache omits `modules` entirely and keeps every
#     legacy field — the "old cached bundle is unaffected" clause;
#   * honesty of the scan source: `live:false` and `online:null` survive the
#     hop, so a stale scan can never render as live-green in the UI;
#   * the emitted string is valid JSON and pure ASCII (the helper's
#     ensure_ascii is what stands in for json_escape on these values).
#
# Method: the four RS-485 functions plus their globals are extracted from the
# shipped status.cgi by function anchor, their absolute roots (/dev/RS-485-,
# the roster cache, the roster helper, the privileged stats helper, the delta
# state file) are retargeted into a scratch tree, and the result is sourced.
# Over-wide or failed extraction, an un-retargeted absolute path, or a missing
# function FAILS rather than passing on zero work.
#
# Proven RED (1.0.6.24) against mutated scratch copies of status.cgi:
#   deleting the `modules_frag` assignment · dropping it from the printf ·
#   emitting `modules` unconditionally (a port with no data gets `"modules":`) ·
#   re-deriving the block instead of passing the helper's output verbatim ·
#   off-by-one port mapping (RS-485-N -> COMN) · reading the cache without the
#   helper. Its comment-out case is registered in `comment-mutation-proof`.
#
# Run: bash scripts/dev/test-rs485-roster-consumer.sh   (bash + python3)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${STATUS_CGI_SRC:-www/network_config/cgi-bin/status.cgi}
HELPER=opt/sa02m-rs485-roster/rs485_roster_cgi.py
JSONLIB=www/network_config/cgi-bin/lib_web_json.sh
[ -f "$SRC" ]     || { echo "FAIL  status.cgi not found: $SRC"; exit 1; }
[ -f "$HELPER" ]  || { echo "FAIL  roster helper not found: $HELPER"; exit 1; }
[ -f "$JSONLIB" ] || { echo "FAIL  json lib not found: $JSONLIB"; exit 1; }

PY=""
for p in python3 python py; do command -v "$p" >/dev/null 2>&1 && { "$p" -c 'import sys' >/dev/null 2>&1 && PY=$p && break; }; done
[ -n "$PY" ] || { echo "FAIL  no working python interpreter (the shipped helper is Python)"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
export T_DIR="$T"
BIN="$T/bin"; DEV="$T/dev"; mkdir -p "$BIN" "$DEV" "$T/cache"
CACHE="$T/roster.json"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── extract the RS-485 block from the shipped CGI ──────────────────────────
EX="$T/rs485-fns.sh"
: > "$EX"
extract_fn() {  # $1 = function name
    awk -v fn="$1" '
        $0 ~ "^" fn "\\(\\) \\{" { inb = 1 }
        inb { print }
        inb && /^\}/ { inb = 0; found = 1 }
        END { exit (found ? 0 : 3) }
    ' "$SRC" >> "$EX" || { echo "FAIL  could not extract ${1}() from $SRC (did the file shape change?)"; exit 1; }
    printf '\n' >> "$EX"
}
# Globals the extracted functions read, taken from the shipped file rather than
# re-declared here — a renamed variable must break this harness, not be papered
# over by a local copy.
{
    printf 'RS485_DRIVER_TEXT=""\ndeclare -A RS485_INUSE\ndeclare -A RS485_MODULES=()\n'
    grep -E '^RS485_ROSTER_CACHE=|^RS485_ROSTER_HELPER=|^RS485_STATS_HELPER=|^RS485_ERR_PREV=|^RS485_ERR_PREV_LOADED=' "$SRC"
    grep -E '^declare -A RS485_ERR_PREV_FE' "$SRC"
} >> "$EX"
printf '\n' >> "$EX"
for fn in rs485_load_roster rs485_tty_in_use rs485_err_prev_load rs485_err_deltas rs485_port_json; do
    extract_fn "$fn"
done

for v in RS485_ROSTER_CACHE RS485_ROSTER_HELPER RS485_ERR_PREV; do
    grep -q "^${v}=" "$EX" || { echo "FAIL  global $v was not extracted from $SRC"; exit 1; }
done
grep -q 'RS485_ERR_PREV_FE' "$EX" || { echo "FAIL  the error-delta arrays were not extracted"; exit 1; }
grep -qE '^(build_rs485_array|apply|status_block_enabled)' "$EX" \
    && { echo "FAIL  the extraction is over-wide — it swallowed code past the RS-485 functions"; exit 1; }

# ── retarget every absolute root into the sandbox ──────────────────────────
sed -i \
    -e "s|/run/sa02m-rs485-roster.json|$CACHE|g" \
    -e "s|/opt/sa02m-rs485-roster/rs485_roster_cgi.py|$PWD/$HELPER|g" \
    -e "s|/usr/local/sbin/sa02m-rs485-stats.sh|$T/no-such-stats-helper|g" \
    -e "s|/usr/bin/sudo|$BIN/sudo|g" \
    -e "s|\"\${CACHE_DIR}/rs485_err_prev.state\"|\"$T/cache/rs485_err_prev.state\"|g" \
    -e "s|/dev/RS-485-|$DEV/RS-485-|g" \
    "$EX"
# The guards match the QUOTED absolute form, because a successful retarget
# leaves the old path as a suffix of the new sandbox path (the repo lives under
# .../opt/sa02m-rs485-roster/ too). A bare substring test would fail on a
# correct retarget — and a guard that cries wolf is a guard that gets deleted.
for abs in '="/run/sa02m-rs485-roster.json"' '="/opt/sa02m-rs485-roster/' \
           '-/usr/local/sbin/sa02m-rs485-stats.sh}' '/usr/bin/sudo' \
           '${CACHE_DIR}' '="/dev/RS-485-'; do
    grep -qF -- "$abs" "$EX" && { echo "FAIL  '$abs' was not retargeted — the harness would touch the host"; exit 1; }
done
grep -qF "RS485_ROSTER_CACHE=\"$CACHE\"" "$EX" \
    || { echo "FAIL  the roster cache was not retargeted into the sandbox"; exit 1; }
grep -q 'modules' "$EX" || { echo "FAIL  no 'modules' handling left in the extracted body (extraction broken)"; exit 1; }

cat > "$BIN/sudo" <<'SHIM'
#!/bin/bash
printf 'sudo %s\n' "$*" >> "$T_DIR/sudo.log"
exit 1
SHIM
chmod +x "$BIN/sudo"
PATH="$BIN:$PATH"; export PATH

# shellcheck source=www/network_config/cgi-bin/lib_web_json.sh
. "$JSONLIB"
# shellcheck disable=SC1090
. "$EX"

# Two present ports; RS-485-3 and RS-485-4 map to COM4 and COM5.
: > "$DEV/RS-485-3"
: > "$DEV/RS-485-4"

# ── the contract's own §1 fixture ──────────────────────────────────────────
cat > "$CACHE" <<'JSON'
{
  "ts": 1737000000.0,
  "ports": {
    "COM4": {
      "source": "bridge",
      "live": true,
      "ts": 1737000000.0,
      "ours": [ { "addr": 6, "model": "6AI6AO", "online": true, "source": "bridge" } ],
      "third_party": { "total": 3, "online": 2 }
    },
    "COM5": {
      "source": "scan",
      "live": false,
      "ts": 1736900000.0,
      "ours": [ { "addr": 3, "model": "4DO6DI", "online": null, "source": "scan" } ],
      "third_party": { "total": 0, "online": null }
    }
  }
}
JSON

jget() {  # $1 = port json, $2 = python expression over `d`
    printf '%s' "$1" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print($2)" 2>/dev/null
}

rs485_load_roster
J3=$(rs485_port_json 3)
J4=$(rs485_port_json 4)

# 1-2: the emitted port object is valid JSON at all.
if printf '%s' "$J3" | "$PY" -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
    ok "1  RS-485-3 emits valid JSON with the modules block attached"
else
    bad "1  RS-485-3 emitted invalid JSON: $J3"
fi
if printf '%s' "$J3" | LC_ALL=C grep -qP '[^\x00-\x7F]' 2>/dev/null; then
    bad "2  the emitted JSON is not pure ASCII — the helper's ensure_ascii guarantee is broken"
else
    ok "2  the emitted JSON is pure ASCII (stands in for json_escape on these values)"
fi

# 3-6: the block is present, mapped RS-485-N <-> COM(N+1), and honest.
[ "$(jget "$J3" 'int("modules" in d)')" = "1" ] \
    && ok "3  RS-485-3 carries the contracted \`modules\` field" \
    || bad "3  RS-485-3 has NO \`modules\` field — contract §2 broken: $J3"
[ "$(jget "$J3" 'd["modules"]["source"]')" = "bridge" ] \
    && ok "4  RS-485-3 <-> COM4 (the bridge port), not COM3" \
    || bad "4  port mapping is wrong: RS-485-3 got source $(jget "$J3" 'd["modules"]["source"]')"
[ "$(jget "$J4" 'd["modules"]["source"]')" = "scan" ] \
    && ok "5  RS-485-4 <-> COM5 (the scan port)" \
    || bad "5  port mapping is wrong: RS-485-4 got source $(jget "$J4" 'd["modules"]["source"]')"
[ "$(jget "$J3" 'd["modules"]["ours"][0]["model"]')" = "6AI6AO" ] \
    && ok "6  the module roster reaches the port object (addr/model)" \
    || bad "6  the module roster did not reach the port object: $J3"

# 7-8: the honesty clauses — a stale scan must never render live/green.
[ "$(jget "$J4" 'repr(d["modules"]["live"])')" = "False" ] \
    && ok "7  a scan-sourced port stays live:false" \
    || bad "7  a scan-sourced port claims live:true — a stale name would render as live"
[ "$(jget "$J4" 'repr(d["modules"]["ours"][0]["online"])')" = "None" ] \
    && ok "8  a scan-sourced module keeps online:null (not a false offline/online)" \
    || bad "8  a scan-sourced module lost its null online state"
[ "$(jget "$J4" 'repr(d["modules"]["third_party"]["online"])')" = "None" ] \
    && ok "9  third_party.online stays null where there is no live source" \
    || bad "9  third_party.online was coerced to a number without a live source"

# 10: VERBATIM — the consumer must embed what the helper produced, not its own
# reshaping of the cache. Compare against the helper run directly.
HELPER_OUT=$("$PY" "$HELPER" "$CACHE" | awk -F'\t' '$1=="3"{print $2}')
EMBEDDED=$(jget "$J3" 'json.dumps(d["modules"],ensure_ascii=True,separators=(",",":"))')
if [ -n "$HELPER_OUT" ] && [ "$HELPER_OUT" = "$EMBEDDED" ]; then
    ok "10 the block is the helper's output VERBATIM (no re-derivation in the CGI)"
else
    bad "10 the CGI reshaped the helper's block — cloud and UI would diverge (rs485-roster.md §3)
        helper:   $HELPER_OUT
        embedded: $EMBEDDED"
fi

# 11-13: legacy fields survive (deployed cached bundles read them).
for f in n dev st open tx rx fe pe oe fe_d pe_d oe_d; do
    [ "$(jget "$J3" "int('$f' in d)")" = "1" ] || bad "11 legacy field '$f' vanished from the port object"
done
ok "11 every legacy port field survives alongside \`modules\`"
[ "$(jget "$J3" 'd["n"]')" = "3" ] && ok "12 the port index is preserved" || bad "12 wrong port index in the object"
[ "$(jget "$J3" 'd["st"]')" = "present" ] && ok "13 a present port still reports st:present" || bad "13 st is not 'present'"

# 14-16: absent / unreadable / malformed cache -> `modules` OMITTED, not empty.
rm -f "$CACHE"
rs485_load_roster
JN=$(rs485_port_json 3)
[ "$(jget "$JN" 'int("modules" in d)')" = "0" ] \
    && ok "14 no cache -> \`modules\` is omitted (old bundles unaffected)" \
    || bad "14 a missing cache produced a \`modules\` field anyway: $JN"
printf 'not json at all' > "$CACHE"
rs485_load_roster
JB=$(rs485_port_json 3)
if printf '%s' "$JB" | "$PY" -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null \
   && [ "$(jget "$JB" 'int("modules" in d)')" = "0" ]; then
    ok "15 a corrupt cache -> valid JSON, \`modules\` omitted (fail closed, never broken JSON)"
else
    bad "15 a corrupt cache produced broken or module-bearing JSON: $JB"
fi
printf '{"ts":1,"ports":{}}' > "$CACHE"
rs485_load_roster
JE=$(rs485_port_json 3)
[ "$(jget "$JE" 'int("modules" in d)')" = "0" ] \
    && ok "16 an empty ports map -> \`modules\` omitted" \
    || bad "16 an empty ports map produced a \`modules\` field: $JE"

# 17: an absent device node keeps the legacy 'absent' shape and no modules.
JA=$(rs485_port_json 9)
if [ "$(jget "$JA" 'd["st"]')" = "absent" ] && [ "$(jget "$JA" 'int("modules" in d)')" = "0" ]; then
    ok "17 an absent port reports st:absent with no \`modules\`"
else
    bad "17 an absent port object is malformed: $JA"
fi

# ── non-vacuity ────────────────────────────────────────────────────────────
if [ -z "$J3" ] || [ -z "$J4" ]; then
    bad "18 the consumer produced no output at all — the run was vacuous"
else
    ok "18 the extracted consumer produced output for both fixture ports"
fi

# The contract doc must keep naming this row, so the next reader knows what to
# re-run for the CONSUMER half. A check nobody can find from the contract is how
# §2 went a year with producer-only coverage (audit C2). Mirrors case 36 of
# mqtt-set-contract (review Q2, 1.0.6.24).
CONTRACT=docs/contracts/rs485-roster.md
if [ -f "$CONTRACT" ] && grep -q 'rs485-roster-consumer' "$CONTRACT"; then
    ok "19 the contract doc names this row"
else
    bad "19 $CONTRACT does not name the \`rs485-roster-consumer\` row — the consumer-side check is undiscoverable from the contract it validates"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "PASS  rs485-roster consumer contract (§2) green"
    exit 0
fi
echo "FAIL  $fails case(s) failed"
exit 1
