#!/bin/bash
# Проверка состояния mplc4 на устройстве: бинарь, версия, init.d, попытка start.
set +e
echo "=== /opt/mplc4 — ключевые файлы ==="
ls -la /opt/mplc4/mplc_daemon /opt/mplc4/mplc /opt/mplc4/mplc_cyntron.so /opt/mplc4/mplc_monitor /opt/mplc4/start_mplc4.sh /opt/mplc4/version.txt 2>&1
echo
echo "=== version.txt ==="
cat /opt/mplc4/version.txt 2>/dev/null
echo
echo "=== mplc_cyntron.so size+sha ==="
ls -la /opt/mplc4/mplc_cyntron.so
sha256sum /opt/mplc4/mplc_cyntron.so 2>/dev/null
echo
echo "=== /etc/init.d/mplc4 (head) ==="
head -40 /etc/init.d/mplc4
echo
echo "=== /etc/systemd/system/mplc4.service ==="
cat /etc/systemd/system/mplc4.service
echo
echo "=== попытка ручного запуска (без enable) ==="
/etc/init.d/mplc4 start 2>&1 | head -40
sleep 3
echo
echo "=== mplc4 status после start ==="
ps -ef | grep -E "mplc|masterplc" | grep -v grep | head -10
ss -ltn 2>/dev/null | head -20
echo
echo "=== /opt/mplc4/log (last 30 lines всех текущих логов) ==="
ls -la /opt/mplc4/log/ 2>/dev/null
for f in /opt/mplc4/log/*.log /opt/mplc4/log/mplc*.log; do
    [ -f "$f" ] || continue
    echo "--- $f ---"
    tail -15 "$f" 2>/dev/null
done
