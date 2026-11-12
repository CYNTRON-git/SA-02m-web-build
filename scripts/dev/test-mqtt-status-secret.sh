#!/usr/bin/env bash
# mqtt-status-secret — the MQTT tab's polled status never carries the external
# broker password; it is revealed only by an explicit POST with session + CSRF.
#
# WHY THIS EXISTS (audit 2026-11-11 A5-05). `mqtt_status.cgi` is polled by the
# MQTT tab and returned `mqtt_password` (the `mqttuser` password of the external
# listener 1884) in EVERY answer — in every poll response, every browser cache
# and proxy log, read through two `sudo` calls per poll. The password is now
# out of the GET and reachable only by `POST {"reveal":"password"}` behind the
# session AND the CSRF check, answered `Cache-Control: no-store`.
#
# METHOD — behavioural, the shipped endpoint in a sandbox (the
# mqtt-config-transport idiom): the SHIPPED mqtt_status.cgi beside the SHIPPED
# lib_web_auth.sh, driven as a real CGI (REQUEST_METHOD / HTTP_COOKIE /
# HTTP_X_SA02M_CSRF / CONTENT_LENGTH, body on stdin). The session and CSRF token
# are REAL, minted through the shipped auth lib. `sudo` is a PATH shim that
# answers `cat /etc/sa02m_mqtt.env` with MQTT_PASS=<the secret> and logs every
# call; `systemctl`, `ss`, `pgrep`, `mosquitto_sub`, `ip`, `hostname` are inert
# shims so nothing reaches the host.
#
# Cases:
#   1  GET with a session: valid JSON, no `mqtt_password` key, the secret
#      appears nowhere in the body, and no `sudo` call was made;
#   2  POST reveal without the CSRF header: E_CSRF, no secret, no sudo;
#   3  POST reveal with session + CSRF: ok:true, mqtt_password = the secret,
#      `Cache-Control: no-store`;
#   4  POST with another body: ok:false bad_request, no secret;
#   5  no session (GET and POST): unauthorized, no secret.
# Non-vacuous: a missing endpoint/lib, a session the lib refuses to mint, or a
# sudo shim never invoked on case 3 FAILS.
#
# RED on the pre-fix endpoint (MQTT_STATUS_CGI_SRC=<git show 42418b57:…>):
# case 1 (the poll carried the password). Commenting out the endpoint's
# `web_csrf_require` turns case 2 RED (the comment-mutation case).
#
# Run: bash scripts/dev/test-mqtt-status-secret.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

CGI_SRC=${MQTT_STATUS_CGI_SRC:-www/network_config/cgi-bin/mqtt_status.cgi}
AUTH_LIB=www/network_config/cgi-bin/lib_web_auth.sh
SECRET='s3cret-Q9x'

fails=0
ok()  { printf 'mqtt-status-secret: ok    %s\n' "$1"; }
bad() { printf 'mqtt-status-secret: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

for f in "$CGI_SRC" "$AUTH_LIB"; do
    [ -f "$f" ] || { echo "mqtt-status-secret: FAIL - not found: $f"; exit 1; }
done
PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import json' >/dev/null 2>&1; then PY=$p; break; fi
done
[ -n "$PY" ] || { echo "mqtt-status-secret: FAIL - no python3; the endpoint needs it too"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; CGIDIR="$T/cgi-bin"; SUDOLOG="$T/sudo.log"
mkdir -p "$BIN" "$CGIDIR" "$T/sessions"
: > "$SUDOLOG"
cp "$CGI_SRC" "$CGIDIR/mqtt_status.cgi"
cp "$AUTH_LIB" "$CGIDIR/lib_web_auth.sh"
chmod +x "$CGIDIR/mqtt_status.cgi"

cat > "$BIN/sudo" <<SHIM
#!/bin/bash
printf '%s\n' "\$*" >> "$SUDOLOG"
case "\$*" in
  *"cat /etc/sa02m_mqtt.env"*) printf 'MQTT_USER=mqttuser\nMQTT_PASS=$SECRET\n'; exit 0 ;;
esac
exit 1
SHIM
for tool in systemctl ss pgrep mosquitto_sub ip hostname getconf; do
    printf '#!/bin/bash\nexit 1\n' > "$BIN/$tool"
done
chmod +x "$BIN"/*
if [ "$PY" != python3 ]; then
    printf '#!/bin/bash\nexec %s "$@"\n' "$(command -v "$PY")" > "$BIN/python3"
    chmod +x "$BIN/python3"
fi
PATH="$BIN:$PATH"; export PATH

export SA02M_SESSION_DIR="$T/sessions"
# shellcheck source=www/network_config/cgi-bin/lib_web_auth.sh
. "$CGIDIR/lib_web_auth.sh"
TOKEN=$(web_session_create admin) || TOKEN=""
[ -n "$TOKEN" ] || { echo "mqtt-status-secret: FAIL - could not mint a session through the shipped lib"; exit 1; }
CSRF=$(web_csrf_token_for_session "$TOKEN") || CSRF=""
[ -n "$CSRF" ] || { echo "mqtt-status-secret: FAIL - could not mint a CSRF token through the shipped lib"; exit 1; }
COOKIE="session_token=$TOKEN"

# run METHOD COOKIE CSRF BODY -> $OUT (headers + body), $BODY (body only)
run() {
    local method=$1 cookie=$2 csrf=$3 body=$4
    : > "$SUDOLOG"
    OUT=$(printf '%s' "$body" | env -i PATH="$PATH" SA02M_SESSION_DIR="$SA02M_SESSION_DIR" \
        REQUEST_METHOD="$method" HTTP_COOKIE="$cookie" HTTP_X_SA02M_CSRF="$csrf" \
        CONTENT_LENGTH="${#body}" CONTENT_TYPE=application/json \
        bash "$CGIDIR/mqtt_status.cgi" 2>"$T/err")
    BODY=$(printf '%s\n' "$OUT" | tr -d '\r' | awk 'f{print} /^$/{f=1}')
}
field() { printf '%s' "$BODY" | "$PY" -c 'import json,sys
d=json.loads(sys.stdin.read() or "null")
v=d.get(sys.argv[1]) if isinstance(d,dict) else None
print("" if v is None else v)' "$1" 2>/dev/null; }
has_key() { printf '%s' "$BODY" | "$PY" -c 'import json,sys; sys.exit(0 if sys.argv[1] in json.loads(sys.stdin.read()) else 1)' "$1" 2>/dev/null; }

# 1 — the poll
run GET "$COOKIE" "" ""
if ! printf '%s' "$BODY" | "$PY" -c 'import json,sys; json.loads(sys.stdin.read())' 2>/dev/null; then
    bad "1 GET body is not JSON: $(printf '%s' "$BODY" | head -c 200)"
elif has_key mqtt_password; then
    bad "1 GET carries the key mqtt_password"
elif printf '%s' "$OUT" | grep -qF "$SECRET"; then
    bad "1 GET carries the password value"
elif [ -s "$SUDOLOG" ]; then
    bad "1 GET still runs sudo: $(tr '\n' '|' < "$SUDOLOG")"
else
    ok "1 GET: JSON without mqtt_password, no secret, no sudo (user=$(field mqtt_user))"
fi

REVEAL='{"reveal":"password"}'
# 2 — no CSRF
run POST "$COOKIE" "" "$REVEAL"
if [ "$(field error_code)" = E_CSRF ] && ! printf '%s' "$OUT" | grep -qF "$SECRET" && [ ! -s "$SUDOLOG" ]; then
    ok "2 POST without CSRF: E_CSRF, no secret, no sudo"
else
    bad "2 POST without CSRF answered: $(printf '%s' "$BODY" | head -c 200) / sudo: $(tr '\n' '|' < "$SUDOLOG")"
fi

# 3 — session + CSRF
run POST "$COOKIE" "$CSRF" "$REVEAL"
if [ "$(field ok)" = True ] && [ "$(field mqtt_password)" = "$SECRET" ] \
        && printf '%s\n' "$OUT" | tr -d '\r' | grep -qx 'Cache-Control: no-store' \
        && grep -q 'cat /etc/sa02m_mqtt.env' "$SUDOLOG"; then
    ok "3 POST reveal with session + CSRF: the password, no-store"
else
    bad "3 POST reveal answered: $(printf '%s' "$BODY" | head -c 200) / sudo: $(tr '\n' '|' < "$SUDOLOG")"
fi

# 4 — another body
run POST "$COOKIE" "$CSRF" '{"reveal":"everything"}'
if [ "$(field error)" = bad_request ] && ! printf '%s' "$OUT" | grep -qF "$SECRET"; then
    ok "4 POST with another body: bad_request, no secret"
else
    bad "4 POST other body answered: $(printf '%s' "$BODY" | head -c 200)"
fi

# 5 — no session
for m in GET POST; do
    # The minted token with its last character changed: unknown to the store.
    run "$m" "session_token=${TOKEN%?}$([ "${TOKEN: -1}" = 0 ] && echo 1 || echo 0)" "$CSRF" "$REVEAL"
    if [ "$(field error)" = unauthorized ] && ! printf '%s' "$OUT" | grep -qF "$SECRET"; then
        ok "5 $m without a session: unauthorized"
    else
        bad "5 $m without a session answered: $(printf '%s' "$BODY" | head -c 200)"
    fi
done

if [ "$fails" -eq 0 ]; then
    echo "mqtt-status-secret: ALL OK"
    exit 0
fi
echo "mqtt-status-secret: $fails FAILURE(S)"
exit 1
