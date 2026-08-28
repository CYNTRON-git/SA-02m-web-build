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
#   * NO predictable `cyntron_mqtt_` / timestamp-derived fallback password.
# NON-VACUOUS: 05-mqtt.sh must exist AND still carry the mqttuser mint block
# (mosquitto_passwd + a /dev/urandom password), so the gate cannot pass because the
# whole feature was deleted.
#
# comment-mutation-proof-exempt: every pin here is fail-IF-PRESENT (assert the
# secret is NOT logged and the weak fallback is ABSENT); a comment-out REMOVES the
# banned line, which is the safe direction and cannot defeat the pin. The opposite
# mutation — re-introducing `log ... $MQTT_PASS` or the timestamp fallback — is what
# turns it RED, and that is exactly the pre-fix tree it was proven against.
#
# PROVEN RED (1.0.6.24): on the pre-fix 05-mqtt.sh (`log OK "...Пароль: $MQTT_PASS"`
# and the `cyntron_mqtt_$(date +%s)` fallback) both pins FAIL; after the fix -> ok.
#
# Run: bash .ai-dev/quality/checks/mqtt-install-secret.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh

F=scripts/05-mqtt.sh
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
