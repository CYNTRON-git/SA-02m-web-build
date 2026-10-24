#!/bin/bash
# Authenticated fixed-operation update controller boundary.
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
. "$(dirname "$0")/lib_web_update_control.sh"
web_session_check_cookie || {
    printf 'Content-type: application/json\r\n\r\n{"ok":false,"error":"unauthorized"}\n'
    exit 0
}
if [ "${REQUEST_METHOD:-GET}" = GET ]; then
    web_update_control inspect
    exit 0
fi
if [ "${REQUEST_METHOD:-GET}" != POST ]; then
    printf 'Content-type: application/json\r\n\r\n{"ok":false,"error":"method_not_allowed"}\n'
    exit 0
fi
web_csrf_require
web_update_control upload "${CONTENT_TYPE:-}" "${CONTENT_LENGTH:-}"
