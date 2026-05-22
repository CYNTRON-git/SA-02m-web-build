echo "=== Итоговая проверка USB ==="
echo
echo "--- /proc/mounts (USB и sdcard) ---"
grep -E 'sda|/media/' /proc/mounts
echo
echo "--- df -h ---"
df -h /media/usb /media/sdcard 2>/dev/null
echo
echo "--- ls /media/usb (первые 5) ---"
ls /media/usb 2>/dev/null | head -5
echo
echo "--- сервисы ---"
echo "  storage-mount@sda.service  = $(systemctl is-active storage-mount@sda.service 2>/dev/null)"
echo "  storage-mount@sda1.service = $(systemctl is-active storage-mount@sda1.service 2>/dev/null)"
echo
echo "--- failed units ---"
systemctl --failed --no-pager
echo
echo "--- что веб увидит ---"
if findmnt -n /media/usb >/dev/null 2>&1; then
    echo "  USB_M=1  -> веб: \"USB-накопитель установлен\""
    findmnt -n /media/usb
else
    echo "  USB_M=0  -> веб: \"USB-накопитель НЕ установлен\""
fi
echo
echo "--- log storage-mount этой загрузки ---"
journalctl -u 'storage-mount@*.service' -b --no-pager | tail -15
