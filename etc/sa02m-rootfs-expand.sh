#!/bin/bash
# SA-02m: expand root partition/filesystem after PiShrink clone (first boot).
# Runs before userspace watchdog services to avoid reboot during resize2fs.
set -euo pipefail

LOG=/var/log/sa02m-rootfs-expand.log
DONE=/var/lib/sa02m-rootfs-expand.done
ROOT_PART="${SA02M_ROOT_PART:-/dev/mmcblk2p2}"
ROOT_DISK="${SA02M_ROOT_DISK:-/dev/mmcblk2}"
PART_NUM="${SA02M_ROOT_PART_NUM:-2}"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

need_expand() {
    local part_bytes disk_bytes
    part_bytes=$(blockdev --getsize64 "$ROOT_PART" 2>/dev/null || echo 0)
    disk_bytes=$(blockdev --getsize64 "$ROOT_DISK" 2>/dev/null || echo 0)
    [ "$part_bytes" -gt 0 ] && [ "$disk_bytes" -gt 0 ] || return 1
    [ "$part_bytes" -lt $((disk_bytes * 85 / 100)) ]
}

mask_watchdogs() {
    for svc in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog; do
        systemctl stop "$svc" 2>/dev/null || true
        systemctl mask "$svc" 2>/dev/null || true
    done
}

expand_partition() {
    local capacity lastsector
    # partprobe + settle ДО чтения — иначе на первом boot таблица разделов
    # ещё не полностью прочитана и `parted print` возвращает пустой capacity
    # (лог: "expand /dev/mmcblk2 p2 -> end -2048s (disk s)").
    partprobe "$ROOT_DISK" 2>/dev/null || true
    udevadm settle --timeout=5 2>/dev/null || true

    # `parted -ms ... print unit s` — обязателен `print` (без него на некоторых
    # версиях parted 3.4 stdout остаётся пустым).
    capacity=$(parted -ms "$ROOT_DISK" unit s print 2>/dev/null \
        | awk -F: '/^\/dev\//{gsub(/s$/,"",$2); print $2; exit}')
    if [ -z "$capacity" ] || [ "$capacity" = "0" ]; then
        log "FAILED: cannot read disk capacity from parted"
        return 1
    fi
    lastsector=$((capacity - 2048))
    log "expand $ROOT_DISK p${PART_NUM} -> end ${lastsector}s (disk ${capacity}s)"

    # ВНИМАНИЕ: `growpart` требует `sfdisk` (пакет fdisk), которого нет в
    # SA-02m minbase rootfs. `parted resizepart` работает автономно — этот
    # путь надёжнее.
    parted -s "$ROOT_DISK" unit s resizepart "$PART_NUM" "$lastsector"
    partprobe "$ROOT_DISK" 2>/dev/null || true
    udevadm settle --timeout=5 2>/dev/null || true
}

case "${1:-start}" in
    start)
        mkdir -p "$(dirname "$LOG")"
        [ -f "$DONE" ] && exit 0
        [ -b "$ROOT_PART" ] && [ -b "$ROOT_DISK" ] || exit 0
        need_expand || { log "rootfs already uses eMMC, nothing to do"; touch "$DONE"; exit 0; }

        log "start: root partition smaller than eMMC"
        mask_watchdogs
        expand_partition
        log "resize2fs $ROOT_PART"
        resize2fs "$ROOT_PART" | tee -a "$LOG"
        touch "$DONE"
        log "done: $(df -h / | tail -1)"
        systemctl disable sa02m-rootfs-expand.service 2>/dev/null || true
        ;;
    *)
        echo "Usage: $0 start" >&2
        exit 2
        ;;
esac
