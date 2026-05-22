#!/bin/bash
echo "=== sources.list ==="
cat /etc/apt/sources.list 2>/dev/null
echo
echo "=== sources.list.d/ ==="
ls /etc/apt/sources.list.d/ 2>/dev/null && cat /etc/apt/sources.list.d/*.list 2>/dev/null || echo "(нет)"
echo
echo "=== apt-cache search modem ppp ==="
apt-cache search modemmanager 2>/dev/null | head -5
apt-cache search '^ppp$' 2>/dev/null | head -5
apt-cache show ppp 2>/dev/null | grep -E '^(Package|Version|Status)' | head -5
echo
echo "=== apt-cache policy ppp modemmanager ==="
apt-cache policy ppp modemmanager 2>/dev/null
echo
echo "=== eth1 detail ==="
udevadm info --query=property --name eth1 2>/dev/null | grep -E '^(ID_BUS|ID_MODEL|ID_VENDOR|ID_NET_DRIVER|ID_PATH|DRIVER|DEVPATH)' | head -15
echo
echo "=== lsusb если есть ==="
lsusb 2>/dev/null | head -20 || echo "  (lsusb нет)"
echo
echo "=== kernel modules USB net ==="
lsmod | grep -E 'cdc|rndis|asix|usbnet|option' || echo "  (нет загруженных)"
