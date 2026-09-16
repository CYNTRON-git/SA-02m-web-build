#!/bin/bash
# SA-02m: expand root partition/filesystem after PiShrink clone (first boot).
# Runs after local-fs; must NOT block networking (see unit Before=).
# Watchdogs ordered After this unit — do NOT systemctl stop them (that cancels
# their queued start for the whole boot).
# Before finish: force the kernel to rewrite the root primary superblock and
# verify its checksum ON DISK (see ensure_primary_sb_checksum) — the board
# kernel leaves it stale after an online resize, and a power cut before the
# first clean reboot then bricks the clone (docs/bugs/BUGLOG.md 2026-09-16).
set -euo pipefail

LOG=/var/log/sa02m-rootfs-expand.log
DONE=/var/lib/sa02m-rootfs-expand.done
CSUM_BAD=/var/lib/sa02m-rootfs-expand.csum-bad
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

# ── Primary superblock checksum guard ─────────────────────────────────────
# The board kernel (6.1.0-rc6) predates upstream "ext4: fix bad checksum after
# online resize" (Baokun Li, 2022-11-16, fs/ext4/resize.c ext4_update_super):
# the primary superblock checksum was computed BEFORE s_overhead_clusters was
# updated, so after resize2fs on the mounted root the ON-DISK primary
# superblock carries a stale crc32c until the kernel next rewrites it (clean
# unmount / remount / freeze). A soft reboot hides it; a power cut before the
# first clean shutdown leaves a root the kernel refuses to mount
# ("Superblock checksum does not match") — reproduced 2/2 on the bench.
# Fix here: force the rewrite now (freeze/unfreeze), then verify the bytes on
# the medium, never the page cache. Tools only the board has: coreutils dd,
# util-linux fsfreeze, python3 stdlib (crc32c in pure python — no pip).

# crc32c-verify an ext4 primary superblock read from stdin (the partition's
# first 4 KiB; the superblock is bytes 1024..2047, its crc32c — Castagnoli,
# seed ~0, no final inversion — covers the first 1020 of them and is stored
# LE32 in the last 4). Prints one line: OK|BAD|NOCSUM|ERR + both checksums.
# Exit 0 = OK/NOCSUM (metadata_csum off: nothing the kernel would verify),
# 1 = BAD, 2 = unreadable.
SB_CSUM_PY='
import struct, sys
blk = sys.stdin.buffer.read()
if len(blk) < 2048:
    print("ERR short read (%d bytes)" % len(blk)); sys.exit(2)
sb = blk[1024:2048]
if struct.unpack_from("<H", sb, 0x38)[0] != 0xEF53:
    print("ERR no ext4 magic at offset 1024"); sys.exit(2)
if not struct.unpack_from("<I", sb, 0x64)[0] & 0x400:
    print("NOCSUM metadata_csum feature off, nothing to verify"); sys.exit(0)
crc = 0xFFFFFFFF
for b in sb[:1020]:
    crc ^= b
    for _ in range(8):
        crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
stored = struct.unpack_from("<I", sb, 1020)[0]
print("%s stored=0x%08x computed=0x%08x" % ("OK" if crc == stored else "BAD", stored, crc))
sys.exit(0 if crc == stored else 1)
'
sb_csum_of_block() {
    python3 -c "$SB_CSUM_PY"
}

# The on-disk primary superblock of $1. O_DIRECT is the point: a buffered read
# is answered by the page cache, which holds the copy the kernel THINKS it has.
primary_sb_checksum() {  # $1=block device
    dd if="$1" bs=4096 count=1 iflag=direct 2>/dev/null | sb_csum_of_block
}

# Make the kernel rewrite the primary superblock with a fresh checksum:
# ext4_freeze() and ext4_unfreeze() both run ext4_commit_super(), and the
# freeze also flushes the journal, so no stale journaled copy can be replayed
# over the fresh one on the next mount. NOTHING may write to / between -f and
# -u — not even log(): a tee onto a frozen root blocks until the timeout.
force_primary_sb_rewrite() {
    sync
    if ! command -v fsfreeze >/dev/null 2>&1; then
        log "ERROR: fsfreeze not found — cannot force the superblock rewrite"
        return 1
    fi
    if ! timeout 20 fsfreeze -f /; then
        # A -f killed by the timeout AFTER the ioctl took effect would leave /
        # frozen for good; -u on an unfrozen fs is a harmless EINVAL.
        timeout 20 fsfreeze -u / 2>/dev/null || true
        log "ERROR: fsfreeze -f / failed"
        return 1
    fi
    if ! timeout 20 fsfreeze -u /; then
        log "ERROR: fsfreeze -u / failed — root may still be frozen"
        return 1
    fi
    sync
    log "freeze/unfreeze cycle done: primary superblock rewritten by the kernel"
}

# Rewrite, verify on disk, retry once. Still not OK: loud ERROR, marker file
# for the failure monitor/audit, return 1 — the caller still sets DONE so the
# next boot is not delayed by a resize that already happened.
ensure_primary_sb_checksum() {
    local attempt state rc
    for attempt in 1 2; do
        force_primary_sb_rewrite || true   # ERROR already logged; verify anyway
        rc=0
        state=$(primary_sb_checksum "$ROOT_PART") || rc=$?
        log "primary superblock checksum ${state:-ERR (no output)} (attempt $attempt)"
        if [ "$rc" -eq 0 ]; then
            rm -f "$CSUM_BAD"
            return 0
        fi
    done
    log "ERROR: primary superblock checksum still not OK after 2 freeze cycles — a power cut before the next clean reboot will brick this board (marker: $CSUM_BAD)"
    touch "$CSUM_BAD"
    return 1
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
        csum_rc=0
        if ! need_expand; then
            log "rootfs already uses eMMC, nothing to resize"
            ensure_primary_sb_checksum || csum_rc=1
            finish_firstboot
            exit "$csum_rc"
        fi

        log "start: root partition smaller than eMMC (networking not blocked)"
        mask_armbian_resize_files
        # Do NOT systemctl stop watchdogs — with Before= that cancels their
        # queued start for the entire boot (net-watchdog stays dead).
        expand_partition
        log "resize2fs $ROOT_PART"
        resize2fs "$ROOT_PART" | tee -a "$LOG"
        log "done: $(df -h / | tail -1)"
        ensure_primary_sb_checksum || csum_rc=1
        finish_firstboot
        exit "$csum_rc"
        ;;
    *)
        echo "Usage: $0 start" >&2
        exit 2
        ;;
esac
