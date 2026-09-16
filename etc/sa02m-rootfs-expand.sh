#!/bin/bash
# SA-02m: expand root partition/filesystem after PiShrink clone (first boot).
# Runs after local-fs; must NOT block networking (see unit Before=).
# Watchdogs ordered After this unit — do NOT systemctl stop them (that cancels
# their queued start for the whole boot).
# Before finish: force the kernel to rewrite the root primary superblock and
# verify its checksum ON DISK (see ensure_primary_sb_checksum) — the board
# kernel leaves it stale after an online resize, and a power cut before the
# first clean reboot then bricks the clone (docs/bugs/BUGLOG.md 2026-09-16).
# Evidence that must outlive a power cut — the verdict file, DONE, the log —
# is fsync'd explicitly (sync_files): / is mounted commit=600 with the
# superblock default journal_data_writeback, so a write not fsync'd within
# ~10 min is simply gone after a cut (bench 2026-09-16: every first-boot log
# and the persistent journal came back 0 bytes; BUGLOG.md 2026-09-16 16:40).
set -euo pipefail

LOG=/var/log/sa02m-rootfs-expand.log
DONE=/var/lib/sa02m-rootfs-expand.done
CSUM_BAD=/var/lib/sa02m-rootfs-expand.csum-bad
RESULT=/var/lib/sa02m-rootfs-expand.result
ROOT_PART="${SA02M_ROOT_PART:-/dev/mmcblk2p2}"
ROOT_DISK="${SA02M_ROOT_DISK:-/dev/mmcblk2}"
PART_NUM="${SA02M_ROOT_PART_NUM:-2}"
WATCHDOGS="sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# Push FILEs (or directories) to the medium: coreutils `sync FILE` fsyncs
# each one (Debian bookworm, coreutils 9.x); a sync without FILE support
# falls back to a global sync. Never fails — the verdict is already decided,
# durability is best-effort on top of it.
sync_files() {
    sync "$@" 2>/dev/null || sync || true
}

# The durable verdict: ONE line, rewritten never appended, staged in a temp
# file that is fsync'd BEFORE the rename over the previous one, so a cut at
# any instant leaves the old line or the new — never a 0-byte file (the
# `install -m` lesson, BUGLOG.md 2026-09-08). Consumers: the repair tool's
# evidence copy (tools/imaging/autorun-repair-rootfs.sh), the audit, a human
# reading docs/deployment.md §12. No daemon watches it.
write_result() {  # $1=verdict text: "<OK|BAD|NOCSUM|ERR> … attempts=N"
    printf '%s %s\n' "$(date '+%FT%T%z')" "$1" > "$RESULT.tmp"
    sync_files "$RESULT.tmp"
    mv -f "$RESULT.tmp" "$RESULT"
}

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
        # Not log(): its tee would write onto the root this branch says may
        # still be frozen. stderr reaches the journal through the unit's
        # socket, never the filesystem.
        echo "[$(date '+%F %T')] ERROR: fsfreeze -u / failed — root may still be frozen" >&2
        return 1
    fi
    sync
    log "freeze/unfreeze cycle done: primary superblock rewritten by the kernel"
}

# Rewrite, verify on disk, retry once; the last attempt's verdict goes to
# $RESULT (durable, see write_result). Still not OK: loud ERROR, the $CSUM_BAD
# marker, return 1 — the caller still sets DONE so the next boot is not delayed
# by a resize that already happened. Nothing watches the marker (the failure
# monitor does not read it): like the verdict line, it is evidence for the
# repair tool's copy, the audit and a human.
ensure_primary_sb_checksum() {
    local attempt state rc
    for attempt in 1 2; do
        force_primary_sb_rewrite || true   # ERROR already logged; verify anyway
        rc=0
        state=$(primary_sb_checksum "$ROOT_PART") || rc=$?
        state=${state:-ERR (no output)}
        log "primary superblock checksum $state (attempt $attempt)"
        if [ "$rc" -eq 0 ]; then
            write_result "$state attempts=$attempt"
            rm -f "$CSUM_BAD"
            return 0
        fi
    done
    write_result "$state attempts=2"
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
    # LAST act: nothing is written after this, so one fsync covers the verdict,
    # DONE, every log line above, and the directory entries (marker included).
    sync_files "$RESULT" "$DONE" "$LOG" "$(dirname "$DONE")"
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
