#!/bin/bash
# SA-02m Cloud Agent CGI — status & activation via web UI
# GET  → JSON status
# POST {"action":"pair"}    → request a cloud pairing code (primary flow)
# POST {"action":"cancel"}  → cancel a pending pairing
# POST {"action":"enable"}  → enable+start cloud agent
# POST {"action":"disable"} → stop tunnel+agent, disable unit (no internet)
# POST {"token":"..."}      → enroll-token fallback (installers)

STATUS_FILE="/run/sa02m-cloud-status.json"
ACTIVATION_TOKEN_FILE="/etc/sa02m-cloud/activation_token"
CLOUD_TRIGGER="/usr/local/sbin/sa02m-cloud-web-trigger.sh"

# shellcheck source=lib_web_auth.sh
. "$(dirname "$0")/lib_web_auth.sh"
# shellcheck source=lib_web_validate.sh
. "$(dirname "$0")/lib_web_validate.sh"

echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

# Auth gate — this endpoint writes an activation token and rewrites the cloud
# agent config as root; it is NOT in the nginx auth_request set, so it must
# guard itself. Fail closed.
if ! web_session_check_cookie; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

read_status() {
    if [ -f "$STATUS_FILE" ]; then
        cat "$STATUS_FILE"
    else
        echo '{"state":"unknown"}'
    fi
}

if [ "$REQUEST_METHOD" = "POST" ]; then
    # Read POST body
    read -r -n "${CONTENT_LENGTH:-0}" POST_DATA

    ACTION=$(echo "$POST_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('action',''))" 2>/dev/null)
    TOKEN=$(echo "$POST_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))" 2>/dev/null)
    SERVER=$(echo "$POST_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('server','cloud.cyntron.ru'))" 2>/dev/null)

    if [ "$ACTION" = "pair" ]; then
        # Writes under /etc/sa02m-cloud require root (dir is 750 root:root).
        if ! OUT=$(sudo -n "$CLOUD_TRIGGER" pair 2>/dev/null); then
            echo '{"ok":false,"error":"cloud trigger unavailable"}'
            exit 0
        fi
        echo "$OUT"
        exit 0
    fi

    if [ "$ACTION" = "cancel" ]; then
        if ! OUT=$(sudo -n "$CLOUD_TRIGGER" cancel 2>/dev/null); then
            echo '{"ok":false,"error":"cloud trigger unavailable"}'
            exit 0
        fi
        echo "$OUT"
        exit 0
    fi

    if [ "$ACTION" = "enable" ] || [ "$ACTION" = "disable" ]; then
        if ! OUT=$(sudo -n "$CLOUD_TRIGGER" "$ACTION" 2>/dev/null); then
            echo '{"ok":false,"error":"cloud trigger unavailable"}'
            exit 0
        fi
        echo "$OUT"
        exit 0
    fi

    if [ -z "$TOKEN" ]; then
        echo '{"ok":false,"error":"token is required"}'
        exit 0
    fi

    # Validate token format (basic: non-empty, reasonable length)
    TOKEN_LEN=${#TOKEN}
    if [ "$TOKEN_LEN" -lt 8 ] || [ "$TOKEN_LEN" -gt 256 ]; then
        echo '{"ok":false,"error":"invalid token format"}'
        exit 0
    fi

    # Validate hostname BEFORE handing it to the privileged helper (sed).
    if [ -n "$SERVER" ] && [ "$SERVER" != "cloud.cyntron.ru" ]; then
        if ! valid_hostname "$SERVER"; then
            echo '{"ok":false,"error":"invalid server hostname"}'
            exit 0
        fi
    fi

    if ! OUT=$(sudo -n "$CLOUD_TRIGGER" token "$TOKEN" "$SERVER" 2>/dev/null); then
        echo '{"ok":false,"error":"cloud trigger unavailable"}'
        exit 0
    fi
    echo "$OUT"

elif [ "$REQUEST_METHOD" = "GET" ]; then
    STATUS=$(read_status)

    # Append service state (is-active/is-enabled exit ≠0 when inactive/disabled —
    # must not append "unknown" via ||, or we get "inactive\nunknown").
    SVC_STATE=$(systemctl is-active sa02m-cloud-agent 2>/dev/null || true)
    [ -n "$SVC_STATE" ] || SVC_STATE=unknown
    SVC_ENABLED=$(systemctl is-enabled sa02m-cloud-agent 2>/dev/null || true)
    [ -n "$SVC_ENABLED" ] || SVC_ENABLED=unknown
    # www-data cannot look inside /etc/sa02m-cloud (750 root:root).
    HAS_TOKEN=false
    if [ -r "$ACTIVATION_TOKEN_FILE" ]; then
        HAS_TOKEN=true
    fi

    # Merge extra fields into status JSON (env avoids shell-quote traps under fcgiwrap)
    STATUS="$STATUS" SVC_STATE="$SVC_STATE" SVC_ENABLED="$SVC_ENABLED" HAS_TOKEN="$HAS_TOKEN" python3 -c '
import os, json, sys
try:
    d = json.loads(os.environ.get("STATUS") or "{}")
except Exception:
    d = {"state": "unknown"}
d["service_active"] = os.environ.get("SVC_STATE", "unknown")
d["service_enabled"] = os.environ.get("SVC_ENABLED", "unknown")
d["has_token_file"] = os.environ.get("HAS_TOKEN", "false").lower() == "true"
print(json.dumps(d))
' 2>/dev/null || echo "$STATUS"

else
    echo '{"ok":false,"error":"method not allowed"}'
fi
