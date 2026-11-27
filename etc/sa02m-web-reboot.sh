#!/bin/sh
# The no-argument sudo entry admits a PID1-owned worker, never a CGI child.
set -u
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
LOG=/var/log/sa02m_install.log
SELF=/usr/local/sbin/sa02m-web-reboot.sh
LOCK=/run/sa02m-web-reboot.lock
SC=/usr/bin/systemctl
RUN=/usr/bin/systemd-run
TIMEOUT=/usr/bin/timeout

log() { printf '%s sa02m-web-reboot: %s\n' "$(date -Iseconds)" "$*" >&3; }
fail() { log "$*" || printf '%s\n' "$*" >&2; exit 1; }

mode=admit
case "$#:${1:-}" in
  0:) ;;
  1:--worker) mode=worker ;;
  2:--fallback)
    case "$2" in ''|*[!0-9]*) exit 2 ;; esac
    [ "${#2}" -le 3 ] && [ "$2" -ge 30 ] && [ "$2" -le 300 ] || exit 2
    delay=$2; mode=fallback ;;
  *) exit 2 ;;
esac
[ "$(id -u)" = 0 ] || exit 1
exec 3>>"$LOG" || exit 1
for bin in "$TIMEOUT" "$SC" "$RUN" /usr/bin/flock "$SELF"; do
  [ -x "$bin" ] || fail "missing executable: $bin"
done

force_reboot() {
  # At this point orderly dispatch failed or the separate 75s timer expired.
  log "forcing reboot: $1" || printf 'forcing reboot: %s\n' "$1" >&2
  "$TIMEOUT" -k 1 10 "$SC" reboot --force --force >&3 2>&1 && return 0
  "$TIMEOUT" -k 1 10 /sbin/reboot -f >&3 2>&1 && return 0
  if [ -w /proc/sysrq-trigger ]; then
    printf b > /proc/sysrq-trigger && return 0
  fi
  fail 'all force attempts failed'
}

case "$mode" in
  admit)
    log 'admission requested' || exit 1
    exec 9>"$LOCK" || fail 'admission lock open failed'
    /usr/bin/flock -w 2 9 || fail 'admission busy'
    # A fixed unit also refuses a second request while the worker is running.
    "$TIMEOUT" -k 1 10 "$RUN" --unit=sa02m-web-reboot --collect \
      --property=Type=exec --property=RuntimeMaxSec=75s --property=TimeoutStopSec=1s \
      "$SELF" --worker >&3 2>&1 || fail 'manager admission failed or unknown; no inline retry'
    exit 0 ;;
  worker)
    /bin/sleep 1
    log 'worker started' || exit 1
    "$TIMEOUT" -k 1 10 /bin/sync >&3 2>&1 || fail 'sync failed or timed out; reboot not dispatched'
    "$TIMEOUT" -k 1 10 "$RUN" --unit=sa02m-web-reboot-fallback --collect \
      --property=Type=exec --property=DefaultDependencies=no \
      --property=RuntimeMaxSec=115s --property=TimeoutStopSec=1s \
      "$SELF" --fallback 75 >&3 2>&1 || fail 'fallback admission failed or unknown; orderly reboot not dispatched'
    log 'fallback armed; requesting orderly reboot' || true
    "$TIMEOUT" -k 1 20 "$SC" reboot --no-block >&3 2>&1 && exit 0
    force_reboot 'orderly dispatch failed or timed out' ;;
  fallback)
    /bin/sleep "$delay"
    log "graceful reboot not finished in $delay s" || true
    "$TIMEOUT" -k 1 10 /bin/sync >&3 2>&1 || true
    force_reboot 'fallback deadline elapsed' ;;
esac
