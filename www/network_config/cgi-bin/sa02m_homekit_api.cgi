#!/bin/bash
# SA-02m Apple HomeKit card API (session auth → CSRF → stdlib Python dispatch
# → pinned privileged nudge). Contract: docs/contracts/homekit-bridge.md.
#
#   GET                                  → bridge status (never mutates)
#   POST {"action":"status"}             → bridge status
#   POST {"action":"setup"}              → setup code + QR (only while running ∧ unpaired)
#   POST {"action":"enable"|"disable"|"reset_pairing"}
#   POST {"action":"set_interface","interface":"eth0|eth1"}
#   POST {"action":"set_port","port":<int>}
#
# The action allow-list and every value allow-list live in the dispatch
# (opt/sa02m-homekit/sa02m_homekit/api.py: unknown ⇒ not_found, a modem or
# wildcard interface ⇒ invalid_interface, a board-owned port ⇒ invalid_port).
# The dispatch runs under the SYSTEM python3 with the package on PYTHONPATH and
# imports nothing from the venv. It prints two lines: the response JSON and the
# privileged verb to nudge (or an empty line); the verb reaches sudo only
# through the `case` below, which names exactly the four verbs the sudoers pin
# grants (etc/sudoers.d/sa02m-homekit).
#
# The setup code is served only on POST (token-checked, never a cacheable GET)
# and every response is no-store.
#
# Budget: dispatch 8 s + nudge 11 s < nginx /cgi-bin/ fastcgi_read_timeout (20 s).

set -u

# shellcheck source=lib_web_auth.sh
. "$(dirname "$0")/lib_web_auth.sh"

printf 'Content-Type: application/json; charset=UTF-8\r\n'
printf 'Cache-Control: no-store\r\n'
printf 'X-Content-Type-Options: nosniff\r\n\r\n'

if ! web_session_check_cookie; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

METHOD="${REQUEST_METHOD:-GET}"
case "$METHOD" in
    GET|POST) ;;
    *)
        echo '{"ok":false,"error":"method_not_allowed"}'
        exit 0
        ;;
esac

# CSRF BEFORE any mutation: every POST (including `setup`, which reveals the
# pairing code) carries X-SA02M-CSRF. Headers are already on the wire, so the
# inline form + the shared error body (docs/decisions/selective-csrf-policy.md).
if [ "$METHOD" = "POST" ]; then
    if ! web_csrf_validate; then
        web_csrf_error_body
        exit 0
    fi
fi

BODY=""
if [ "$METHOD" = "POST" ]; then
    # Bounded read — never hang fcgiwrap on a short body.
    CL="${CONTENT_LENGTH:-0}"
    case "$CL" in
        ''|*[!0-9]*) CL=0 ;;
    esac
    if [ "${#CL}" -gt 6 ] || [ "$CL" -gt 16384 ]; then
        echo '{"ok":false,"error":"payload_too_large"}'
        exit 0
    fi
    if [ "$CL" -gt 0 ]; then
        BODY=$(timeout 5 head -c "$CL" 2>/dev/null || true)
    fi
fi

HK_ROOT="${SA02M_HOMEKIT_ROOT:-/opt/sa02m-homekit}"
# Dev/checkout fallback when the package is not installed.
if [ ! -f "$HK_ROOT/sa02m_homekit/api.py" ]; then
    _here="$(cd "$(dirname "$0")/../../.." && pwd)"
    if [ -f "$_here/opt/sa02m-homekit/sa02m_homekit/api.py" ]; then
        HK_ROOT="$_here/opt/sa02m-homekit"
    fi
fi

# A board updated by the web path alone may carry this CGI without the package
# (the bridge needs install.sh — D9): answer the card honestly, run nothing.
if [ ! -f "$HK_ROOT/sa02m_homekit/api.py" ]; then
    if [ "$METHOD" = "GET" ]; then
        echo '{"ok":true,"state":"not_installed","reason":"","message":"HomeKit module is not installed (install.sh --with-homekit)","enabled":false,"setup_available":false}'
    else
        echo '{"ok":false,"error":"not_installed","state":"not_installed"}'
    fi
    exit 0
fi

OUT=$(printf '%s' "$BODY" | PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HK_ROOT" timeout 8 python3 -m sa02m_homekit.api "$METHOD") || OUT=""
RESULT="" VERB=""
{ IFS= read -r RESULT || true; IFS= read -r VERB || true; } <<<"$OUT"
case "$RESULT" in
    '{"'*'}') ;;
    *)
        echo '{"ok":false,"error":"homekit_api_failed","message":"python dispatch failed or timed out"}'
        exit 0
        ;;
esac

# The privileged nudge: only on POST, only the four pinned verbs. The outcome
# is appended to the response as a fixed enum (+ the helper's own error code,
# admitted only as [a-z_]) — no helper text is ever echoed through.
TRIGGER=none
TRIGGER_ERROR=""
if [ "$METHOD" = "POST" ]; then
    case "$VERB" in
        enable|disable|restart|reset-pairing)
            TRIG_RC=0
            TRIG_OUT=$(timeout 11 sudo -n /usr/local/sbin/sa02m-homekit-web-trigger.sh "$VERB" 2>/dev/null) || TRIG_RC=$?
            case "$TRIG_RC" in
                0)   TRIGGER=ok ;;
                124) TRIGGER=timeout ;;
                *)   TRIGGER=failed ;;
            esac
            if [[ $TRIG_OUT =~ \"error\":\"([a-z_]{1,32})\" ]]; then
                TRIGGER_ERROR=${BASH_REMATCH[1]}
            fi
            ;;
    esac
fi

if [ "$TRIGGER" = none ]; then
    printf '%s\n' "$RESULT"
elif [ -n "$TRIGGER_ERROR" ]; then
    printf '%s,"trigger":"%s","trigger_error":"%s"}\n' "${RESULT%\}}" "$TRIGGER" "$TRIGGER_ERROR"
else
    printf '%s,"trigger":"%s"}\n' "${RESULT%\}}" "$TRIGGER"
fi
