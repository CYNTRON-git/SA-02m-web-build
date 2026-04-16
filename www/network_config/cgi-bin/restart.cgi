#!/bin/bash
[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || {
    echo "Content-type: text/html"; echo "Location: /login.html"; echo ""; exit 0; }

sudo systemctl restart nginx fcgiwrap networking.service fix-eth.service 2>/dev/null || true
echo "$(date '+%Y-%m-%d %H:%M:%S') services restarted" >> /var/log/sa02m_install.log 2>&1

echo "Content-type: application/json"
echo ""
echo '{"ok":true}'
