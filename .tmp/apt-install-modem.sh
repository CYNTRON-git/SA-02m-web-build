#!/bin/bash
set -u
say() { echo "[$(date '+%H:%M:%S')] $*"; }

say "=== apt-get update ==="
DEBIAN_FRONTEND=noninteractive apt-get update 2>&1 | tail -5

say "=== Устанавливаем пакеты ==="
for pkg in modemmanager ppp; do
    cand=$(apt-cache policy "$pkg" 2>/dev/null | awk '/Кандидат:|Candidate:/{print $2}')
    if [ -z "$cand" ] || [ "$cand" = "(отсутствует)" ] || [ "$cand" = "(none)" ]; then
        echo "  $pkg: НЕДОСТУПЕН в репозитории (нет для armhf)"
    else
        echo "  $pkg кандидат: $cand — устанавливаем"
        DEBIAN_FRONTEND=noninteractive apt-get -y install "$pkg" 2>&1 | tail -3
    fi
done

say "=== Итог ==="
for pkg in modemmanager ppp isc-dhcp-client usb-modeswitch; do
    state=$(dpkg -s "$pkg" 2>/dev/null | awk '/^Status:/{print $NF}')
    echo "  $pkg: ${state:-не установлен}"
done
echo
echo "  pppd: $(which pppd 2>/dev/null || echo 'нет')"
echo "  mmcli: $(which mmcli 2>/dev/null || echo 'нет')"
echo "  dhclient: $(which dhclient 2>/dev/null || echo 'нет')"
