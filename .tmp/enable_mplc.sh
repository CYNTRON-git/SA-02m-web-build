#!/bin/bash
# Включить mplc4 на устройстве, не трогая наш пересобранный mplc_cyntron.so.
set +e

echo "=== sha нашего пересобранного драйвера ==="
sha256sum /opt/mplc4/mplc_cyntron.so
ls -la /opt/mplc4/mplc_cyntron.so

echo
echo "=== остановить ручной запуск (если был) ==="
/etc/init.d/mplc4 stop 2>&1 | tail -3
sleep 2

echo
echo "=== systemctl enable --now mplc4 ==="
systemctl enable mplc4.service 2>&1
systemctl start  mplc4.service 2>&1
sleep 5

echo
echo "=== state ==="
systemctl status mplc4 --no-pager 2>&1 | head -20
echo
echo "is-enabled: $(systemctl is-enabled mplc4.service)"
echo "is-active : $(systemctl is-active  mplc4.service)"

echo
echo "=== процессы ==="
ps -ef | grep -E "mplc_daemon|mplc_monitor|masterplc" | grep -v grep | head -10

echo
echo "=== драйвер cyntron не пострадал? ==="
sha256sum /opt/mplc4/mplc_cyntron.so

echo
echo "=== userspace watchdog лог (mplc4 переход missing -> running) ==="
sleep 15
tail -15 /var/log/sa02m_userspace_watchdog.log
