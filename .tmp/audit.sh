#!/bin/bash
echo '=== SYSTEMD SERVICES ==='
for svc in nginx fcgiwrap sa02m-flasher sa02m-cloud-agent; do
  active=$(systemctl is-active $svc 2>/dev/null)
  enabled=$(systemctl is-enabled $svc 2>/dev/null)
  echo "  $svc: active=$active enabled=$enabled"
done

echo ''
echo '=== WEB UI PORT 9999 ==='
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:9999/

echo ''
echo '=== WIREGUARD ==='
wg --version
ls /etc/wireguard/ 2>/dev/null && echo "  configs present" || echo "  no configs yet (ok - needs cloud activation)"

echo ''
echo '=== CLOUD AGENT FILES ==='
ls -la /opt/sa02m-cloud-agent/
systemctl cat sa02m-cloud-agent 2>/dev/null | grep -E 'ExecStart|Restart|After'

echo ''
echo '=== SSH HOST KEYS ==='
ls /etc/ssh/ssh_host_* 2>/dev/null
grep -h 'UseDNS' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null

echo ''
echo '=== DNS ==='
cat /etc/resolv.conf | grep nameserver

echo ''
echo '=== CLOCK ==='
date
timedatectl status 2>/dev/null | grep -E 'Local time|NTP|synchronized'

echo ''
echo '=== ENABLED SERVICES (reboot survival) ==='
systemctl list-unit-files --state=enabled 2>/dev/null | grep -E 'nginx|fcgiwrap|sa02m|ssh'

echo ''
echo '=== CURRENT NET CONFIG ==='
cat /etc/network/interfaces.d/eth0.conf 2>/dev/null

echo ''
echo '=== APT SOURCES (https check) ==='
head -4 /etc/apt/sources.list
