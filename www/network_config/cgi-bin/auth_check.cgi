#!/bin/bash
# Служебная проверка авторизации для nginx auth_request.
# Успех — 204 (без тела), отказ — 401. Никаких побочных действий.
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
if web_session_check_cookie; then
    echo "Status: 204 No Content"
    echo ""
    exit 0
fi
echo "Status: 401 Unauthorized"
echo "Content-type: text/plain"
echo ""
echo "unauthorized"
