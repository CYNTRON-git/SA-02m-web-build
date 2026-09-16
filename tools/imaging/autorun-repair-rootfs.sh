#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
#  SA-02m  •  autorun-repair-rootfs.sh — repair a bricked clone IN PLACE
#
#  WHAT. Repairs the root filesystem (eMMC p2) of a board that booted once
#  after flashing, was power-cut, and now never comes up (no link, no
#  console login): the primary ext4 superblock carries a STALE checksum left
#  by the first-boot online resize2fs on the board kernel 6.1.0-rc6 (upstream
#  fix: "ext4: fix bad checksum after online resize", Baokun Li, 2022-11-16;
#  our guard: etc/sa02m-rootfs-expand.sh ensure_primary_sb_checksum, shipped
#  from 1.0.6.47). The kernel refuses the mount ("Superblock checksum does
#  not match") and panics before networking; the backup superblocks are
#  valid, and e2fsck rewrites the primary from them. No reflash, the board's
#  data survives. Recovery procedure and the field symptom: docs/deployment.md
#  «Первая загрузка клона»; the record: docs/bugs/BUGLOG.md 2026-09-16.
#
#  WHEN. Only on a board that does not boot AND was flashed from an image
#  whose first boot ran BEFORE 1.0.6.47 (a board that reached one clean
#  reboot is not affected — the kernel rewrote the superblock). A board that
#  boots does not need this and must not run it (see the mount rule below).
#
#  HOW. Runs on the FEL / USB receiver initramfs (busybox sh + e2fsprogs
#  1.46 — there is NO bash, NO blockdev, NO lsblk, NO python here; keep it
#  that way), with the board's eMMC exposed as /dev/mmcblk2 and NOT mounted:
#     1. copy this file onto the flash media and run it from the receiver
#        console:  sh /mnt/autorun-repair-rootfs.sh   (or as autorun.sh —
#        tools/imaging/README.md «Вариант C» is the manual-autorun flow);
#     2. it writes evidence and logs next to itself (repair-N/ on the
#        media; /tmp/repair if the media is read-only) — BEFORE and AFTER;
#     3. e2fsck -fy runs TWICE, deliberately: on the bench the first pass
#        only rewrote the primary superblock from the group-1 backup and
#        exited 12 with «unable to set superblock flags» (e2fsck refuses to
#        flag a superblock it has just replaced); the second pass runs the
#        full check on the now-valid primary and exits 0/1;
#     4. verdict = `e2fsck -n -f` AFTER the repair (0 = clean), then the fs
#        is mounted READ-ONLY to copy the systemd journal and the first-boot
#        markers into the evidence dir, and unmounted;
#     5. sync, exit. Power-cycle the board without the FEL media; it boots
#        into the same rootfs it had.
#
#  NEVER: writes the image on the media, touches p1 (boot), SPL/MBR, or the
#  media's own files other than its repair-N/ dir. Only p2's metadata is
#  changed, and only by e2fsck.
#
#  Idempotent: a second run on a repaired board finds a clean fs (both
#  passes are no-ops, verdict 0) and adds one more evidence dir. Every call
#  that can hang (e2fsck, dumpe2fs, mount, cp) is bounded by `timeout`
#  where the receiver's busybox provides it (it does on ours; a busybox
#  built without the applet runs unbounded — logged once at start).
#  Line endings: LF only (.gitattributes) — a CR breaks busybox sh.
# ═══════════════════════════════════════════════════════════════════════════
set -u

P2=${P2:-/dev/mmcblk2p2}
# Backup superblock of block group 1: block 32768 with 4 KiB blocks and
# 32768 blocks per group (our images; `dumpe2fs -h` prints both).
BACKUP_SB_BLOCK=32768
FSCK_T=${FSCK_T:-1800}      # seconds; e2fsck -f on ~7 GiB eMMC takes 1-3 min
SHORT_T=60

SELF="$(readlink -f "$0" 2>/dev/null || echo "$0")"
OUT_BASE="$(dirname "$SELF")"
touch "$OUT_BASE/.rwtest" 2>/dev/null || mount -o remount,rw "$OUT_BASE" 2>/dev/null || true
rm -f "$OUT_BASE/.rwtest" 2>/dev/null
n=1; while [ -d "$OUT_BASE/repair-$n" ]; do n=$((n+1)); done
OUT="$OUT_BASE/repair-$n"
mkdir -p "$OUT" 2>/dev/null || { OUT=/tmp/repair; mkdir -p "$OUT"; }

log() { echo "[$(date '+%H:%M:%S' 2>/dev/null || echo 0)] $*" | tee -a "$OUT/collector.log"; }
die() { log "FATAL: $*"; sync; exit 1; }

if command -v timeout >/dev/null 2>&1; then
    bounded() { timeout "$@"; }
else
    bounded() { shift; "$@"; }
    log "WARN: no timeout applet in this busybox — e2fsck/mount run unbounded"
fi

log "start; target=$P2 evidence=$OUT"
[ -b "$P2" ] || die "$P2 is not a block device — is the eMMC exposed to this initramfs?"
if grep -q "^$P2 " /proc/mounts 2>/dev/null; then
    die "$P2 is mounted — this tool runs only from the receiver initramfs with the rootfs offline"
fi
# The receiver may still export the eMMC over USB (g_mass_storage); take it back.
rmmod -f g_mass_storage 2>/dev/null || true; sleep 1

# ── evidence BEFORE repair: first 256 KiB of p2 (primary sb + GDT), the
#    group-1 backup sb, e2fsck -n and dumpe2fs of both superblocks ──────────
dd if="$P2" bs=4096 count=64 2>/dev/null > "$OUT/p2-head-256k.before.bin" || log "dd p2 head failed"
dd if="$P2" bs=4096 skip=$BACKUP_SB_BLOCK count=2 2>/dev/null > "$OUT/p2-backup-sb-g1.before.bin" || log "dd backup sb failed"
( bounded $FSCK_T e2fsck -n -f "$P2" 2>&1; echo "exit=$?" ) > "$OUT/e2fsck-n.before.txt" || true
( bounded $SHORT_T dumpe2fs -h "$P2" 2>&1 ) > "$OUT/dumpe2fs-primary.before.txt" || true
( bounded $SHORT_T dumpe2fs -h -o superblock=$BACKUP_SB_BLOCK -o blocksize=4096 "$P2" 2>&1 ) > "$OUT/dumpe2fs-backup.before.txt" || true
sync
log "before: e2fsck -n $(tail -1 "$OUT/e2fsck-n.before.txt")"

# ── repair: two passes (see HOW, step 3) ───────────────────────────────────
log "e2fsck -fy $P2 (pass 1: primary superblock from backup)"
( bounded $FSCK_T e2fsck -fy "$P2" 2>&1; echo "exit=$?" ) > "$OUT/e2fsck-fy.pass1.txt" || true
sync
log "pass 1: $(tail -1 "$OUT/e2fsck-fy.pass1.txt")"
log "e2fsck -fy $P2 (pass 2: full check on the rewritten primary)"
( bounded $FSCK_T e2fsck -fy "$P2" 2>&1; echo "exit=$?" ) > "$OUT/e2fsck-fy.pass2.txt" || true
sync
log "pass 2: $(tail -1 "$OUT/e2fsck-fy.pass2.txt")"

# ── evidence AFTER repair + the verdict ────────────────────────────────────
dd if="$P2" bs=4096 count=64 2>/dev/null > "$OUT/p2-head-256k.after.bin" || true
( bounded $FSCK_T e2fsck -n -f "$P2" 2>&1; echo "exit=$?" ) > "$OUT/e2fsck-n.after.txt" || true
( bounded $SHORT_T dumpe2fs -h "$P2" 2>&1 ) > "$OUT/dumpe2fs-primary.after.txt" || true
verdict=$(tail -1 "$OUT/e2fsck-n.after.txt")
if [ "$verdict" = "exit=0" ]; then
    log "REPAIRED: e2fsck -n -f is clean (exit=0)"
else
    log "NOT CLEAN after two passes: e2fsck -n -f $verdict — read $OUT/e2fsck-n.after.txt before reflashing"
fi

# ── mount READ-ONLY, copy the journal + first-boot markers (evidence only) ──
mkdir -p /tmp/p2
if bounded $SHORT_T mount -o ro "$P2" /tmp/p2 2>>"$OUT/collector.log"; then
    log "p2 mounted ro"
    mkdir -p "$OUT/journal"
    bounded $FSCK_T cp -a /tmp/p2/var/log/journal/. "$OUT/journal/" 2>>"$OUT/collector.log" || true
    for f in var/log/sa02m-rootfs-expand.log var/log/sa02m-reboot-reason.log var/log/syslog var/log/kern.log \
             etc/machine-id etc/fake-hwclock.data var/lib/sa02m-rootfs-expand.done \
             var/lib/sa02m-rootfs-expand.csum-bad var/lib/sa02m-clean-shutdown \
             etc/ssh/ssh_host_ed25519_key.pub; do
        [ -e "/tmp/p2/$f" ] && { mkdir -p "$OUT/files/$(dirname "$f")"; cp -a "/tmp/p2/$f" "$OUT/files/$f" 2>/dev/null || true; }
    done
    ls -la /tmp/p2/var/lib/ 2>/dev/null | grep -i sa02m > "$OUT/var-lib-sa02m.txt" 2>&1 || true
    ls -la /tmp/p2/lost+found > "$OUT/lost+found.txt" 2>&1 || true
    umount /tmp/p2 || log "WARN: umount /tmp/p2 failed"
else
    log "p2 mount FAILED even after repair — the fs is not the only problem; keep $OUT and reflash"
fi
sync; sync
log "done -> $OUT"
exit 0
