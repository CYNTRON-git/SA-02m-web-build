IF=enx344b50000000

echo "=== udev properties enx344b50000000 ==="
udevadm info --query=property --name "$IF" 2>/dev/null | grep -E '^(ID_BUS|ID_NET_DRIVER|DRIVER|ID_PATH|SUBSYSTEM|ID_VENDOR|ID_MODEL)'
echo
echo "=== sa02m-modem-dhcp service status ==="
systemctl status "sa02m-modem-dhcp@${IF}.service" --no-pager 2>/dev/null | head -15
echo
echo "=== Запускаем DHCP вручную ==="
ip link set "$IF" up
dhclient -v -4 -pf "/run/dhclient.${IF}.pid" \
         -lf "/var/lib/dhcp/dhclient.${IF}.leases" \
         -timeout 30 "$IF" 2>&1
echo "dhclient exit=$?"
echo
echo "=== IP и маршруты после DHCP ==="
ip addr show "$IF"
ip route
echo
echo "=== Пинг 8.8.8.8 через модем ==="
ping -c3 -W3 -I "$IF" 8.8.8.8 2>&1 || ping -c3 -W3 8.8.8.8 2>&1
echo
echo "=== mmcli -L ==="
mmcli -L 2>/dev/null
