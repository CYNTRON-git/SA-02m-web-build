IF=enx344b50000000
GW=192.168.0.1

echo "=== Текущие маршруты ==="
ip route

echo
echo "=== Ждём завершения dhclient (max 10с) ==="
for i in $(seq 1 10); do
    # проверяем что dhclient уже применил lease (IP присутствует)
    ip addr show "$IF" | grep -q 'inet 192.168' && break
    sleep 1
done
ip route

echo
echo "=== Добавляем маршрут через ZTE (metric 100 — ниже eth0) ==="
# Удалить старый default через модем если есть
ip route del default dev "$IF" 2>/dev/null || true
# Добавить с метрикой 100 (eth0 имеет по умолчанию 0 или выше)
ip route add default via "$GW" dev "$IF" metric 100 2>/dev/null || true
echo "  route added"

echo
echo "=== Маршруты после ==="
ip route

echo
echo "=== ping 8.8.8.8 через модем ==="
# Используем source-адрес с интерфейса модема
ping -c3 -W4 -s 56 8.8.8.8 2>&1
echo "ping exit=$?"

echo
echo "=== DNS через ZTE ==="
# ZTE DHCP обычно передаёт DNS
cat /etc/resolv.conf 2>/dev/null | head -5
# Пробуем получить lease-info
grep -A5 'option domain-name-servers' /var/lib/dhcp/dhclient.${IF}.leases 2>/dev/null | head -5 || echo "  (нет lease-файла)"

echo
echo "=== curl http через модем ==="
curl -s --max-time 10 --interface "$IF" http://ifconfig.me/ip 2>/dev/null && echo || echo "  (нет ответа)"
