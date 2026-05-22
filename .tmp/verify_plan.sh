#!/bin/bash
# Полная проверка 7 пунктов плана system hardening на доноре.
set +e

pass=0
fail=0
check() {
    local desc=$1 cond=$2
    if eval "$cond"; then
        printf '  [PASS] %s\n' "$desc"
        pass=$((pass + 1))
    else
        printf '  [FAIL] %s\n' "$desc"
        fail=$((fail + 1))
    fi
}

echo "=== [1] Старый sa02m-watchdog-feed снят, /dev/watchdog свободен от bash ==="
check "watchdog-feed inactive"  '[ "$(systemctl is-active sa02m-watchdog-feed.service 2>&1)" != "active" ]'
check "watchdog-feed disabled" '[ "$(systemctl is-enabled sa02m-watchdog-feed.service 2>&1)" != "enabled" ]'
check "no bash feeder process" '! pgrep -fa "sa02m-watchdog-feed" >/dev/null 2>&1'

echo
echo "=== [2] systemd PID1 владеет HW WDT, RuntimeWatchdogSec=15s/RebootWatchdog=2min ==="
RT=$(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null)
RB=$(systemctl show -p RebootWatchdogUSec  --value 2>/dev/null)
WDEV=$(systemctl show -p WatchdogDevice    --value 2>/dev/null)
echo "  running: RuntimeWatchdog=$RT  Reboot=$RB  Device=$WDEV"
check "/dev/watchdog0 owned by PID1" 'dmesg | grep -q "systemd\[1\]: Using hardware watchdog"'
check "config file 15s" 'grep -q "RuntimeWatchdogSec=15s" /etc/systemd/system.conf.d/sa02m-watchdog.conf 2>/dev/null'
check "config file 2min" 'grep -q "RebootWatchdogSec=2min" /etc/systemd/system.conf.d/sa02m-watchdog.conf 2>/dev/null'

echo
echo "=== [3] /usr/local/sbin/sa02m-userspace-watchdog ==="
check "binary exists" '[ -x /usr/local/sbin/sa02m-userspace-watchdog ]'
check "REQUIRED_PROCS без mplc4" 'grep -q "REQUIRED_PROCS=\"\${REQUIRED_PROCS:-nginx sshd}\"" /usr/local/sbin/sa02m-userspace-watchdog'

echo
echo "=== [4] sa02m-userspace-watchdog.service active+enabled ==="
check "active" '[ "$(systemctl is-active sa02m-userspace-watchdog.service 2>&1)" = "active" ]'
check "enabled" '[ "$(systemctl is-enabled sa02m-userspace-watchdog.service 2>&1)" = "enabled" ]'

echo
echo "=== [5] serial-getty@ttyS0 (115200) активен ==="
check "active" '[ "$(systemctl is-active serial-getty@ttyS0.service 2>&1)" = "active" ]'
check "enabled" '[ "$(systemctl is-enabled serial-getty@ttyS0.service 2>&1)" = "enabled" ]'

echo
echo "=== [6] sa02m-failure-monitor active+enabled ==="
check "active" '[ "$(systemctl is-active sa02m-failure-monitor.service 2>&1)" = "active" ]'
check "enabled" '[ "$(systemctl is-enabled sa02m-failure-monitor.service 2>&1)" = "enabled" ]'

echo
echo "=== [7] HW WDT принадлежит systemd, dmesg чистый, всё работает ==="
check "no 'watchdog did not stop' spam in last 200 dmesg" \
    '! dmesg 2>/dev/null | tail -200 | grep -q "watchdog did not stop"'
check "ssh listening on :22" 'ss -ltn 2>/dev/null | awk "{print \$4}" | grep -q ":22$"'
check "nginx listening on :9999" 'ss -ltn 2>/dev/null | awk "{print \$4}" | grep -q ":9999$"'
check "eth0 link up" '[ "$(cat /sys/class/net/eth0/operstate)" = "up" ]'

echo
echo "─────────────────────────────────────────────"
echo "  TOTAL: PASS=$pass  FAIL=$fail"
echo "─────────────────────────────────────────────"
exit "$fail"
