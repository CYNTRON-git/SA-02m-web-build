echo "=== Процессы dhclient ==="
pgrep -a dhclient 2>/dev/null || echo "  нет"

echo
echo "=== Состояние sa02m-modem-dhcp ==="
systemctl status 'sa02m-modem-dhcp@*.service' --no-pager 2>/dev/null | grep -E 'Active|Loaded|Main PID' | head -6

echo
echo "=== net-watchdog: последние 20 строк лога ==="
tail -20 /var/log/fix-eth.log 2>/dev/null || echo "(нет лога)"

echo
echo "=== Периодические задачи (systemd timers) ==="
systemctl list-timers --no-pager 2>/dev/null | head -15

echo
echo "=== dmesg: последние события eth0/сеть ==="
dmesg --since "10 min ago" 2>/dev/null | grep -iE 'eth0|link|carrier|phy|reset|timeout' | tail -15 || \
  dmesg | grep -iE 'eth0|link|carrier|phy' | tail -15

echo
echo "=== ARP кэш ==="
ip neigh show dev eth0 2>/dev/null

echo
echo "=== Текущие маршруты ==="
ip route
