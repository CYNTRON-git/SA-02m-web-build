#!/usr/bin/env bash
#
# apply.sh — copy the SA-02м overlay (defconfig + DTS) into a checkout of
# `wirenboard/linux` and apply the small patches under `patches/`.
#
# Usage:
#   ./kernel-port/apply.sh /path/to/wb-linux-checkout
#
# The target checkout is expected to be at branch/tag
# `release/wb-2606/wb7-bullseye` (kernel 5.10.35). Any other base is
# accepted but not tested — check patch hunks manually.
#
# The script is IDEMPOTENT: re-running it on an already-patched tree only
# refreshes copied files and skips patches that are already applied.

set -euo pipefail

PORT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $# -lt 1 ]]; then
	echo "usage: $0 <path-to-wirenboard-linux-checkout>" >&2
	exit 2
fi

TARGET="$(cd "$1" && pwd)"

if [[ ! -f "$TARGET/Makefile" ]] || ! grep -q 'ARM64\|VERSION' "$TARGET/Makefile" 2>/dev/null; then
	echo "error: $TARGET does not look like a Linux kernel tree (no top-level Makefile)" >&2
	exit 2
fi

echo "[apply.sh] overlay source: $PORT_DIR/overlay"
echo "[apply.sh] target kernel : $TARGET"

echo "[apply.sh] copying overlay files"
find "$PORT_DIR/overlay" -type f | while read -r src; do
	rel="${src#$PORT_DIR/overlay/}"
	dst="$TARGET/$rel"
	mkdir -p "$(dirname "$dst")"
	cp -v "$src" "$dst"
done

apply_patch_if_needed() {
	local patchfile="$1"
	pushd "$TARGET" >/dev/null

	if patch -p1 --dry-run --silent < "$patchfile" >/dev/null 2>&1; then
		echo "[apply.sh] applying $(basename "$patchfile")"
		patch -p1 < "$patchfile"
	elif patch -p1 --dry-run --reverse --silent < "$patchfile" >/dev/null 2>&1; then
		echo "[apply.sh] $(basename "$patchfile") already applied — skipping"
	else
		echo "[apply.sh] WARNING: $(basename "$patchfile") does not apply cleanly"
		echo "[apply.sh] falling back to a 3-way merge attempt (patch --merge)"
		if ! patch -p1 --merge=diff3 < "$patchfile"; then
			echo "[apply.sh] ERROR: $(basename "$patchfile") requires manual inspection"
			popd >/dev/null
			exit 1
		fi
	fi

	popd >/dev/null
}

for p in "$PORT_DIR"/patches/*.patch; do
	[[ -f "$p" ]] || continue
	apply_patch_if_needed "$p"
done

echo "[apply.sh] done"
echo ""
echo "  Next steps (from $TARGET):"
echo "    make ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- sa02m_defconfig"
echo "    make ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- -j\$(nproc) zImage modules dtbs"
echo ""
echo "  Deb-package via WB build system:"
echo "    make -f wb.mk deb KERNEL_FLAVOUR=sa02m"
echo ""
