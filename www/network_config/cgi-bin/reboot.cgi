#!/bin/bash
[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || {
    echo "Content-type: text/html"; echo "Location: /login.html"; echo ""; exit 0; }

echo "Content-type: application/json"
echo ""
echo '{"ok":true}'
echo "$(date '+%Y-%m-%d %H:%M:%S') reboot initiated" >> /var/log/sa02m_install.log 2>&1
sleep 1
sudo reboot &
