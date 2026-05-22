echo "=== lsusb ==="
lsusb 2>/dev/null
echo
echo "=== ip link ==="
ip link show
echo
echo "=== /dev/ttyUSB* /dev/ttyACM* /dev/modem* /dev/cdc* ==="
ls -la /dev/ttyUSB* /dev/ttyACM* /dev/modem* /dev/cdc-wdm* 2>/dev/null || echo "  (нет)"
echo
echo "=== ModemManager list ==="
mmcli -L 2>/dev/null
echo
echo "=== udev modem events (journal) ==="
journalctl -k --no-pager --since "5 min ago" 2>/dev/null | grep -iE 'usb|modem|ttyUSB|ttyACM|cdc|rndis|modeswitch' | tail -20
echo
echo "=== ip addr (все интерфейсы) ==="
ip addr show
echo
echo "=== lsmod USB net ==="
lsmod | grep -iE 'cdc|rndis|usbnet|option|zte|qmi' | head -10
