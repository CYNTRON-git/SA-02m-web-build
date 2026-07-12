#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
echo "Content-type: text/plain; charset=UTF-8"
echo "Content-Disposition: attachment; filename=\"sa02m_journal.txt\""
echo "Cache-Control: no-store"
echo ""

web_session_check_cookie || {
  echo "Нет доступа"
  exit 0
}

LOG_FILE="/var/log/sa02m_install.log"
if [ -f "$LOG_FILE" ]; then
  cat "$LOG_FILE"
else
  echo "Журнал пуст или недоступен"
fi
