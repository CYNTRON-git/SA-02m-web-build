#!/bin/bash
set +u
echo "=== lsblk ==="
lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,VENDOR,MODEL 2>/dev/null
echo
echo "=== blkid ==="
blkid 2>/dev/null
echo
echo "=== /proc/mounts (только sda/sdcard/usb/exfat/ntfs) ==="
grep -E 'sd[a-z]|sdcard|/media|exfat|ntfs|fuse' /proc/mounts || echo "  (ничего не смонтировано)"
echo
echo "=== /media/usb /media/sdcard ==="
ls -la /media/ 2>/dev/null
echo
echo "=== storage-mount log (последние 30 строк) ==="
journalctl -u 'storage-mount@*.service' --no-pager -n 30 2>/dev/null
echo
echo "=== udev для sda ==="
udevadm info --query=property --name=/dev/sda 2>/dev/null | grep -E '^(DEVNAME|ID_BUS|ID_VENDOR|ID_MODEL|ID_FS_TYPE|ID_FS_LABEL|DEVPATH|PARTNAME)' | head -20
echo
echo "=== попробуем mount ntfs3 руками ==="
mkdir -p /media/usb
mount -t ntfs3 -o rw,noatime,uid=1000,gid=1000,umask=000 /dev/sda /media/usb 2>&1
echo "exit=$?"
echo "after-mount /proc/mounts:"
grep -E 'sda|/media' /proc/mounts || echo "  ничего"
umount /media/usb 2>/dev/null && echo "  (размонтирован после теста)"
echo
echo "=== попробуем mount ntfs3 на sda1 если есть ==="
if [ -b /dev/sda1 ]; then
    mount -t ntfs3 -o rw,noatime,uid=1000,gid=1000,umask=000 /dev/sda1 /media/usb 2>&1
    echo "exit=$?"
    grep sda /proc/mounts || echo "  ничего"
    umount /media/usb 2>/dev/null && echo "  (размонтирован)"
fi
echo
echo "=== kernel ntfs / ntfs3 модули ==="
grep -E 'ntfs|fuse' /proc/filesystems
lsmod | grep -iE 'ntfs|fuse'
modinfo ntfs3 2>/dev/null | head -3
echo
echo "=== ntfs-3g/fuse в системе? ==="
which ntfs-3g ntfsfix mount.ntfs-3g 2>/dev/null
dpkg -l ntfs-3g fuse fuse3 2>/dev/null | grep -E '^ii'
echo
echo "=== storage-mount config ==="
cat /etc/sa02m_storage.conf 2>/dev/null
echo
echo "=== как web смотрит на USB ==="
# Найдём CGI/обработчик веба, который пишет «USB-накопитель не установлен»
grep -rln -E 'USB.*накопитель|USB.*установ|/media/usb' /var/www 2>/dev/null | head -10
grep -rln -E 'USB.*накопитель|USB.*установ|/media/usb' /opt/sa02m-flasher 2>/dev/null | head -10
