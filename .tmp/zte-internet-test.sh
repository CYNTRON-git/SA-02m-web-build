IF=enx344b50000000

echo "=== curl через модемный интерфейс ==="
curl -s --max-time 8 --interface "$IF" http://ipinfo.io/ip 2>&1
echo

echo "=== ZTE SIM и статус ==="
curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=sim_imsi' 2>/dev/null
echo
curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=network_type' 2>/dev/null
echo
curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=ppp_status' 2>/dev/null
echo
curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=modem_main_state' 2>/dev/null
echo

echo "=== Проверка системы (sa02m-modem-dhcp@) ==="
systemctl status 'sa02m-modem-dhcp@enx344b50000000.service' --no-pager 2>/dev/null | head -10

echo
echo "=== Итог маршрутов ==="
ip route
