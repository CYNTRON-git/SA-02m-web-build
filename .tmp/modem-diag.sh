#!/bin/bash
echo "=== Установленные пакеты: modem/ppp/wwan ==="
dpkg -l 2>/dev/null | awk '/modem|ppp|wwan|modeswitch|usb.mode|gammu|sakis|umbim|umbdk|qmi|mbim|comgt|wvdial|ofono|NetworkManager/ && /^ii/' | grep -v node || echo "  (нет)"

echo
echo "=== Установленные утилиты ==="
for b in pppd pppoe chat wvdial usb_modeswitch modem-manager gammu sakis3g qmicli mbimcli comgt; do
    which "$b" 2>/dev/null && echo "  + $b: $(which $b)" || true
done

echo
echo "=== Службы: modem/ppp/NetworkManager ==="
systemctl list-units --no-pager --all 2>/dev/null | grep -iE 'modem|ppp|wwan|modeswitch|ofono|NetworkManager|connman' || echo "  (нет)"

echo
echo "=== udev rules связанные с модемом ==="
grep -rn -i 'modem\|modeswitch\|wwan\|ttyUSB\|ttyACM\|cdc_ether\|rndis\|qmi\|mbim' \
    /etc/udev/rules.d/ /lib/udev/rules.d/ 2>/dev/null | grep -v '#' | head -30 || echo "  (нет кастомных)"

echo
echo "=== Текущие /dev/ttyUSB* и /dev/ttyACM* ==="
ls -la /dev/ttyUSB* /dev/ttyACM* /dev/wwan* 2>/dev/null || echo "  (нет)"

echo
echo "=== Сетевые интерфейсы USB (wwan/usb/enp*u*) ==="
ip link show 2>/dev/null | grep -E 'wwan|usb[0-9]|enp[0-9]+u|eth[1-9]' || echo "  (нет)"

echo
echo "=== /etc/ppp/ ==="
ls /etc/ppp/ 2>/dev/null || echo "  (нет)"

echo
echo "=== 99-storage.rules — текущая версия на устройстве ==="
cat /etc/udev/rules.d/99-storage.rules

echo
echo "=== Модули ядра: cdc_ether, rndis, qmi, mbim, option ==="
lsmod | grep -E 'cdc_ether|rndis|usbnet|qmi|mbim|option|cdc_acm|cdc_ncm|cdc_wdm|huawei' || echo "  (не загружены)"
echo "В ядре:"
for m in cdc_ether rndis_host cdc_mbim cdc_wdm qmi_wwan cdc_ncm option cdc_acm; do
    modinfo "$m" 2>/dev/null | awk -v m="$m" '/^filename:/{print "  " m ": " $2; exit}' || echo "  $m: нет"
done
