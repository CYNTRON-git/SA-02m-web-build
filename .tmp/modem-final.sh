IF=enx344b50000000

echo "=== Состояние интерфейса ==="
ip addr show "$IF" 2>/dev/null || echo "интерфейс отсутствует"
ip route

echo
echo "=== Настраиваем маршруты если нужно ==="
# Поднимаем интерфейс
ip link set "$IF" up 2>/dev/null

# Запускаем DHCP если нет IP
if ! ip addr show "$IF" 2>/dev/null | grep -q 'inet 192.168'; then
    echo "  нет IP — запускаем dhclient"
    pkill -f "dhclient.*$IF" 2>/dev/null; sleep 0.3
    dhclient -4 -nw -pf "/run/dhclient.${IF}.pid" -lf "/var/lib/dhcp/dhclient.${IF}.leases" "$IF" 2>&1
    sleep 5
fi

# Меняем метрику eth0-default на 200
if ip route | grep -q 'default via 192.168.1.1'; then
    ip route del default via 192.168.1.1 dev eth0 2>/dev/null || true
    ip route del default dev eth0 2>/dev/null || true
    ip route add default via 192.168.1.1 dev eth0 metric 200
    echo "  eth0 default → metric 200"
fi

# Добавить modем-default с metric 100 если нет
if ! ip route | grep -q "default.*$IF"; then
    ip route add default via 192.168.0.1 dev "$IF" metric 100
    echo "  modем default → metric 100"
fi

echo
echo "=== Финальные маршруты ==="
ip route

echo
echo "=== ping 8.8.8.8 ==="
ping -c3 -W4 8.8.8.8 && echo INET-OK || echo INET-FAIL

echo
echo "=== Внешний IP через модем ==="
curl -s --max-time 10 http://ipinfo.io/ip 2>/dev/null && echo

echo
echo "=== ModemManager ==="
mmcli -L 2>/dev/null
