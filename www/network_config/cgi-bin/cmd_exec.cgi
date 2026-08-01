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

TMP_IN=$(mktemp /tmp/sa02m-cmd-in.XXXXXX)
TMP_OUT=$(mktemp /tmp/sa02m-cmd-out.XXXXXX)
trap 'rm -f "$TMP_IN" "$TMP_OUT"' EXIT

CL=$(printf '%s' "${CONTENT_LENGTH:-}" | tr -cd '0-9')
if [ -n "$CL" ] && [ "$CL" -gt 0 ] 2>/dev/null; then
    dd bs=1 count="$CL" 2>/dev/null >"$TMP_IN" || true
fi

CMD=$(python3 - "$TMP_IN" <<'PY' 2>/dev/null
import sys
from urllib.parse import parse_qs
raw = open(sys.argv[1], "rb").read().decode("utf-8", "replace")
print((parse_qs(raw, keep_blank_values=True).get("cmd") or [""])[0])
PY
)

CMD=${CMD//$'\r'/}
if [ -z "$CMD" ]; then
    echo '{"ok":false,"error":"empty_command"}'
    exit 0
fi
if [ "${#CMD}" -gt 1000 ]; then
    echo '{"ok":false,"error":"command_too_long"}'
    exit 0
fi

TIMEOUT_SEC=20
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

timeout "$TIMEOUT_SEC" /bin/bash -lc "$CMD" >"$TMP_OUT" 2>&1
RC=$?
if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
    printf '\n[timeout after %ss]\n' "$TIMEOUT_SEC" >>"$TMP_OUT"
fi

python3 - "$TMP_OUT" "$RC" <<'PY'
import json
import sys

path = sys.argv[1]
rc = int(sys.argv[2])
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
    "output": text,
    "truncated": truncated,
}, ensure_ascii=False))
PY
