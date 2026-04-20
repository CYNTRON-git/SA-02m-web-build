#!/bin/bash
# Служебная проверка авторизации для nginx auth_request.
# Успех — 204 (без тела), отказ — 401. Никаких побочных действий.
if [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]]; then
    echo "Status: 204 No Content"
    echo ""
    exit 0
fi
echo "Status: 401 Unauthorized"
echo "Content-type: text/plain"
echo ""
echo "unauthorized"
