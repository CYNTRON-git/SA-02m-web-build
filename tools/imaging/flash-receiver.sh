#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
#  SA-02m  •  flash-receiver.sh   v1.0
#
#  Заменитель прежнего autorun.sh (rmmod g_mass_storage; dd; sync; reboot).
#  Пишет компактный сжатый образ (.img.xz) в /dev/mmcblk2 на приёмнике.
#
#  Кладётся вместе с образом на носитель, который монтируется как USB
#  mass storage gadget на доноре или физически вставляется в приёмник.
#
#  Ожидаемое содержимое носителя (по умолчанию /mnt):
#      sa02m-shrunk.img.xz
#      sa02m-shrunk.img.xz.sha256
#      flash-receiver.sh
#      autorun.sh                  (= ln -s flash-receiver.sh)
#
#  Поведение:
#    1) выгружает g_mass_storage (отключает USB-gadget, чтобы освободить mmcblk2)
#    2) проверяет sha256 образа (если .sha256 файл присутствует)
#    3) распаковывает xz на лету и пишет dd в /dev/mmcblk2
#    4) sync + reboot
#
#  Все этапы логируются в /var/log/flash-receiver.log
# ═══════════════════════════════════════════════════════════════════════════
set -eu

IMG_DIR="${IMG_DIR:-/mnt}"
IMG_NAME="${IMG_NAME:-sa02m-shrunk.img.xz}"
TARGET_DEV="${TARGET_DEV:-/dev/mmcblk2}"
LOG="/var/log/flash-receiver.log"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG" >&2; }
die() { log "FATAL: $*"; exit 1; }

mkdir -p "$(dirname "$LOG")"
log "─── flash-receiver.sh start ───"
log "IMG_DIR=$IMG_DIR  IMG_NAME=$IMG_NAME  TARGET=$TARGET_DEV"

IMG="$IMG_DIR/$IMG_NAME"
SHA="$IMG.sha256"

# ── 1) освобождаем eMMC от USB gadget'а ─────────────────────────────────
log "[1/4] rmmod g_mass_storage"
rmmod -f g_mass_storage 2>/dev/null || true
sleep 1
sync

# ── 2) проверки ──────────────────────────────────────────────────────────
[ -b "$TARGET_DEV" ]                || die "$TARGET_DEV не блочное устройство"
[ -f "$IMG" ]                       || die "образ не найден: $IMG"
[ -r "$IMG" ]                       || die "образ не читается: $IMG"

if [ -f "$SHA" ]; then
    log "[2/4] sha256 verify"
    EXP=$(awk '{print $1}' "$SHA")
    GOT=$(sha256sum "$IMG" | awk '{print $1}')
    [ -n "$EXP" ] && [ "$EXP" = "$GOT" ] \
        || die "sha256 mismatch: exp=$EXP got=$GOT"
    log "    OK: $GOT"
else
    log "[2/4] sha256 verify пропущен (нет $SHA)"
fi

# ── 3) запись ────────────────────────────────────────────────────────────
log "[3/4] stop services; xz -dc $IMG_NAME | dd of=$TARGET_DEV bs=4M"
systemctl stop nginx fcgiwrap sa02m-flasher mplc mplc4 2>/dev/null || true
sync

if ! xz -dc "$IMG" | dd of="$TARGET_DEV" bs=4M conv=fsync status=none 2>>"$LOG"; then
    die "запись dd завершилась ошибкой"
fi
sync; sync
log "    запись завершена"

# ── 4) reboot ────────────────────────────────────────────────────────────
log "[4/4] reboot"
sleep 2
reboot
