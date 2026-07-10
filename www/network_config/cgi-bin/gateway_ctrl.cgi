#!/bin/bash
. "$(dirname "$0")/lib_web_session.sh"
# POST {"action": "start"|"stop"|"restart"|"reload"} → controls sa02m-serial-gateway

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    web_session_check_cookie "${HTTP_COOKIE:-}" && return 0
    return 1
}
if ! check_auth; then echo '{"ok":false,"error":"unauthorized"}'; exit 0; fi
if [ "$REQUEST_METHOD" != "POST" ]; then echo '{"ok":false,"error":"method"}'; exit 0; fi

TMP=$(mktemp /tmp/sa02m-gwctrl.XXXXXX)
trap "rm -f '$TMP'" EXIT
dd bs=1 count="${CONTENT_LENGTH:-0}" 2>/dev/null > "$TMP"

ACTION=$(python3 -c "import sys,json; print(json.load(open('$TMP')).get('action',''))" 2>/dev/null || true)

run_svc() { sudo /usr/bin/systemctl "$1" sa02m-serial-gateway >/dev/null 2>&1; echo $?; }

case "$ACTION" in
    start)   rc=$(run_svc start)   ;;
    stop)    rc=$(run_svc stop)    ;;
    restart) rc=$(run_svc restart) ;;
    reload)  rc=$(run_svc reload)  ;;
    *)       echo '{"ok":false,"error":"unknown_action"}'; exit 0 ;;
esac

if [ "${rc:-1}" = "0" ]; then
    echo "{\"ok\":true,\"action\":\"${ACTION}\"}"
else
    echo "{\"ok\":false,\"action\":\"${ACTION}\",\"error\":\"systemctl_rc_${rc}\"}"
fi
