#!/bin/bash
[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || {
    echo "Content-type: text/html"; echo "Location: /login.html"; echo ""; exit 0; }

# Отправляем ответ ПЕРВЫМ — до запуска сервисов. Иначе перезапуск networking
# может дропнуть TCP-соединение раньше, чем nginx успеет доставить ответ.
echo "Content-type: application/json"
echo ""
echo '{"ok":true}'

echo "$(date '+%Y-%m-%d %H:%M:%S') services restart requested" >> /var/log/sa02m_install.log 2>&1

# Не держим fcgiwrap/nginx до завершения systemctl: на рабочих платах systemctl
# может подвисать на device jobs и тогда CGI уходит в 504, хотя команда уже
# отправлена на выполнение.
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
' >/dev/null 2>&1 &
