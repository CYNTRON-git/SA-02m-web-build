#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
#  SA-02m  •  flash-receiver.sh   v1.2
#
#  ВАЖНО: этот скрипт запускается на ПРИЁМНИКЕ, загруженном с ВНЕШНЕГО
#  носителя (USB mass storage gadget или физический USB/SD).
#  НИКОГДА не запускать на работающей системе с тем же /dev/mmcblk2!
#
#  Кладётся вместе с образом на носитель:
#      sa02m-shrunk.img.xz
#      sa02m-shrunk.img.xz.sha256
#      flash-receiver.sh
#      autorun.sh  (= копия flash-receiver.sh)
#
#  Поведение:
#    1) выгружает g_mass_storage (освобождает mmcblk2 от USB gadget'а)
#    2) останавливает и маскирует watchdog-сервисы (предотвращает reboot во время dd)
#    3) проверяет sha256 образа
#    4) распаковывает xz на лету и пишет dd в /dev/mmcblk2
#    5) sync + reboot
#
#  Все этапы логируются в /var/log/flash-receiver.log
# ═══════════════════════════════════════════════════════════════════════════
set -eu

# IMG_DIR: автоопределение по директории скрипта.
# Работает из /media/usb, /mnt, /media/sdcard и любого другого места.
# Переопределяется переменной окружения: IMG_DIR=/other/path sh flash-receiver.sh
_SELF="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
_SELF_DIR="$(dirname "$_SELF")"
IMG_DIR="${IMG_DIR:-$_SELF_DIR}"
IMG_NAME="${IMG_NAME:-sa02m-shrunk.img.xz}"
TARGET_DEV="${TARGET_DEV:-/dev/mmcblk2}"
LOG="/var/log/flash-receiver.log"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG" >&2; }
die() { log "FATAL: $*"; exit 1; }

mkdir -p "$(dirname "$LOG")"
log "─── flash-receiver.sh v1.1 start ───"
log "IMG_DIR=$IMG_DIR  IMG_NAME=$IMG_NAME  TARGET=$TARGET_DEV"

IMG="$IMG_DIR/$IMG_NAME"
SHA="$IMG.sha256"

# ── 1) освобождаем eMMC от USB gadget'а ─────────────────────────────────
log "[1/5] rmmod g_mass_storage"
rmmod -f g_mass_storage 2>/dev/null || true
sleep 1
sync

# ── 2) останавливаем watchdog-сервисы ───────────────────────────────────
log "[2/5] stop & mask watchdog/reboot-trigger services"
for svc in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog \
           sa02m-watchdog-feed nginx fcgiwrap sa02m-flasher mplc mplc4 php8.3-fpm; do
    systemctl stop  "$svc" 2>/dev/null || true
    systemctl mask  "$svc" 2>/dev/null || true
done
sync

# ── 3) проверки и sha256 ─────────────────────────────────────────────────
[ -b "$TARGET_DEV" ] || die "$TARGET_DEV не блочное устройство"
[ -f "$IMG" ]        || die "образ не найден: $IMG"
[ -r "$IMG" ]        || die "образ не читается: $IMG"

if [ -f "$SHA" ]; then
    log "[3/5] sha256 verify"
    EXP=$(awk '{print $1}' "$SHA")
    GOT=$(sha256sum "$IMG" | awk '{print $1}')
    [ -n "$EXP" ] && [ "$EXP" = "$GOT" ] \
        || die "sha256 mismatch: exp=$EXP got=$GOT"
    log "    OK: $GOT"
else
    log "[3/5] sha256 verify пропущен (нет $SHA)"
fi

# ── 4) запись ────────────────────────────────────────────────────────────
log "[4/5] xz -dc $IMG_NAME | dd of=$TARGET_DEV bs=4M"
if ! xz -dc "$IMG" | dd of="$TARGET_DEV" bs=4M conv=fsync status=none 2>>"$LOG"; then
    die "запись dd завершилась ошибкой"
fi
sync; sync
log "    запись завершена"

# ── 5) reboot ────────────────────────────────────────────────────────────
log "[5/5] reboot"
sleep 2
reboot
