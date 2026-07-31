#!/bin/sh
# SA-02m USB autorun: write eMMC image, apply firstboot overlay, reboot.
# BusyBox-compatible (no bash, no dd status=progress).
# Media layout:
#   sdcard.img              — PiShrink .img (patched)
#   autorun.sh              — this script
#   firstboot-overlay/      — optional files mirrored onto new rootfs after dd
set -eu

LOG=/tmp/autorun-flash.log
TARGET=/dev/mmcblk2
IMG_NAME=sdcard.img
IMG=
MEDIA=

ts() { date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 0; }
log() { echo "[$(ts)] $*" | tee -a "$LOG" 2>/dev/null || echo "[$(ts)] $*"; }
die() { log "FATAL: $*"; exit 1; }

ensure_img() {
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

# Copy overlay tree onto freshly written rootfs (scripts/units from USB).
apply_overlay() {
    local root=$1
    local ov="${MEDIA}/firstboot-overlay"
    [ -d "$ov" ] || { log "no firstboot-overlay on media (OK if image pre-patched)"; return 0; }
    log "apply firstboot-overlay from $ov"
    # shellcheck disable=SC2044
    for f in $(find "$ov" -type f 2>/dev/null); do
        rel=${f#"$ov"/}
        dest="$root/$rel"
        mkdir -p "$(dirname "$dest")"
        cp -f "$f" "$dest"
        case "$rel" in
            usr/local/sbin/*|usr/local/bin/*) chmod 755 "$dest" 2>/dev/null || true ;;
        esac
    done
}

apply_firstboot_wiring() {
    local root=$1
    mkdir -p "$root/etc/systemd/system" \
             "$root/etc/systemd/system.conf.d" \
             "$root/etc/systemd/system/basic.target.wants" \
             "$root/etc/systemd/system/multi-user.target.wants"

    # Expand on multi-user only — never basic.target (that gated networking for minutes).
    if [ -f "$root/etc/systemd/system/sa02m-rootfs-expand.service" ] \
       || [ -f "$root/lib/systemd/system/sa02m-rootfs-expand.service" ]; then
        rm -f "$root/etc/systemd/system/basic.target.wants/sa02m-rootfs-expand.service"
        ln -sf /etc/systemd/system/sa02m-rootfs-expand.service \
            "$root/etc/systemd/system/multi-user.target.wants/sa02m-rootfs-expand.service"
    fi
    ln -sf /dev/null "$root/etc/systemd/system/armbian-resize-filesystem.service"
    rm -f "$root/etc/systemd/system/basic.target.wants/armbian-resize-filesystem.service" 2>/dev/null || true

    for u in net-watchdog sa02m-userspace-watchdog sa02m-failure-monitor sa02m-eth-coldboot; do
        if [ -L "$root/etc/systemd/system/${u}.service" ]; then
            tgt=$(readlink "$root/etc/systemd/system/${u}.service" 2>/dev/null || true)
            [ "$tgt" = "/dev/null" ] && rm -f "$root/etc/systemd/system/${u}.service"
        fi
        if [ -f "$root/etc/systemd/system/${u}.service" ]; then
            ln -sf "/etc/systemd/system/${u}.service" \
                "$root/etc/systemd/system/multi-user.target.wants/${u}.service"
        fi
    done
    rm -f "$root/etc/systemd/system/multi-user.target.wants/sa02m-eth1-coldboot.service" 2>/dev/null || true

    printf '%s\n' '[Manager]' 'RuntimeWatchdogSec=8s' 'RebootWatchdogSec=2min' \
        > "$root/etc/systemd/system.conf.d/sa02m-watchdog.conf"
    if [ -f "$root/etc/systemd/system.conf" ]; then
        sed -i 's/^RuntimeWatchdogSec=/#RuntimeWatchdogSec=/' \
            "$root/etc/systemd/system.conf" 2>/dev/null || true
    fi
    rm -f "$root/var/lib/sa02m-rootfs-expand.done"

    # Donor cloud identity must not clone onto new boards.
    mkdir -p "$root/etc/sa02m-cloud"
    chmod 750 "$root/etc/sa02m-cloud"
    rm -f "$root/etc/sa02m-cloud/device_secret" \
          "$root/etc/sa02m-cloud/frpc.toml" \
          "$root/etc/sa02m-cloud"/frpc.toml.bak* \
          "$root/etc/sa02m-cloud/pair_request" \
          "$root/etc/sa02m-cloud/activation_token"
    printf '%s\n' \
      '[cloud]' \
      'api_url = https://cloud.cyntron.ru/api/v1' \
      'server_host = cloud.cyntron.ru' \
      'enrolled = false' \
      'device_id =' \
      'heartbeat_interval = 30' \
      '' \
      '[device]' \
      'serial =' \
      'web_port = 9999' \
      > "$root/etc/sa02m-cloud/agent.conf"
    chmod 640 "$root/etc/sa02m-cloud/agent.conf"
}

log "=== SA-02m autorun flash start ==="
rmmod -f g_mass_storage 2>/dev/null || true

ensure_img || die "image not found: $IMG_NAME (mount USB failed)"
[ -b "$TARGET" ] || die "$TARGET missing"
[ -r "$IMG" ] || die "cannot read $IMG"

log "image: $IMG"
log "target: $TARGET"
log "dd start (bs=4M conv=fsync)"
dd if="$IMG" of="$TARGET" bs=4M conv=fsync
sync
sync
log "dd done"

log "first-boot wiring on new rootfs"
MNT=/tmp/newroot.$$
mkdir -p "$MNT"
partx -u "$TARGET" 2>/dev/null || true
blockdev --rereadpt "$TARGET" 2>/dev/null || true
sleep 1

ROOTPART="${TARGET}p2"
if mount "$ROOTPART" "$MNT" 2>/dev/null; then
    apply_overlay "$MNT"
    apply_firstboot_wiring "$MNT"
    sync
    umount "$MNT" 2>/dev/null || true
    log "first-boot wiring OK on $ROOTPART"
else
    log "WARN: could not mount $ROOTPART for first-boot wiring"
fi
rmdir "$MNT" 2>/dev/null || true

log "reboot"
sleep 1
reboot -f 2>/dev/null || reboot
