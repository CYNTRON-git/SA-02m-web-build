echo "=== dmesg: link events за последние 2 минуты ==="
dmesg --since "2 min ago" 2>/dev/null | grep -iE 'eth0.*link|link.*eth0|Link is' || echo "  НЕТ link-down событий (хорошо!)"

echo
echo "=== fix-eth.log: последние 15 строк ==="
tail -15 /var/log/fix-eth.log 2>/dev/null

echo
echo "=== Текущие маршруты ==="
ip route

echo
echo "=== ping 8.8.8.8 (должен идти через модем) ==="
ping -c3 -W4 8.8.8.8 && echo "INET-OK" || echo "INET-FAIL"

echo
echo "=== Внешний IP ==="
curl -s --max-time 6 http://ipinfo.io/ip 2>/dev/null && echo
