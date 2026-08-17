#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
echo "Content-type: application/json; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    web_session_check_cookie && return 0
    return 1
}

if ! check_auth; then
    echo '{"ok":false,"error":"unauthorized"}'
    exit 0
fi

# POST-only + CSRF BEFORE any mutation (body-driven auto-format toggle).
# policy: docs/decisions/selective-csrf-policy.md. Headers already emitted.
if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
    echo '{"ok":false,"error":"method_not_allowed"}'
    exit 0
fi
if ! web_csrf_validate; then
    echo '{"ok":false,"error":"csrf","error_code":"E_CSRF"}'
    exit 0
fi

POST_DATA=""
if [ -n "${CONTENT_LENGTH:-}" ]; then
    CL=$(printf '%s' "${CONTENT_LENGTH}" | tr -cd '0-9')
    if [ -n "$CL" ] && [ "$CL" -gt 0 ] 2>/dev/null; then
        POST_DATA=$(dd bs=1 count="$CL" 2>/dev/null) || POST_DATA=""
    fi
fi
decode() {
    printf '%s' "$POST_DATA" | sed -n "s/^.*$1=\([^&]*\).*$/\1/p" \
        | sed 's/%\([0-9A-F][0-9A-F]\)/\\x\1/gI' \
        | xargs -0 printf '%b'
}

VAL=$(decode enabled)
if [ "$VAL" != "0" ] && [ "$VAL" != "1" ]; then
    echo '{"ok":false,"error":"bad_value"}'
    exit 0
fi

if [ ! -x /usr/local/sbin/sa02m-set-storage-auto-format ] || [ ! -x /usr/local/bin/storage-mount.sh ]; then
    echo '{"ok":false,"error":"storage_tools_not_installed"}'
    exit 0
fi

if sudo -n /usr/local/sbin/sa02m-set-storage-auto-format "$VAL"; then
    echo "{\"ok\":true,\"storage_auto_format\":${VAL}}"
else
    echo '{"ok":false,"error":"write_failed"}'
fi
