#!/bin/bash
# Live recovery after expand oneshot timeout / watchdog not started.
set -euo pipefail
install -m 755 /usr/local/sbin/sa02m-rootfs-expand.sh
# unit already uploaded separately
systemctl daemon-reload
systemctl reset-failed sa02m-rootfs-expand.service 2>/dev/null || true
# expand already done
touch /var/lib/sa02m-rootfs-expand.done
systemctl disable sa02m-rootfs-expand.service 2>/dev/null || true
systemctl mask armbian-resize-filesystem.service 2>/dev/null || true

for u in net-watchdog sa02m-userspace-watchdog sa02m-failure-monitor; do
  if [ -L "/etc/systemd/system/${u}.service" ] \
     && [ "$(readlink "/etc/systemd/system/${u}.service" 2>/dev/null)" = "/dev/null" ]; then
    rm -f "/etc/systemd/system/${u}.service"
  fi
  systemctl unmask "${u}.service" 2>/dev/null || true
  systemctl enable "${u}.service" 2>/dev/null || true
  systemctl restart "${u}.service" 2>/dev/null || true
done

systemctl reset-failed 2>/dev/null || true
sleep 1
echo VERIFY
df -hT /
systemctl is-system-running || true
systemctl is-active net-watchdog sa02m-userspace-watchdog sa02m-failure-monitor networking ssh nginx
systemctl --failed --no-pager | head -10
ip -4 -br addr
