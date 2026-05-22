#!/bin/bash
set +e
echo "=== mplc4 init.d / unit ==="
ls -la /etc/init.d/mplc4 /etc/systemd/system/mplc4.service 2>&1
cat /etc/systemd/system/mplc4.service 2>/dev/null | head -40
echo
echo "=== mplc4 binary ==="
ls -la /opt/mplc* /usr/local/bin/mplc* 2>/dev/null
which mplc mplc4 mplc_monitor 2>&1
echo
echo "=== systemd .conf.d  ==="
ls -la /etc/systemd/system.conf.d/ 2>/dev/null
grep -RE 'RuntimeWatchdog|RebootWatchdog' /etc/systemd/system.conf.d/ 2>/dev/null
echo
echo "=== current systemd values ==="
systemctl show -p RuntimeWatchdogUSec -p RebootWatchdogUSec -p WatchdogDevice -p ShutdownWatchdogUSec -p KexecWatchdogUSec 2>&1
echo
echo "=== systemd daemon-reload status ==="
systemctl daemon-reload 2>&1
systemctl daemon-reexec 2>&1
systemctl show -p RuntimeWatchdogUSec -p RebootWatchdogUSec 2>&1
echo
echo "=== boot times ==="
last -x | head -10
