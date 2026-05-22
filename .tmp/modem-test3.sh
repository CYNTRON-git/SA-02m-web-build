IF=enx344b50000000

echo "=== Все udev-свойства интерфейса ==="
udevadm info --query=all --name "$IF" 2>/dev/null | head -40

echo
echo "=== /dev/ttyUSB* после инициализации ==="
ls -la /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "  (нет)"

echo
echo "=== ZTE web UI ping (192.168.0.1) ==="
ip link set "$IF" up 2>/dev/null
# Пробуем добавить link-local адрес для проверки
ip addr add 192.168.0.100/24 dev "$IF" 2>/dev/null || true
sleep 2
ip addr show "$IF"
ping -c2 -W2 192.168.0.1 2>&1 || echo "  (нет ответа)"

echo
echo "=== carrier status ==="
cat /sys/class/net/"$IF"/carrier 2>/dev/null && echo "  (1=link up)" || echo "  нет carrier"
cat /sys/class/net/"$IF"/operstate 2>/dev/null

echo
echo "=== Убираем тестовый IP ==="
ip addr del 192.168.0.100/24 dev "$IF" 2>/dev/null || true

echo
echo "=== Ожидаем carrier (до 30с) ==="
for i in $(seq 1 30); do
    carrier=$(cat /sys/class/net/"$IF"/carrier 2>/dev/null)
    [ "$carrier" = "1" ] && { echo "  carrier UP на ${i}с"; break; }
    [ $((i % 5)) -eq 0 ] && echo "  ... ${i}с"
    sleep 1
done

echo
echo "=== DHCP без -timeout ==="
ip addr flush dev "$IF" 2>/dev/null || true
dhclient -v -4 -pf "/run/dhclient.${IF}.pid" \
         -lf "/var/lib/dhcp/dhclient.${IF}.leases" \
         -nw "$IF" 2>&1
sleep 5
ip addr show "$IF"
ip route

echo
echo "=== ping 8.8.8.8 ==="
ping -c3 -W3 8.8.8.8 2>&1 || echo "FAIL"
