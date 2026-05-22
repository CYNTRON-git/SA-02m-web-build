echo "=== ZTE redirect target ==="
curl -sL --max-time 5 -D - http://192.168.0.1/ 2>/dev/null | head -30

echo
echo "=== ZTE /goform/getStatus ==="
curl -s --max-time 5 'http://192.168.0.1/goform/getStatus' 2>/dev/null | head -20

echo
echo "=== ZTE /goform/get_status_info ==="
curl -s --max-time 5 'http://192.168.0.1/goform/get_status_info?info=multiStatus' 2>/dev/null | head -20

echo
echo "=== ZTE /goform/goform_get_cmd_process?isTest=false&cmd=network_type,rssi,wan_ipaddr,wan_active_band ==="
curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=network_type,rssi,wan_ipaddr,wan_active_band,modem_main_state,ppp_status' 2>/dev/null

echo
echo
echo "=== Traceroute до 8.8.8.8 (только 5 хопов) ==="
traceroute -m5 -w2 -n 8.8.8.8 2>&1 | head -10
