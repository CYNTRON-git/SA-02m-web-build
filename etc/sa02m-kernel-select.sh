#!/bin/bash
# SA-02m: переключение zImage SMP <-> RT на FAT /dev/mmcblk2p1
# /usr/local/sbin/sa02m-kernel-select.sh {init|status|set smp|rt}
set -euo pipefail

CONF=/etc/sa02m_kernel.conf
LOG=/var/log/sa02m_install.log
LOCK=/run/sa02m-kernel-switch.lock
FAT_DEV=/dev/mmcblk2p1
FAT_MNT=/mnt/fat
CANON_DIR=/usr/local/share/sa02m/kernel
ZIMAGE_MIN=5000000
ZIMAGE_MAX=12000000
# С 2026-07: SA-02м переходит с локально патченного Starterkit-ядра
# 6.1.0-rc6 на форк wirenboard/linux (5.10.35). Значения ниже — по умолчанию
# для linux-image-sa02m / linux-image-sa02m-rt из [../kernel-port/](../kernel-port/README.md).
# На «старом» ядре /etc/sa02m_kernel.conf сохранил свои значения, они
# перекроют defaults ниже — миграция происходит после первой установки .deb.
SMP_VER_DEFAULT=5.10.35-sa02m
RT_VER_DEFAULT=5.10.35-sa02m-rt

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
    local want=$1 d name
    for d in /lib/modules/*; do
        [ -d "$d" ] || continue
        name=$(basename "$d")
        case "$name" in
            *sa02m*)
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
    # каталог с суффиксом (например 5.10.35-sa02m+ вместо 5.10.35-sa02m) —
    # берём реальное имя. Это устраняет ложный smp_modules_missing после
    # установки пакета linux-image-* с EXTRAVERSION="+".
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
    cat >"$CONF" <<EOF
# Управляется веб-панелью СА-02m. Желаемое ядро после reboot.
SA02M_KERNEL_DESIRED=$desired
SA02M_KERNEL_SMP_VER=$smp_ver
SA02M_KERNEL_RT_VER=$rt_ver
SA02M_KERNEL_INIT_DONE=$init_done
EOF
    chmod 644 "$CONF"
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
        printf '{"ok":true,"noop":true,"target":"%s","reboot_required":false,"warnings":""}\n' "$target"
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

    cp -f "$canon" "$FAT_MNT/zImage"
    cp -f "$canon" "$FAT_MNT/zImage.$target"
    sync
    umount_fat_if_needed

    write_conf "$target" 1
    log "set $target: zImage updated on FAT, reboot required (was running=$run)"

    printf '{"ok":true,"target":"%s","reboot_required":true,"warnings":"%s"}\n' "$target" "$(json_str "$warnings")"
}

main() {
    mkdir -p "$CANON_DIR"
    case "${1:-status}" in
        init) cmd_init ;;
        status) cmd_status_json ;;
        set) cmd_set "${2:-}" ;;
        *)
            printf '{"ok":false,"error":"usage","hint":"init|status|set smp|rt"}\n'
            return 1
            ;;
    esac
}

main "$@"
