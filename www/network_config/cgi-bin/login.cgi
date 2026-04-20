#!/bin/bash
read -r -n "${CONTENT_LENGTH:-0}" POST_DATA

decode() { printf '%b' "$(echo "$1" | sed 's/+/ /g; s/%/\\x/g')"; }

get_field() {
    local val
    val=$(echo "$POST_DATA" | tr '&' '\n' | grep "^${1}=" | cut -d= -f2-)
    decode "$val"
}

USERNAME=$(get_field "username")
PASSWORD=$(get_field "password")

AUTH_ENV="/etc/sa02m_web.env"
if [ -f "$AUTH_ENV" ]; then
    # shellcheck disable=1090
    . "$AUTH_ENV"
fi
: "${SA02M_WEB_USER:=admin}"
: "${SA02M_WEB_PASS:=cyntron}"

if [ "$USERNAME" = "$SA02M_WEB_USER" ] && [ "$PASSWORD" = "$SA02M_WEB_PASS" ]; then
    echo "Status: 302 Found"
    echo "Content-type: text/html; charset=UTF-8"
    # Без HttpOnly: guard в app.js/login.html читает document.cookie (HttpOnly в JS не виден → вечный редирект на логин)
    echo "Set-Cookie: session_token=cyntron_session; Path=/; SameSite=Lax; Max-Age=864000"
    echo "Location: /"
    echo ""
else
    echo "Status: 302 Found"
    echo "Content-type: text/html; charset=UTF-8"
    echo "Location: /login.html?error=1"
    echo ""
fi
