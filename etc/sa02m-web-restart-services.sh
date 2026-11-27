#!/bin/sh
# PID1 owns the worker so restarting fcgiwrap cannot kill its executor.
set -u
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
LOG=/var/log/sa02m_install.log
SELF=/usr/local/sbin/sa02m-web-restart-services.sh
LOCK=/run/sa02m-web-restart.lock
SC=/usr/bin/systemctl
RUN=/usr/bin/systemd-run
TIMEOUT=/usr/bin/timeout

log() { printf '%s sa02m-web-restart: %s\n' "$(date -Iseconds)" "$*" >&3; }
fail() { log "$*" || printf '%s\n' "$*" >&2; exit 1; }

case "$#:${1:-}" in 0:) mode=admit ;; 1:--worker) mode=worker ;; *) exit 2 ;; esac
[ "$(id -u)" = 0 ] || exit 1
exec 3>>"$LOG" || exit 1
for bin in "$TIMEOUT" "$SC" "$RUN" /usr/bin/flock "$SELF"; do
  [ -x "$bin" ] || fail "missing executable: $bin"
done

if [ "$mode" = admit ]; then
  log 'admission requested' || exit 1
  exec 9>"$LOCK" || fail 'admission lock open failed'
  /usr/bin/flock -w 2 9 || fail 'admission busy'
  "$TIMEOUT" -k 1 10 "$RUN" --unit=sa02m-web-restart --collect \
    --property=Type=exec --property=RuntimeMaxSec=75s --property=TimeoutStopSec=1s \
    "$SELF" --worker >&3 2>&1 || fail 'manager admission failed or unknown; no inline retry'
  exit 0
fi

/bin/sleep 1
log 'worker started' || exit 1
for unit in nginx.service fcgiwrap.service; do
  "$TIMEOUT" -k 1 30 "$SC" restart "$unit" >&3 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # A client timeout never cancels PID1's job. Do not start a competing job.
    case "$rc" in 124|137) fail "$unit restart outcome unknown (client timeout)" ;; esac
    # Even a definite nginx failure gets a reload only after terminal job proof.
    if [ "$unit" = nginx.service ]; then
      state=$("$TIMEOUT" -k 1 3 "$SC" show --property=Job --property=ActiveState "$unit" 2>&3) \
        || fail 'nginx restart failed; manager state unknown'
      case "$state" in
        'ActiveState=active
Job='|'Job=
ActiveState=active')
          "$TIMEOUT" -k 1 3 /usr/sbin/nginx -s reload >&3 2>&1 \
            || fail 'nginx restart and reload failed'
          log 'nginx reload completed after terminal restart failure' || exit 1 ;;
        *) fail 'nginx restart failed; no active terminal state for reload' ;;
      esac
    else
      fail "fcgiwrap restart failed (rc=$rc)"
    fi
  fi
done
log 'done: nginx and fcgiwrap completed' || exit 1
