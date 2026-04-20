#!/bin/bash
echo "Content-type: text/plain; charset=UTF-8"
echo "Content-Disposition: attachment; filename=\"sa02m_journal.txt\""
echo "Cache-Control: no-store"
echo ""

[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || {
  echo "Нет доступа"
  exit 0
}

LOG_FILE="/var/log/sa02m_install.log"
if [ -f "$LOG_FILE" ]; then
  cat "$LOG_FILE"
else
  echo "Журнал пуст или недоступен"
fi
