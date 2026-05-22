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
QS="${QUERY_STRING:-}"

_json_status() {
  local status="${1:-idle}"
  local log_tail=""
  if [ -f "$LOGFILE" ]; then
    log_tail=$(tail -30 "$LOGFILE" 2>/dev/null | \
      sed 's/\\/\\\\/g; s/"/\\"/g; s/$/\\n/' | tr -d '\r\n')
    log_tail="${log_tail%\\n}"
  fi
  local lock_alive=0
  if [ -f "$LOCKFILE" ]; then
    local pid; pid=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      lock_alive=1
    fi
  fi
  [ "$lock_alive" = "1" ] && status="running"
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
  pid=$(cat "$LOCKFILE" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    _json_status "running"
    exit 0
  fi
  rm -f "$LOCKFILE"
fi

printf 'running' > "$STATUS_FILE" 2>/dev/null || true
chmod 644 "$STATUS_FILE" 2>/dev/null || true

if ! command -v sudo >/dev/null 2>&1; then
  printf '{"status":"error","log":"sudo не найден"}\n'
  printf 'error' > "$STATUS_FILE" 2>/dev/null || true
  exit 0
fi

# Запускаем в фоне; sudo пишет stdout/stderr в LOGFILE
sudo -n /usr/local/sbin/sa02m-web-update-apply >> "$LOGFILE" 2>&1 &
BGPID=$!

# Небольшая пауза — проверяем что процесс не упал мгновенно
sleep 0.5
if kill -0 "$BGPID" 2>/dev/null || [ -f "$LOCKFILE" ]; then
  printf '{"status":"running","log":"Обновление запущено (pid %d)"}\n' "$BGPID"
else
  status="error"
  [ -f "$STATUS_FILE" ] && status=$(tr -d ' \r\n' < "$STATUS_FILE" 2>/dev/null || echo "error")
  _json_status "$status"
fi
