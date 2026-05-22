#!/bin/bash
set -u
SSH='ssh -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=4'

echo "=== Перед reboot ==="
$SSH sa02m 'mount | grep /media/usb' || echo "  (не смонтировано)"

echo
echo "=== Reboot ==="
$SSH sa02m 'fake-hwclock save; sync; (sleep 0.5; systemctl --no-wall reboot) >/dev/null 2>&1 &' || true

START=$(date +%s)
echo "Жду пока устройство уйдёт в reboot..."
for i in $(seq 1 60); do
    nc -w1 -z 192.168.1.136 22 >/dev/null 2>&1 || break
    sleep 1
done
DOWN=$(date +%s)
echo "[+$((DOWN - START))s] SSH порт закрыт"

echo "Жду возврата..."
for i in $(seq 1 240); do
    if nc -w1 -z 192.168.1.136 22 >/dev/null 2>&1; then
        UP=$(date +%s)
        echo "[+$((UP - START))s] SSH порт открыт; offline=$((UP - DOWN))s"
        break
    fi
    sleep 1
done
sleep 3

echo
echo "=== После reboot ==="
$SSH sa02m 'uptime; echo; echo --- /proc/mounts: --- ; grep -E "sda|/media" /proc/mounts; echo; echo --- df /media/usb: ---; df -h /media/usb 2>&1; echo; echo --- storage-mount log: ---; journalctl -u "storage-mount@*.service" -b --no-pager | tail -20; echo; echo --- failed: ---; systemctl --failed --no-pager'
