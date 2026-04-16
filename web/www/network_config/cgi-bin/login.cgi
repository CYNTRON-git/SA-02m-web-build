#!/bin/bash
read -r POST_DATA
LOGIN=$(echo "$POST_DATA" | sed -n 's/^.*login=\([^&]*\).*$/\1/p' | sed 's/%\([0-9A-F][0-9A-F]\)/\\x\1/gI' | xargs -0 printf '%b')
PASSWORD=$(echo "$POST_DATA" | sed -n 's/^.*password=\([^&]*\).*$/\1/p' | sed 's/%\([0-9A-F][0-9A-F]\)/\\x\1/gI' | xargs -0 printf '%b')

if [[ "$LOGIN" == "admin" && "$PASSWORD" == "cyntron" ]]; then
    echo "Content-type: text/html; charset=utf-8"
    echo "Set-Cookie: session_token=cyntron_session; Path=/; HttpOnly"
    echo "Location: /cgi-bin/index.cgi"
    echo ""
    { echo "Login OK!"; } >> /var/log/sa02m_install.log 2>&1
    exit 0
else
    echo "Content-type: text/html; charset=utf-8"
    echo "Location: /cgi-bin/index.cgi?error=1"
    echo ""
    { echo "Login Error!"; } >> /var/log/sa02m_install.log 2>&1
    exit 1
fi
