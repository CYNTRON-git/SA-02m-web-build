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
# reading. Why the ID string and not a vendor string: phylib picks a driver by
# the PHY ID, never by a vendor `compatible` (icplus.c has no OF match table);
# fwnode_get_phy_id() parses an `ethernet-phy-idAAAA.BBBB` FIRST string and the
# core creates the device with that ID without reading, and the icplus driver's
# match_phy_device() probes a register later, after the reset deassert + the
# reset-deassert-us sleep. The GMAC PHY keeps `icplus,ip101a` (its ID read
# works; a forced ID there would fabricate a PHY on a board without one).
#
# The edit (dtb_edit, one home): the blob itself, property by property, through
# fdtget/fdtput on a tmp COPY — never a dts text round trip: a string list
# parsed out of dts text is where the duplicated strings and the lost c22 came
# from (BUGLOG 2026-10-30, 2026-10-31). Eight edits, each with its own guard,
# at the fixed DT paths of the SA-02m blob:
#   dc1sw            regulator-always-on (EMPTY) — only when its
#                    regulator-name is vcc-gmac-phy and the property is absent
#   i2c@1c2b800      status disabled -> okay (the node's own status only)
#   ethernet@1c50000 (GMAC) every status disabled -> okay in its subtree,
#                    children included; syscon -> 2 only when present as one
#                    cell; the PHY below only when GMAC mdio is snps,dwmac-mdio
#   each PHY node    compatible = the canonical list (canonical_list): the
#                    node's first string (EMAC: ethernet-phy-id0243.0c54,
#                    GMAC: icplus,ip101a), the other strings in order without a
#                    duplicate (and, for the id string, without another
#                    ethernet-phy-id…), ethernet-phy-ieee802.3-c22 appended when
#                    missing; reset-deassert-us -> 500000 only when present as
#                    one cell
# A missing GMAC or EMAC-PHY node fails the name (rc 1); any fdtget/fdtput
# failure fails it too (rc 2) — the copy is dropped, the FAT never touched.
# Nothing to change => no fdtput call => the copy is byte-equal to the live
# file => «уже исправлен». fdtput appends a NEW property at the end of its node
# (the old text patcher inserted it before regulator-name): the tree is the
# same, the byte layout may differ from a dtc-recompiled blob.
#
# Commands:
#   check             read-only, no root, no mount: prints the running DT's EMAC
#                     PHY compatible, phy_id and bound driver (sysfs uevent), the
#                     GMAC PHY's where present, and a ВНИМАНИЕ line for each live
#                     PHY list that is not canonical (a string twice, or no
#                     ethernet-phy-ieee802.3-c22 — a blob from the early patcher).
#                     The warning never changes the exit code: 0 = ICPlus
#                     IP101A|IP101G, 1 = another driver (Generic PHY), 2 =
#                     node/sysfs unreadable. On a cold boot the link comes from
#                     fix-eth's link cycles (down/up + autoneg restart), not from
#                     the driver; under icplus the down/up did it and a lone
#                     autoneg restart did not (bench 1.135, BUGLOG 2026-10-30).
#   apply [--dry-run] root. For EVERY name of DTB_NAMES present on FAT,
#                     independently: tmp copy -> dtb_edit (unchanged =>
#                     "already-patched") -> fdtget verify (EMAC
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
#                     3 = prerequisites (root, fdtget, fdtput, mountpoint) or
#                     the lock; 4 = FAT not mountable / no DTB
#                     name present (nothing written); 5 = at least one name
#                     failed (that name's FAT file untouched; names that
#                     succeeded stay — each is a complete, verified blob).
#
# Three names may coexist on a board (the default boot name, the opt-in
# boot.cmd.sa02m chain, pack-sa02m-image.sh); each is patched on its own copy
# so blobs that differ elsewhere (rtc/usb0_vbus fdtput edits) keep their
# differences. Both canonical-copy homes are refreshed because
# sa02m-restore-dtb.sh (boot + apt hook) would otherwise revert the FAT file
# to the old copy on md5 mismatch.
#
# Rollback: copy /root/dtb-backup-<name>-<ts> back over the FAT name (from a
# shell, or via the FEL stand / autorun-repair-rootfs.sh — docs/deployment.md).
# After apply: reboot, then `check` again.
#
# Tests: scripts/dev/test-dtb-patch-tool.sh (quality row dtb-patch-tool) runs
# this file sed-retargeted into a sandbox with fdtget/fdtput/mount shims (the
# wrapper flow); scripts/dev/test-dtb-fdtput.sh (row dtb-fdtput-real) SOURCES
# it and runs dtb_edit with the REAL fdtget/fdtput on the repo blob against the
# goldens (CI and the bench; a skip elsewhere). Sourced, the file only defines
# — the dispatch at the bottom runs when it is executed.
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
LOCK_DIR=/run/lock/sa02m-dtb-patch.d

EMAC_ID_COMPAT=ethernet-phy-id0243.0c54   # IC+ IP101A rev 4 (mii-tool -v / BUGLOG 2026-10-28)
GMAC_COMPAT=icplus,ip101a
PHY_FALLBACK_COMPAT=ethernet-phy-ieee802.3-c22
PHY_ID_PREFIX=ethernet-phy-id
DEASSERT_US=500000
SIZE_MIN=16384
SIZE_SLACK=8192
EMAC_MDIO_PATH=/soc/mdio@1c0b080
EMAC_PHY_PATH=/soc/mdio@1c0b080/ethernet-phy@0
GMAC_PATH=/soc/ethernet@1c50000
GMAC_MDIO_PATH=/soc/ethernet@1c50000/mdio
GMAC_PHY_PATH=/soc/ethernet@1c50000/mdio/ethernet-phy@0
I2C_PATH=/soc/i2c@1c2b800
DC1SW_PATH=/soc/i2c@1c2ac00/pmic@34/regulators/dc1sw

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
            echo "eth0 PHY под драйвером «$drv», не icplus. Исправление DTB: $(basename "$0") apply, затем перезагрузка — только если это разрешено в docs/deployment.md (строка «boot DTB»). Линк при холодной загрузке поднимают циклы fix-eth (перезапуск интерфейса и автосогласования), а не драйвер"
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

# The canonical compatible list of a PHY node, into the array CANON (the rule:
# header, «each PHY node»). One home for dtb_edit's PHY edit.
canonical_list() {  # $1 = the string that leads, $2.. = the node's current strings
    local first=$1 s t seen
    shift
    CANON=("$first")
    for s in "$@"; do
        [ "$s" = "$first" ] && continue
        # Two ID overrides would be a lie: phylib reads only the first one.
        if [[ "$first" == "$PHY_ID_PREFIX"* && "$s" == "$PHY_ID_PREFIX"* ]]; then continue; fi
        seen=0
        for t in "${CANON[@]}"; do [ "$t" = "$s" ] && seen=1; done
        [ "$seen" = 1 ] || CANON+=("$s")
    done
    seen=0
    for t in "${CANON[@]}"; do [ "$t" = "$PHY_FALLBACK_COMPAT" ] && seen=1; done
    [ "$seen" = 1 ] || CANON+=("$PHY_FALLBACK_COMPAT")
}

# The edit step: the eight edits of the header, IN PLACE on the blob $1 (a tmp
# copy — never the live file). One stdout line per change, in the old text
# patcher's wording so operators read the same lines. rc 0 = done (changed or
# not); 1 = the GMAC or EMAC-PHY node is missing (not an SA-02m blob); 2 = an
# fdtget/fdtput call failed. A subshell, so every failure path is a plain exit.
dtb_edit() (
    f=$1
    edit_fail() { printf 'sa02m-dtb-patch: %s\n' "$1" >&2; exit 2; }
    node_missing() { printf 'sa02m-dtb-patch: node not found: %s\n' "$1" >&2; exit 1; }
    node_exists() { fdtget -l "$f" "$1" >/dev/null 2>&1; }
    has_prop() {  # $1 = an existing node, $2 = one of its OWN properties
        local props
        props=$(fdtget -p "$f" "$1") || edit_fail "fdtget -p $1"
        grep -qxF -- "$2" <<<"$props"
    }
    get() {  # $1 = s|u, $2 = node, $3 = prop -> the value (strings / cells space-separated)
        fdtget -t "$1" "$f" "$2" "$3" || edit_fail "fdtget $2 $3"
    }
    one_cell() { [[ "$1" =~ ^[0-9]+$ ]]; }   # the old patcher rewrote a single <N> only
    subtree() {  # $1 = node -> it and every descendant, one path per line
        local kids c
        printf '%s\n' "$1"
        kids=$(fdtget -l "$f" "$1") || return 1
        while IFS= read -r c; do
            [ -n "$c" ] || continue
            subtree "$1/$c" || return 1
        done <<<"$kids"
    }
    patch_phy() {  # $1 = PHY node, $2 = the string that leads, $3 = label of the change lines
        local cur v have=() s c22=0
        if has_prop "$1" compatible; then
            cur=$(get s "$1" compatible) || exit 2
            read -r -a have <<<"$cur"
            canonical_list "$2" "${have[@]}"
            if [ "${CANON[*]}" != "${have[*]}" ]; then
                fdtput -t s "$f" "$1" compatible "${CANON[@]}" || edit_fail "fdtput $1 compatible"
                for s in "${have[@]}"; do [ "$s" = "$PHY_FALLBACK_COMPAT" ] && c22=1; done
                if [ "${have[0]:-}" = "$2" ] && [ "$c22" = 0 ]; then
                    echo "$3: compatible $PHY_FALLBACK_COMPAT appended"
                else
                    echo "$3: compatible $2 first"
                fi
            fi
        fi
        if has_prop "$1" reset-deassert-us; then
            v=$(get u "$1" reset-deassert-us) || exit 2
            if one_cell "$v" && [ "$v" != "$DEASSERT_US" ]; then
                fdtput -t u "$f" "$1" reset-deassert-us "$DEASSERT_US" || edit_fail "fdtput $1 reset-deassert-us"
                echo "$3: reset-deassert-us $DEASSERT_US"
            fi
        fi
    }

    # dc1sw: the GMAC PHY supply stays on.
    if node_exists "$DC1SW_PATH" && has_prop "$DC1SW_PATH" regulator-name \
       && ! has_prop "$DC1SW_PATH" regulator-always-on; then
        v=$(get s "$DC1SW_PATH" regulator-name) || exit 2
        if [ "$v" = vcc-gmac-phy ]; then
            fdtput "$f" "$DC1SW_PATH" regulator-always-on || edit_fail "fdtput $DC1SW_PATH regulator-always-on"
            echo "dc1sw: regulator-always-on"
        fi
    fi

    # i2c@1c2b800 (pre-start + PCA9536): its own status only.
    if node_exists "$I2C_PATH" && has_prop "$I2C_PATH" status; then
        v=$(get s "$I2C_PATH" status) || exit 2
        if [ "$v" = disabled ]; then
            fdtput -t s "$f" "$I2C_PATH" status okay || edit_fail "fdtput $I2C_PATH status"
            echo "i2c@1c2b800: status okay"
        fi
    fi

    # GMAC: every `disabled` in the subtree (the subtree carries no node that
    # must stay disabled), syscon, then its PHY.
    node_exists "$GMAC_PATH" || node_missing "$GMAC_PATH (GMAC)"
    nodes=$(subtree "$GMAC_PATH") || edit_fail "fdtget -l under $GMAC_PATH"
    changed=0
    while IFS= read -r n; do
        has_prop "$n" status || continue
        v=$(get s "$n" status) || exit 2
        if [ "$v" = disabled ]; then
            fdtput -t s "$f" "$n" status okay || edit_fail "fdtput $n status"
            changed=1
        fi
    done <<<"$nodes"
    [ "$changed" = 0 ] || echo "ethernet@1c50000: status okay"
    if has_prop "$GMAC_PATH" syscon; then
        v=$(get u "$GMAC_PATH" syscon) || exit 2
        if one_cell "$v" && [ "$v" != 2 ]; then
            fdtput -t u "$f" "$GMAC_PATH" syscon 2 || edit_fail "fdtput $GMAC_PATH syscon"
            echo "ethernet@1c50000: syscon 0x02"
        fi
    fi
    if node_exists "$GMAC_MDIO_PATH" && has_prop "$GMAC_MDIO_PATH" compatible; then
        v=$(get s "$GMAC_MDIO_PATH" compatible) || exit 2
        if [[ " $v " == *" snps,dwmac-mdio "* ]] && node_exists "$GMAC_PHY_PATH"; then
            patch_phy "$GMAC_PHY_PATH" "$GMAC_COMPAT" "ethernet@1c50000/mdio/ethernet-phy@0"
        fi
    fi

    # EMAC PHY: the edit this tool exists for.
    node_exists "$EMAC_MDIO_PATH" || node_missing "$EMAC_MDIO_PATH (EMAC mdio)"
    node_exists "$EMAC_PHY_PATH" || node_missing "$EMAC_PHY_PATH (EMAC PHY)"
    patch_phy "$EMAC_PHY_PATH" "$EMAC_ID_COMPAT" "mdio@1c0b080/ethernet-phy@0"
    exit 0
)

# Every check reads the EDITED blob through fdtget (what the kernel will see).
# Any miss => the name fails and its FAT file stays as is.
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
    local dry=0 missing="" t n src new ts d written=0 failed=0 present="" rc
    case "${1:-}" in
        "") ;;
        --dry-run) dry=1 ;;
        *) usage ;;
    esac
    if [ "$(id -u)" != 0 ]; then
        echo "Нужен root (apply пишет на загрузочный раздел)" >&2
        return 3
    fi
    for t in fdtget fdtput mountpoint; do
        command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
    done
    if [ -n "$missing" ]; then
        echo "Не хватает:$missing (fdtget и fdtput: apt install device-tree-compiler)" >&2
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
        new="$T/$n.new.dtb"
        if ! cp -f "$src" "$new"; then
            report "$n" "failed: копия во временный каталог не записана — FAT не тронут"
            failed=1; continue
        fi
        dtb_edit "$new" >"$T/patch.out" 2>"$T/patch.err"; rc=$?
        if [ "$rc" -ne 0 ]; then
            report "$n" "failed: edit rc=$rc: $(head -c 200 "$T/patch.err" | tr '\n' ' ')"
            failed=1; continue
        fi
        if cmp -s "$src" "$new"; then
            report "$n" "already-patched (уже исправлен)"
            continue
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

# Sourced (scripts/dev/test-dtb-fdtput.sh calls dtb_edit on its own blobs):
# define only — no trap, no dispatch.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    trap cleanup EXIT
    case "${1:-}" in
        check) cmd_check ;;
        apply) shift; cmd_apply "$@" ;;
        *) usage ;;
    esac
fi
