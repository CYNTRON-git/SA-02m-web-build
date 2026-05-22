set -e
echo "=== Деплой исправлений модема ==="

# 1. udev 99-modem.rules (SUBSYSTEMS/DRIVERS вместо ENV)
echo "  [1] udev 99-modem.rules"
cat > /etc/udev/rules.d/99-modem.rules << 'UDEV_EOF'
# ─────────────────────────────────────────────────────────────────────────────
# 99-modem.rules  — СА-02м: USB-модемы (3G/4G/LTE)
# ─────────────────────────────────────────────────────────────────────────────

# ── A. CDC-ECM / RNDIS / NCM — USB-ethernet-модем ────────────────────────────
# ENV{ID_NET_DRIVER} и ENV{ID_BUS} не заполняются на момент события add,
# поэтому используем SUBSYSTEMS=="usb" (проверяет родительские устройства)
# и DRIVERS=="cdc_ether|..." (драйвер родительского USB-интерфейса).
ACTION=="add", SUBSYSTEM=="net", \
  SUBSYSTEMS=="usb", \
  DRIVERS=="cdc_ether|rndis_host|cdc_ncm|cdc_mbim|qmi_wwan", \
  RUN+="/bin/systemctl start sa02m-modem-dhcp@%k.service"

ACTION=="remove", SUBSYSTEM=="net", \
  SUBSYSTEMS=="usb", \
  DRIVERS=="cdc_ether|rndis_host|cdc_ncm|cdc_mbim|qmi_wwan", \
  RUN+="/bin/systemctl stop sa02m-modem-dhcp@%k.service"

# ── B. ttyUSB / ttyACM — последовательный (PPP/AT) модем ─────────────────────
ACTION=="add", SUBSYSTEM=="tty", \
  KERNEL=="ttyUSB*", \
  SUBSYSTEMS=="usb", \
  ATTRS{bInterfaceNumber}=="00", \
  SYMLINK+="modem_at"

ACTION=="add", SUBSYSTEM=="tty", \
  KERNEL=="ttyUSB*|ttyACM*", \
  SUBSYSTEMS=="usb", \
  ATTRS{bInterfaceNumber}=="02", \
  SYMLINK+="modem"

# ── C. QMI / MBIM — cdc-wdm контроллер ───────────────────────────────────────
ACTION=="add", SUBSYSTEM=="usbmisc", \
  KERNEL=="cdc-wdm*", \
  SUBSYSTEMS=="usb", \
  SYMLINK+="cdc-wdm-modem"
UDEV_EOF

# 2. sa02m-modem-dhcp@.service (убрать -timeout, добавить -nw)
echo "  [2] sa02m-modem-dhcp@.service"
cat > /etc/systemd/system/sa02m-modem-dhcp@.service << 'SVC_EOF'
[Unit]
Description=SA-02m DHCP on USB modem interface %I
Documentation=man:dhclient(8)
BindsTo=sys-subsystem-net-devices-%i.device
After=sys-subsystem-net-devices-%i.device

[Service]
Type=forking
PIDFile=/run/dhclient.%i.pid
# -nw = немедленно уйти в фон, не ждать получения адреса.
# Метрика маршрута устанавливается в /etc/dhcp/dhclient-exit-hooks.d/sa02m-modem-metric.
ExecStartPre=/sbin/ip link set %i up
ExecStart=/sbin/dhclient -4 -nw -pf /run/dhclient.%i.pid \
          -lf /var/lib/dhcp/dhclient.%i.leases %i
ExecStop=/sbin/dhclient -r -pf /run/dhclient.%i.pid %i
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SVC_EOF

# 3. dhclient exit-hook: metric 100 для USB-модемных интерфейсов
echo "  [3] dhclient-exit-hooks.d/sa02m-modem-metric"
mkdir -p /etc/dhcp/dhclient-exit-hooks.d
cat > /etc/dhcp/dhclient-exit-hooks.d/sa02m-modem-metric << 'HOOK_EOF'
#!/bin/sh
# SA-02m: metric 100 для default-маршрута USB-модемных интерфейсов.
case "$interface" in
    enx*|usb[0-9]*|eth[1-9]*)
        ;;
    *)
        return 0 ;;
esac

case "$reason" in
    BOUND|RENEW|REBIND|REBOOT)
        if [ -n "$new_routers" ]; then
            /sbin/ip route del default dev "$interface" 2>/dev/null || true
            /sbin/ip route add default via "$new_routers" dev "$interface" metric 100 2>/dev/null || true
        fi
        ;;
    EXPIRE|FAIL|RELEASE|STOP)
        /sbin/ip route del default dev "$interface" metric 100 2>/dev/null || true
        ;;
esac
HOOK_EOF
chmod 755 /etc/dhcp/dhclient-exit-hooks.d/sa02m-modem-metric

echo "  [4] Перезагрузка udev + systemd"
udevadm control --reload-rules
systemctl daemon-reload

echo
echo "=== Тест udev правила: simulate add event для enx344b50000000 ==="
# Проверяем что правило РАСПОЗНАЕТ устройство
udevadm test /sys/class/net/enx344b50000000 2>&1 | grep -E 'SUBSYSTEMS|DRIVERS|RUN|sa02m-modem' | head -10

echo
echo "=== Перезапуск DHCP вручную для enx344b50000000 ==="
IF=enx344b50000000
pkill -f "dhclient.*$IF" 2>/dev/null; sleep 0.3
ip link set "$IF" up
dhclient -4 -nw -pf "/run/dhclient.${IF}.pid" \
         -lf "/var/lib/dhcp/dhclient.${IF}.leases" "$IF" 2>&1
sleep 4

echo
echo "=== IP и маршруты ==="
ip addr show "$IF"
ip route

echo
echo "=== Проверка ZTE web ==="
ping -c2 -W2 192.168.0.1 2>&1 | tail -2
