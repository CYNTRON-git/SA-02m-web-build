#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"

echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

if ! web_session_check_cookie; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
    echo '{"ok":false,"error":"method_not_allowed"}'
    exit 0
fi

# CSRF BEFORE any mutation (web-code-rigor.md ## Bash CGI floors; policy:
# docs/decisions/selective-csrf-policy.md). Headers already emitted above, so
# validate inline and print the shared error shape.
if ! web_csrf_validate; then
    echo '{"ok":false,"error":"csrf","error_code":"E_CSRF"}'
    exit 0
fi

TMP_IN=$(mktemp /tmp/sa02m-cmd-in.XXXXXX)
TMP_OUT=$(mktemp /tmp/sa02m-cmd-out.XXXXXX)
TMP_PASS=$(mktemp /tmp/sa02m-cmd-pass.XXXXXX)
TMP_CMD=$(mktemp /tmp/sa02m-cmd-cmd.XXXXXX)
trap 'rm -f "$TMP_IN" "$TMP_OUT" "$TMP_PASS" "$TMP_CMD"' EXIT

CL=$(printf '%s' "${CONTENT_LENGTH:-}" | tr -cd '0-9')
if [ -n "$CL" ] && [ "$CL" -gt 0 ] 2>/dev/null; then
    dd bs=1 count="$CL" 2>/dev/null >"$TMP_IN" || true
fi

form_value() {
    python3 - "$TMP_IN" "$1" <<'PY' 2>/dev/null
import sys
from urllib.parse import parse_qs
raw = open(sys.argv[1], "rb").read().decode("utf-8", "replace")
print((parse_qs(raw, keep_blank_values=True).get(sys.argv[2]) or [""])[0])
PY
}

CMD=$(form_value cmd)
MODE=$(form_value mode)
ROOT_PASSWORD=$(form_value root_password)

CMD=${CMD//$'\r'/}
MODE=${MODE//$'\r'/}
ROOT_PASSWORD=${ROOT_PASSWORD//$'\r'/}
[ -n "$MODE" ] || MODE=web
if [ -z "$CMD" ]; then
    echo '{"ok":false,"error":"empty_command"}'
    exit 0
fi
if [ "${#CMD}" -gt 1000 ]; then
    echo '{"ok":false,"error":"command_too_long"}'
    exit 0
fi
case "$MODE" in
    web|root) ;;
    *)
        echo '{"ok":false,"error":"invalid_mode"}'
        exit 0
        ;;
esac
if [ "$MODE" = "root" ] && [ -z "$ROOT_PASSWORD" ]; then
    echo '{"ok":false,"error":"root_password_required"}'
    exit 0
fi

# Keep BELOW nginx `fastcgi_read_timeout` for /cgi-bin/ (20s, network_config.conf):
# the command output is buffered and only returned after the command ends, so a
# never-terminating command (e.g. `ping 8.8.8.8` without -c) must be killed and
# the JSON returned before nginx times out — else nginx serves a 504 HTML page
# and the client's JSON.parse fails ("Unexpected token '<'").
TIMEOUT_SEC=15
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

if [ "$MODE" = "root" ]; then
    HELPER=/usr/local/sbin/sa02m-web-root-cmd.sh
    if [ ! -x "$HELPER" ]; then
        echo '{"ok":false,"error":"root_helper_missing"}'
        exit 0
    fi
    printf '%s\n' "$ROOT_PASSWORD" >"$TMP_PASS"
    printf '%s\n' "$CMD" >"$TMP_CMD"
    chmod 600 "$TMP_PASS" "$TMP_CMD" 2>/dev/null || true
    sudo -n -u root "$HELPER" "$TMP_PASS" "$TMP_CMD" >"$TMP_OUT" 2>&1
else
    timeout "$TIMEOUT_SEC" /bin/bash -lc "$CMD" >"$TMP_OUT" 2>&1
fi
RC=$?
if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
    printf '\n[timeout after %ss]\n' "$TIMEOUT_SEC" >>"$TMP_OUT"
fi

python3 - "$TMP_OUT" "$RC" "$MODE" <<'PY'
import json
import sys

path = sys.argv[1]
rc = int(sys.argv[2])
mode = sys.argv[3]
limit = 32768
with open(path, "rb") as f:
    data = f.read()
truncated = len(data) > limit
if truncated:
    data = data[-limit:]
text = data.decode("utf-8", "replace")
print(json.dumps({
    "ok": True,
    "rc": rc,
    "mode": mode,
    "output": text,
    "truncated": truncated,
}, ensure_ascii=False))
PY
