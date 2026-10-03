#!/bin/bash
# Panel control plane for the agent API. The daemon can be off; this CGI still
# enables it. Session and CSRF before any sudo (docs/contracts/agent-api.md).
set -euo pipefail
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

if ! web_session_check_cookie; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

if [ "${REQUEST_METHOD:-GET}" = "GET" ]; then
    PYTHONPATH="/opt/sa02m-agent-api${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -m sa02m_agent_api.panel status
    exit 0
fi

if [ "${REQUEST_METHOD:-}" != "POST" ]; then
    echo '{"ok":false,"error":"method"}'
    exit 0
fi

if ! web_csrf_validate; then
    web_csrf_error_body
    exit 0
fi

BODY=$(mktemp /tmp/sa02m-agent-body.XXXXXX)
cleanup() { rm -f "$BODY" "${record:-}" "${response:-}" "${pass:-}"; return 0; }
trap cleanup EXIT
head -c 65536 >"$BODY"

if ! INSTR=$(PYTHONPATH="/opt/sa02m-agent-api${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -m sa02m_agent_api.panel prepare "$BODY"); then
    printf '%s\n' "$INSTR"
    exit 0
fi

verb=""
id=""
record=""
response=""
pass=""
while IFS='=' read -r k v; do
    case "$k" in
        verb) verb=$v ;;
        id) id=$v ;;
        record) record=$v ;;
        response) response=$v ;;
        pass) pass=$v ;;
    esac
done <<<"$INSTR"

case "$verb" in
    enable)
        if ! timeout 15 sudo -n /usr/local/sbin/sa02m-agent-api-ctl.sh enable; then
            echo '{"ok":false,"error":"sudo"}'
            exit 0
        fi
        echo '{"ok":true,"active":"active"}'
        ;;
    disable)
        if ! timeout 15 sudo -n /usr/local/sbin/sa02m-agent-api-ctl.sh disable; then
            echo '{"ok":false,"error":"sudo"}'
            exit 0
        fi
        echo '{"ok":true,"active":"inactive"}'
        ;;
    restart)
        if ! timeout 15 sudo -n /usr/local/sbin/sa02m-agent-api-ctl.sh restart; then
            echo '{"ok":false,"error":"sudo"}'
            exit 0
        fi
        echo '{"ok":true}'
        ;;
    create)
        if ! timeout 15 sudo -n /usr/local/sbin/sa02m-agent-token-store.sh put "$record"; then
            echo '{"ok":false,"error":"store"}'
            exit 0
        fi
        if [ -n "$pass" ]; then
            if ! timeout 15 sudo -n /usr/local/sbin/sa02m-agent-root-cap.sh grant "$id" "$pass"; then
                timeout 15 sudo -n /usr/local/sbin/sa02m-agent-token-store.sh delete "$id" || true
                echo '{"ok":false,"error":"root_auth"}'
                exit 0
            fi
        fi
        cat "$response"
        ;;
    revoke)
        timeout 15 sudo -n /usr/local/sbin/sa02m-agent-token-store.sh delete "$id" || true
        timeout 15 sudo -n /usr/local/sbin/sa02m-agent-root-cap.sh revoke "$id" || true
        echo '{"ok":true}'
        ;;
    *)
        printf '%s\n' "$INSTR"
        ;;
esac
