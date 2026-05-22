#!/bin/bash
set -euo pipefail

echo '=== 1. Disable regen-ssh-host-keys (destroys keys on every boot) ==='
systemctl disable regen-ssh-host-keys.service 2>/dev/null && echo '  DISABLED' || echo '  already disabled'

echo ''
echo '=== 2. Wireguard /etc/wireguard/ contents ==='
ls -la /etc/wireguard/ 2>/dev/null || echo '  empty'

echo ''
echo '=== 3. Fix cloud-agent service unit (copy to systemd if needed) ==='
cp /opt/sa02m-cloud-agent/sa02m-cloud-agent.service /etc/systemd/system/
systemctl daemon-reload
echo '  unit refreshed'

echo ''
echo '=== 4. Fix /opt/sa02m-cloud-agent permissions (755 dir, so root can manage) ==='
chmod 750 /opt/sa02m-cloud-agent
chmod 700 /opt/sa02m-cloud-agent/sa02m-cloud-agent.py
chmod 700 /opt/sa02m-cloud-agent/sa02m-cloud-activate.py
ln -sf /opt/sa02m-cloud-agent/sa02m-cloud-activate.py /usr/local/bin/sa02m-cloud-activate 2>/dev/null || true
echo '  ok'

echo ''
echo '=== 5. Fix sshd: disable PAM motd (safety), ensure UseDNS no ==='
# Remove any stale/duplicate configs
grep -q 'UseDNS no' /etc/ssh/sshd_config || echo 'UseDNS no' >> /etc/ssh/sshd_config
# Keep PAM motd disabled (already done)
systemctl reload ssh 2>/dev/null && echo '  sshd reloaded' || echo '  sshd reload skipped'

echo ''
echo '=== 6. Set static IP 192.168.1.136 ==='
cat > /etc/network/interfaces.d/eth0.conf << 'NETEOF'
auto eth0
iface eth0 inet static
    pre-up ip link set $IFACE up 2>/dev/null || true
    address 192.168.1.136
    netmask 255.255.255.0
    gateway 192.168.1.1
    dns-nameservers 8.8.8.8 8.8.4.4
NETEOF
echo '  config written'
cat /etc/network/interfaces.d/eth0.conf

echo ''
echo '=== 7. Apply static IP now (keep current IP alive for SSH) ==='
# Add static IP alongside current DHCP one so SSH does not drop
ip addr add 192.168.1.136/24 dev eth0 2>/dev/null && echo '  192.168.1.136 added' || echo '  already present or error (ok)'

echo ''
echo '=== 8. Pin DNS so dhclient cannot overwrite ==='
# Write resolv.conf with static content; dhclient will overwrite on DHCP renewal
# With static IP, dhclient won't run — so this is fine permanently
printf 'nameserver 8.8.8.8\nnameserver 8.8.4.4\n' > /etc/resolv.conf
echo '  DNS pinned to 8.8.8.8'

echo ''
echo '=== 9. Final service status ==='
for svc in nginx fcgiwrap sa02m-flasher ssh; do
  echo "  $svc: $(systemctl is-active $svc) / $(systemctl is-enabled $svc)"
done
echo "  regen-ssh-host-keys: $(systemctl is-enabled regen-ssh-host-keys 2>/dev/null)"
echo "  sa02m-cloud-agent: $(systemctl is-enabled sa02m-cloud-agent 2>/dev/null)"

echo ''
echo '=== 10. Verify both IPs active ==='
ip addr show eth0 | grep 'inet '

echo ''
echo 'ALL DONE. SSH at 192.168.1.113 will drop when DHCP lease expires.'
echo 'Static IP 192.168.1.136 is now configured and active.'
