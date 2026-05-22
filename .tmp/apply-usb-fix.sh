#!/bin/bash
set -u
ts() { date '+%H:%M:%S'; }
say() { echo "[$(ts)] $*"; }

say "=== A. Очистим старые «застрявшие» сервисы и точки монтирования ==="
for u in storage-mount@sda.service storage-mount@sda1.service; do
    systemctl stop "$u" 2>/dev/null || true
    systemctl reset-failed "$u" 2>/dev/null || true
done
# Если sda1 уже смонтирован от наших ручных тестов — оставим, но точку
# смены не трогаем — нужно перезапустить udev.

say "=== B. Перезагрузим udev-правила ==="
udevadm control --reload-rules
udevadm trigger --subsystem-match=block --action=change
sleep 1
udevadm settle --timeout=5

say "=== C. Установим ntfs-3g (FUSE) как fallback ==="
if ! command -v mount.ntfs-3g >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get -y install ntfs-3g 2>&1 | tail -3 || true
fi
command -v mount.ntfs-3g && echo "  ntfs-3g: установлен"

say "=== D. Поправим опечатку в /etc/sa02m_storage.conf (STORAGE_AUTO_FORMAT=0n → =0) ==="
if [ -f /etc/sa02m_storage.conf ] && grep -q 'STORAGE_AUTO_FORMAT=0n' /etc/sa02m_storage.conf; then
    sed -i 's/STORAGE_AUTO_FORMAT=0n/STORAGE_AUTO_FORMAT=0/' /etc/sa02m_storage.conf
    echo "  исправлено"
fi
cat /etc/sa02m_storage.conf 2>/dev/null

say "=== E. Сейчас примонтируем то, что есть на /dev/sda* через скрипт ==="
# Сброс предыдущих ручных тестов
umount /media/usb 2>/dev/null || true
# Запустим scan_and_mount — он сам найдёт sda1
/usr/local/bin/storage-mount.sh scan 2>&1

say "=== F. Проверка ==="
echo "--- /proc/mounts ---"
grep -E 'sda|/media' /proc/mounts || echo "  ничего"
echo "--- ls /media/usb ---"
ls -la /media/usb 2>/dev/null | head -10
echo "--- units status ---"
for u in storage-mount@sda.service storage-mount@sda1.service; do
    state=$(systemctl is-active "$u" 2>/dev/null)
    echo "  $u: $state"
done
