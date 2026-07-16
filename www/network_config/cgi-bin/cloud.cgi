#!/bin/bash
# SA-02m Cloud Agent CGI — status & activation via web UI
# GET  → JSON status
# POST {"action":"pair"}   → request a cloud pairing code (primary flow)
# POST {"action":"cancel"} → cancel a pending pairing
# POST {"token":"..."}     → enroll-token fallback (installers)

STATUS_FILE="/run/sa02m-cloud-status.json"
ACTIVATION_TOKEN_FILE="/etc/sa02m-cloud/activation_token"
PAIR_REQUEST_FILE="/etc/sa02m-cloud/pair_request"

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
        # Claim-code flow: the trigger file tells the agent to request a
        # pairing code; the code appears in the status JSON (state=pairing).
        mkdir -p /etc/sa02m-cloud
        : > "$PAIR_REQUEST_FILE"
        chmod 600 "$PAIR_REQUEST_FILE"
        systemctl start sa02m-cloud-agent 2>/dev/null || true
        echo '{"ok":true,"message":"pairing requested"}'
        exit 0
    fi

    if [ "$ACTION" = "cancel" ]; then
        rm -f "$PAIR_REQUEST_FILE"
        echo '{"ok":true,"message":"pairing cancelled"}'
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

    # Write server to config if provided — validate as a hostname FIRST; the
    # value is interpolated into sed replacement text, where an unescaped '|'
    # (or GNU sed 'e') would break out. A hostname allow-list has no metachars.
    if [ -n "$SERVER" ] && [ "$SERVER" != "cloud.cyntron.ru" ]; then
        if ! valid_hostname "$SERVER"; then
            echo '{"ok":false,"error":"invalid server hostname"}'
            exit 0
        fi
        mkdir -p /etc/sa02m-cloud
        CFG="/etc/sa02m-cloud/agent.conf"
        if [ -f "$CFG" ]; then
            sed -i "s|^server_host.*|server_host = $SERVER|" "$CFG" || true
            sed -i "s|^api_url.*|api_url = https://$SERVER/api/v1|" "$CFG" || true
        fi
    fi

    # Write activation token — agent will detect and activate
    mkdir -p /etc/sa02m-cloud
    echo "$TOKEN" > "$ACTIVATION_TOKEN_FILE"
    chmod 600 "$ACTIVATION_TOKEN_FILE"

    # Ensure agent service is running (it will pick up the token file)
    systemctl start sa02m-cloud-agent 2>/dev/null || true

    echo '{"ok":true,"message":"Activation started. Status will update in ~10 seconds."}'

elif [ "$REQUEST_METHOD" = "GET" ]; then
    STATUS=$(read_status)

    # Append service state
    SVC_STATE=$(systemctl is-active sa02m-cloud-agent 2>/dev/null || echo "unknown")
    SVC_ENABLED=$(systemctl is-enabled sa02m-cloud-agent 2>/dev/null || echo "unknown")

    # Merge extra fields into status JSON
    echo "$STATUS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['service_active']  = '$SVC_STATE'
d['service_enabled'] = '$SVC_ENABLED'
d['has_token_file']  = $([ -f '$ACTIVATION_TOKEN_FILE' ] && echo 'true' || echo 'false')
print(json.dumps(d))
" 2>/dev/null || echo "$STATUS"

else
    echo '{"ok":false,"error":"method not allowed"}'
fi
