#!/bin/bash
# SA-02m: expand root partition/filesystem after PiShrink clone (first boot).
# Runs after local-fs; must NOT block networking (see unit Before=).
# Watchdogs ordered After this unit — do NOT systemctl stop them (that cancels
# their queued start for the whole boot).
set -euo pipefail

LOG=/var/log/sa02m-rootfs-expand.log
DONE=/var/lib/sa02m-rootfs-expand.done
ROOT_PART="${SA02M_ROOT_PART:-/dev/mmcblk2p2}"
ROOT_DISK="${SA02M_ROOT_DISK:-/dev/mmcblk2}"
PART_NUM="${SA02M_ROOT_PART_NUM:-2}"
WATCHDOGS="sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

need_expand() {
    local part_bytes disk_bytes
    part_bytes=$(blockdev --getsize64 "$ROOT_PART" 2>/dev/null || echo 0)
    disk_bytes=$(blockdev --getsize64 "$ROOT_DISK" 2>/dev/null || echo 0)
    [ "$part_bytes" -gt 0 ] && [ "$disk_bytes" -gt 0 ] || return 1
    [ "$part_bytes" -lt $((disk_bytes * 85 / 100)) ]
}

# File-level only — avoid slow/racy `systemctl mask|enable|stop` during boot.
unmask_watchdogs_files() {
    local svc
    for svc in $WATCHDOGS; do
        if [ -L "/etc/systemd/system/${svc}.service" ] \
           && [ "$(readlink "/etc/systemd/system/${svc}.service" 2>/dev/null)" = "/dev/null" ]; then
            rm -f "/etc/systemd/system/${svc}.service"
            log "removed stale mask: ${svc}.service"
        fi
    done
}

# Mask armbian dual-resize without calling systemctl (seconds of D-Bus).
mask_armbian_resize_files() {
    mkdir -p /etc/systemd/system
    ln -sfn /dev/null /etc/systemd/system/armbian-resize-filesystem.service
    rm -f /etc/systemd/system/basic.target.wants/armbian-resize-filesystem.service \
          /etc/systemd/system/multi-user.target.wants/armbian-resize-filesystem.service \
          /lib/systemd/system/basic.target.wants/armbian-resize-filesystem.service \
          2>/dev/null || true
}

expand_partition() {
    local capacity lastsector
    partprobe "$ROOT_DISK" 2>/dev/null || true
    udevadm settle --timeout=5 2>/dev/null || true

    capacity=$(parted -ms "$ROOT_DISK" unit s print 2>/dev/null \
        | awk -F: '/^\/dev\//{gsub(/s$/,"",$2); print $2; exit}')
    if [ -z "$capacity" ] || [ "$capacity" = "0" ]; then
        log "FAILED: cannot read disk capacity from parted"
        return 1
    fi
    lastsector=$((capacity - 2048))
    log "expand $ROOT_DISK p${PART_NUM} -> end ${lastsector}s (disk ${capacity}s)"

    parted -s "$ROOT_DISK" unit s resizepart "$PART_NUM" "$lastsector"
    partprobe "$ROOT_DISK" 2>/dev/null || true
    udevadm settle --timeout=5 2>/dev/null || true
}

finish_firstboot() {
    touch "$DONE"
    mask_armbian_resize_files
    unmask_watchdogs_files
    # DONE ConditionPathExists skips next boot; drop wants symlink (no systemctl).
    rm -f /etc/systemd/system/multi-user.target.wants/sa02m-rootfs-expand.service \
          /etc/systemd/system/basic.target.wants/sa02m-rootfs-expand.service \
          2>/dev/null || true
    log "firstboot finish: DONE set; watchdogs will start via Before= ordering"
}

case "${1:-start}" in
    start)
        mkdir -p "$(dirname "$LOG")" "$(dirname "$DONE")"
        [ -f "$DONE" ] && exit 0
        [ -b "$ROOT_PART" ] && [ -b "$ROOT_DISK" ] || exit 0
        if ! need_expand; then
            log "rootfs already uses eMMC, nothing to do"
            finish_firstboot
            exit 0
        fi

        log "start: root partition smaller than eMMC (networking not blocked)"
        mask_armbian_resize_files
        # Do NOT systemctl stop watchdogs — with Before= that cancels their
        # queued start for the entire boot (net-watchdog stays dead).
        expand_partition
        log "resize2fs $ROOT_PART"
        resize2fs "$ROOT_PART" | tee -a "$LOG"
        log "done: $(df -h / | tail -1)"
        finish_firstboot
        ;;
    *)
        echo "Usage: $0 start" >&2
        exit 2
        ;;
esac
