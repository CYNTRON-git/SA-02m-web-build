#!/bin/bash
set +e
echo "=== uname/uptime ==="
uname -nrm
uptime
echo
echo "=== service states ==="
for s in ssh nginx fcgiwrap mplc4 sa02m-flasher \
         sa02m-userspace-watchdog sa02m-failure-monitor \
         serial-getty@ttyS0 serial-getty@ttyGS0 \
         sa02m-watchdog-feed regen-ssh-host-keys; do
    a=$(systemctl is-active "$s" 2>&1)
    e=$(systemctl is-enabled "$s" 2>&1)
    printf '%-32s active=%-12s enabled=%s\n' "$s" "$a" "$e"
done
echo
echo "=== /etc/systemd/system.conf watchdog ==="
grep -E '^(Runtime|Reboot|Shutdown).*Watchdog' /etc/systemd/system.conf 2>/dev/null
echo
echo "=== systemctl show watchdog ==="
systemctl show --property=RuntimeWatchdogUSec,RebootWatchdogUSec,WatchdogDevice 2>/dev/null
echo
echo "=== userspace watchdog log tail ==="
tail -30 /var/log/sa02m_userspace_watchdog.log 2>/dev/null
echo
echo "=== failure-monitor log tail ==="
tail -20 /var/log/sa02m_failure_monitor.log 2>/dev/null
echo
echo "=== mplc4 status ==="
systemctl status mplc4 --no-pager 2>&1 | head -20
echo
echo "=== ip / link ==="
ip -br a
echo
echo "=== eth0 link state ==="
cat /sys/class/net/eth0/operstate 2>/dev/null
ethtool eth0 2>/dev/null | grep -E 'Speed|Duplex|Link detected'
echo
echo "=== dmesg watchdog tail ==="
dmesg 2>/dev/null | grep -iE 'watchdog|wdt|sunxi' | tail -10
