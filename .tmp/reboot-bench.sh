#!/bin/bash
set -u
echo "Before reboot: $(date)"
ssh sa02m 'fake-hwclock save 2>/dev/null; sync; (sleep 1; reboot) >/dev/null 2>&1 &' 2>/dev/null
START_NS=$(date +%s%N)
echo "Sent reboot at $(date +%H:%M:%S.%N)"
echo "waiting for SSH port 22 to drop..."

while nc -w1 -z 192.168.1.136 22 >/dev/null 2>&1; do sleep 0.3; done
DOWN_NS=$(date +%s%N)
DOWN_SEC=$(awk -v a=$DOWN_NS -v b=$START_NS 'BEGIN { printf "%.1f", (a-b)/1e9 }')
echo "[+${DOWN_SEC}s] SSH port closed (device rebooting)"

while ! nc -w1 -z 192.168.1.136 22 >/dev/null 2>&1; do sleep 0.3; done
UP_NS=$(date +%s%N)
FULL=$(awk -v a=$UP_NS -v b=$START_NS 'BEGIN { printf "%.1f", (a-b)/1e9 }')
REBOOT=$(awk -v a=$UP_NS -v b=$DOWN_NS 'BEGIN { printf "%.1f", (a-b)/1e9 }')
echo "[+${FULL}s] SSH port open again; reboot-to-ssh = ${REBOOT}s"

echo "---"
echo "Post-reboot state:"
ssh sa02m 'uptime; systemd-analyze; date; systemctl --failed --no-pager'
