#!/bin/bash
[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || {
    echo "Content-type: text/html"; echo "Location: /login.html"; echo ""; exit 0; }

echo "Content-type: application/json"
echo ""
echo '{"ok":true}'
echo "$(date '+%Y-%m-%d %H:%M:%S') reboot requested" >> /var/log/sa02m_install.log 2>&1

# Полностью отсоединяем reboot от fcgiwrap. Иначе при проблемах с systemctl/sudo
# nginx может ждать CGI до fastcgi_read_timeout и отдавать 504.
nohup sh -c 'sleep 1; sudo -n /sbin/reboot >/dev/null 2>&1 || sudo -n /usr/sbin/reboot >/dev/null 2>&1 || sudo -n /usr/bin/systemctl reboot >/dev/null 2>&1 || true' >/dev/null 2>&1 &
