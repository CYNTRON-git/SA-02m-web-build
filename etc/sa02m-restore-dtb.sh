#!/bin/bash
# SA-02m: защита DTB и boot.scr от перезаписи пакетами обновления ядра/DTB
# Вызывается: apt hook (/etc/apt/apt.conf.d/99-sa02m-dtb-protect)
#              systemd (sa02m-restore-dtb.service) при загрузке
set -euo pipefail

CANONICAL_DTB="/usr/local/share/sa02m/sun8i-a40i-sk.dtb"
CANONICAL_BOOTSCR="/usr/local/share/sa02m/boot.scr"
FAT_DEV="/dev/mmcblk2p1"
FAT_MNT="/mnt/fat"
FAT_DTB_NAME="sun8i-a40i-sk.dtb"
FAT_BOOTSCR_NAME="boot.scr"
LOG="/var/log/sa02m_install.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-restore-dtb: $*" >> "$LOG" 2>&1 || true; }

if [ ! -f "$CANONICAL_DTB" ]; then
    log "ERROR: canonical DTB not found at $CANONICAL_DTB"
    exit 1
fi

MOUNTED=0
if ! mountpoint -q "$FAT_MNT" 2>/dev/null; then
    mkdir -p "$FAT_MNT"
    mount "$FAT_DEV" "$FAT_MNT" || { log "ERROR: cannot mount FAT partition $FAT_DEV"; exit 1; }
    MOUNTED=1
fi

restore_if_mismatch() {
    local canonical="$1" fat_file="$2" label="$3"
    [ -f "$canonical" ] || { log "WARN: canonical $label not found at $canonical, skipping"; return 0; }
    local canon_md5
    canon_md5=$(md5sum "$canonical" | cut -d' ' -f1)
    if [ -f "$fat_file" ]; then
        local cur_md5
        cur_md5=$(md5sum "$fat_file" | cut -d' ' -f1)
        if [ "$cur_md5" = "$canon_md5" ]; then
            log "$label OK (md5 match), no restore needed"
            return 0
        fi
        log "$label MISMATCH detected! current=$cur_md5 canonical=$canon_md5"
        cp "$fat_file" "${fat_file}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
    fi
    log "Restoring canonical $label to FAT partition..."
    # tmp on the SAME FAT dir + sync + byte check + rename (the write_fat_zimage
    # idiom of sa02m-kernel-select.sh): a `cp` over the live name truncated it
    # first, and a power cut mid-copy left U-Boot a 0-byte boot.scr / DTB — a
    # board that no longer boots (recovery = FEL). The live name is now OLD or
    # NEW at every instant; a failed step leaves it untouched.
    local tmp staged=0 replaced=0
    tmp="$(dirname "$fat_file")/.$(basename "$fat_file").new"
    cp -f "$canonical" "$tmp" && sync && cmp -s "$canonical" "$tmp" && staged=1
    [ "$staged" = 1 ] && mv -f "$tmp" "$fat_file" && replaced=1
    if [ "$replaced" = 1 ]; then
        sync
        log "$label restored successfully"
        return 0
    fi
    rm -f "$tmp" 2>/dev/null || true
    log "ERROR: $label restore failed — FAT copy left as it was"
    return 1
}

rc=0
restore_if_mismatch "$CANONICAL_DTB"     "$FAT_MNT/$FAT_DTB_NAME"     "DTB"      || rc=1
restore_if_mismatch "$CANONICAL_BOOTSCR" "$FAT_MNT/$FAT_BOOTSCR_NAME" "boot.scr" || rc=1

[ "$MOUNTED" = "1" ] && umount "$FAT_MNT" 2>/dev/null || true
exit "$rc"
