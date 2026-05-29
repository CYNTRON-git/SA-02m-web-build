#!/bin/bash
# GET  — список управляемых прикладных служб
# POST — {"id":"mosquitto","action":"start"|"stop"}

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
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
if [[ ! -x "$CTL" ]]; then
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    echo '{"ok":false,"error":"ctl_missing"}'
    exit 0
fi

METHOD="${REQUEST_METHOD:-GET}"

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

BODY=""
if [[ -n "${CONTENT_LENGTH:-}" ]] && [[ "$CONTENT_LENGTH" =~ ^[0-9]+$ ]] && (( CONTENT_LENGTH > 0 )); then
    read -r -n "$CONTENT_LENGTH" BODY
fi

ACTION=""
SID=""
if command -v python3 >/dev/null 2>&1; then
    read -r ACTION SID <<EOF
$(printf '%s' "$BODY" | python3 -c "import json,sys
try:
 d=json.load(sys.stdin)
except Exception:
 d={}
print(d.get('action',''))
print(d.get('id',''))
" 2>/dev/null)
EOF
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

if [[ -z "$SID" ]]; then
    echo "Content-type: application/json; charset=UTF-8"
    echo ""
    echo '{"ok":false,"error":"missing_id"}'
    exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') services_ctrl.cgi: ${ACTION} ${SID} (web)" >> /var/log/sa02m_install.log 2>&1

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""
OUT=$(sudo -n "$CTL" "$ACTION" "$SID" 2>&1) || true
if [[ -z "$OUT" ]]; then
    echo '{"ok":false,"error":"sudo_failed"}'
    exit 0
fi
printf '%s\n' "$OUT" | head -n1
