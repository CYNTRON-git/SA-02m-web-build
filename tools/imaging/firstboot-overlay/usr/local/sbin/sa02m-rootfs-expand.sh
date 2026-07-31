#!/bin/bash
# SA-02m: expand root partition/filesystem after PiShrink clone (first boot).
# Runs early after local-fs; must NOT block networking (see unit Before=).
# Watchdogs that reboot on load are ordered After this unit.
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

# Prefer stop-only: `systemctl mask` replaces /etc unit files with /dev/null and
# permanently destroys units that live only under /etc/systemd/system/.
stop_watchdogs() {
    for svc in $WATCHDOGS; do
        systemctl stop "$svc" 2>/dev/null || true
    done
}

restore_watchdogs() {
    # Enable only — never systemctl start/restart here (deadlock if Before= those units).
    for svc in $WATCHDOGS; do
        if [ -L "/etc/systemd/system/${svc}.service" ] \
           && [ "$(readlink "/etc/systemd/system/${svc}.service" 2>/dev/null)" = "/dev/null" ]; then
            rm -f "/etc/systemd/system/${svc}.service"
        fi
        systemctl unmask "$svc" 2>/dev/null || true
        systemctl enable "$svc" 2>/dev/null || true
    done
}

# Avoid dual-resize udev storms (armbian-resize || sa02m-rootfs-expand in parallel).
disable_armbian_resize() {
    systemctl stop armbian-resize-filesystem.service 2>/dev/null || true
    systemctl disable armbian-resize-filesystem.service 2>/dev/null || true
    systemctl mask armbian-resize-filesystem.service 2>/dev/null || true
}

expand_partition() {
    local capacity lastsector
    # partprobe + settle ДО чтения — иначе на первом boot таблица разделов
    # ещё не полностью прочитана и `parted print` возвращает пустой capacity
    # (лог: "expand /dev/mmcblk2 p2 -> end -2048s (disk s)").
    partprobe "$ROOT_DISK" 2>/dev/null || true
    udevadm settle --timeout=5 2>/dev/null || true

    # `parted -ms ... print unit s` — обязателен `print`.
    capacity=$(parted -ms "$ROOT_DISK" unit s print 2>/dev/null \
        | awk -F: '/^\/dev\//{gsub(/s$/,"",$2); print $2; exit}')
    if [ -z "$capacity" ] || [ "$capacity" = "0" ]; then
        log "FAILED: cannot read disk capacity from parted"
        return 1
    fi
    lastsector=$((capacity - 2048))
    log "expand $ROOT_DISK p${PART_NUM} -> end ${lastsector}s (disk ${capacity}s)"

    # growpart needs sfdisk (not in minbase); parted resizepart is autonomous.
    parted -s "$ROOT_DISK" unit s resizepart "$PART_NUM" "$lastsector"
    partprobe "$ROOT_DISK" 2>/dev/null || true
    udevadm settle --timeout=5 2>/dev/null || true
}

finish_firstboot() {
    touch "$DONE"
    disable_armbian_resize
    restore_watchdogs
    # ConditionPathExists=!DONE skips next boots; disable must not block oneshot.
    systemctl --no-block disable sa02m-rootfs-expand.service 2>/dev/null || true
}

case "${1:-start}" in
    start)
        mkdir -p "$(dirname "$LOG")"
        [ -f "$DONE" ] && exit 0
        [ -b "$ROOT_PART" ] && [ -b "$ROOT_DISK" ] || exit 0
        if ! need_expand; then
            log "rootfs already uses eMMC, nothing to do"
            finish_firstboot
            exit 0
        fi

        log "start: root partition smaller than eMMC (networking not blocked)"
        disable_armbian_resize
        stop_watchdogs
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
