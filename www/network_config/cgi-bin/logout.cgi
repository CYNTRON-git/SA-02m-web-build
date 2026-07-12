#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
web_session_destroy_cookie
echo "Content-type: text/html"
echo "Set-Cookie: session_token=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; SameSite=Lax; Max-Age=0"
echo "Location: /login.html"
echo ""
