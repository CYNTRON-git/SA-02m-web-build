#!/bin/bash
set +u
echo "=== watchdog spam check (last 2 minutes) ==="
journalctl -k --since '2 minutes ago' --no-pager 2>/dev/null | grep -c 'watchdog did not stop'
echo
echo "=== storage-mount log ==="
journalctl -u storage-mount@sda.service -b --no-pager 2>/dev/null | tail -15
echo
echo "=== storage-mount unit ==="
cat /etc/systemd/system/storage-mount@.service 2>/dev/null
echo
echo "=== userspace-watchdog (unknown unit) ==="
systemctl cat sa02m-userspace-watchdog.service 2>/dev/null
echo
echo "=== last 5 boots ==="
journalctl --list-boots --no-pager 2>/dev/null | tail -10
echo
echo "=== boot -1 last kernel messages ==="
journalctl -k -b -1 --no-pager 2>/dev/null | tail -5
echo
echo "=== ethtool link timing for eth0 ==="
ethtool eth0 2>/dev/null | grep -E "Speed|Duplex|Auto-neg|Link|Wake"
echo
echo "=== ssh start chain ==="
journalctl -u ssh.service -b --no-pager 2>/dev/null | tail -10
echo
echo "=== how soon was ssh available after boot ==="
# Найдём первый log "Started Ssh.service" и время от boot
journalctl --no-pager -b 2>/dev/null | head -20 | tail -10
echo
echo "=== chrony sources ==="
chronyc -n sources 2>&1 | head -10
chronyc -n tracking 2>&1 | head -15
echo
echo "=== logs spam in last 2 minutes ==="
journalctl --since '2 minutes ago' --no-pager 2>/dev/null | wc -l
echo "(messages count)"
echo
echo "=== fix-eth.log last 5 ==="
tail -5 /var/log/fix-eth.log 2>/dev/null
echo
echo "=== nodered residual ==="
journalctl --since '2 minutes ago' --no-pager 2>/dev/null | grep -ci nodered
echo "(nodered messages count)"
