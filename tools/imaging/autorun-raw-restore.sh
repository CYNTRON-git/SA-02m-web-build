#!/bin/sh
# SA-02m raw restore autorun: write a known-good captured image as-is.
# Intentionally does NOT patch rootfs, boot.scr, cloud enrollment, alice
# enrollment, watchdogs, or first-boot wiring: the image is restored byte for
# byte. Use only from external USB/buildroot, never on the
# running eMMC rootfs.
set -eu

LOG=/tmp/autorun-raw-restore.log
TARGET=/dev/mmcblk2
IMG_NAME=sdcard.img
IMG=
MEDIA=

ts() { date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 0; }
log() { echo "[$(ts)] $*" | tee -a "$LOG" 2>/dev/null || echo "[$(ts)] $*"; }
die() { log "FATAL: $*"; exit 1; }

find_image() {
    if [ -f "/mnt/$IMG_NAME" ]; then
        IMG="/mnt/$IMG_NAME"
        MEDIA=/mnt
        return 0
    fi
    if [ -f "/mnt/usb/$IMG_NAME" ]; then
        IMG="/mnt/usb/$IMG_NAME"
        MEDIA=/mnt/usb
        return 0
    fi

    mkdir -p /mnt
    for dev in /dev/sda1 /dev/sdb1 /dev/sdc1; do
        [ -b "$dev" ] || continue
        umount /mnt 2>/dev/null || true
        if mount "$dev" /mnt 2>/dev/null; then
            if [ -f "/mnt/$IMG_NAME" ]; then
                IMG="/mnt/$IMG_NAME"
                MEDIA=/mnt
                log "mounted $dev -> /mnt"
                return 0
            fi
            umount /mnt 2>/dev/null || true
        fi
    done
    return 1
}

log "=== SA-02m raw restore start ==="
rmmod -f g_mass_storage 2>/dev/null || true

find_image || die "image not found: $IMG_NAME"
[ -b "$TARGET" ] || die "$TARGET missing"
[ -r "$IMG" ] || die "cannot read $IMG"

log "media: ${MEDIA:-unknown}"
log "image: $IMG"
log "target: $TARGET"
log "raw dd start (no post-flash patching)"
dd if="$IMG" of="$TARGET" bs=4M conv=fsync
sync
sync
log "raw dd done"

log "reboot"
sleep 1
reboot -f 2>/dev/null || reboot
