dpkg -l modemmanager ppp 2>/dev/null | awk '/^ii/{print $2,$3}'
which mmcli pppd 2>/dev/null || echo "not found"
systemctl is-active ModemManager.service 2>/dev/null
ping -c1 -W2 8.8.8.8 >/dev/null 2>&1 && echo INET-OK || echo INET-FAIL
