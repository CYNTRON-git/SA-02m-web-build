#!/bin/bash
set -u
echo "--- watchdog-feed status ---"
systemctl status sa02m-watchdog-feed.service --no-pager 2>&1 | head -25
echo
echo "--- recent watchdog spam (last 20) ---"
journalctl -k --since '2 minutes ago' --no-pager 2>/dev/null | grep -i 'watchdog' | tail -20
echo
echo "--- /dev/watchdog users ---"
fuser /dev/watchdog 2>&1
fuser /dev/watchdog0 2>&1
ps -p $(fuser /dev/watchdog 2>/dev/null) -o pid,cmd 2>/dev/null
echo
echo "--- ntp / chrony availability ---"
apt-cache policy systemd-timesyncd 2>&1 | head -15
apt-cache policy chrony ntpsec ntp 2>&1 | grep -E '^(chrony|ntpsec|ntp|  Candidate)' | head -10
echo
echo "--- chrony quick test ---"
which chronyd 2>&1
which ntpdate 2>&1
which ntpdig 2>&1
echo
echo "--- hwclock ---"
hwclock --show 2>&1
hwclock -w 2>&1
hwclock -w -f /dev/rtc1 2>&1
ls -la /dev/rtc* 2>/dev/null
echo
echo "--- time-sources ---"
timedatectl 2>&1
