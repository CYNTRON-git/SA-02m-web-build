#!/bin/bash
# SA-02m — подготовка распакованного .img для ImageUSB (Windows, §11.2 guide)
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage:
  prepare-imageusb.sh --image PATH [--dest DIR]

Вход:  sa02m-*-shrunk.img.xz  (или уже распакованный .img)
Выход в --dest (по умолчанию ./imageusb-media/):
  SA-02m-*.img
  SA-02m-*.img.sha256
  IMAGEUSB.txt          — краткая инструкция для цеха

ImageUSB пишет .img напрямую на eMMC (FEL/USB). xz распаковывается на ПК, не на устройстве.

После первой загрузки rootfs должен стать ~7G (sa02m-rootfs-expand / armbian-resize).
EOF
}

IMAGE=""
DEST=""

while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE="$2"; shift 2 ;;
        --dest)  DEST="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$IMAGE" ] || { echo "ERROR: --image required" >&2; usage; exit 2; }
[ -f "$IMAGE" ] || { echo "ERROR: not found: $IMAGE" >&2; exit 1; }

if [ -z "$DEST" ]; then
    DEST="$SCRIPT_DIR/imageusb-media"
fi
mkdir -p "$DEST"

WORK_IMG=""
case "$IMAGE" in
    *.img.xz)
        BASENAME="$(basename "${IMAGE%.img.xz}")"
        WORK_IMG="$DEST/${BASENAME}.img"
        echo "Unpack: $IMAGE -> $WORK_IMG"
        xz -dc "$IMAGE" > "$WORK_IMG"
        ;;
    *.img)
        BASENAME="$(basename "${IMAGE%.img}")"
        WORK_IMG="$DEST/${BASENAME}.img"
        cp -f "$IMAGE" "$WORK_IMG"
        ;;
    *)
        echo "ERROR: expected .img or .img.xz" >&2
        exit 1
        ;;
esac

IMG_NAME="$(basename "$WORK_IMG")"
SHA=$(sha256sum "$WORK_IMG" | awk '{print $1}')
printf '%s  %s\n' "$SHA" "$IMG_NAME" > "$DEST/${IMG_NAME}.sha256"

cat > "$DEST/IMAGEUSB.txt" <<EOF
SA-02m — серийная прошивка через ImageUSB
=========================================

Файл образа : $IMG_NAME
SHA256        : $SHA

Шаги (Windows):
  1. Подключить SA-02m (FEL / USB mass storage — см. README проекта).
  2. ImageUSB → Write → выбрать $IMG_NAME → Write.
  3. Дождаться окончания записи, отключить, подать питание.
  4. Первая загрузка ~2–3 мин (расширение rootfs до ~7 ГБ).
  5. Проверка: ssh root@192.168.1.136  →  df -h /  (Size ≈ 7.0G)

Если df показывает ~1.8G:
  ssh root@<IP> '/usr/lib/armbian/armbian-resize-filesystem start'

Образ собран с PiShrink; расширение выполняет sa02m-rootfs-expand.service
(или armbian-resize-filesystem на старых образах).
EOF

echo "READY: $DEST"
echo "  $IMG_NAME"
echo "  ${IMG_NAME}.sha256"
echo "  IMAGEUSB.txt"
echo
echo "ImageUSB: выбрать $DEST/$IMG_NAME"
