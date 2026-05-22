#!/bin/bash
# Полное удаление Node-RED с СА-02м.
set -u

ts() { date '+%H:%M:%S'; }
say() { echo "[$(ts)] $*"; }

say "=== 0. Что у нас есть до удаления ==="
echo "--- units (включая masked) ---"
systemctl list-unit-files --no-pager 2>/dev/null | grep -iE 'nodered|node-red' || echo "  (нет)"
echo "--- installed packages ---"
dpkg -l 2>/dev/null | awk '/nodered|node-red/{print "  "$2" "$3}' || echo "  (нет)"
echo "--- npm globals ---"
if command -v npm >/dev/null 2>&1; then
    npm ls -g --depth=0 2>/dev/null | grep -iE 'node-red|nodered' || echo "  (нет)"
fi
echo "--- processes ---"
pgrep -af -i nodered || echo "  (нет процессов)"
echo "--- files ---"
ls -la /etc/systemd/system/nodered* /etc/systemd/system/node-red* \
       /usr/lib/systemd/system/nodered* /usr/lib/systemd/system/node-red* \
       /lib/systemd/system/nodered* /lib/systemd/system/node-red* \
       /usr/bin/node-red* /usr/local/bin/node-red* \
       /etc/default/nodered /etc/nodered* \
       /opt/nodered /opt/node-red \
       /root/.node-red /home/*/.node-red 2>/dev/null | sed 's/^/  /' || true

say "=== 1. Стоп + disable + unmask ==="
for u in nodered.service node-red.service nodered.socket node-red.socket; do
    if systemctl list-unit-files "$u" 2>/dev/null | grep -q "^$u"; then
        systemctl stop    "$u" 2>/dev/null || true
        systemctl disable "$u" 2>/dev/null || true
        systemctl unmask  "$u" 2>/dev/null || true
    fi
done
# Убить ещё работающие процессы (на случай раннего relaunch).
pkill -9 -f 'node-red' 2>/dev/null || true
pkill -9 -f 'nodered' 2>/dev/null || true

say "=== 2. Удаляем unit-файлы и override-каталоги ==="
rm -fv /etc/systemd/system/nodered.service \
       /etc/systemd/system/node-red.service \
       /etc/systemd/system/nodered.socket \
       /etc/systemd/system/node-red.socket \
       /usr/lib/systemd/system/nodered.service \
       /usr/lib/systemd/system/node-red.service \
       /lib/systemd/system/nodered.service \
       /lib/systemd/system/node-red.service
rm -rfv /etc/systemd/system/nodered.service.d \
        /etc/systemd/system/node-red.service.d \
        /etc/systemd/system/multi-user.target.wants/nodered.service \
        /etc/systemd/system/multi-user.target.wants/node-red.service

say "=== 3. apt purge (если установлен через пакет) ==="
DEBIAN_FRONTEND=noninteractive apt-get -y purge nodered node-red 2>&1 | tail -5 || true
# Скрипт официального инсталлятора кладёт node-red как npm-пакет:
if command -v npm >/dev/null 2>&1; then
    npm uninstall -g --force node-red node-red-admin 2>&1 | tail -5 || true
fi

say "=== 4. Удаляем бинарники / симлинки / конфиги / данные ==="
rm -fv  /usr/bin/node-red /usr/bin/node-red-pi /usr/bin/node-red-start /usr/bin/node-red-stop \
        /usr/bin/node-red-log /usr/bin/node-red-restart /usr/bin/node-red-reload \
        /usr/local/bin/node-red /usr/local/bin/node-red-pi /usr/local/bin/node-red-start \
        /usr/local/bin/node-red-stop /usr/local/bin/node-red-log
rm -fv  /etc/default/nodered /etc/default/node-red
rm -rfv /etc/nodered /etc/node-red /opt/nodered /opt/node-red \
        /usr/lib/node_modules/node-red /usr/lib/node_modules/node-red-admin \
        /usr/local/lib/node_modules/node-red /usr/local/lib/node_modules/node-red-admin \
        /var/log/nodered /var/log/node-red \
        /root/.node-red /root/.node-red-backups
for h in /home/*; do
    [ -d "$h" ] || continue
    rm -rfv "$h/.node-red" "$h/.node-red-backups" 2>/dev/null || true
done

say "=== 5. Чистим journal/failed-state и daemon-reload ==="
systemctl reset-failed nodered.service node-red.service 2>/dev/null || true
systemctl daemon-reload

say "=== 6. Проверка после удаления ==="
echo "--- units ---"
systemctl list-unit-files --no-pager 2>/dev/null | grep -iE 'nodered|node-red' || echo "  (нет — OK)"
echo "--- packages ---"
dpkg -l 2>/dev/null | awk '/nodered|node-red/{print "  "$2" "$3}' || echo "  (нет — OK)"
if command -v npm >/dev/null 2>&1; then
    echo "--- npm globals ---"
    npm ls -g --depth=0 2>/dev/null | grep -iE 'node-red|nodered' || echo "  (нет — OK)"
fi
echo "--- processes ---"
pgrep -af -i nodered && echo "  (всё ещё есть процессы)" || echo "  (нет — OK)"
echo "--- residual files ---"
ls -d /etc/systemd/system/*nodered* /etc/systemd/system/*node-red* \
      /usr/lib/systemd/system/*nodered* /lib/systemd/system/*nodered* \
      /usr/bin/node-red* /usr/local/bin/node-red* \
      /opt/node*red /etc/default/nodered /root/.node-red 2>/dev/null \
      || echo "  (нет — OK)"

say "=== 7. Logs мусор за последние 5 минут (должно стать 0) ==="
journalctl --since '5 minutes ago' --no-pager 2>/dev/null | grep -ci nodered
echo "  nodered messages in last 5 min"
