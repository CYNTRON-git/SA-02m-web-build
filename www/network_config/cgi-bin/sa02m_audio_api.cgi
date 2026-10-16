#!/bin/bash
# SA-02m «Запись звука» card API (session auth → CSRF on POST → stdlib Python
# dispatch → a pinned follow-up). Contract: docs/contracts/audio-recorder.md §15.
#
#   GET                                     → recorder status, clips, events, conf
#   GET ?action=download&id=<id>[&inline=1] → the WAV itself (audio/wav, ≤ 2 MB)
#   POST {"action":"status"}                → recorder status
#   POST {"action":"record","seconds":N} | {"action":"stop"}
#        {"action":"delete","id":"<id>"|"all"}
#        {"action":"save", …conf keys…, "ack_removable":true}
#        {"action":"enable"|"disable"|"restart"}
#
# The action allow-list and every value allow-list live in the dispatch
# (opt/sa02m-audio/sa02m_audio/api.py). It runs under the SYSTEM python3 with
# the package on PYTHONPATH (stdlib only) and prints two lines: the response
# JSON and the follow-up. The follow-up reaches mosquitto_pub or sudo only
# through the `case` below, which re-checks it against the fixed grammar:
# exactly two command topics on the LOCAL broker, never retained, and exactly
# the three verbs the sudoers pin grants (etc/sudoers.d/sa02m-audio).
# A download is written by the dispatch itself (headers + bytes): the bytes
# never pass through a shell variable.
#
# Every panel mutation leaves one journal line (logger tag sa02m-audio-panel,
# source=panel): MQTT carries no sender, so this is the only place a panel
# action is attributable.
#
# Budget: dispatch 8 s + publish 5 s / nudge 11 s < nginx /cgi-bin/
# fastcgi_read_timeout (20 s).

set -u

# shellcheck source=lib_web_auth.sh
. "$(dirname "$0")/lib_web_auth.sh"

json_headers() {
    printf 'Content-Type: application/json; charset=UTF-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    printf 'X-Content-Type-Options: nosniff\r\n\r\n'
}

if ! web_session_check_cookie; then
    json_headers
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

METHOD="${REQUEST_METHOD:-GET}"
case "$METHOD" in
    GET|POST) ;;
    *)
        json_headers
        echo '{"ok":false,"error":"method_not_allowed"}'
        exit 0
        ;;
esac

AU_ROOT="${SA02M_AUDIO_ROOT:-/opt/sa02m-audio}"
# Dev/checkout fallback when the package is not installed.
if [ ! -f "$AU_ROOT/sa02m_audio/api.py" ]; then
    _here="$(cd "$(dirname "$0")/../../.." && pwd)"
    if [ -f "$_here/opt/sa02m-audio/sa02m_audio/api.py" ]; then
        AU_ROOT="$_here/opt/sa02m-audio"
    fi
fi

# A board updated by the web path alone may carry this CGI without the package:
# answer the card honestly, run nothing.
if [ ! -f "$AU_ROOT/sa02m_audio/api.py" ]; then
    json_headers
    if [ "$METHOD" = "GET" ]; then
        echo '{"ok":true,"state":"not_installed","installed":false,"enabled":false,"live":false}'
    else
        echo '{"ok":false,"error":"not_installed","state":"not_installed"}'
    fi
    exit 0
fi

# Download: read-only, so GET without the token (selective CSRF policy). The
# id and the medium are judged by the dispatch, which writes the response.
if [ "$METHOD" = "GET" ]; then
    case "${QUERY_STRING:-}" in
        action=download\&*)
            # Pass the query through unchanged. The dispatch answers
            # bad_request when it is longer than 512 bytes (contract §15);
            # truncating it here would still serve the clip.
            PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$AU_ROOT" \
                timeout 10 python3 -m sa02m_audio.api DOWNLOAD || true
            exit 0
            ;;
    esac
fi

json_headers

# CSRF BEFORE any mutation: every POST carries X-SA02M-CSRF. Headers are
# already on the wire, so the inline form + the shared error body
# (docs/decisions/selective-csrf-policy.md).
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

OUT=$(printf '%s' "$BODY" | PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$AU_ROOT" timeout 8 python3 -m sa02m_audio.api "$METHOD") || OUT=""
RESULT="" FOLLOW=""
{ IFS= read -r RESULT || true; IFS= read -r FOLLOW || true; } <<<"$OUT"
case "$RESULT" in
    '{"'*'}') ;;
    *)
        echo '{"ok":false,"error":"audio_api_failed","message":"python dispatch failed or timed out"}'
        exit 0
        ;;
esac

au_audit() {
    logger -t sa02m-audio-panel -- "source=panel $*" 2>/dev/null || true
}

ID_RE='^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$'
DONE=none
DONE_ERROR=""
if [ "$METHOD" = "POST" ]; then
    TOPIC="" PAYLOAD="" VERB=""
    case "$FOLLOW" in
        "pub record stop")
            TOPIC=/devices/sa02m-audio/controls/record/on; PAYLOAD=stop ;;
        "pub record "*)
            PAYLOAD=${FOLLOW#pub record }
            # Closed grammar is 2…30 (contract §15). 1 is the daemon's
            # default-duration word and must not be published from the panel.
            [[ $PAYLOAD =~ ^(30|[12][0-9]|[2-9])$ ]] && TOPIC=/devices/sa02m-audio/controls/record/on ;;
        "pub delete "*)
            PAYLOAD=${FOLLOW#pub delete }
            if [ "$PAYLOAD" = all ] || [[ $PAYLOAD =~ $ID_RE ]]; then
                TOPIC=/devices/sa02m-audio/controls/delete/on
            fi ;;
        "sudo enable"|"sudo disable"|"sudo restart")
            VERB=${FOLLOW#sudo } ;;
    esac
    if [ -n "$TOPIC" ]; then
        # Local broker, QoS 1, NEVER retained: a retained command would
        # re-record on every daemon start (the daemon also refuses one).
        if timeout 5 mosquitto_pub -h 127.0.0.1 -p 1883 -q 1 -t "$TOPIC" -m "$PAYLOAD" >/dev/null 2>&1; then
            DONE=ok
        else
            DONE=failed
            DONE_ERROR=publish_failed
        fi
        au_audit "publish ${TOPIC##*/controls/}=$PAYLOAD result=$DONE"
    elif [ -n "$VERB" ]; then
        TRIG_RC=0
        TRIG_OUT=$(timeout 11 sudo -n /usr/local/sbin/sa02m-audio-web-trigger.sh "$VERB" 2>/dev/null) || TRIG_RC=$?
        case "$TRIG_RC" in
            0)   DONE=ok ;;
            124) DONE=timeout ;;
            *)   DONE=failed ;;
        esac
        if [[ $TRIG_OUT =~ \"error\":\"([a-z_]{1,32})\" ]]; then
            DONE_ERROR=${BASH_REMATCH[1]}
        fi
        au_audit "$VERB result=$DONE"
    elif [[ $RESULT =~ ^\{\"ok\":\ true,\ \"action\":\ \"(save|set_token)\" ]]; then
        # The action name only — never a value (a token is a secret).
        au_audit "${BASH_REMATCH[1]}"
    fi
fi

# The outcome is appended as a fixed enum (+ an error code admitted only as
# [a-z_]) — no helper or broker text is ever echoed through.
if [ "$DONE" = none ]; then
    printf '%s\n' "$RESULT"
elif [ -n "$DONE_ERROR" ]; then
    printf '%s,"done":"%s","done_error":"%s"}\n' "${RESULT%\}}" "$DONE" "$DONE_ERROR"
else
    printf '%s,"done":"%s"}\n' "${RESULT%\}}" "$DONE"
fi
