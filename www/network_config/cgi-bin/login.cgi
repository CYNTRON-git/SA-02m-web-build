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

if [ "$USERNAME" = "admin" ] && [ "$PASSWORD" = "cyntron" ]; then
    echo "Content-type: text/html"
    echo "Set-Cookie: session_token=cyntron_session; Path=/; HttpOnly; SameSite=Strict"
    echo "Location: /"
    echo ""
else
    echo "Content-type: text/html"
    echo "Location: /login.html?error=1"
    echo ""
fi
