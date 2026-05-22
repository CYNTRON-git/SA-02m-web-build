#!/bin/bash
set -u
HOST=${HOST:-192.168.1.136}
SSH="ssh -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=4"

echo "=== Triggering reboot at $(date '+%H:%M:%S.%3N') ==="
$SSH sa02m 'fake-hwclock save; sync; (sleep 1; systemctl --no-wall reboot) >/dev/null 2>&1 &' || true

START=$(date +%s)
LAST=open
echo "=== Watching SSH on $HOST ==="
for i in $(seq 1 240); do
    if nc -w1 -z "$HOST" 22 >/dev/null 2>&1; then
        STATE=open
    else
        STATE=closed
    fi
    if [ "$STATE" != "$LAST" ]; then
        NOW=$(date +%s)
        echo "[+$((NOW - START))s $(date +%H:%M:%S)] $LAST -> $STATE"
        LAST=$STATE
        if [ "$STATE" = "open" ] && [ "$NOW" -gt "$((START + 5))" ]; then
            # Came back up — exit
            break
        fi
    fi
    sleep 1
done

echo
echo "=== Post-reboot state ==="
sleep 2
$SSH sa02m 'uptime; date; systemd-analyze; echo ---; systemctl --failed --no-pager; echo ---; ls -la /etc/fake-hwclock.data; cat /etc/fake-hwclock.data; echo ---; systemctl is-active ssh chrony fake-hwclock net-watchdog' 2>&1
