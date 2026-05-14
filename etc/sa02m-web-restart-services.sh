#!/bin/sh
# root через sudo из restart.cgi. При сбое dbus systemctl молча не делает работу —
# пишем stderr в журнал и для nginx пробуем nginx -s reload.
LOG=/var/log/sa02m_install.log
SC=/usr/bin/systemctl
sync 2>/dev/null || true
echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-restart-services.sh: start" >>"$LOG"

try_sc() {
  _unit=$1
  _label=$2
  _out=$($SC restart "$_unit" 2>&1) || {
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-restart: systemctl restart $_label failed: $_out" >>"$LOG"
    return 1
  }
  echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-restart: systemctl restart $_label ok" >>"$LOG"
  return 0
}

try_sc networking networking || true

ok_nginx=0
try_sc nginx nginx && ok_nginx=1 || true
if [ "$ok_nginx" != 1 ]; then
  NGX=$(command -v nginx 2>/dev/null) || true
  [ -z "$NGX" ] && [ -x /usr/sbin/nginx ] && NGX=/usr/sbin/nginx
  [ -z "$NGX" ] && [ -x /sbin/nginx ] && NGX=/sbin/nginx
  if [ -n "$NGX" ] && [ -x "$NGX" ]; then
    if "$NGX" -s reload >>"$LOG" 2>&1; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-restart: nginx -s reload ok (fallback)" >>"$LOG"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-restart: nginx -s reload failed" >>"$LOG"
    fi
  fi
fi

try_sc fcgiwrap fcgiwrap || true
try_sc fix-eth.service fix-eth || true

echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-restart-services.sh: done" >>"$LOG"
exit 0
