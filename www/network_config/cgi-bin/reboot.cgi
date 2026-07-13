#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
web_session_check_cookie || {
    echo "Content-type: text/html"; echo "Location: /login.html"; echo ""; exit 0; }

echo "Content-type: application/json"
echo ""
echo '{"ok":true}'
echo "$(date '+%Y-%m-%d %H:%M:%S') reboot requested (web)" >> /var/log/sa02m_install.log 2>&1

# Ответ до перезагрузки (избегаем 504). В фоне: принудительный reboot (-f) при сбое dbus.
if [[ -x /usr/local/sbin/sa02m-web-reboot.sh ]]; then
  nohup sh -c 'sleep 1; sync 2>/dev/null || true; sudo -n /usr/local/sbin/sa02m-web-reboot.sh' \
    >>/var/log/sa02m_install.log 2>&1 &
else
  nohup sh -c 'sleep 1; sync 2>/dev/null || true; sudo -n /sbin/reboot -f || sudo -n /usr/sbin/reboot -f || sudo -n /usr/bin/reboot -f || sudo -n /sbin/reboot || sudo -n /usr/sbin/reboot || sudo -n /usr/bin/reboot || sudo -n /sbin/shutdown -r now || sudo -n /usr/sbin/shutdown -r now || sudo -n /usr/bin/systemctl reboot || { echo "$(date) reboot.cgi: all sudo reboot paths failed" >> /var/log/sa02m_install.log; }' \
    >>/var/log/sa02m_install.log 2>&1 &
fi
