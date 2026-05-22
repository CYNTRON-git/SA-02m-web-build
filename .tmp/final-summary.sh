#!/bin/bash
set +u
echo "===5MIN-JOURNAL-LINES==="
journalctl --since '5 minutes ago' --no-pager 2>/dev/null | wc -l
echo
echo "===TOP-NOISE==="
journalctl --since '5 minutes ago' --no-pager 2>/dev/null | awk '{$1=$2=$3=$4=""; print}' | sed 's/^ *//' | sort | uniq -c | sort -rn | head -10
echo
echo "===WATCHDOG-MESSAGES-IN-5MIN==="
journalctl --since '5 minutes ago' --no-pager 2>/dev/null | grep -ic watchdog
echo
echo "===CPU-TEMP==="
for z in /sys/class/thermal/thermal_zone*/temp; do
    t=$(cat "$z" 2>/dev/null)
    [ -n "$t" ] && awk -v t="$t" 'BEGIN { printf "  %.1fC\n", t/1000 }'
done
echo
echo "===NEW-FAILED==="
systemctl --failed --no-pager
echo
echo "===SSH-CONFIG-ACTIVE==="
sshd -T 2>/dev/null | grep -E '^(clientalive|tcpkeep|usedns|loginGraceTime|maxstartups|maxsessions)' | sort
echo
echo "===WATCHDOG-CONFIG-ACTIVE==="
systemctl show -p RuntimeWatchdogUSec,RebootWatchdogUSec,ShutdownWatchdogUSec,DefaultTimeoutStopUSec,DefaultTimeoutStartUSec
echo
echo "===CHRONY==="
chronyc -n sources 2>&1 | head -8
echo
echo "===FIX-ETH-LOG-RECENT==="
tail -5 /var/log/fix-eth.log 2>/dev/null
echo
echo "===SA02M-SERVICES==="
for u in ssh nginx chrony net-watchdog fix-eth fake-hwclock sa02m-userspace-watchdog sa02m-flasher sa02m-pre-start sa02m-watchdog-feed NetworkManager NetworkManager-wait-online nodered fcgiwrap; do
    st=$(systemctl is-active "$u" 2>/dev/null)
    en=$(systemctl is-enabled "$u" 2>/dev/null)
    printf '  %-32s active=%-10s enabled=%s\n' "$u" "$st" "$en"
done
