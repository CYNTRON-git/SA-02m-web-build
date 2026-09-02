#!/usr/bin/env bash
# mqtt-install-secret — the MQTT broker password minted by scripts/05-mqtt.sh must
# never be written to /var/log/sa02m_install.log, and the generated secret must be
# unpredictable or absent. That log is served VERBATIM by the web panel
# (log.cgi / log_export.cgi) to any authenticated session, while the same secret is
# deliberately 0600 in /etc/sa02m_mqtt.env — so `log ... $MQTT_PASS` leaks a
# process-control credential (:1884, `readwrite /devices/#`) through a one-click
# export (audit 2026-08-28, M2). The former urandom fallback
# `|| echo "cyntron_mqtt_$(date +%s)"` was an offline-guessable, timestamp-derived
# secret (L4); the fix fails closed instead.
#
# WHAT IT PINS (all comment-stripped via lib_check.sh):
#   * NO `log` command interpolates $MQTT_PASS — the secret's value never reaches
#     the served install log (log the fact, not the value).
#   * NO OTHER sink either. The `log` pin alone enumerated ONE of several ways to
#     reach that file: a bare `echo "pass=$MQTT_PASS" >> /var/log/sa02m_install.log`
#     left the gate ALL OK (review Q5, 1.0.6.24 — shape (b), incomplete
#     enumeration, in a gate whose header claimed "never written to"). So the pin
#     is now an ALLOW-LIST over every comment-stripped line naming MQTT_PASS: only
#     the four sanctioned shapes are permitted (the urandom assignment, the length
#     check, the `mosquitto_passwd` mint, the 0600 /etc/sa02m_mqtt.env write). Any
#     other line touching the secret FAILS and is printed verbatim — a redirect, a
#     `tee`, a `logger`, or a helper this gate has never heard of.
#   * NO predictable `cyntron_mqtt_` / timestamp-derived fallback password.
# NON-VACUOUS: 05-mqtt.sh must exist AND still carry the mqttuser mint block
# (mosquitto_passwd + a /dev/urandom password), so the gate cannot pass because the
# whole feature was deleted; and the allow-list sweep must still SEE the secret
# (>=3 lines naming MQTT_PASS), so a sweep that stopped matching FAILS rather than
# passing on zero lines.
#
# Re-run the leak proof against a sandbox copy without touching the repo:
#   MQTT_INSTALL_SRC=/tmp/05-mqtt.sh bash .ai-dev/quality/checks/mqtt-install-secret.sh
#
# NOT exempt from comment-mutation-proof, and the reason it used to give was
# wrong: only the two LEAK pins are fail-IF-PRESENT. The non-vacuity pin is a
# PRESENCE pin — commenting the `mosquitto_passwd` mint block out turns this gate
# RED, which is a registered case in comment-mutation-proof rather than a claim
# (review Q8, 1.0.6.24). The opposite mutation — re-introducing
# `log ... $MQTT_PASS` or the timestamp fallback — is what turns the leak pins
# RED, and that is exactly the pre-fix tree they were proven against.
#
# PROVEN RED (1.0.6.24): on the pre-fix 05-mqtt.sh (`log OK "...Пароль: $MQTT_PASS"`
# and the `cyntron_mqtt_$(date +%s)` fallback) both pins FAIL; after the fix -> ok.
#
# Run: bash .ai-dev/quality/checks/mqtt-install-secret.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh

F=${MQTT_INSTALL_SRC:-scripts/05-mqtt.sh}
fails=0
ok()  { printf 'mqtt-install-secret: ok    %s\n' "$*"; }
bad() { printf 'mqtt-install-secret: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

[ -f "$F" ] || { echo "mqtt-install-secret: FAIL — $F absent"; exit 1; }

# Non-vacuity: the mqttuser mint block must still be here.
if stripped_matches "$F" 'mosquitto_passwd' && stripped_matches "$F" 'MQTT_PASS=\$\(tr'; then
    ok "the mqttuser mint block is present (non-vacuous)"
else
    bad "the mqttuser mint block (mosquitto_passwd + urandom MQTT_PASS) is gone — this gate is checking a file that lost the feature"
fi

# The secret's value must never be logged (log the fact, not $MQTT_PASS).
if stripped_matches "$F" '^[[:space:]]*log[[:space:]].*MQTT_PASS'; then
    bad "a 'log' command interpolates \$MQTT_PASS — the MQTT password leaks into the web-served install log (M2)"
else
    ok "no 'log' line carries \$MQTT_PASS — the secret's value is not written to the install log"
fi

# ...and no OTHER sink. Allow-list every comment-stripped line that names the
# secret: anything outside the four sanctioned shapes is a candidate leak and is
# printed verbatim rather than guessed at. This is what makes the header's
# "never written to the served log" true of more than the `log` helper.
SANCTIONED='^[[:space:]]*MQTT_PASS=\$\(tr|\$\{#MQTT_PASS\}|mosquitto_passwd|/etc/sa02m_mqtt\.env'
n_secret=0; n_unsanctioned=0
while IFS= read -r ln; do
    [ -n "$ln" ] || continue
    n_secret=$((n_secret + 1))
    if ! grep -qE "$SANCTIONED" <<<"$ln"; then
        n_unsanctioned=$((n_unsanctioned + 1))
        bad "an unsanctioned line handles \$MQTT_PASS — every use of the secret must be the mint, the length check or the 0600 env write, or it is a candidate leak into a served file: $ln"
    fi
done < <(stripped_text "$F" | grep -E 'MQTT_PASS' || true)

if [ "$n_secret" -lt 3 ]; then
    bad "only $n_secret comment-stripped line(s) name MQTT_PASS — the sweep is broken or the mint was gutted (expected >=3); a leak pin that sees nothing proves nothing"
elif [ "$n_unsanctioned" -eq 0 ]; then
    ok "all $n_secret line(s) touching \$MQTT_PASS are the sanctioned mint / length-check / 0600 env write — no other sink can reach the served log"
fi

# No predictable, timestamp-derived fallback password.
if stripped_matches "$F" 'cyntron_mqtt_'; then
    bad "the predictable 'cyntron_mqtt_<timestamp>' fallback password is present — an offline-guessable secret (L4)"
else
    ok "no timestamp-derived fallback password"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "mqtt-install-secret: ALL OK"
    exit 0
fi
echo "mqtt-install-secret: $fails FAILURE(S)"
exit 1
