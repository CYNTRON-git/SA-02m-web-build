#!/bin/bash
# SA-02m — compress-bin.sh v1.1
# Обрабатывает сырой образ .bin / .img (снятый imageUSB в FEL-режиме):
#   1. Авто-снятие 512-байт заголовка imageUSB (если размер = EMMC_BYTES + 512)
#   2. Опциональный zerofill через loop-mount (--zerofill)
#   3. PiShrink (-a -v): сжимает ext4-раздел до минимума + патч firstboot-resize
#   4. xz -T0 -9e: финальное сжатие
#   5. sha256sum + вывод итога
#
# ВАЖНО: imageUSB добавляет 512-байт заголовок в начало .bin — скрипт снимает его
#        автоматически. Без этого PiShrink/parted не видят корректный MBR.
#
# Требования (WSL2): pishrink.sh, e2fsprogs, xz-utils, parted, util-linux
#   sudo apt install -y e2fsprogs xz-utils parted util-linux zerofree
#   sudo wget -O /usr/local/bin/pishrink.sh \
#     https://raw.githubusercontent.com/Drewsif/PiShrink/master/pishrink.sh
#   sudo chmod +x /usr/local/bin/pishrink.sh
#
# Использование:
#   ./compress-bin.sh --image /mnt/c/images/SA-02m-v1.0.3.21.bin [--out-dir ./out]
#                     [--name SA-02m_v1.0.3.21] [--xz-level 9e] [--zerofill]
#                     [--patch-watchdog] [--keep-raw] [--skip-pishrink]
set -euo pipefail
LC_ALL=C

IMAGE=""
OUT_DIR=""
OUTPUT_NAME=""
XZ_LEVEL="9e"
DO_ZEROFILL=0
DO_PATCH_WATCHDOG=0
DO_PISHRINK=1
KEEP_RAW=0
EMMC_BYTES=7818182656        # 7.28 GiB — SA-02m (R40/A40i, 8 GiB nominal)
IMAGEUSB_HEADER_BYTES=512    # imageUSB prepends 512-byte metadata header

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

usage() { cat <<'EOF'
compress-bin.sh — .bin/.img (imageUSB FEL) → PiShrink → .img.xz

Опции:
  --image PATH         Входной файл (.bin или .img), обязательно
  --out-dir DIR        Выходная папка (по умолчанию: рядом с .bin)
  --name NAME          Имя выхода без расширения (по умолчанию: basename без .bin/.img)
  --xz-level LEVEL     Уровень xz: 9e (по умолчанию), 6, 3 и т.д.
  --zerofill           Zerofill свободного места ext4 (лучшее сжатие, +10–30 мин)
  --patch-watchdog     Патчить watchdog unit-файлы из репозитория после PiShrink
  --skip-pishrink      Пропустить PiShrink (только xz — полезно если образ уже сжат)
  --keep-raw           Сохранить промежуточный .img после PiShrink
  -h|--help

Пример:
  ./compress-bin.sh --image /mnt/c/images/SA-02m-v1.0.3.21.bin \\
      --name SA-02m_v1.0.3.21 --out-dir ./out --zerofill --patch-watchdog
EOF
}

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --image)          IMAGE="$2";        shift 2 ;;
        --out-dir)        OUT_DIR="$2";      shift 2 ;;
        --name)           OUTPUT_NAME="$2";  shift 2 ;;
        --xz-level)       XZ_LEVEL="$2";     shift 2 ;;
        --zerofill)       DO_ZEROFILL=1;     shift ;;
        --patch-watchdog) DO_PATCH_WATCHDOG=1; shift ;;
        --skip-pishrink)  DO_PISHRINK=0;     shift ;;
        --keep-raw)       KEEP_RAW=1;        shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$IMAGE" ] || { echo "ERROR: --image required" >&2; usage; exit 2; }
[ -f "$IMAGE" ] || die "файл не найден: $IMAGE"

# Выходная папка
if [ -z "$OUT_DIR" ]; then
    OUT_DIR="$(dirname "$IMAGE")"
fi
mkdir -p "$OUT_DIR"

# Имя без расширения
if [ -z "$OUTPUT_NAME" ]; then
    BASE="$(basename "$IMAGE")"
    OUTPUT_NAME="${BASE%.bin}"
    OUTPUT_NAME="${OUTPUT_NAME%.img}"
fi

WORK="$(mktemp -d /tmp/compress-bin-XXXXXX)"
cleanup_work() { rm -rf "$WORK"; }
trap cleanup_work EXIT

WORK_IMG="$WORK/${OUTPUT_NAME}.img"
SHRUNK_IMG="$WORK/${OUTPUT_NAME}-shrunk.img"
SHRUNK_XZ="$OUT_DIR/${OUTPUT_NAME}-shrunk.img.xz"
SHA_FILE="${SHRUNK_XZ}.sha256"

log "[0/5] Проверка зависимостей"
for bin in xz sha256sum e2fsck resize2fs; do
    command -v "$bin" >/dev/null || die "не найден '$bin' (sudo apt install e2fsprogs xz-utils)"
done
if [ "$DO_PISHRINK" -eq 1 ]; then
    command -v pishrink.sh >/dev/null || die "pishrink.sh не найден — см. комментарий в начале скрипта"
fi
if [ "$DO_ZEROFILL" -eq 1 ]; then
    command -v zerofree >/dev/null || die "zerofree не найден (sudo apt install zerofree)"
fi

log "[1/5] Копируем .bin → рабочий .img (снимаем imageUSB header если есть)"
INPUT_SIZE=$(stat -c%s "$IMAGE")
log "    вход:   $IMAGE"
log "    размер: $(numfmt --to=iec --suffix=B "$INPUT_SIZE")"

# Авто-определение 512-байт заголовка imageUSB
# imageUSB .bin = [512 байт header] + [raw eMMC]; итого на 512 больше EMMC_BYTES
SKIP_BYTES=0
if [ "$INPUT_SIZE" -eq $(( EMMC_BYTES + IMAGEUSB_HEADER_BYTES )) ]; then
    SKIP_BYTES=$IMAGEUSB_HEADER_BYTES
    log "    обнаружен 512-байт imageUSB header — будет пропущен (skip=$SKIP_BYTES)"
elif [ "$INPUT_SIZE" -eq "$EMMC_BYTES" ]; then
    log "    заголовок отсутствует — чистый raw образ"
else
    echo "WARN: размер $INPUT_SIZE != $EMMC_BYTES и != $((EMMC_BYTES + IMAGEUSB_HEADER_BYTES))" >&2
    echo "      Продолжаем (нестандартная ёмкость eMMC — убедитесь что образ полный)" >&2
fi

# PiShrink модифицирует файл на месте — копируем в рабочий .img
# iflag=skip_bytes позволяет skip задавать в байтах, а bs=4M — копировать быстро
if [ "$SKIP_BYTES" -gt 0 ]; then
    log "    dd bs=4M skip=${SKIP_BYTES}B (iflag=skip_bytes) → $WORK_IMG"
    dd if="$IMAGE" of="$WORK_IMG" bs=4M skip="$SKIP_BYTES" iflag=skip_bytes status=progress conv=sparse
else
    log "    cp --sparse=always → $WORK_IMG"
    cp --sparse=always "$IMAGE" "$WORK_IMG"
fi

WORK_SIZE=$(stat -c%s "$WORK_IMG")
[ "$WORK_SIZE" -eq "$EMMC_BYTES" ] || \
    echo "WARN: рабочий образ $WORK_SIZE != $EMMC_BYTES (продолжаем)" >&2
log "    рабочий образ: $(numfmt --to=iec --suffix=B "$WORK_SIZE")"

if [ "$DO_ZEROFILL" -eq 1 ]; then
    log "[1b/5] Zerofill свободного места ext4 (улучшает сжатие)"
    # Определить смещение и размер корневого (последнего) раздела
    PART_INFO=$(parted -s "$WORK_IMG" unit B print 2>/dev/null) || die "parted не может прочитать образ"
    log "    таблица разделов:"
    echo "$PART_INFO" | grep -E '^\s+[0-9]' | while read -r line; do log "      $line"; done || true
    # Последний раздел по номеру (обычно p2 = ext4 root)
    ROOT_START=$(echo "$PART_INFO" | awk 'NF>=5 && $1~/^[0-9]+$/ {start=$2; size=$4} END {gsub("B","",start); print start}')
    ROOT_SIZE=$(echo  "$PART_INFO" | awk 'NF>=5 && $1~/^[0-9]+$/ {start=$2; size=$4} END {gsub("B","",size); print size}')
    [ -n "$ROOT_START" ] && [ "$ROOT_START" -gt 0 ] || die "не удалось определить смещение раздела из parted"
    log "    root offset=$ROOT_START size=$ROOT_SIZE байт"
    LOOP=$(sudo losetup -o "$ROOT_START" ${ROOT_SIZE:+--sizelimit "$ROOT_SIZE"} -f --show "$WORK_IMG")
    log "    loop: $LOOP"
    sudo e2fsck -fy "$LOOP" || true
    sudo zerofree -v "$LOOP"
    sudo losetup -d "$LOOP"
    log "    zerofill завершён"
fi

if [ "$DO_PISHRINK" -eq 1 ]; then
    log "[2/5] PiShrink (сжатие ext4 до минимума + firstboot resize)"
    sudo pishrink.sh -a -v "$WORK_IMG"
    AFTER_SHRINK=$(stat -c%s "$WORK_IMG")
    log "    после PiShrink: $(numfmt --to=iec --suffix=B "$AFTER_SHRINK")"
else
    log "[2/5] PiShrink пропущен (--skip-pishrink)"
fi

if [ "$DO_PATCH_WATCHDOG" -eq 1 ]; then
    log "[2b/5] Патч watchdog unit-файлов из репозитория"
    patch_image() {
        local img="$1"
        local loop mnt rootpart
        loop=$(sudo losetup --partscan -f --show "$img" 2>/dev/null) || {
            log "    WARN: losetup не удался — патч пропущен"
            return 0
        }
        mnt=$(mktemp -d /tmp/sa02m-patch-XXXXXX)
        rootpart="${loop}p2"
        local i
        for i in 1 2 3 4 5; do [ -b "$rootpart" ] && break; sleep 1; done
        if ! sudo mount "$rootpart" "$mnt" 2>/dev/null; then
            log "    WARN: mount не удался — патч пропущен"
            sudo losetup -d "$loop" 2>/dev/null || true
            rm -rf "$mnt"; return 0
        fi
        sudo cp -f "$REPO_ROOT/etc/systemd/sa02m-userspace-watchdog.service" \
            "$mnt/etc/systemd/system/sa02m-userspace-watchdog.service" 2>/dev/null || true
        sudo cp -f "$REPO_ROOT/etc/net-watchdog.service" \
            "$mnt/etc/systemd/system/net-watchdog.service" 2>/dev/null || true
        sudo cp -f "$REPO_ROOT/etc/sa02m-failure-monitor.service" \
            "$mnt/etc/systemd/system/sa02m-failure-monitor.service" 2>/dev/null || true
        sudo install -d "$mnt/etc/systemd/system.conf.d"
        sudo cp -f "$REPO_ROOT/etc/systemd/sa02m-watchdog.conf" \
            "$mnt/etc/systemd/system.conf.d/sa02m-watchdog.conf" 2>/dev/null || true
        sudo sed -i 's/^RuntimeWatchdogSec=/#RuntimeWatchdogSec=/' \
            "$mnt/etc/systemd/system.conf" 2>/dev/null || true
        sudo rm -f "$mnt/var/lib/sa02m-rootfs-expand.done"
        log "    watchdog unit-файлы восстановлены, DONE-флаг удалён"
        sudo umount "$mnt"
        sudo losetup -d "$loop" 2>/dev/null || true
        rm -rf "$mnt"
    }
    patch_image "$WORK_IMG"
fi

log "[3/5] Финальный xz (-T0 -${XZ_LEVEL})"
rm -f "$SHRUNK_XZ"
xz -T0 "-${XZ_LEVEL}" -v -c "$WORK_IMG" > "$SHRUNK_XZ"

if [ "$KEEP_RAW" -eq 1 ]; then
    KEEP_PATH="$OUT_DIR/${OUTPUT_NAME}-shrunk.img"
    cp "$WORK_IMG" "$KEEP_PATH"
    log "    raw img сохранён: $KEEP_PATH"
fi

log "[4/5] sha256"
( cd "$OUT_DIR" && sha256sum "$(basename "$SHRUNK_XZ")" > "$(basename "$SHA_FILE")" )

FINAL_SIZE=$(stat -c%s "$SHRUNK_XZ")
RATIO=$(awk "BEGIN{printf \"%.1f\", $INPUT_SIZE / $FINAL_SIZE}")

echo
echo "    ════════════════════════════════════════════════════════════════"
echo "    READY: $SHRUNK_XZ"
printf "           вход:   %s\n" "$(numfmt --to=iec --suffix=B "$INPUT_SIZE")"
printf "           выход:  %s (степень сжатия ×%s)\n" "$(numfmt --to=iec --suffix=B "$FINAL_SIZE")" "$RATIO"
echo "           sha256: $(awk '{print $1}' "$SHA_FILE")"
echo "    ════════════════════════════════════════════════════════════════"
echo
echo "    Подготовить для ImageUSB:"
echo "      ./prepare-imageusb.sh --image $SHRUNK_XZ --dest /mnt/c/USB/SA02m-imageusb"
echo
echo "    Подготовить USB для flash-receiver:"
echo "      ./prepare-flash-media.sh --image $SHRUNK_XZ --dest /mnt/c/USB/SA02m"
