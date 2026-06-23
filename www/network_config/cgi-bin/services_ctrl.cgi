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
