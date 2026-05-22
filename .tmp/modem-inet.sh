IF=enx344b50000000

echo "=== ZTE статус после включения интернета ==="
curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=network_type' 2>/dev/null
echo
curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=ppp_status' 2>/dev/null
echo
curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=wan_ipaddr' 2>/dev/null
echo
curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=rssi' 2>/dev/null
echo

echo
echo "=== Маршруты ==="
ip route

echo
echo "=== curl через модемный интерфейс → внешний IP ==="
curl -s --max-time 10 --interface "$IF" http://ipinfo.io/ip 2>&1
echo

echo
echo "=== ping 8.8.8.8 через модемный интерфейс ==="
curl -s --max-time 8 --interface "$IF" http://1.1.1.1/ 2>&1 | head -3
# ping через source address
ip route get 8.8.8.8 from 192.168.0.169 2>/dev/null
