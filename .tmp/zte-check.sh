IF=enx344b50000000

echo "=== ping ZTE web UI ==="
ping -c2 -W2 192.168.0.1 2>&1

echo
echo "=== ZTE /api/device/information ==="
curl -s --max-time 5 http://192.168.0.1/api/device/information 2>/dev/null | head -20

echo
echo "=== ZTE /api/monitoring/status ==="
curl -s --max-time 5 http://192.168.0.1/api/monitoring/status 2>/dev/null

echo
echo "=== ZTE /api/net/current-plmn ==="
curl -s --max-time 5 http://192.168.0.1/api/net/current-plmn 2>/dev/null

echo
echo "=== ZTE web root ==="
curl -s --max-time 5 -o /dev/null -w "HTTP %{http_code} %{url_effective}\n" http://192.168.0.1/ 2>/dev/null

echo
echo "=== ZTE /api/wlan/basic-settings ==="
curl -s --max-time 5 http://192.168.0.1/api/wlan/basic-settings 2>/dev/null | head -10
