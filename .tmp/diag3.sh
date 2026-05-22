#!/bin/bash
set +e
echo "=== install fixed watchdog (REQUIRED_PROCS without mplc4) ==="
install -m 755 /tmp/sa02m-userspace-watchdog /usr/local/sbin/sa02m-userspace-watchdog
ls -la /usr/local/sbin/sa02m-userspace-watchdog

echo "=== fix system.conf.d drop-in (10s/4min -> 15s/2min) ==="
cat > /etc/systemd/system.conf.d/sa02m-watchdog.conf <<'EOF'
# SA-02m hardware watchdog policy (drop-in for /etc/systemd/system.conf).
# RuntimeWatchdogSec — PID1 пингует HW WDT каждые N/2 сек. 0 = выключено.
# RebootWatchdogSec — даём системе максимум N на корректный reboot,
#   затем kernel WDT делает hard reset.
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min
EOF
cat /etc/systemd/system.conf.d/sa02m-watchdog.conf

echo "=== restart userspace watchdog (apply new REQUIRED_PROCS) ==="
systemctl restart sa02m-userspace-watchdog.service
sleep 2
systemctl status sa02m-userspace-watchdog --no-pager 2>&1 | head -15
echo
echo "=== mplc4: сделать запуск best-effort (попробовать поднять, не падать если нельзя) ==="
systemctl is-enabled mplc4.service 2>&1
# Если ранее был enabled — оставим, иначе не трогаем (по дизайну этой платы).
echo
echo "=== userspace watchdog log tail ==="
sleep 6
tail -10 /var/log/sa02m_userspace_watchdog.log 2>/dev/null
echo
echo "=== reboot timer check ==="
last -x | head -5
