IF=enx344b50000000
GW_MODEM=192.168.0.1
GW_ETH=192.168.1.1

echo "=== ip route get 8.8.8.8 (какой маршрут используется) ==="
ip route get 8.8.8.8 2>&1

echo
echo "=== Меняем метрику eth0-default с 0 на 200, modем остаётся на 100 ==="
ip route del default via "$GW_ETH" dev eth0 2>/dev/null || ip route del default dev eth0 2>/dev/null || true
ip route add default via "$GW_ETH" dev eth0 metric 200
echo "  eth0 default: metric 200"

echo
echo "=== Маршруты ==="
ip route

echo
echo "=== ip route get 8.8.8.8 теперь ==="
ip route get 8.8.8.8 2>&1

echo
echo "=== ping 8.8.8.8 через модем ==="
ping -c3 -W4 8.8.8.8 2>&1
echo "ping exit=$?"

echo
echo "=== curl ip через модем ==="
curl -s --max-time 10 http://ifconfig.me/ip 2>/dev/null && echo
curl -s --max-time 10 http://2ip.ru/ 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true
