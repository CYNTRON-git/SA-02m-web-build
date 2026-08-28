#!/usr/bin/env bash
# mqtt-set-contract — the validating check for docs/contracts/mqtt-set-endpoint.md.
#
# WHY THIS EXISTS. `www/network_config/cgi-bin/mqtt_set.cgi` is the ONLY web
# endpoint that switches a real relay output (MR-02m DO coils, AO setpoints, DTV
# coils). Until 1.0.6.24 the only automation reaching it was `bash -n` plus the
# CI lint row — so a change that dropped the CSRF gate, widened the control
# allow-list, or re-added the MQTT retain flag passed the whole build beat
# green. The contract's own "Валидирующая проверка" was a MANUAL recipe
# (mqtt-set-endpoint.md §Валидирующая проверка) — a recipe nobody runs is not a
# check, and the endpoint's own comment claimed a contract check that did not
# exist (2026-08-28 audit, finding C1).
#
# (A comment line here must never OPEN with the linter's own name: it is then
# parsed as a directive and the whole file fails to lint — SC1072/SC1073, hit
# while writing this file.)
#
# The retain flag is the sharpest of the floors: `mosquitto_pub -r` on a
# `/devices/<id>/controls/<x>/on` topic makes the broker REPLAY that write to
# every subscriber on the next bridge restart, so a board reboot re-fires every
# output that was ever set. That is a physical-world failure, not a data one.
#
# METHOD — behaviour first, source floors second.
#
#   Part A (behavioural, cases 1-31): the SHIPPED CGI is copied into a sandbox
#   together with the SHIPPED lib_web_auth.sh, its one absolute write path
#   (/var/log/sa02m_install.log) is retargeted into the sandbox, and it is then
#   driven as a real CGI: REQUEST_METHOD / HTTP_COOKIE / HTTP_X_SA02M_CSRF /
#   CONTENT_LENGTH in the environment, the form body on stdin. Sessions are
#   REAL — lib_web_auth.sh honours $SA02M_SESSION_DIR, so the harness mints a
#   genuine session + CSRF token through the shipped functions rather than
#   stubbing the auth decision it is trying to test. `mosquitto_pub` and
#   `timeout` are recording PATH shims: every publish attempt lands in a log
#   with its full argv, so "no publish happened" is asserted, never assumed.
#
#   Part B (source floors, cases 32-36): the invariants a single happy-path run
#   cannot observe — that the retain flag appears NOWHERE in the file, that the
#   broker host/port are constants, that the publish is bounded by a timeout,
#   that the auth → CSRF → allow-list → publish ORDER holds, and that the
#   contract doc still names this row (an unfindable check is how the endpoint's
#   own comment came to name one that did not exist). Read through lib_check.sh,
#   so a commented-out floor cannot satisfy a pin (the comment-blindness class).
#
# NON-VACUOUS: a failed or over-wide extraction, an un-retargeted absolute path,
# a shim that was never invoked, or a case table that ran zero publishes FAILS
# rather than passing on an empty sweep.
#
# Proven RED (1.0.6.24), nine mutations of a scratch copy of the CGI driven
# through MQTT_SET_SRC so the real endpoint is never touched:
#   * adding `-r` to the mosquitto_pub argv           -> cases 26 + 32 FAIL
#   * deleting the `web_csrf_validate` guard          -> cases 3-4 + 35 FAIL
#   * deleting the auth guard                         -> cases 1-2 FAIL
#   * dropping the device charset guard               -> cases 6, 7, 9 FAIL
#   * widening the control regex to accept do_17      -> case 10 FAIL
#   * dropping the do_ 0/1 value check                -> cases 15-17 FAIL
#   * dropping the ao_ upper bound (value=1001)       -> case 18 FAIL
#   * dropping `timeout 5`                            -> cases 28, 31, 33 FAIL
#   * pointing the publish at the external 1884       -> cases 27 + 34 FAIL
# and by the standing `comment-mutation-proof` row, which comments the
# `timeout 5 mosquitto_pub` line and the `web_csrf_validate` line out.
#
# Run: bash .ai-dev/quality/checks/mqtt-set-contract.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1

# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. "$ROOT/.ai-dev/quality/checks/lib_check.sh"

CGI_SRC=${MQTT_SET_SRC:-www/network_config/cgi-bin/mqtt_set.cgi}
AUTH_LIB=www/network_config/cgi-bin/lib_web_auth.sh

fails=0
ok()  { printf 'mqtt-set-contract: ok    %s\n' "$1"; }
bad() { printf 'mqtt-set-contract: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -f "$CGI_SRC" ]  || { echo "mqtt-set-contract: FAIL — endpoint not found: $CGI_SRC"; exit 1; }
[ -f "$AUTH_LIB" ] || { echo "mqtt-set-contract: FAIL — auth lib not found: $AUTH_LIB"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
export T_DIR="$T"
BIN="$T/bin"; CGIDIR="$T/cgi-bin"
mkdir -p "$BIN" "$CGIDIR" "$T/sessions"
AUDIT="$T/audit.log"
PUBLOG="$T/pub.log"
TMOLOG="$T/timeout.log"

# ── sandbox the endpoint ───────────────────────────────────────────────────
# The ONLY absolute path the endpoint writes is the audit log; retarget it so
# the harness never touches the host filesystem. Everything else it touches
# (the session store) is already env-directed by lib_web_auth.sh.
sed "s|/var/log/sa02m_install.log|$AUDIT|g" "$CGI_SRC" > "$CGIDIR/mqtt_set.cgi"
cp "$AUTH_LIB" "$CGIDIR/lib_web_auth.sh"
chmod +x "$CGIDIR/mqtt_set.cgi"

grep -q '/var/log/sa02m_install.log' "$CGIDIR/mqtt_set.cgi" \
    && { echo "mqtt-set-contract: FAIL — audit path not retargeted; the run would write to the host /var/log"; exit 1; }
grep -q 'mosquitto_pub' "$CGIDIR/mqtt_set.cgi" \
    || { echo "mqtt-set-contract: FAIL — no mosquitto_pub call left in the sandboxed copy (extraction broken)"; exit 1; }
grep -q "$AUDIT" "$CGIDIR/mqtt_set.cgi" \
    || { echo "mqtt-set-contract: FAIL — retarget produced no sandbox audit path"; exit 1; }

# ── PATH shims ─────────────────────────────────────────────────────────────
# `timeout` records the duration it was asked for and then runs the real command
# (minus the duration): the endpoint's `timeout 5 mosquitto_pub …` shape is
# asserted from the log, and the publish still reaches the mosquitto_pub shim.
# Shimming it rather than relying on the host's coreutils also makes the run
# identical on Windows git-bash and Linux CI.
cat > "$BIN/timeout" <<'SHIM'
#!/bin/bash
printf '%s\n' "$1" >> "$T_DIR/timeout.log"
shift
exec "$@"
SHIM
cat > "$BIN/mosquitto_pub" <<'SHIM'
#!/bin/bash
printf '%s\n' "$*" >> "$T_DIR/pub.log"
exit "${SHIM_PUB_RC:-0}"
SHIM
chmod +x "$BIN/timeout" "$BIN/mosquitto_pub"
PATH="$BIN:$PATH"; export PATH

# ── a REAL session + CSRF token, minted by the shipped auth lib ────────────
export SA02M_SESSION_DIR="$T/sessions"
# shellcheck source=www/network_config/cgi-bin/lib_web_auth.sh
. "$CGIDIR/lib_web_auth.sh"
TOKEN=$(web_session_create admin) || TOKEN=""
[ -n "$TOKEN" ] || { echo "mqtt-set-contract: FAIL — could not mint a session through the shipped lib"; exit 1; }
CSRF=$(web_csrf_token_for_session "$TOKEN") || CSRF=""
[ -n "$CSRF" ] || { echo "mqtt-set-contract: FAIL — could not mint a CSRF token through the shipped lib"; exit 1; }
GOOD_COOKIE="session_token=$TOKEN"
FORGED_COOKIE="session_token=$(printf 'd%.0s' $(seq 1 64))"

# ── driver ────────────────────────────────────────────────────────────────
# call <method> <cookie> <csrf-header> <body>  -> response on stdout,
# publish argv in $PUBLOG, timeout durations in $TMOLOG.
call() {
    : > "$PUBLOG"; : > "$TMOLOG"
    printf '%s' "$4" | env \
        REQUEST_METHOD="$1" \
        HTTP_COOKIE="$2" \
        HTTP_X_SA02M_CSRF="$3" \
        CONTENT_LENGTH="${#4}" \
        SA02M_SESSION_DIR="$SA02M_SESSION_DIR" \
        PATH="$PATH" \
        bash "$CGIDIR/mqtt_set.cgi" 2>/dev/null
}

pub_count() { [ -s "$PUBLOG" ] && grep -c . "$PUBLOG" || printf '0\n'; }

# expect_error <label> <expected error token> <method> <cookie> <csrf> <body>
expect_error() {
    local label="$1" want="$2" out n
    out=$(call "$3" "$4" "$5" "$6")
    n=$(pub_count)
    if [[ "$out" != *"\"error\":\"$want\""* ]]; then
        bad "$label — expected error \"$want\", got: $out"
    elif [ "$n" != "0" ]; then
        bad "$label — rejected but PUBLISHED anyway ($n publish(es)): $(cat "$PUBLOG")"
    else
        ok "$label -> $want, no publish"
    fi
}

# expect_publish <label> <device> <control> <value> <expected payload>
expect_publish() {
    local label="$1" dev="$2" ctl="$3" val="$4" want="$5" out n argv
    out=$(call POST "$GOOD_COOKIE" "$CSRF" "device=$dev&control=$ctl&value=$val")
    n=$(pub_count)
    argv=$(cat "$PUBLOG")
    if [[ "$out" != *'"ok":true'* ]]; then
        bad "$label — expected ok:true, got: $out"
        return
    fi
    if [ "$n" != "1" ]; then
        bad "$label — expected exactly ONE publish, got $n: $argv"
        return
    fi
    if [[ "$argv" != *"-t /devices/$dev/controls/$ctl/on"* ]]; then
        bad "$label — wrong topic in argv: $argv"
        return
    fi
    if [[ "$argv" != *"-m $want"* ]]; then
        bad "$label — expected payload '$want' in argv: $argv"
        return
    fi
    ok "$label -> one publish, topic + payload '$want' correct"
}

# ═══ Part A — behaviour ═══════════════════════════════════════════════════
# 1-4: the auth/CSRF/method gates, each proven to publish NOTHING.
expect_error "1  no session cookie"             unauthorized  POST ""              "$CSRF" "device=d1&control=do_3&value=1"
expect_error "2  forged (unknown) session"      unauthorized  POST "$FORGED_COOKIE" "$CSRF" "device=d1&control=do_3&value=1"
expect_error "3  valid session, CSRF header absent" csrf      POST "$GOOD_COOKIE"  ""      "device=d1&control=do_3&value=1"
expect_error "4  valid session, wrong CSRF"     csrf          POST "$GOOD_COOKIE"  "deadbeef" "device=d1&control=do_3&value=1"
expect_error "5  GET is refused"                post_required GET  "$GOOD_COOKIE"  "$CSRF" ""

# 6-9: device allow-list.
expect_error "6  device with a shell metachar"  bad_device POST "$GOOD_COOKIE" "$CSRF" "device=a;rm&control=do_3&value=1"
expect_error "7  device empty"                  bad_device POST "$GOOD_COOKIE" "$CSRF" "device=&control=do_3&value=1"
expect_error "8  device over the 64-char cap"   bad_device POST "$GOOD_COOKIE" "$CSRF" "device=$(printf 'a%.0s' $(seq 1 65))&control=do_3&value=1"
expect_error "9  device with a slash"           bad_device POST "$GOOD_COOKIE" "$CSRF" "device=a%2Fb&control=do_3&value=1"

# 10-14: control enum (closed).
expect_error "10 control do_17 (past DO range)" bad_control POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=do_17&value=1"
expect_error "11 control do_0"                  bad_control POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=do_0&value=1"
expect_error "12 control ao_13 (past AO range)" bad_control POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=ao_13&value=500"
expect_error "13 control do_1;x"                bad_control POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=do_1;x&value=1"
expect_error "14 control unknown word"          bad_control POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=relay&value=1"

# 15-19: value grammar per control class.
expect_error "15 do_1 value=2"                  bad_value POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=do_1&value=2"
expect_error "16 do_1 value=x"                  bad_value POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=do_1&value=x"
expect_error "17 buzzer value empty"            bad_value POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=buzzer&value="
expect_error "18 ao_1 value=1001 (over range)"  bad_value POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=ao_1&value=1001"
expect_error "19 ao_1 value=x"                  bad_value POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=ao_1&value=x"

# 20-24: the accepted vectors — exactly one publish each, correct topic/payload.
expect_publish "20 DO on"        "mr02m-COM1-5" do_3   1    1
expect_publish "21 DO off"       "mr02m-COM1-5" do_16  0    0
expect_publish "22 buzzer"       "sa02m-local"  buzzer 1    1
expect_publish "23 AO mid"       "mr02m-COM4-6" ao_1   500  500
expect_publish "24 AO bounds hi" "mr02m-COM4-6" ao_12  1000 1000
expect_publish "25 AO leading zeros normalised" "mr02m-COM4-6" ao_2 0500 500

# 26: the retain flag must not appear in ANY accepted publish's argv, and the
# broker coordinates must be the loopback constants.
argv=$(cat "$PUBLOG")
if [[ " $argv " == *" -r "* || " $argv " == *" --retain "* ]]; then
    bad "26 retain flag present in the publish argv: $argv"
else
    ok "26 no retain flag in the publish argv"
fi
if [[ "$argv" == *"-h 127.0.0.1"* && "$argv" == *"-p 1883"* ]]; then
    ok "27 publish goes to the loopback listener 127.0.0.1:1883"
else
    bad "27 publish is not addressed to 127.0.0.1:1883: $argv"
fi

# 28: the publish is bounded by `timeout 5`.
if [ "$(head -1 "$TMOLOG" 2>/dev/null)" = "5" ]; then
    ok "28 publish is wrapped in timeout 5"
else
    bad "28 publish was not wrapped in \`timeout 5\` (timeout log: $(cat "$TMOLOG" 2>/dev/null))"
fi

# 29: a broker failure fails CLOSED — never a false ok:true.
out=$(SHIM_PUB_RC=1 call POST "$GOOD_COOKIE" "$CSRF" "device=d1&control=do_1&value=1")
if [[ "$out" == *'"error":"publish_failed"'* && "$out" != *'"ok":true'* ]]; then
    ok "29 broker failure -> publish_failed, never a false ok"
else
    bad "29 broker failure did not fail closed: $out"
fi

# 30: every accepted mutation writes its audit line.
if [ -s "$AUDIT" ] && grep -q 'mqtt_set.cgi: device=' "$AUDIT"; then
    ok "30 accepted mutations write the audit line"
else
    bad "30 no audit line written for the accepted mutations (contract §Действие)"
fi

# Non-vacuity of Part A: the shims must actually have been exercised.
if [ -s "$TMOLOG" ]; then
    ok "31 the timeout/mosquitto_pub shims were exercised (Part A is not vacuous)"
else
    bad "31 no publish was ever attempted — Part A never reached the broker call"
fi

# ═══ Part B — source floors ═══════════════════════════════════════════════
# Read comment-stripped: a floor that has been commented out must not satisfy
# its pin (1.0.6.24 comment-blindness class, lib_check.sh).
SRC_TEXT=$(stripped_text "$CGI_SRC")
[ -n "$SRC_TEXT" ] || { echo "mqtt-set-contract: FAIL — the endpoint reads back empty after comment stripping"; exit 1; }

if text_matches "$SRC_TEXT" 'mosquitto_pub.*(-r|--retain)' || text_matches "$SRC_TEXT" '(-r|--retain).*mosquitto_pub'; then
    bad "32 the retain flag appears in the endpoint source — a retained /on re-fires every output on the next bridge restart"
else
    ok "32 no retain flag anywhere in the endpoint source"
fi

if text_matches "$SRC_TEXT" 'timeout[[:space:]]+[0-9]+[[:space:]]+mosquitto_pub'; then
    ok "33 the publish is bounded by a timeout in source"
else
    bad "33 mosquitto_pub is not wrapped in \`timeout N\` — a wedged broker blocks the endpoint"
fi

if text_has "$SRC_TEXT" '-h 127.0.0.1' && text_has "$SRC_TEXT" '-p 1883'; then
    ok "34 broker host/port are the loopback constants, not request inputs"
else
    bad "34 broker host/port are not the pinned loopback constants (contract §Действие)"
fi

# Ordering: auth, then CSRF, then the allow-lists, then the publish.
ln_auth=$(stripped_first_line "$CGI_SRC" 'web_session_check_cookie')
ln_csrf=$(stripped_first_line "$CGI_SRC" 'web_csrf_validate')
ln_allow=$(stripped_first_line "$CGI_SRC" 'bad_control')
ln_pub=$(stripped_first_line "$CGI_SRC" 'mosquitto_pub')
if [ -n "$ln_auth" ] && [ -n "$ln_csrf" ] && [ -n "$ln_allow" ] && [ -n "$ln_pub" ]; then
    if [ "$ln_auth" -lt "$ln_csrf" ] && [ "$ln_csrf" -lt "$ln_allow" ] && [ "$ln_allow" -lt "$ln_pub" ]; then
        ok "35 order holds: auth($ln_auth) -> CSRF($ln_csrf) -> allow-list($ln_allow) -> publish($ln_pub)"
    else
        bad "35 the auth->CSRF->allow-list->publish order is broken: auth=$ln_auth csrf=$ln_csrf allow=$ln_allow publish=$ln_pub"
    fi
else
    bad "35 one of the pinned stages is absent from the endpoint (auth=$ln_auth csrf=$ln_csrf allow=$ln_allow publish=$ln_pub)"
fi

# The contract doc must keep naming this row, so the next reader knows what to
# re-run. (Doc half of the contract, mqtt-set-endpoint.md §Проверка контракта.)
CONTRACT=docs/contracts/mqtt-set-endpoint.md
if [ -f "$CONTRACT" ] && grep -q 'mqtt-set-contract' "$CONTRACT"; then
    ok "36 the contract doc names this row"
else
    bad "36 $CONTRACT does not name the \`mqtt-set-contract\` row — the check is undiscoverable from the contract"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "mqtt-set-contract: ALL OK — 36 case(s) green (behaviour + source floors)"
    exit 0
fi
echo "mqtt-set-contract: $fails FAILURE(S)"
exit 1
