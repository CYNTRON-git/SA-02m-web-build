#!/bin/bash
# SA-02m: защита DTB от перезаписи пакетами обновления ядра/DTB
# Вызывается: apt hook (/etc/apt/apt.conf.d/99-sa02m-dtb-protect)
#              systemd (sa02m-restore-dtb.service) при загрузке
set -euo pipefail

CANONICAL_DTB="/usr/local/share/sa02m/sun8i-a40i-sk.dtb"
FAT_DEV="/dev/mmcblk2p1"
FAT_MNT="/mnt/fat"
FAT_DTB_NAME="sun8i-a40i-sk.dtb"
LOG="/var/log/sa02m_install.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-restore-dtb: $*" >> "$LOG" 2>&1 || true; }

if [ ! -f "$CANONICAL_DTB" ]; then
    log "ERROR: canonical DTB not found at $CANONICAL_DTB"
    exit 1
fi

CANONICAL_MD5=$(md5sum "$CANONICAL_DTB" | cut -d' ' -f1)

# Монтируем FAT если не смонтирован
MOUNTED=0
if ! mountpoint -q "$FAT_MNT" 2>/dev/null; then
    mkdir -p "$FAT_MNT"
    mount "$FAT_DEV" "$FAT_MNT" || { log "ERROR: cannot mount FAT partition $FAT_DEV"; exit 1; }
    MOUNTED=1
fi

CURRENT_DTB="$FAT_MNT/$FAT_DTB_NAME"

if [ -f "$CURRENT_DTB" ]; then
    CURRENT_MD5=$(md5sum "$CURRENT_DTB" | cut -d' ' -f1)
    if [ "$CURRENT_MD5" = "$CANONICAL_MD5" ]; then
        log "DTB OK (md5 match), no restore needed"
        [ "$MOUNTED" = "1" ] && umount "$FAT_MNT" 2>/dev/null || true
        exit 0
    fi
    log "DTB MISMATCH detected! current=$CURRENT_MD5 canonical=$CANONICAL_MD5"
    cp "$CURRENT_DTB" "${CURRENT_DTB}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
fi

log "Restoring canonical DTB to FAT partition..."
cp "$CANONICAL_DTB" "$CURRENT_DTB"
sync
log "DTB restored successfully"

[ "$MOUNTED" = "1" ] && umount "$FAT_MNT" 2>/dev/null || true
exit 0
