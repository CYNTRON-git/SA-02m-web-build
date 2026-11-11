#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-bootscr-apply.sh — put the shipped U-Boot boot script on an INSTALLED
# board: rebuild boot.scr from /usr/local/share/sa02m/boot.cmd.sa02m.min and
# write it to BOTH homes U-Boot depends on — the canonical copy
# /usr/local/share/sa02m/boot.scr and the live FAT copy.
#
# Run BY HAND as root on the board, with the Operator at the bench; nothing in
# the installer, OTA, the offline pack or www-only calls it (Operator decision
# В2 (a), 2026-11-11). boot.scr is the one file without which the board does
# not boot at all (recovery = FEL), so it changes only under a human's eye.
#
# Why it exists (audit 2026-11-11 A1-01 / A3-01): the kernel parameter
# watchdog.stop_on_reboot=0 — the half of the hung-reboot fix that stops the
# kernel disarming the hardware watchdog in kernel_restart_prepare() (notes:
# .ai-dev/notes/reboot-hang-2026-10-21.md) — reaches only boards built from a
# NEW image; an installed board keeps its boot.scr forever, and a FAT copy
# edited alone is put back from the canonical copy by sa02m-restore-dtb.sh at
# the next boot.
#
# Order and atomicity: the CANONICAL copy first (ext4: tmp + fsync + rename),
# THEN the FAT copy (tmp in the FAT dir + sync + byte check + rename + sync —
# the write_fat_zimage idiom). A power cut after the canonical and before the
# FAT write is finished at the next boot by sa02m-restore-dtb.sh (itself
# atomic); a cut inside the FAT write leaves the OLD name intact. Residual: a
# vfat rename is not journaled — a cut exactly on the directory-entry update is
# not covered. Both old copies are backed up first to
# $BACKUP_DIR/bootscr-backup-<ts>/ (boot.scr.fat, boot.scr.canonical).
#
# The image is built by the repo's own writer (uboot-script.py, the format's
# one home, installed to /usr/local/lib/sa02m/ by scripts/01-system.sh) and
# validated before anything is written. The payload must carry
# watchdog.stop_on_reboot=0 on its first `setenv bootargs` line — anything
# else is refused (this tool exists to put that parameter on the board).
#
# Commands:
#   check   no root needed: prints whether /proc/cmdline carries the parameter,
#           /sys/module/watchdog/parameters/stop_on_reboot, and the md5 of the
#           canonical and FAT copies (FAT only when it is mounted or when run as
#           root). Exit 0 = parameter active (cmdline AND stop_on_reboot = 0);
#           1 = not active; 2 = /proc/cmdline unreadable.
#   apply   root. Exit 0 = written (reboot needed) or already applied; 3 =
#           prerequisites (root, payload, writer, python3) or the lock; 4 = FAT
#           not mountable; 5 = a write failed (see the order above for what
#           state that leaves).
# Rollback: copy the backup back (boot.scr.canonical -> CANON_BOOTSCR,
# boot.scr.fat -> FAT boot.scr) and reboot. Delivery and procedure:
# docs/deployment.md «Пути деплоя» (boot.scr).
#
# Path constants are single-assignment literals on purpose — no env seam on a
# root tool: scripts/dev/test-bootscr-apply.sh sed-retargets them into a sandbox.
# ═══════════════════════════════════════════════════════════════════════════
set -u

FAT_DEV=/dev/mmcblk2p1
FAT_MNT=/mnt/fat
CANON_BOOTSCR=/usr/local/share/sa02m/boot.scr
PAYLOAD=/usr/local/share/sa02m/boot.cmd.sa02m.min
UBOOT_PY=/usr/local/lib/sa02m/uboot-script.py
BACKUP_DIR=/root
PROC_CMDLINE=/proc/cmdline
WDT_PARAM=/sys/module/watchdog/parameters/stop_on_reboot
LOCK_DIR=/run/lock/sa02m-bootscr-apply.d
LOG=/var/log/sa02m_install.log

KPARAM=watchdog.stop_on_reboot=0
FAT_NAME=boot.scr

T=""
HAVE_LOCK=0
FAT_MOUNTED=0

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-bootscr-apply: $*" >> "$LOG" 2>/dev/null || true; }

usage() {
    echo "Использование: $(basename "$0") check | apply" >&2
    exit 2
}

cleanup() {
    [ -n "$T" ] && rm -rf "$T"
    if [ "$FAT_MOUNTED" = 1 ]; then umount "$FAT_MNT" 2>/dev/null || true; fi
    if [ "$HAVE_LOCK" = 1 ]; then rmdir "$LOCK_DIR" 2>/dev/null || true; fi
}

mount_fat() {
    if mountpoint -q "$FAT_MNT" 2>/dev/null; then
        return 0
    fi
    mkdir -p "$FAT_MNT" || return 1
    mount "$FAT_DEV" "$FAT_MNT" || return 1
    FAT_MOUNTED=1
}

# Same script image, ignoring the 64-byte legacy header: the writer stamps the
# build time into it, so two builds of the same payload differ only there.
# `cmp -i 64` (GNU diffutils) compares from byte 64 of both files on.
same_script() {  # $1 $2 = boot.scr files
    [ -f "$1" ] && [ -f "$2" ] && cmp -s -i 64 "$1" "$2"
}

md5_of() {  # $1 = file -> md5 or a word saying why not
    if [ -f "$1" ]; then
        md5sum < "$1" | awk '{print $1}'
    else
        echo "нет файла"
    fi
}

# First active `setenv bootargs` line of the payload carries the parameter.
payload_has_param() {
    local line
    line=$(grep -E '^[[:space:]]*setenv[[:space:]]+bootargs([[:space:]]|$)' "$PAYLOAD" 2>/dev/null) || return 1
    line=${line%%$'\n'*}
    case " $line " in
        *[[:space:]]"$KPARAM"[[:space:]]*) return 0 ;;
    esac
    return 1
}

cmd_check() {
    local cmdline wdt in_cmdline=0 rc
    if ! cmdline=$(cat "$PROC_CMDLINE" 2>/dev/null); then
        echo "Не читается $PROC_CMDLINE" >&2
        return 2
    fi
    case " $cmdline " in *[[:space:]]"$KPARAM"[[:space:]]*) in_cmdline=1 ;; esac
    wdt=$(cat "$WDT_PARAM" 2>/dev/null) || wdt="нет параметра"
    if [ "$in_cmdline" = 1 ]; then
        echo "Параметр ядра $KPARAM: есть в /proc/cmdline"
    else
        echo "Параметр ядра $KPARAM: НЕТ в /proc/cmdline"
    fi
    echo "stop_on_reboot: $wdt"
    echo "boot.scr (каноническая копия, $CANON_BOOTSCR): md5 $(md5_of "$CANON_BOOTSCR")"
    if mountpoint -q "$FAT_MNT" 2>/dev/null; then
        echo "boot.scr (FAT, $FAT_MNT/$FAT_NAME): md5 $(md5_of "$FAT_MNT/$FAT_NAME")"
    elif [ "$(id -u)" = 0 ] && mount_fat; then
        echo "boot.scr (FAT, $FAT_MNT/$FAT_NAME): md5 $(md5_of "$FAT_MNT/$FAT_NAME")"
    else
        echo "boot.scr (FAT): не смонтирован — md5 только от root"
    fi
    if payload_has_param; then
        echo "Payload $PAYLOAD: несёт $KPARAM"
    else
        echo "Payload $PAYLOAD: без $KPARAM (или нет файла)"
    fi
    rc=1
    [ "$in_cmdline" = 1 ] && [ "$wdt" = 0 ] && rc=0
    return "$rc"
}

# Rename-over on ext4: tmp in the same dir, fsync (sync FILE), rename, fsync dir.
write_canonical() {  # $1 = new image
    local tmp
    tmp="$(dirname "$CANON_BOOTSCR")/.$(basename "$CANON_BOOTSCR").new"
    mkdir -p "$(dirname "$CANON_BOOTSCR")" || return 1
    if cp -f "$1" "$tmp" && chmod 644 "$tmp" && { sync -- "$tmp" 2>/dev/null || sync; } \
       && mv -f "$tmp" "$CANON_BOOTSCR"; then
        sync -- "$(dirname "$CANON_BOOTSCR")" 2>/dev/null || sync
        return 0
    fi
    rm -f "$tmp" 2>/dev/null || true
    return 1
}

# FAT: tmp in the FAT dir, sync, byte check, rename, sync.
write_fat() {  # $1 = new image
    local dst="$FAT_MNT/$FAT_NAME" tmp
    tmp="$FAT_MNT/.$FAT_NAME.new"
    if cp -f "$1" "$tmp" && sync && cmp -s "$1" "$tmp" && mv -f "$tmp" "$dst"; then
        sync
        return 0
    fi
    rm -f "$tmp" 2>/dev/null || true
    return 1
}

cmd_apply() {
    local new ts bk m_new m_fat m_can
    if [ "$(id -u)" != 0 ]; then
        echo "Нужен root (apply пишет загрузочный скрипт)" >&2
        return 3
    fi
    if [ ! -f "$PAYLOAD" ]; then
        echo "Нет payload $PAYLOAD — сначала полная установка (install.sh --refresh) этой версии" >&2
        return 3
    fi
    if ! payload_has_param; then
        echo "Payload $PAYLOAD не несёт $KPARAM в строке setenv bootargs — отказ, ничего не записано" >&2
        return 3
    fi
    if [ ! -f "$UBOOT_PY" ] || ! command -v python3 >/dev/null 2>&1; then
        echo "Нет $UBOOT_PY или python3 — собрать и проверить boot.scr нечем" >&2
        return 3
    fi
    if ! command -v mountpoint >/dev/null 2>&1; then
        echo "Нет mountpoint (util-linux)" >&2
        return 3
    fi
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "Уже выполняется (lock $LOCK_DIR; после прерванного запуска — rmdir $LOCK_DIR)" >&2
        return 3
    fi
    HAVE_LOCK=1
    T=$(mktemp -d) || { echo "mktemp не удался" >&2; return 3; }

    new="$T/boot.scr"
    if ! python3 "$UBOOT_PY" build "$PAYLOAD" "$new" --name SA-02m >"$T/build.out" 2>&1 \
       || ! python3 "$UBOOT_PY" validate "$new" >"$T/validate.out" 2>&1; then
        echo "Сборка/проверка boot.scr не прошла — ничего не записано: $(tr '\n' ' ' < "$T/build.out" "$T/validate.out" 2>/dev/null)" >&2
        return 3
    fi
    if ! mount_fat; then
        echo "Не удалось смонтировать $FAT_DEV в $FAT_MNT" >&2
        return 4
    fi
    # The two copies must end up byte-identical (sa02m-restore-dtb.sh compares
    # them by md5 at every boot): a canonical copy that already carries this
    # script keeps its header and becomes the image for FAT too.
    if same_script "$CANON_BOOTSCR" "$new"; then
        cp -f "$CANON_BOOTSCR" "$new" || { echo "Не читается $CANON_BOOTSCR" >&2; return 3; }
    fi
    m_new=$(md5_of "$new")
    m_can=$(md5_of "$CANON_BOOTSCR")
    m_fat=$(md5_of "$FAT_MNT/$FAT_NAME")
    if [ "$m_can" = "$m_new" ] && [ "$m_fat" = "$m_new" ]; then
        echo "boot.scr уже применено (обе копии md5 $m_new) — ничего не записано"
        return 0
    fi

    ts=$(date +%Y%m%d%H%M%S)
    bk="$BACKUP_DIR/bootscr-backup-$ts"
    if ! mkdir -p "$bk" \
       || { [ -f "$FAT_MNT/$FAT_NAME" ] && ! cp -f "$FAT_MNT/$FAT_NAME" "$bk/boot.scr.fat"; } \
       || { [ -f "$CANON_BOOTSCR" ] && ! cp -f "$CANON_BOOTSCR" "$bk/boot.scr.canonical"; }; then
        echo "Бэкап в $bk не записан — ничего не изменено" >&2
        return 5
    fi
    sync

    if [ "$m_can" != "$m_new" ]; then
        if ! write_canonical "$new"; then
            echo "Каноническая копия $CANON_BOOTSCR не записана — FAT не тронут; бэкап $bk" >&2
            log "apply: canonical write FAILED (FAT untouched), backup $bk"
            return 5
        fi
    fi
    if [ "$m_fat" != "$m_new" ]; then
        if ! write_fat "$new"; then
            echo "FAT-копия не заменена (старая цела); каноническая уже новая — sa02m-restore-dtb допишет FAT при загрузке. Бэкап $bk" >&2
            log "apply: FAT write FAILED after the canonical copy was written, backup $bk"
            return 5
        fi
    fi
    m_can=$(md5_of "$CANON_BOOTSCR")
    m_fat=$(md5_of "$FAT_MNT/$FAT_NAME")
    if [ "$m_can" != "$m_new" ] || [ "$m_fat" != "$m_new" ]; then
        echo "Сверка md5 не сошлась: новая $m_new, каноническая $m_can, FAT $m_fat — бэкап $bk" >&2
        log "apply: md5 mismatch after write (new=$m_new canonical=$m_can fat=$m_fat), backup $bk"
        return 5
    fi
    log "apply: boot.scr with $KPARAM written (md5 $m_new), backup $bk"
    echo "boot.scr записан (md5 $m_new), бэкап $bk"
    echo "Требуется перезагрузка; затем $(basename "$0") check"
    return 0
}

trap cleanup EXIT
case "${1:-}" in
    check) cmd_check ;;
    apply) cmd_apply ;;
    *) usage ;;
esac
