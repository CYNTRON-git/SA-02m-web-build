#!/bin/bash
# SA-02m Alice config API bridge (session auth → Python dispatch).
# GET  → full config + link/probe status
# POST {"action":"enable"|"disable"|"link"|"unlink"|"upsert_room"|"delete_room"|
#               "upsert_device"|"delete_device"|"available"|"mqtt_topics"|"status", ...}

set -u

# shellcheck source=lib_web_auth.sh
. "$(dirname "$0")/lib_web_auth.sh"

echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

if ! web_session_check_cookie; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

ALICE_ROOT="${SA02M_ALICE_ROOT:-/opt/sa02m-alice}"
# Dev/checkout fallback when package not installed yet
if [ ! -d "$ALICE_ROOT/sa02m_alice" ]; then
    _here="$(cd "$(dirname "$0")/../../.." && pwd)"
    if [ -d "$_here/opt/sa02m-alice/sa02m_alice" ]; then
        ALICE_ROOT="$_here/opt/sa02m-alice"
    fi
fi

export PYTHONPATH="$ALICE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export SA02M_ALICE_ETC="${SA02M_ALICE_ETC:-/etc/sa02m-alice}"

METHOD="${REQUEST_METHOD:-GET}"

# CSRF guard BEFORE any mutation — POST/PUT/DELETE dispatch to the Alice config
# API (enable/disable/link/unlink/upsert/delete) and a privileged sudo trigger.
# Headers already emitted above, so validate directly and print the shared
# error shape (web_csrf_require would re-emit headers into the body). Mirrors
# cloud.cgi's CSRF check.
if [ "$METHOD" = "POST" ] || [ "$METHOD" = "PUT" ] || [ "$METHOD" = "DELETE" ]; then
    if ! web_csrf_validate; then
        echo '{"ok":false,"error":"csrf","error_code":"E_CSRF"}'
        exit 0
    fi
fi

BODY=""
if [ "$METHOD" = "POST" ] || [ "$METHOD" = "PUT" ] || [ "$METHOD" = "DELETE" ]; then
    # Bound read — avoid hanging fcgiwrap
    CL="${CONTENT_LENGTH:-0}"
    case "$CL" in
        ''|*[!0-9]*) CL=0 ;;
    esac
    if [ "$CL" -gt 65536 ]; then
        echo '{"ok":false,"error":"payload_too_large"}'
        exit 0
    fi
    if [ "$CL" -gt 0 ]; then
        BODY=$(dd bs=1 count="$CL" 2>/dev/null || true)
    fi
fi

export SA02M_ALICE_METHOD="$METHOD"
export SA02M_ALICE_BODY="$BODY"
export SA02M_ALICE_PATH="${PATH_INFO:-/integrations/alice/}"

# Result captured (still printed verbatim below) so the post-dispatch nudge
# can act only on a successful mutation.
RESULT=$(timeout 8 python3 - <<'PY'
import json, os, sys
method = os.environ.get("SA02M_ALICE_METHOD", "GET")
path = os.environ.get("SA02M_ALICE_PATH") or "/integrations/alice/"
raw = os.environ.get("SA02M_ALICE_BODY") or ""
body = {}
if raw.strip():
    try:
        body = json.loads(raw)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "invalid_json", "message": str(exc)}))
        sys.exit(0)
try:
    from sa02m_alice.config.api import dispatch
except Exception as exc:
    print(json.dumps({"ok": False, "error": "import_failed", "message": str(exc)}))
    sys.exit(0)
# CGI without PATH_INFO: prefer action in body
if method == "GET" and not (body or {}).get("action"):
    code, result = dispatch("GET", "/integrations/alice/")
else:
    code, result = dispatch(method, path, body if isinstance(body, dict) else {})
print(json.dumps(result, ensure_ascii=False))
PY
) || RESULT='{"ok":false,"error":"alice_api_failed","message":"python dispatch failed or timed out"}'
printf '%s\n' "$RESULT"

# Best-effort unit nudge after enable/disable (needs sudoers from 06-alice.sh)
if [ "$METHOD" = "POST" ]; then
    ACTION=$(printf '%s' "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('action',''))" 2>/dev/null || true)
    if [ "$ACTION" = "enable" ] || [ "$ACTION" = "disable" ]; then
        sudo -n /usr/local/sbin/sa02m-alice-web-trigger.sh "$ACTION" >/dev/null 2>&1 || true
    fi
    # The running client builds its DeviceRegistry once (MQTT subs taken at
    # connect) — after a successful binding mutation restart it so edits
    # apply, but only when the operator has the client enabled.
    case "$ACTION" in
        upsert_device|delete_device|upsert_room|delete_room)
            case "$RESULT" in
                *'"ok": true'*|*'"ok":true'*)
                    if grep -Eq '^\s*client_enabled\s*=\s*[Tt]rue' /etc/sa02m-alice/sa02m-alice-client.conf 2>/dev/null; then
                        sudo -n /usr/local/sbin/sa02m-alice-web-trigger.sh restart >/dev/null 2>&1 || true
                    fi
                    ;;
            esac
            ;;
    esac
fi
