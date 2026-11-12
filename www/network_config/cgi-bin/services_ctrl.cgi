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
if [[ "$METHOD" = "GET" ]] && [[ "${QUERY_STRING:-}" =~ (^|&)result=1(&|$) ]]; then
    RID=""
    if [[ "${QUERY_STRING:-}" =~ (^|&)id=([a-zA-Z0-9_-]{1,64})(&|$) ]]; then
        RID="${BASH_REMATCH[2]}"
    fi
    OP_ID=""
    echo "Content-type: application/json; charset=UTF-8"
    echo "Cache-Control: no-store"
    echo ""
    if [[ -z "$RID" ]]; then
        echo '{"ok":false,"error":"missing_id"}'
        exit 0
    fi
    if [[ "${QUERY_STRING:-}" =~ (^|&)op_id= ]]; then
        if [[ "${QUERY_STRING:-}" =~ (^|&)op_id=([a-f0-9]{32})(&|$) ]]; then
            OP_ID="${BASH_REMATCH[2]}"
        else
            echo '{"ok":false,"error":"invalid_operation"}'
            exit 0
        fi
    fi
    RF="${RESULT_DIR}/${RID}.json"
    if [[ -n "$OP_ID" ]]; then
        python3 -c 'import json, sys
try:
    with open(sys.argv[1]) as f:
        result = json.load(f)
except (OSError, ValueError):
    result = {}
if not isinstance(result, dict) or result.get("op_id") != sys.argv[3] or result.get("id") != sys.argv[2]:
    result = {"ok": True, "pending": True, "id": sys.argv[2], "op_id": sys.argv[3]}
print(json.dumps(result))' "$RF" "$RID" "$OP_ID"
    elif [[ -s "$RF" ]]; then
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
    # The helper's `list` leaves its exact answer in list.json (root-owned dir,
    # 0644, dropped by every start/stop/install/uninstall); a copy younger than
    # 10 s is served without a `sudo CTL list` (A8-04). Read directly — the
    # sudoers grant pins the bare `list` verb, and a read forks no sudo.
    LIST_CACHE="${RESULT_DIR}/list.json"
    if [[ -f "$LIST_CACHE" && -s "$LIST_CACHE" ]]; then
        LIST_AGE=$(( EPOCHSECONDS - $(stat -c %Y "$LIST_CACHE" 2>/dev/null || echo 0) ))
        if (( LIST_AGE >= 0 && LIST_AGE < 10 )) && cat "$LIST_CACHE" 2>/dev/null; then
            exit 0
        fi
    fi
    # Bounded below this location's 120 s nginx read timeout (A8-11).
    timeout -k 2 100 sudo -n "$CTL" list
    rc=$?
    if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
        echo "services_ctrl.cgi: list timed out after 100s" >&2
        echo '{"ok":false,"error":"timeout"}'
    fi
    exit 0
fi

if [[ "$METHOD" != "POST" ]]; then
    echo "Content-type: application/json; charset=UTF-8"
    echo ""
    echo '{"ok":false,"error":"method_not_allowed"}'
    exit 0
fi

# CSRF BEFORE any mutation (policy: docs/decisions/selective-csrf-policy.md).
# Headers not yet emitted on the POST path, so web_csrf_require emits its own.
web_csrf_require

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
    stop|start|install|uninstall) ;;
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
if [[ ${#SID} -gt 64 ]]; then
    printf 'Content-type: application/json; charset=UTF-8\n\n{"ok":false,"error":"invalid_id"}\n'
    exit 0
fi

# Fresh admission identity prevents a previous terminal result completing this request.
OP_ID=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
if [[ ! "$OP_ID" =~ ^[a-f0-9]{32}$ ]]; then
    printf 'Content-type: application/json; charset=UTF-8\n\n{"ok":false,"error":"operation_id_failed"}\n'
    exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') services_ctrl.cgi: ${ACTION} ${SID} (web, async)" >> /var/log/sa02m_install.log 2>&1

# Ответ сразу (stop/start CODESYS и др. могут занимать >20s → nginx 504).
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""
printf '{"ok":true,"pending":true,"id":"%s","action":"%s","op_id":"%s"}\n' "$SID" "$ACTION" "$OP_ID"

nohup sudo -n "$CTL" "$ACTION" "$SID" "$OP_ID" </dev/null >> /var/log/sa02m_install.log 2>&1 &
