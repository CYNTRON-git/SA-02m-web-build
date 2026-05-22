#!/bin/bash
set -u
ts() { date '+%H:%M:%S'; }
say() { echo "[$(ts)] $*"; }

say "=== A. storage-mount.sh: «skip» больше не failed ==="
# Скрипт обновляется отдельно через scp; здесь только сбросим состояние.
systemctl reset-failed 'storage-mount@*.service' 2>/dev/null || true

say "=== B. Отключаем NetworkManager (eth0/eth1/can0 — все unmanaged) ==="
# NetworkManager.service удлиняет boot на ~6 с, но реально не управляет ни одним
# интерфейсом устройства (eth0 — ifupdown, can0 — отдельный setup-скрипт).
# Маскируем, чтобы при следующем обновлении пакетов он не вернулся.
systemctl disable NetworkManager.service 2>/dev/null || true
systemctl stop NetworkManager.service 2>/dev/null || true
systemctl mask NetworkManager.service 2>/dev/null || true
# Связанные сервисы NM
for u in NetworkManager-wait-online.service NetworkManager-dispatcher.service; do
    systemctl disable "$u" 2>/dev/null || true
    systemctl mask "$u" 2>/dev/null || true
done

say "=== C. Ускоряем ifupdown для eth0 ==="
# Опция up-delay не действует без support, но multicast tries и dhclient timeout
# нам не нужны (статика). Главное — без waits.
# Также включим bring-up без ожидания carrier (allow-hotplug auto-up).
# Проверим текущий /etc/network/interfaces.d/eth0.conf.
if [ -f /etc/network/interfaces.d/eth0.conf ] && ! grep -q '^[[:space:]]*pre-up' /etc/network/interfaces.d/eth0.conf; then
    # Добавим pre-up: форсируем link up даже если carrier появляется не сразу.
    sed -i '/^iface eth0 inet static/a\    pre-up ip link set $IFACE up 2>/dev/null || true' /etc/network/interfaces.d/eth0.conf
    say "  -> добавлен pre-up в eth0.conf"
fi

say "=== D. Сбрасываем failed-state, перечитываем юниты ==="
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

say "=== E. Текущие статусы ==="
echo "--- Failed units ---"
systemctl --failed --no-pager
echo
echo "--- Active SA02m services ---"
systemctl is-active ssh nginx chrony net-watchdog sa02m-userspace-watchdog sa02m-flasher mplc4 2>&1 | paste -d= <(echo -e "ssh\nnginx\nchrony\nnet-watchdog\nsa02m-userspace-watchdog\nsa02m-flasher\nmplc4") -
echo
echo "--- NM-related (should be masked) ---"
for u in NetworkManager.service NetworkManager-wait-online.service; do
    state=$(systemctl is-enabled "$u" 2>&1)
    active=$(systemctl is-active "$u" 2>&1)
    echo "$u: enabled=$state active=$active"
done
