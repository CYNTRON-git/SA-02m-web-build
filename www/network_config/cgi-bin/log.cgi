#!/bin/bash
echo "Content-type: text/plain; charset=UTF-8"
echo "Cache-Control: no-cache"
echo ""

check_auth() {
    [[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] && return 0
    return 1
}

if ! check_auth; then
    echo "Нет доступа"
    exit 0
fi

LOG_FILE="/var/log/sa02m_install.log"
if [ -f "$LOG_FILE" ]; then
    tail -n 60 "$LOG_FILE"
else
    echo "Журнал пуст или недоступен"
fi
