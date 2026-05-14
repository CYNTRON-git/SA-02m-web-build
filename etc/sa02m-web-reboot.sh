#!/bin/sh
# Вызывается от root: sudo -n /usr/local/sbin/sa02m-web-reboot.sh (www-data из reboot.cgi).
# При сбое dbus обычный reboot может не сработать — сначала -f, затем shutdown, systemctl.
LOG=/var/log/sa02m_install.log
sync 2>/dev/null || true

run() {
  if "$@" >>"$LOG" 2>&1; then
    exit 0
  fi
}

run /sbin/reboot -f
run /usr/sbin/reboot -f
run /usr/bin/reboot -f
run /sbin/reboot
run /usr/sbin/reboot
run /usr/bin/reboot
run /sbin/shutdown -r now
run /usr/sbin/shutdown -r now
run /usr/bin/systemctl reboot

echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-web-reboot.sh: all reboot attempts failed" >>"$LOG"
exit 1
