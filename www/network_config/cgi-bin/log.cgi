#!/bin/bash
. "$(dirname "$0")/lib_web_session.sh"
echo "Content-type: text/plain; charset=UTF-8"
echo "Cache-Control: no-cache"
echo ""

check_auth() {
    web_session_check_cookie "${HTTP_COOKIE:-}" && return 0
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
