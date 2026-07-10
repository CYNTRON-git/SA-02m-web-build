#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_session.sh"

# Удалить серверную сессию (если токен валиден по формату).
if TOK=$(web_session_from_cookie "${HTTP_COOKIE:-}"); then
    web_session_destroy "$TOK"
fi

echo "Content-type: text/html"
echo "Set-Cookie: session_token=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; SameSite=Lax; Max-Age=0"
echo "Location: /login.html"
echo ""
