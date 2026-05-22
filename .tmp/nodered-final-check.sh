#!/bin/bash
set +u
rm -fv /usr/share/applications/Node-RED.desktop /usr/share/applications/node-red.desktop 2>/dev/null

echo
echo === остатки в стандартных местах ===
ls -la \
    /etc/systemd/system/*nodered* /etc/systemd/system/*node-red* \
    /usr/lib/systemd/system/*nodered* /usr/lib/systemd/system/*node-red* \
    /lib/systemd/system/*nodered* /lib/systemd/system/*node-red* \
    /usr/bin/node-red* /usr/local/bin/node-red* \
    /etc/default/nodered /etc/nodered* /etc/logrotate.d/nodered \
    /opt/node-red* /opt/nodered* \
    /usr/share/applications/*ode-*ed* /usr/share/icons/hicolor/*/apps/node-red* \
    /var/log/nodered* /var/log.hdd/nodered* \
    /root/.node-red /home/*/.node-red \
    /usr/lib/node_modules/node-red* /usr/local/lib/node_modules/node-red* \
    2>/dev/null || echo "  (нет — OK)"

echo
echo === unit-files ===
out=$(systemctl list-unit-files --no-pager 2>/dev/null | grep -iE 'nodered|node-red')
echo "${out:-  unit-files: 0}"

echo
echo === пакеты ===
out=$(dpkg -l 2>/dev/null | awk '/nodered|node-red/{print}')
echo "${out:-  пакеты: 0}"

echo
echo === процессы ===
out=$(ps -eo pid,cmd 2>/dev/null | grep -i nodered | grep -vE '(grep|bash -c|/tmp/.*sh)')
echo "${out:-  процессы: 0}"

echo
echo === Failed units ===
systemctl --failed --no-pager

echo
echo === journal за 5 минут ===
echo "  nodered-сообщений: $(journalctl --since '5 minutes ago' --no-pager 2>/dev/null | grep -ci nodered)"
