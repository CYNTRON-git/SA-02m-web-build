#!/bin/bash
[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || {
    echo "Content-type: text/html"; echo "Location: /login.html"; echo ""; exit 0; }

echo "Content-type: application/json"
echo ""
echo '{"ok":true}'
echo "$(date '+%Y-%m-%d %H:%M:%S') services restart requested (web)" >> /var/log/sa02m_install.log 2>&1

if [[ -x /usr/local/sbin/sa02m-web-restart-services.sh ]]; then
  nohup sh -c 'sleep 1; sync 2>/dev/null || true; sudo -n /usr/local/sbin/sa02m-web-restart-services.sh' \
    >>/var/log/sa02m_install.log 2>&1 &
else
  nohup sh -c '
log_file=/var/log/sa02m_install.log
run_restart() {
    unit="$1"
    shift
    if ! sudo -n /usr/bin/systemctl restart "$@" >/dev/null 2>&1; then
        echo "$(date "+%Y-%m-%d %H:%M:%S") restart.cgi: failed to restart ${unit}" >> "$log_file" 2>&1
    fi
}
run_restart networking networking
run_restart nginx nginx
run_restart fcgiwrap fcgiwrap
run_restart fix-eth fix-eth.service
' >>/var/log/sa02m_install.log 2>&1 &
fi
