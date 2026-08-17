#!/bin/bash
# SA-02m — publish make-image artifact as stand current.img.xz
set -euo pipefail
LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGES_DIR="${IMAGES_DIR:-$SCRIPT_DIR/stand-data/images}"

usage() {
    cat <<'EOF'
Usage:
  publish-image.sh --image PATH [--dest DIR]

Copies shrunk .img.xz + .sha256 into stand images dir as:
  current.img.xz
  current.img.xz.sha256

Also keeps a dated copy of the source basename.

Example:
  ./publish-image.sh --image ./out/sa02m-1eth-v1.0.0-shrunk.img.xz
EOF
}

IMAGE=""
DEST="$IMAGES_DIR"

while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE="$2"; shift 2 ;;
        --dest)  DEST="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown: $1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$IMAGE" ] || { echo "ERROR: --image required" >&2; usage; exit 2; }
[ -f "$IMAGE" ] || { echo "ERROR: not found: $IMAGE" >&2; exit 1; }

case "$IMAGE" in
    *.img.xz) ;;
    *) echo "ERROR: expected *.img.xz (not raw 3–4G .img)" >&2; exit 1 ;;
esac

SHA_FILE="${IMAGE}.sha256"
if [ ! -f "$SHA_FILE" ]; then
    echo "WARN: missing $SHA_FILE — computing" >&2
    (cd "$(dirname "$IMAGE")" && sha256sum "$(basename "$IMAGE")" > "$(basename "$SHA_FILE")")
fi

mkdir -p "$DEST"
BASE="$(basename "$IMAGE")"
cp -f "$IMAGE" "$DEST/$BASE"
SHA=$(awk '{print $1}' "$SHA_FILE")
printf '%s  %s\n' "$SHA" "$BASE" > "$DEST/${BASE}.sha256"

# Atomic-ish switch of current.*
cp -f "$DEST/$BASE" "$DEST/current.img.xz.tmp"
mv -f "$DEST/current.img.xz.tmp" "$DEST/current.img.xz"
printf '%s  current.img.xz\n' "$SHA" > "$DEST/current.img.xz.sha256"

# Symlink convenience (may fail on FAT dest — ignore)
ln -sfn "$BASE" "$DEST/current.img.xz.link" 2>/dev/null || true

echo "PUBLISHED: $DEST/current.img.xz"
echo "  sha256: $SHA"
echo "  source copy: $DEST/$BASE"
ls -lh "$DEST/current.img.xz" "$DEST/current.img.xz.sha256"
