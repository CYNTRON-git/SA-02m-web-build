#!/bin/bash
set -u
SSH_OPTS='-o ControlMaster=no -o ControlPath=none -o ConnectTimeout=3'

echo "Before reboot: $(date '+%H:%M:%S.%3N')"
# Закроем любые ControlMaster-каналы — иначе порт 22 "виден" ещё после reboot.
ssh -O exit sa02m 2>/dev/null || true
rm -f /tmp/sa02m-ssh-*

ssh $SSH_OPTS sa02m 'fake-hwclock save 2>/dev/null; sync; (sleep 0.5; systemctl --force --no-wall reboot) >/dev/null 2>&1 &' 2>/dev/null || true
START_NS=$(date +%s%N)
echo "Sent reboot at $(date '+%H:%M:%S.%3N')"
echo "waiting for SSH port 22 to drop..."

# Ждём пока порт перестанет открываться (т.е. начался shutdown).
DOWN_NS=0
for i in $(seq 1 100); do
    if ! nc -w1 -z 192.168.1.136 22 >/dev/null 2>&1; then
        DOWN_NS=$(date +%s%N)
        break
    fi
done
if [ "$DOWN_NS" -eq 0 ]; then
    echo "WARNING: SSH port still open after 100 probes"
    DOWN_NS=$(date +%s%N)
fi
DOWN_SEC=$(awk -v a=$DOWN_NS -v b=$START_NS 'BEGIN { printf "%.1f", (a-b)/1e9 }')
echo "[+${DOWN_SEC}s] SSH port closed (device down)"

# Ждём возврата.
while ! nc -w1 -z 192.168.1.136 22 >/dev/null 2>&1; do sleep 0.2; done
UP_NS=$(date +%s%N)
FULL=$(awk -v a=$UP_NS -v b=$START_NS 'BEGIN { printf "%.1f", (a-b)/1e9 }')
REBOOT=$(awk -v a=$UP_NS -v b=$DOWN_NS 'BEGIN { printf "%.1f", (a-b)/1e9 }')
echo "[+${FULL}s] SSH port open; full-cycle = ${FULL}s, device-offline = ${REBOOT}s"

echo "---"
ssh $SSH_OPTS sa02m 'uptime; systemd-analyze; date; echo ---; journalctl -b -1 --no-pager 2>/dev/null | tail -30 | grep -iE "stop|shutdown|reached target|kill" || journalctl -b -1 --no-pager 2>/dev/null | tail -15'
