#!/bin/bash
# SA-02m — подготовка USB/носителя для flash-receiver.sh (§11.1 SA02M_IMAGING_GUIDE.md)
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLASH_NAME="${FLASH_NAME:-sa02m-shrunk.img.xz}"
RECEIVER="$SCRIPT_DIR/flash-receiver.sh"

usage() {
    cat <<'EOF'
Usage:
  prepare-flash-media.sh --image PATH [--dest DIR] [--flash-name NAME]

Копирует на носитель (по умолчанию ./flash-media/):
  sa02m-shrunk.img.xz (+ .sha256 с правильным именем файла)
  flash-receiver.sh
  autorun.sh -> flash-receiver.sh
  manifest.json (если лежит рядом с образом)

Пример:
  ./prepare-flash-media.sh --image ./out/sa02m-1eth-v1.0.0-shrunk.img.xz --dest /mnt/c/USB/SA02m
EOF
}

IMAGE=""
DEST=""

while [ $# -gt 0 ]; do
    case "$1" in
        --image)      IMAGE="$2"; shift 2 ;;
        --dest)       DEST="$2"; shift 2 ;;
        --flash-name) FLASH_NAME="$2"; shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$IMAGE" ] || { echo "ERROR: --image required" >&2; usage; exit 2; }
[ -f "$IMAGE" ] || { echo "ERROR: image not found: $IMAGE" >&2; exit 1; }
[ -r "$RECEIVER" ] || { echo "ERROR: flash-receiver.sh not found: $RECEIVER" >&2; exit 1; }

if [ -z "$DEST" ]; then
    DEST="$SCRIPT_DIR/flash-media"
fi

mkdir -p "$DEST"

SHA_FILE="${IMAGE}.sha256"
if [ ! -f "$SHA_FILE" ]; then
    echo "ERROR: missing ${SHA_FILE}" >&2
    exit 1
fi

SHA=$(awk '{print $1}' "$SHA_FILE")
[ -n "$SHA" ] || { echo "ERROR: empty sha256 in $SHA_FILE" >&2; exit 1; }

cp -f "$IMAGE" "$DEST/$FLASH_NAME"
printf '%s  %s\n' "$SHA" "$FLASH_NAME" > "$DEST/${FLASH_NAME}.sha256"
cp -f "$RECEIVER" "$DEST/flash-receiver.sh"
chmod +x "$DEST/flash-receiver.sh"
# Копия вместо симлинка — FAT32 не поддерживает symlinks
rm -f "$DEST/autorun.sh"
cp -f "$RECEIVER" "$DEST/autorun.sh"
chmod +x "$DEST/autorun.sh"

MANIFEST_SRC="${IMAGE%.img.xz}.manifest.json"
if [ -f "$MANIFEST_SRC" ]; then
    cp -f "$MANIFEST_SRC" "$DEST/manifest.json"
fi

echo "READY: $DEST"
echo "  $FLASH_NAME"
echo "  ${FLASH_NAME}.sha256"
echo "  flash-receiver.sh"
echo "  autorun.sh -> flash-receiver.sh"
[ -f "$DEST/manifest.json" ] && echo "  manifest.json"
