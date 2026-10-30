#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-dtb-patch.sh — patch the boot DTB on the FAT partition so the eth0
# (EMAC) PHY binds the icplus driver instead of the kernel's "Generic PHY".
#
# Run BY HAND as root on the board; nothing in the installer calls it. Every
# write is backed up first, verified with fdtget BEFORE it lands, and landed by
# tmp + sync + rename-over on the SAME filesystem (the write_fat_zimage idiom of
# sa02m-kernel-select.sh): the live boot name is OLD or NEW at every instant,
# never 0-byte — boot.scr loads exactly that name with no fallback, so a torn
# write would brick the board to U-Boot (recovery = the FEL stand).
#
# Why: the eth0 PHY (IC+ IP101A) answers phy_id 0x00000000 because its ID is
# read while PH12 still holds it in reset, so no driver matches and the generic
# fallback is bound — the structural half of the 2026-10-28 late-cable stall
# (docs/bugs/BUGLOG.md). The fix is the id-form string FIRST in the node's
# compatible (`ethernet-phy-id0243.0c54`): phylib takes the ID from it instead of
# reading. The text edit lives in ONE place, usr/local/sbin/sa02m-dtb-patch.py
# (its header has the kernel facts); this wrapper only moves blobs around it.
#
# Commands:
#   check             read-only, no root, no mount: prints the running DT's EMAC
#                     PHY compatible, phy_id and bound driver (sysfs uevent), the
#                     GMAC PHY's where present, and a ВНИМАНИЕ line for each live
#                     PHY list that is not canonical (a string twice, or no
#                     ethernet-phy-ieee802.3-c22 — a blob from the early patcher).
#                     The warning never changes the exit code: 0 = ICPlus
#                     IP101A|IP101G, 1 = another driver (Generic PHY), 2 =
#                     node/sysfs unreadable. The driver does not bring the link
#                     up by itself under either: on a cold boot the interface
#                     down/up of fix-eth's link cycles does (bench 1.135, BUGLOG
#                     2026-10-30); a lone autoneg restart did not under icplus.
#   apply [--dry-run] root. For EVERY name of DTB_NAMES present on FAT,
#                     independently: dtc decompile -> patcher -> (unchanged =>
#                     "already-patched") -> dtc compile -> fdtget verify (EMAC
#                     compatible[0], reset-deassert-us, the EMAC list canonical
#                     — no string twice, ethernet-phy-ieee802.3-c22 present —,
#                     then GMAC icplus and the same canonical-list rule where the
#                     node exists, size >= 16 KiB and within 8 KiB of the
#                     original) -> backup to BACKUP_DIR/dtb-backup-<name>-<ts>
#                     -> atomic write to FAT -> the same for each canonical copy
#                     that already EXISTS (never created). --dry-run stops after
#                     verify and prints what would change. Idempotent: a second
#                     apply writes nothing and makes no backup.
#                     Exit 0 = every present name patched or already patched;
#                     3 = prerequisites (root, dtc, fdtget, python3, patcher,
#                     mountpoint) or the lock; 4 = FAT not mountable / no DTB
#                     name present (nothing written); 5 = at least one name
#                     failed (that name's FAT file untouched; names that
#                     succeeded stay — each is a complete, verified blob).
#
# Three names may coexist on a board (the default boot name, the opt-in
# boot.cmd.sa02m chain, pack-sa02m-image.sh); each is patched on its own text
# so blobs that differ elsewhere (rtc/usb0_vbus fdtput edits) keep their
# differences. Both canonical-copy homes are refreshed because
# sa02m-restore-dtb.sh (boot + apt hook) would otherwise revert the FAT file
# to the old copy on md5 mismatch.
#
# Rollback: copy /root/dtb-backup-<name>-<ts> back over the FAT name (from a
# shell, or via the FEL stand / autorun-repair-rootfs.sh — docs/deployment.md).
# After apply: reboot, then `check` again.
#
# Test: scripts/dev/test-dtb-patch-tool.sh (quality row dtb-patch-tool) runs
# this file sed-retargeted into a sandbox with dtc/fdtget/python3/mount shims.
# The constants below are single-assignment literals ON PURPOSE (no env seam):
# the harness retargets them, production never does.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

FAT_DEV=/dev/mmcblk2p1
FAT_MNT=/mnt/fat
DTB_NAMES="sun8i-a40i-sk.dtb sun8i-r40-sa02m.dtb sun8i-a40i-nano2e-none-sk.dtb"
CANON_DIRS="/usr/local/share/sa02m /usr/local/share/sa02m/kernel"
BACKUP_DIR=/root
PROC_PHY=/proc/device-tree/soc/mdio@1c0b080/ethernet-phy@0
SYS_PHY=/sys/bus/mdio_bus/devices/1c0b080.mdio-mii:00
PATCHER=/usr/local/sbin/sa02m-dtb-patch.py
LOCK_DIR=/run/lock/sa02m-dtb-patch.d

EMAC_ID_COMPAT=ethernet-phy-id0243.0c54
GMAC_COMPAT=icplus,ip101a
PHY_FALLBACK_COMPAT=ethernet-phy-ieee802.3-c22
DEASSERT_US=500000
SIZE_MIN=16384
SIZE_SLACK=8192
EMAC_PHY_PATH=/soc/mdio@1c0b080/ethernet-phy@0
GMAC_PHY_PATH=/soc/ethernet@1c50000/mdio/ethernet-phy@0

T=""
HAVE_LOCK=0
FAT_MOUNTED=0

usage() {
    echo "Использование: $(basename "$0") check | apply [--dry-run]" >&2
    exit 2
}

cleanup() {
    [ -n "$T" ] && rm -rf "$T"
    if [ "$FAT_MOUNTED" = 1 ]; then umount "$FAT_MNT" 2>/dev/null || true; fi
    if [ "$HAVE_LOCK" = 1 ]; then rmdir "$LOCK_DIR" 2>/dev/null || true; fi
}

# The bound driver's name, read from the device's uevent (a plain file: the
# `driver` symlink needs readlink and the harness host cannot make symlinks).
phy_driver() {  # $1 = sysfs device dir
    local d
    d=$(sed -n 's/^DRIVER=//p' "$1/uevent" 2>/dev/null | head -n 1)
    printf '%s' "${d:-none}"
}

# What is wrong with a PHY compatible list, or nothing for a canonical one:
# a token twice, or the generic c22 binding missing — the two shapes the
# pre-1.0.7.21 patcher left on bench 1.135 (GMAC icplus,ip101a x4, EMAC id
# only; docs/bugs/BUGLOG.md 2026-10-30). One home for verify_blob and check.
list_defect() {  # $1 = the strings, space-separated (fdtget -ts / proc NULs as spaces)
    local dup
    dup=$(awk '{ for (i = 1; i <= NF; i++) if (seen[$i]++) { print $i; exit } }' <<<"$1")
    [ -z "$dup" ] || { printf '%s повторён' "$dup"; return 0; }
    [[ " $1 " == *" $PHY_FALLBACK_COMPAT "* ]] || printf 'нет %s' "$PHY_FALLBACK_COMPAT"
}

# check only warns: the exit code stays the driver verdict.
warn_live_list() {  # $1 = eth0|eth1, $2 = the live strings
    local bad
    bad=$(list_defect "$2")
    [ -z "$bad" ] && return 0
    printf '%s PHY: ВНИМАНИЕ: список compatible не канонический: %s (%s) — так его портила ранняя версия патчера (BUGLOG 2026-10-30); исправление: %s apply, затем перезагрузка\n' \
        "$1" "$bad" "${2% }" "$(basename "$0")"
}

cmd_check() {
    local compat phy_id drv gmac gproc
    if [ ! -r "$PROC_PHY/compatible" ] || [ ! -r "$SYS_PHY/phy_id" ] || [ ! -r "$SYS_PHY/uevent" ]; then
        echo "eth0 PHY: узел DTB или sysfs недоступен ($PROC_PHY, $SYS_PHY) — не SA-02m или загружен другой DTB"
        return 2
    fi
    compat=$(tr '\0' ' ' < "$PROC_PHY/compatible")
    phy_id=$(head -n 1 "$SYS_PHY/phy_id")
    drv=$(phy_driver "$SYS_PHY")
    printf 'eth0 PHY: compatible = %s\n' "$compat"
    warn_live_list eth0 "$compat"
    printf 'eth0 PHY: phy_id = %s, driver = %s\n' "$phy_id" "$drv"
    gmac="$(dirname "$SYS_PHY")/stmmac-1:00"
    if [ -r "$gmac/phy_id" ]; then
        printf 'eth1 PHY: phy_id = %s, driver = %s\n' "$(head -n 1 "$gmac/phy_id")" "$(phy_driver "$gmac")"
    fi
    # The GMAC PHY node: PROC_PHY with its EMAC DT path swapped for the GMAC one.
    gproc="${PROC_PHY%"$EMAC_PHY_PATH"}$GMAC_PHY_PATH"
    if [ -r "$gproc/compatible" ]; then
        warn_live_list eth1 "$(tr '\0' ' ' < "$gproc/compatible")"
    fi
    case "$drv" in
        "ICPlus IP101A"|"ICPlus IP101G")
            echo "eth0 PHY под драйвером icplus — OK"
            return 0 ;;
        *)
            echo "eth0 PHY под драйвером «$drv», не icplus. Исправление DTB: $(basename "$0") apply, затем перезагрузка — только если это разрешено в docs/deployment.md (строка «boot DTB»). Линк при холодной загрузке поднимает не драйвер, а перезапуск интерфейса в циклах fix-eth"
            return 1 ;;
    esac
}

mount_fat() {
    if mountpoint -q "$FAT_MNT" 2>/dev/null; then
        return 0
    fi
    mkdir -p "$FAT_MNT" || return 1
    mount "$FAT_DEV" "$FAT_MNT" || return 1
    FAT_MOUNTED=1
}

report() {  # $1 = name, $2 = status line
    printf '%s: %s\n' "$1" "$2"
}

# Atomic replace: tmp on the destination's own directory, sync, rename over, sync.
write_atomic() {  # $1 = src, $2 = dst
    local tmp
    tmp="$(dirname "$2")/.$(basename "$2").new"
    cp -f "$1" "$tmp" || { rm -f "$tmp" 2>/dev/null || true; return 1; }
    sync
    mv -f "$tmp" "$2" || { rm -f "$tmp" 2>/dev/null || true; return 1; }
    sync
}

# Every check reads the COMPILED blob through fdtget (what the kernel will see),
# never the dts text. Any miss => the name fails and its FAT file stays as is.
verify_blob() {  # $1 = new blob, $2 = original blob, $3 = name
    local ecompat first deassert gcompat bad size osize delta
    ecompat=$(fdtget -ts "$1" "$EMAC_PHY_PATH" compatible 2>/dev/null)
    first=$(awk '{ print $1; exit }' <<<"$ecompat")
    [ "$first" = "$EMAC_ID_COMPAT" ] || { report "$3" "failed: verify: EMAC compatible[0] = '${first:-}' (ожидался $EMAC_ID_COMPAT)"; return 1; }
    deassert=$(fdtget "$1" "$EMAC_PHY_PATH" reset-deassert-us 2>/dev/null | awk '{ print $1; exit }')
    [ "$deassert" = "$DEASSERT_US" ] || { report "$3" "failed: verify: EMAC reset-deassert-us = '${deassert:-}' (ожидалось $DEASSERT_US)"; return 1; }
    bad=$(list_defect "$ecompat")
    [ -z "$bad" ] || { report "$3" "failed: verify: EMAC compatible: $bad ('$ecompat')"; return 1; }
    if fdtget "$1" "$GMAC_PHY_PATH" compatible >/dev/null 2>&1; then
        gcompat=$(fdtget -ts "$1" "$GMAC_PHY_PATH" compatible 2>/dev/null)
        case " $gcompat " in
            *" $GMAC_COMPAT "*) ;;
            *) report "$3" "failed: verify: GMAC compatible без $GMAC_COMPAT ('${gcompat:-}')"; return 1 ;;
        esac
        bad=$(list_defect "$gcompat")
        [ -z "$bad" ] || { report "$3" "failed: verify: GMAC compatible: $bad ('$gcompat')"; return 1; }
    fi
    size=$(stat -c %s "$1" 2>/dev/null || echo 0)
    osize=$(stat -c %s "$2" 2>/dev/null || echo 0)
    [ "$size" -ge "$SIZE_MIN" ] || { report "$3" "failed: verify: размер $size < $SIZE_MIN"; return 1; }
    delta=$((size - osize))
    if [ "$delta" -gt "$SIZE_SLACK" ] || [ "$delta" -lt "-$SIZE_SLACK" ]; then
        report "$3" "failed: verify: размер $size против исходных $osize (допуск ±$SIZE_SLACK)"; return 1
    fi
    return 0
}

cmd_apply() {
    local dry=0 missing="" t n src dts pdts new ts d written=0 failed=0 present="" rc
    case "${1:-}" in
        "") ;;
        --dry-run) dry=1 ;;
        *) usage ;;
    esac
    if [ "$(id -u)" != 0 ]; then
        echo "Нужен root (apply пишет на загрузочный раздел)" >&2
        return 3
    fi
    for t in dtc fdtget python3 mountpoint; do
        command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
    done
    [ -r "$PATCHER" ] || missing="$missing $PATCHER"
    if [ -n "$missing" ]; then
        echo "Не хватает:$missing (dtc и fdtget: apt install device-tree-compiler)" >&2
        return 3
    fi
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "Уже выполняется (lock $LOCK_DIR; после прерванного запуска — rmdir $LOCK_DIR)" >&2
        return 3
    fi
    HAVE_LOCK=1
    T=$(mktemp -d) || { echo "mktemp не удался" >&2; return 3; }

    if ! mount_fat; then
        echo "Не удалось смонтировать $FAT_DEV в $FAT_MNT" >&2
        return 4
    fi
    for n in $DTB_NAMES; do
        [ -f "$FAT_MNT/$n" ] && present="$present $n"
    done
    if [ -z "$present" ]; then
        echo "На FAT ($FAT_MNT) нет ни одного DTB из: $DTB_NAMES" >&2
        return 4
    fi

    ts=$(date +%Y%m%d%H%M%S)
    for n in $present; do
        src="$FAT_MNT/$n"
        dts="$T/$n.dts"; pdts="$T/$n.patched.dts"; new="$T/$n.new.dtb"
        if ! dtc -I dtb -O dts -q -o "$dts" "$src" 2>"$T/dtc.err"; then
            report "$n" "failed: dtc decompile: $(head -c 200 "$T/dtc.err" | tr '\n' ' ')"
            failed=1; continue
        fi
        python3 "$PATCHER" --in "$dts" --out "$pdts" >"$T/patch.out" 2>"$T/patch.err"; rc=$?
        if [ "$rc" -ne 0 ]; then
            report "$n" "failed: patcher rc=$rc: $(head -c 200 "$T/patch.err" | tr '\n' ' ')"
            failed=1; continue
        fi
        if cmp -s "$dts" "$pdts"; then
            report "$n" "already-patched (уже исправлен)"
            continue
        fi
        if ! dtc -I dts -O dtb -q -o "$new" "$pdts" 2>"$T/dtc.err"; then
            report "$n" "failed: dtc compile: $(head -c 200 "$T/dtc.err" | tr '\n' ' ')"
            failed=1; continue
        fi
        verify_blob "$new" "$src" "$n" || { failed=1; continue; }
        if [ "$dry" = 1 ]; then
            report "$n" "would-patch: $(tr '\n' ';' < "$T/patch.out")"
            continue
        fi
        if ! cp -f "$src" "$BACKUP_DIR/dtb-backup-$n-$ts"; then
            report "$n" "failed: backup в $BACKUP_DIR не записан — FAT не тронут"
            failed=1; continue
        fi
        if ! write_atomic "$new" "$src"; then
            report "$n" "failed: запись на FAT (live-файл не заменён)"
            failed=1; continue
        fi
        for d in $CANON_DIRS; do
            if [ -f "$d/$n" ]; then
                write_atomic "$new" "$d/$n" || report "$n" "warn: каноническая копия $d/$n не обновлена (FAT уже записан)"
            fi
        done
        report "$n" "patched: $(tr '\n' ';' < "$T/patch.out"); backup $BACKUP_DIR/dtb-backup-$n-$ts"
        written=1
    done

    if [ "$written" = 1 ]; then
        echo "Требуется перезагрузка: новый DTB вступит в силу при следующей загрузке; затем $(basename "$0") check"
    fi
    [ "$failed" = 0 ] || return 5
    return 0
}

trap cleanup EXIT
case "${1:-}" in
    check) cmd_check ;;
    apply) shift; cmd_apply "$@" ;;
    *) usage ;;
esac
