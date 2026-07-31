#!/bin/bash
set -euo pipefail
chmod 755 /usr/local/sbin/sa02m-rootfs-expand.sh

for u in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog; do
  if [ -L "/etc/systemd/system/${u}.service" ] \
     && [ "$(readlink -f "/etc/systemd/system/${u}.service")" = "/dev/null" ]; then
    rm -f "/etc/systemd/system/${u}.service"
  fi
  systemctl unmask "${u}.service" 2>/dev/null || true
  systemctl enable "${u}.service" 2>/dev/null || true
done

systemctl stop armbian-resize-filesystem 2>/dev/null || true
systemctl disable armbian-resize-filesystem 2>/dev/null || true
systemctl mask armbian-resize-filesystem 2>/dev/null || true
systemctl daemon-reload
systemctl reset-failed ifupdown-pre.service 2>/dev/null || true
systemctl restart net-watchdog.service
systemctl start sa02m-userspace-watchdog.service sa02m-failure-monitor.service 2>/dev/null || true
/usr/local/bin/fix-eth.sh eth0 || true
sleep 2

echo "=== VERIFY ==="
df -hT /
systemctl is-active sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog networking ssh nginx || true
systemctl is-enabled sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog 2>&1 || true
systemctl is-enabled armbian-resize-filesystem 2>&1 || true
ip -br link
ip -4 -br addr
grep -E RuntimeWatchdog /etc/systemd/system.conf.d/sa02m-watchdog.conf || true
systemctl is-system-running || true
systemctl --failed --no-pager | head -15 || true
tail -30 /var/log/fix-eth.log || true
