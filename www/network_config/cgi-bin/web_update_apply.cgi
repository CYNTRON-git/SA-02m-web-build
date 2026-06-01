#!/bin/bash
[[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] || {
  printf 'Content-type: application/json; charset=UTF-8\r\n\r\n'
  printf '{"error":"unauthorized"}\n'
  exit 0
}

STATEDIR=/var/lib/sa02m-web-build
LOCKFILE="$STATEDIR/update.lock"
STATUS_FILE="$STATEDIR/update_status"
LOGFILE="$STATEDIR/update.log"

METHOD="${REQUEST_METHOD:-GET}"

_json_status() {
  local status="${1:-idle}"
  local log_tail=""
  if [ -f "$LOGFILE" ]; then
    log_tail=$(tail -40 "$LOGFILE" 2>/dev/null | \
      sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n",$0}')
    log_tail="${log_tail%\\n}"
  fi
  # Живой процесс → принудительно "running"
  if [ -f "$LOCKFILE" ]; then
    local pid; pid=$(tr -d ' \r\n' < "$LOCKFILE" 2>/dev/null)
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      status="running"
    fi
  fi
  printf '{"status":"%s","log":"%s"}\n' "$status" "$log_tail"
}

printf 'Content-type: application/json; charset=UTF-8\r\n'
printf 'Cache-Control: no-cache\r\n\r\n'

if [ "$METHOD" = "GET" ]; then
  status="idle"
  [ -f "$STATUS_FILE" ] && status=$(tr -d ' \r\n' < "$STATUS_FILE" 2>/dev/null || echo "idle")
  _json_status "$status"
  exit 0
fi

# POST — запуск обновления
if [ -f "$LOCKFILE" ]; then
  pid=$(tr -d ' \r\n' < "$LOCKFILE" 2>/dev/null || echo "")
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    _json_status "running"
    exit 0
  fi
fi

if ! command -v sudo >/dev/null 2>&1; then
  printf '{"status":"error","log":"sudo не найден"}\n'
  exit 0
fi

# Запускаем обновление в фоне; скрипт сам создаёт lockfile и пишет в logfile
nohup sudo -n /usr/local/sbin/sa02m-web-update-apply >/dev/null 2>&1 &
BGPID=$!

sleep 1

# Проверяем что lockfile появился (скрипт стартовал) или процесс ещё жив
if [ -f "$LOCKFILE" ] || kill -0 "$BGPID" 2>/dev/null; then
  printf '{"status":"running","log":"Обновление запущено..."}\n'
else
  printf 'error' > "$STATUS_FILE" 2>/dev/null || true
  _json_status "error"
fi
