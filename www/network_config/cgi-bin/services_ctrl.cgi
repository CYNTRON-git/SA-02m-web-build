#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# GET  — список управляемых прикладных служб
# POST — {"id":"mosquitto","action":"start"|"stop"}

check_auth() {
    web_session_check_cookie && return 0
    return 1
}

if ! check_auth; then
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

CTL=/usr/local/sbin/sa02m-web-service-ctl.sh
RESULT_DIR=/var/run/sa02m-svcctl
if [[ ! -x "$CTL" ]]; then
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    echo '{"ok":false,"error":"ctl_missing"}'
    exit 0
fi

METHOD="${REQUEST_METHOD:-GET}"

# GET ?result=1&id=<svc> — результат async start/stop (JSON из /var/run/sa02m-svcctl/<id>.json).
if [[ "$METHOD" = "GET" ]] && [[ "${QUERY_STRING:-}" =~ result=1 ]]; then
    RID=""
    if [[ "${QUERY_STRING:-}" =~ (^|&)id=([a-zA-Z0-9_-]+) ]]; then
        RID="${BASH_REMATCH[2]}"
    fi
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    if [[ -z "$RID" ]]; then
        echo '{"ok":false,"error":"missing_id"}'
        exit 0
    fi
    RF="${RESULT_DIR}/${RID}.json"
    if [[ -s "$RF" ]]; then
        cat "$RF"
    else
        echo '{"ok":true,"pending":true,"id":"'"$RID"'"}'
    fi
    exit 0
fi

if [[ "$METHOD" = "GET" ]]; then
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    sudo -n "$CTL" list
    exit 0
fi

if [[ "$METHOD" != "POST" ]]; then
    echo "Content-type: application/json; charset=UTF-8"
    echo ""
    echo '{"ok":false,"error":"method_not_allowed"}'
    exit 0
fi

# FCGI: читать ровно CONTENT_LENGTH байт (как mqtt_ctrl.cgi / apply.cgi).
TMP=$(mktemp /tmp/sa02m-svcctrl.XXXXXX)
trap 'rm -f "$TMP"' EXIT
CL=$(printf '%s' "${CONTENT_LENGTH:-}" | tr -cd '0-9')
if [[ -n "$CL" ]] && [[ "$CL" -gt 0 ]] 2>/dev/null; then
    dd bs=1 count="$CL" 2>/dev/null >"$TMP" || true
fi

ACTION=""
SID=""
if [[ -s "$TMP" ]] && command -v python3 >/dev/null 2>&1; then
    ACTION=$(python3 -c "import json
try:
    with open('$TMP') as f:
        d = json.load(f)
except Exception:
    d = {}
print(d.get('action', ''))" 2>/dev/null | tr -d '\r\n')
    SID=$(python3 -c "import json
try:
    with open('$TMP') as f:
        d = json.load(f)
except Exception:
    d = {}
print(d.get('id', ''))" 2>/dev/null | tr -d '\r\n')
fi

case "$ACTION" in
    stop|start) ;;
    *)
        echo "Content-type: application/json; charset=UTF-8"
        echo ""
        echo '{"ok":false,"error":"bad_action"}'
        exit 0
        ;;
esac

SID=$(printf '%s' "$SID" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
if [[ -z "$SID" ]]; then
    echo "Content-type: application/json; charset=UTF-8"
    echo ""
    echo '{"ok":false,"error":"missing_id"}'
    exit 0
fi

case "$SID" in
    ''|*[!a-zA-Z0-9_-]*)
        echo "Content-type: application/json; charset=UTF-8"
        echo ""
        echo '{"ok":false,"error":"invalid_id"}'
        exit 0
        ;;
esac

echo "$(date '+%Y-%m-%d %H:%M:%S') services_ctrl.cgi: ${ACTION} ${SID} (web, async)" >> /var/log/sa02m_install.log 2>&1

# Ответ сразу (stop/start CODESYS и др. могут занимать >20s → nginx 504).
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""
if command -v python3 >/dev/null 2>&1; then
    python3 -c "import json; print(json.dumps({'ok': True, 'pending': True, 'id': '$SID', 'action': '$ACTION'}))"
else
    printf '{"ok":true,"pending":true,"id":"%s","action":"%s"}\n' "$SID" "$ACTION"
fi

nohup sudo -n "$CTL" "$ACTION" "$SID" >> /var/log/sa02m_install.log 2>&1 &
