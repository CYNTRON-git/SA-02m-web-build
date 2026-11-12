#!/bin/bash
# SA-02m: переключение zImage SMP <-> RT на FAT /dev/mmcblk2p1
# /usr/local/sbin/sa02m-kernel-select.sh {init|status|set smp|rt|refresh}
set -euo pipefail

CONF=/etc/sa02m_kernel.conf
LOG=/var/log/sa02m_install.log
LOCK=/run/sa02m-kernel-switch.lock
FAT_DEV=/dev/mmcblk2p1
FAT_MNT=/mnt/fat
CANON_DIR=/usr/local/share/sa02m/kernel
ZIMAGE_MIN=5000000
ZIMAGE_MAX=12000000
# Пара ядер, которую реально несёт флот, — `6.1.0-rc6` (SMP) и `6.1.0-rc6-rt4`
# (PREEMPT_RT), сборка `tools/buildroot/prepare-rt-docker-kernel.sh`; дом факта —
# docs/contracts/kernel-conditional-services.md §6, гейт
# .ai-dev/quality/checks/kernel-policy-contract.sh (пин 8).
# Значения ниже — ТОЛЬКО дефолты: если /etc/sa02m_kernel.conf существует, он их
# перекрывает, а load_conf() дополнительно чинит их по факту (`uname -r` для
# работающего профиля, detect_installed_module_ver() для второго).
# Почему это важно: дефолт, который не совпадает ни с одним каталогом
# /lib/modules/ и который детектор не умеет найти, попадает в conf через
# write_conf() и остаётся там — панель после этого отказывается переключаться на
# профиль, который на плате есть ("modules_missing"). Ровно так вело себя
# прежнее значение 5.10.35 / 5.10.35-rt36 (ядро того семейства на устройствах
# не стоит) на 6.1-плате без conf.
SMP_VER_DEFAULT=6.1.0-rc6
RT_VER_DEFAULT=6.1.0-rc6-rt4

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-kernel-select: $*" >>"$LOG" 2>&1 || true; }

json_str() {
    local s=${1:-}
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}
    s=${s//$'\r'/}
    printf '%s' "$s"
}

running_profile() {
    local kr
    kr=$(uname -r 2>/dev/null || true)
    case "$kr" in
        *-rt*|*rt[0-9]*) printf 'rt' ;;
        *) printf 'smp' ;;
    esac
}

preempt_rt_active() {
    grep -q 'PREEMPT_RT' /proc/version 2>/dev/null
}

detect_installed_module_ver() {
    # $1 = pattern: "smp" (без rt) или "rt"
    # Белый список имён каталогов /lib/modules/, которые считаем НАШИМИ ядрами:
    # текущая линия флота (`6.1.0-rc6`, `6.1.0-rc6-rt4`) плюс исторические имена
    # (`*sa02m*`, `5.10.35*`) — их оставляем ради плат, которые могли остаться на
    # старом ядре. Список НАМЕРЕННО узкий: без него детектор подхватил бы
    # стоковое ядро дистрибутива и панель предложила бы переключиться на ядро,
    # которое собирали не мы. Цена узости — этот список надо расширять при
    # каждой смене линии ядра; гейт `kernel-policy-contract` (пин 8) следит,
    # чтобы он как минимум покрывал оба дефолта.
    local want=$1 d name
    for d in /lib/modules/*; do
        [ -d "$d" ] || continue
        name=$(basename "$d")
        case "$name" in
            *sa02m*|5.10.35*|6.1.0-rc6*)
                if [ "$want" = "rt" ]; then
                    case "$name" in *rt*|*-rt*) printf '%s\n' "$name"; return 0 ;; esac
                else
                    case "$name" in *rt*|*-rt*) ;; *) printf '%s\n' "$name"; return 0 ;; esac
                fi
                ;;
        esac
    done
    return 1
}

load_conf() {
    SA02M_KERNEL_DESIRED="${SA02M_KERNEL_DESIRED:-}"
    SA02M_KERNEL_SMP_VER="${SA02M_KERNEL_SMP_VER:-$SMP_VER_DEFAULT}"
    SA02M_KERNEL_RT_VER="${SA02M_KERNEL_RT_VER:-$RT_VER_DEFAULT}"
    SA02M_KERNEL_INIT_DONE="${SA02M_KERNEL_INIT_DONE:-0}"
    if [ -f "$CONF" ]; then
        # shellcheck disable=SC1090
        . "$CONF" 2>/dev/null || true
    fi

    # Автоопределение фактических версий модулей: если в конфиге прописана
    # версия, для которой нет каталога /lib/modules/, но есть реальный SMP/RT
    # каталог с другим суффиксом (`<ver>+` вместо `<ver>` после установки
    # linux-image-* с EXTRAVERSION="+") — берём реальное имя. Это устраняет
    # ложный smp_modules_missing.
    local kr detected
    kr=$(uname -r 2>/dev/null || echo "")
    if ! modules_ok "$SA02M_KERNEL_SMP_VER"; then
        detected=""
        if [ "$(running_profile)" = "smp" ] && [ -n "$kr" ] && modules_ok "$kr"; then
            detected="$kr"
        else
            detected=$(detect_installed_module_ver smp 2>/dev/null || true)
        fi
        if [ -n "$detected" ]; then
            SA02M_KERNEL_SMP_VER="$detected"
        fi
    fi
    if ! modules_ok "$SA02M_KERNEL_RT_VER"; then
        detected=""
        if [ "$(running_profile)" = "rt" ] && [ -n "$kr" ] && modules_ok "$kr"; then
            detected="$kr"
        else
            detected=$(detect_installed_module_ver rt 2>/dev/null || true)
        fi
        if [ -n "$detected" ]; then
            SA02M_KERNEL_RT_VER="$detected"
        fi
    fi
}

write_conf() {
    local desired=$1 init_done=${2:-1}
    umask 022
    # Пишем актуальные, авто-детектированные значения (после load_conf),
    # чтобы не терять уточнённую версию модулей при следующей загрузке.
    local smp_ver=${SA02M_KERNEL_SMP_VER:-$SMP_VER_DEFAULT}
    local rt_ver=${SA02M_KERNEL_RT_VER:-$RT_VER_DEFAULT}
    # tmp + fsync + rename: a truncate-in-place left an empty conf on a power
    # cut, and an empty conf silently drops the Operator's desired profile.
    local tmp="$CONF.tmp.$$"
    cat >"$tmp" <<EOF || { rm -f "$tmp"; return 1; }
# Управляется веб-панелью СА-02m. Желаемое ядро после reboot.
SA02M_KERNEL_DESIRED=$desired
SA02M_KERNEL_SMP_VER=$smp_ver
SA02M_KERNEL_RT_VER=$rt_ver
SA02M_KERNEL_INIT_DONE=$init_done
EOF
    chmod 644 "$tmp"
    sync -- "$tmp" 2>/dev/null || sync
    mv -f "$tmp" "$CONF" || { rm -f "$tmp"; return 1; }
}

zimage_ok() {
    local f=$1
    [ -f "$f" ] || return 1
    local sz
    sz=$(stat -c '%s' "$f" 2>/dev/null || echo 0)
    [ "$sz" -ge "$ZIMAGE_MIN" ] && [ "$sz" -le "$ZIMAGE_MAX" ]
}

modules_ok() {
    local ver=$1
    [ -d "/lib/modules/$ver" ] && [ -n "$(ls -A "/lib/modules/$ver" 2>/dev/null)" ]
}

# zimage_md5_ok <profile> <file> — the canonical image against the md5 that
# tools/buildroot/sa02m-kernel-deploy.sh records in $CANON_DIR/manifest.json.
# Size alone let a damaged image of the right size onto the boot slot (A4-03).
# No manifest, or no md5 for this profile (boards installed before the
# manifest existed): pass with a log line — size check only, stated honestly.
zimage_md5_ok() {
    local prof=$1 f=$2 man="$CANON_DIR/manifest.json" want="" have
    if [ -f "$man" ] && command -v python3 >/dev/null 2>&1; then
        want=$(python3 -c '
import json, sys
try:
    v = json.load(open(sys.argv[1]))["images"][sys.argv[2]]["md5"]
    print(v if isinstance(v, str) else "")
except Exception:
    print("")
' "$man" "$prof" 2>/dev/null || true)
    fi
    if ! [[ "$want" =~ ^[0-9a-f]{32}$ ]]; then
        log "no manifest md5 for $prof — size check only"
        return 0
    fi
    have=$(md5sum < "$f" 2>/dev/null | awk '{print $1}') || have=""
    if [ "$have" != "$want" ]; then
        log "zImage.$prof md5 ${have:-unreadable} != manifest $want — refused"
        return 1
    fi
    return 0
}

artifact_smp_ok() { zimage_ok "$CANON_DIR/zImage.smp"; }
artifact_rt_ok() { zimage_ok "$CANON_DIR/zImage.rt"; }

mount_fat() {
    FAT_MOUNTED=0
    if mountpoint -q "$FAT_MNT" 2>/dev/null; then
        return 0
    fi
    mkdir -p "$FAT_MNT"
    mount "$FAT_DEV" "$FAT_MNT" || return 1
    FAT_MOUNTED=1
}

umount_fat_if_needed() {
    [ "${FAT_MOUNTED:-0}" = "1" ] && umount "$FAT_MNT" 2>/dev/null || true
}

# Atomically replace a boot-critical zImage on the FAT partition.
# Copy to a temp on the SAME FAT dir, sync, verify size BEFORE swapping, then
# rename over the target (vfat rename() replaces the dir-entry atomically — the
# active zImage is never left truncated; the risk window is only the temp copy,
# during which the old dst is untouched). Returns 1 without touching dst on any
# failure (disk full, short copy).
write_fat_zimage() {
    local src=$1 dst=$2 tmp
    tmp="$(dirname "$dst")/.$(basename "$dst").new"
    cp -f "$src" "$tmp" || { rm -f "$tmp" 2>/dev/null || true; return 1; }
    sync
    # Size AND bytes: the FAT copy must equal its source before it may replace
    # the boot slot (a short or corrupted copy of the right size is refused).
    { zimage_ok "$tmp" && cmp -s "$src" "$tmp"; } || { rm -f "$tmp" 2>/dev/null || true; return 1; }
    mv -f "$tmp" "$dst" || { rm -f "$tmp" 2>/dev/null || true; return 1; }
    sync
}

# restore_active_slot <profile> — put the canonical zImage.<profile> on the FAT
# boot slot unless it is already byte-identical there. FAT must be mounted.
# Sets RESTORE_REFRESHED (0|1) and RESTORE_ERR ("" | zimage_mismatch |
# fat_write_failed); returns 1 on error. Shared by `refresh` and by `set` when
# the target is the running profile (A4-02: the slot may hold the OTHER image).
restore_active_slot() {
    local run=$1 canon="$CANON_DIR/zImage.$1"
    RESTORE_REFRESHED=0
    RESTORE_ERR=""
    if ! zimage_md5_ok "$run" "$canon"; then
        RESTORE_ERR="zimage_mismatch"
        return 1
    fi
    # A missing/corrupt active slot counts as needs-refresh (recovery); cmp -s
    # is set -e safe inside the if.
    if zimage_ok "$FAT_MNT/zImage" && cmp -s "$canon" "$FAT_MNT/zImage"; then
        return 0
    fi
    if ! write_fat_zimage "$canon" "$FAT_MNT/zImage"; then
        RESTORE_ERR="fat_write_failed"
        return 1
    fi
    # Suffix mirror is non-critical (active boot slot already written): a
    # failure here must NOT abort under set -e — degrade to a logged warning.
    write_fat_zimage "$canon" "$FAT_MNT/zImage.$run" || log "restore $run: warn: suffix mirror zImage.$run failed (active already written)"
    RESTORE_REFRESHED=1
    return 0
}

codesys_active() {
    pgrep -f '[c]odesyscontrol' >/dev/null 2>&1 || \
        pgrep -f '[C]ODESYSControl' >/dev/null 2>&1
}

cmd_init() {
    load_conf
    local run prof
    run=$(running_profile)
    prof="${SA02M_KERNEL_DESIRED:-$run}"
    [ -z "$prof" ] && prof="$run"
    write_conf "$prof" 1
    log "init: running=$run desired=$prof (zImage on FAT unchanged)"
    mkdir -p "$CANON_DIR"
    if mount_fat; then
        if zimage_ok "$FAT_MNT/zImage"; then
            if [ "$run" = "rt" ] && ! artifact_rt_ok; then
                cp -f "$FAT_MNT/zImage" "$CANON_DIR/zImage.rt"
                log "init: seeded zImage.rt from active FAT zImage"
            fi
            if [ "$run" = "smp" ] && ! artifact_smp_ok; then
                cp -f "$FAT_MNT/zImage" "$CANON_DIR/zImage.smp"
                log "init: seeded zImage.smp from active FAT zImage"
            fi
        fi
        umount_fat_if_needed
    fi
    return 0
}

cmd_status_json() {
    load_conf
    local run desired rb smp_a rt_a smp_m rt_m kr warnings=""
    run=$(running_profile)
    desired="${SA02M_KERNEL_DESIRED:-$run}"
    [ -z "$desired" ] && desired="$run"
    rb=0
    [ "$run" != "$desired" ] && rb=1
    smp_a=0; rt_a=0; smp_m=0; rt_m=0
    artifact_smp_ok && smp_a=1
    artifact_rt_ok && rt_a=1
    modules_ok "${SA02M_KERNEL_SMP_VER:-$SMP_VER_DEFAULT}" && smp_m=1
    modules_ok "${SA02M_KERNEL_RT_VER:-$RT_VER_DEFAULT}" && rt_m=1
    kr=$(uname -r 2>/dev/null || echo "")
    preempt_rt_active && warnings="${warnings}preempt_rt_active,"
    [ "$rb" = 1 ] && warnings="${warnings}reboot_pending,"
    if [ "$desired" = "smp" ] && [ "$smp_a" = 0 ]; then warnings="${warnings}smp_zimage_missing,"; fi
    if [ "$desired" = "rt" ] && [ "$rt_a" = 0 ]; then warnings="${warnings}rt_zimage_missing,"; fi
    if [ "$desired" = "smp" ] && [ "$smp_m" = 0 ]; then warnings="${warnings}smp_modules_missing,"; fi
    if [ "$desired" = "rt" ] && [ "$rt_m" = 0 ]; then warnings="${warnings}rt_modules_missing,"; fi
    warnings=${warnings%,}
    printf '{"ok":true,"running":"%s","desired":"%s","reboot_pending":%s,"kernel_version":"%s","preempt_rt":%s,"smp_zimage":%s,"rt_zimage":%s,"smp_modules":%s,"rt_modules":%s,"smp_modules_ver":"%s","rt_modules_ver":"%s","warnings":"%s"}\n' \
        "$run" "$desired" "$rb" "$(json_str "$kr")" \
        "$(preempt_rt_active && echo true || echo false)" \
        "$smp_a" "$rt_a" "$smp_m" "$rt_m" \
        "$(json_str "${SA02M_KERNEL_SMP_VER:-$SMP_VER_DEFAULT}")" \
        "$(json_str "${SA02M_KERNEL_RT_VER:-$RT_VER_DEFAULT}")" \
        "$(json_str "$warnings")"
}

cmd_set() {
    local target=${1:-}
    case "$target" in
        smp|rt) ;;
        *)
            printf '{"ok":false,"error":"bad_profile"}\n'
            return 1
            ;;
    esac

    exec 200>"$LOCK"
    if ! flock -n 200; then
        printf '{"ok":false,"error":"busy"}\n'
        return 1
    fi

    load_conf
    local run warnings=""
    run=$(running_profile)
    if [ "$run" = "$target" ]; then
        write_conf "$target" 1
        # The running kernel is already the target, but the FAT slot may hold the
        # other image (an SMP switch cancelled by choosing RT again, A4-02):
        # put the running profile's image back, or the next boot is the
        # cancelled one.
        if ! zimage_ok "$CANON_DIR/zImage.$target"; then
            log "set $target: running already; no canonical image to verify the FAT slot"
            printf '{"ok":true,"noop":true,"restored":0,"target":"%s","reboot_required":false,"warnings":"zimage_missing"}\n' "$target"
            return 0
        fi
        if ! mount_fat; then
            printf '{"ok":false,"error":"fat_mount_failed","target":"%s"}\n' "$target"
            return 1
        fi
        restore_active_slot "$target" || true
        umount_fat_if_needed
        if [ -n "$RESTORE_ERR" ]; then
            printf '{"ok":false,"error":"%s","target":"%s"}\n' "$RESTORE_ERR" "$target"
            return 1
        fi
        log "set $target: running already; FAT slot restored=$RESTORE_REFRESHED"
        printf '{"ok":true,"noop":true,"restored":%s,"target":"%s","reboot_required":false,"warnings":""}\n' "$RESTORE_REFRESHED" "$target"
        return 0
    fi

    local canon="$CANON_DIR/zImage.$target"
    local mod_ver
    if [ "$target" = "rt" ]; then
        mod_ver="${SA02M_KERNEL_RT_VER:-$RT_VER_DEFAULT}"
    else
        mod_ver="${SA02M_KERNEL_SMP_VER:-$SMP_VER_DEFAULT}"
    fi

    if ! zimage_ok "$canon"; then
        log "set $target: missing canonical $canon"
        printf '{"ok":false,"error":"zimage_missing","target":"%s"}\n' "$target"
        return 1
    fi
    if ! zimage_md5_ok "$target" "$canon"; then printf '{"ok":false,"error":"zimage_mismatch","target":"%s"}\n' "$target"; return 1; fi
    if ! modules_ok "$mod_ver"; then
        log "set $target: missing modules /lib/modules/$mod_ver"
        printf '{"ok":false,"error":"modules_missing","target":"%s","modules_ver":"%s"}\n' "$target" "$(json_str "$mod_ver")"
        return 1
    fi

    if [ "$target" = "smp" ] && codesys_active; then
        warnings="codesys_requires_rt"
    fi
    if [ "$target" = "rt" ]; then
        warnings="recommend_start_codesys"
    fi
    if ! mount_fat; then
        printf '{"ok":false,"error":"fat_mount_failed"}\n'
        return 1
    fi

    if ! write_fat_zimage "$canon" "$FAT_MNT/zImage"; then
        umount_fat_if_needed
        printf '{"ok":false,"error":"fat_write_failed"}\n'
        return 1
    fi
    # Suffix mirror is non-critical (active boot slot already written): a failure
    # here must NOT abort under set -e and leak the mount / mask the success.
    write_fat_zimage "$canon" "$FAT_MNT/zImage.$target" || log "set $target: warn: suffix mirror zImage.$target failed (active already written)"
    umount_fat_if_needed

    write_conf "$target" 1
    log "set $target: zImage updated on FAT, reboot required (was running=$run)"

    printf '{"ok":true,"target":"%s","reboot_required":true,"warnings":"%s"}\n' "$target" "$(json_str "$warnings")"
}

cmd_refresh() {
    exec 200>"$LOCK"
    if ! flock -n 200; then
        printf '{"ok":false,"error":"busy"}\n'
        return 1
    fi

    load_conf
    local run canon mod_ver
    run=$(running_profile)
    canon="$CANON_DIR/zImage.$run"
    if [ "$run" = "rt" ]; then
        mod_ver="${SA02M_KERNEL_RT_VER:-$RT_VER_DEFAULT}"
    else
        mod_ver="${SA02M_KERNEL_SMP_VER:-$SMP_VER_DEFAULT}"
    fi

    if ! zimage_ok "$canon"; then
        log "refresh $run: missing canonical $canon"
        printf '{"ok":false,"error":"zimage_missing","target":"%s"}\n' "$run"
        return 1
    fi
    if ! modules_ok "$mod_ver"; then
        log "refresh $run: missing modules /lib/modules/$mod_ver"
        printf '{"ok":false,"error":"modules_missing","target":"%s","modules_ver":"%s"}\n' "$run" "$(json_str "$mod_ver")"
        return 1
    fi

    if ! mount_fat; then
        printf '{"ok":false,"error":"fat_mount_failed"}\n'
        return 1
    fi

    # Local why: reinstall the RUNNING profile's canonical zImage onto the boot
    # FAT slot (same-profile update flow) — no profile change, so no codesys/RT
    # warnings apply. Single umount path: result computed by
    # restore_active_slot, then umount, then print.
    local refreshed=0 err=""
    restore_active_slot "$run" || true
    refreshed=$RESTORE_REFRESHED
    err=$RESTORE_ERR
    umount_fat_if_needed

    if [ -n "$err" ]; then
        printf '{"ok":false,"error":"%s"}\n' "$err"
        return 1
    fi

    write_conf "$run" 1
    log "refresh $run: refreshed=$refreshed (active zImage on FAT)"
    printf '{"ok":true,"refreshed":%s,"target":"%s","reboot_required":%s,"warnings":""}\n' "$refreshed" "$run" "$refreshed"
}

main() {
    mkdir -p "$CANON_DIR"
    case "${1:-status}" in
        init) cmd_init ;;
        status) cmd_status_json ;;
        set) cmd_set "${2:-}" ;;
        refresh) cmd_refresh ;;
        *)
            printf '{"ok":false,"error":"usage","hint":"init|status|set smp|rt|refresh"}\n'
            return 1
            ;;
    esac
}

main "$@"
