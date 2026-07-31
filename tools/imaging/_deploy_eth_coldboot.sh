#!/bin/bash
set -euo pipefail
chmod 755 /usr/local/sbin/sa02m-eth-coldboot.sh /usr/local/bin/fix-eth.sh
systemctl daemon-reload
systemctl enable sa02m-eth-coldboot.service
systemctl disable sa02m-eth1-coldboot.service 2>/dev/null || true
systemctl unmask net-watchdog.service 2>/dev/null || true
systemctl restart net-watchdog.service
# Run coldboot once now (no-op if carrier already up)
/usr/local/sbin/sa02m-eth-coldboot.sh || true
echo VERIFY
systemctl is-enabled sa02m-eth-coldboot net-watchdog 2>&1
systemctl is-active net-watchdog
ip -br link | head -5
cat /sys/class/net/eth0/carrier /sys/class/net/eth0/operstate
