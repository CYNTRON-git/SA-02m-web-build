#!/bin/bash
set -u
say() { echo "[$(date '+%H:%M:%S')] $*"; }

say "=== 1. Установка пакетов ==="
for pkg in modemmanager ppp isc-dhcp-client usb-modeswitch usb-modeswitch-data; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        echo "  $pkg: уже установлен"
    else
        echo "  $pkg: устанавливаем..."
        DEBIAN_FRONTEND=noninteractive apt-get -y install "$pkg" 2>&1 | tail -2 || echo "  WARN: $pkg не установлен"
    fi
done

say "=== 2. udev-правила ==="
cat > /etc/udev/rules.d/99-modem.rules << 'UDEV_EOF'
# SA-02m: USB-modem rules
ACTION=="add", SUBSYSTEM=="net", \
  ENV{ID_NET_DRIVER}=="cdc_ether|rndis_host|cdc_ncm|cdc_mbim|asix|ax88179_178a|qmi_wwan", \
  ENV{ID_BUS}=="usb", \
  RUN+="/bin/systemctl start sa02m-modem-dhcp@%k.service"
ACTION=="remove", SUBSYSTEM=="net", \
  ENV{ID_NET_DRIVER}=="cdc_ether|rndis_host|cdc_ncm|cdc_mbim|asix|ax88179_178a|qmi_wwan", \
  ENV{ID_BUS}=="usb", \
  RUN+="/bin/systemctl stop sa02m-modem-dhcp@%k.service"
ACTION=="add", SUBSYSTEM=="tty", KERNEL=="ttyUSB*", ENV{ID_BUS}=="usb", \
  ATTRS{bInterfaceNumber}=="00", SYMLINK+="modem_at"
ACTION=="add", SUBSYSTEM=="tty", KERNEL=="ttyUSB*|ttyACM*", ENV{ID_BUS}=="usb", \
  ATTRS{bInterfaceNumber}=="02", SYMLINK+="modem"
ACTION=="add", SUBSYSTEM=="usbmisc", KERNEL=="cdc-wdm*", ENV{ID_BUS}=="usb", \
  SYMLINK+="cdc-wdm-modem"
UDEV_EOF
echo "  99-modem.rules: OK"

say "=== 3. 99-storage.rules — добавить CD-ROM guard ==="
if ! grep -q 'storage_rules_end' /etc/udev/rules.d/99-storage.rules; then
    sed -i '1s/^/# Optical drives and CD-ROM mode of modems — skip.\nSUBSYSTEM=="block", KERNEL=="sr*",         GOTO="storage_rules_end"\nSUBSYSTEM=="block", ENV{ID_CDROM}=="1",    GOTO="storage_rules_end"\nSUBSYSTEM=="block", ENV{ID_TYPE}=="cd",    GOTO="storage_rules_end"\n\n/' \
        /etc/udev/rules.d/99-storage.rules
    echo 'LABEL="storage_rules_end"' >> /etc/udev/rules.d/99-storage.rules
    echo "  Guard добавлен"
else
    echo "  Guard уже есть"
fi

say "=== 4. Systemd-сервис DHCP для CDC-ethernet модема ==="
cat > /etc/systemd/system/sa02m-modem-dhcp@.service << 'SVCEOF'
[Unit]
Description=SA-02m DHCP on USB modem interface %I
BindsTo=sys-subsystem-net-devices-%i.device
After=sys-subsystem-net-devices-%i.device

[Service]
Type=forking
PIDFile=/run/dhclient.%i.pid
ExecStartPre=/sbin/ip link set %i up
ExecStart=/sbin/dhclient -4 -v -pf /run/dhclient.%i.pid \
          -lf /var/lib/dhcp/dhclient.%i.leases \
          -timeout 30 %i
ExecStop=/sbin/dhclient -r -pf /run/dhclient.%i.pid %i
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF
echo "  sa02m-modem-dhcp@.service: OK"

say "=== 5. PPP peers/modem шаблон ==="
mkdir -p /etc/ppp/peers /etc/ppp/ip-up.d /etc/ppp/ip-down.d
[ -f /etc/ppp/peers/modem ] || cat > /etc/ppp/peers/modem << 'PPPEOF'
/dev/modem
115200
connect '/usr/sbin/chat -v TIMEOUT 30 ABORT "ERROR" ABORT "NO CARRIER" "" AT OK ATZ OK ATD*99# CONNECT ""'
noauth
defaultroute
usepeerdns
noipdefault
novj
novjccomp
persist
maxfail 0
holdoff 5
debug
logfile /var/log/ppp-modem.log
PPPEOF
echo "  /etc/ppp/peers/modem: OK"

say "=== 6. sa02m_modem.conf ==="
[ -f /etc/sa02m_modem.conf ] || cat > /etc/sa02m_modem.conf << 'CONFEOF'
MODEM_MODE=auto
MODEM_APN=internet
MODEM_PIN=
MODEM_METRIC=100
MODEM_DHCP_TIMEOUT=30
CONFEOF
echo "  /etc/sa02m_modem.conf: OK"

say "=== 7. ModemManager ==="
systemctl unmask ModemManager.service 2>/dev/null || true
if command -v mmcli >/dev/null 2>&1; then
    systemctl enable ModemManager.service 2>/dev/null || true
    systemctl start ModemManager.service 2>/dev/null || true
    systemctl is-active ModemManager.service
else
    echo "  ModemManager не установлен (пакет недоступен в репозитории)"
fi

say "=== 8. Перезагрузка udev ==="
systemctl daemon-reload
udevadm control --reload-rules
udevadm trigger --subsystem-match=net --action=change 2>/dev/null || true
echo "  udev: OK"

say "=== 9. Итоговое состояние ==="
echo "--- Пакеты ---"
for pkg in modemmanager ppp isc-dhcp-client usb-modeswitch; do
    state=$(dpkg -s "$pkg" 2>/dev/null | awk '/^Status:/{print $NF}')
    echo "  $pkg: ${state:-не установлен}"
done
echo
echo "--- Сетевые интерфейсы ---"
ip link show | grep -E 'eth[0-9]|usb[0-9]|wwan|ppp' | awk '{print "  " $0}'
echo
echo "--- /dev/ttyUSB* /dev/ttyACM* /dev/modem ---"
ls -la /dev/ttyUSB* /dev/ttyACM* /dev/modem 2>/dev/null || echo "  (нет — модем ещё не подключён)"
echo
echo "--- storage rules CD-ROM guard ---"
grep -n 'storage_rules_end\|ID_CDROM\|ID_TYPE.*cd\|sr\*' /etc/udev/rules.d/99-storage.rules | head -10
echo
echo "--- ModemManager ---"
systemctl is-active ModemManager.service 2>/dev/null || echo "  не запущен"
echo
say "=== Готово к подключению модема ==="
