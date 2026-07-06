#!/usr/bin/env bash
#
# prepare-sa02m-flash-usb.sh — упаковка .img.xz на USB для flash-receiver.sh
#
# Usage:
#   ./tools/debian-rootfs/prepare-sa02m-flash-usb.sh \
#       --image ./out/sa02m-bullseye-v1.0.3.37-shrunk.img.xz \
#       --dest /mnt/c/USB/SA02m
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGING="$SCRIPT_DIR/../imaging/prepare-flash-media.sh"

IMAGE=""
DEST=""

while [ "$#" -gt 0 ]; do
	case "$1" in
		--image) IMAGE="$2"; shift 2 ;;
		--dest) DEST="$2"; shift 2 ;;
		-h|--help)
			echo "Usage: $0 --image PATH [--dest DIR]"
			exit 0
			;;
		*) echo "unknown: $1" >&2; exit 2 ;;
	esac
done

[ -n "$IMAGE" ] || { echo "ERROR: --image required" >&2; exit 2; }
[ -f "$IMAGING" ] || { echo "ERROR: $IMAGING not found" >&2; exit 1; }

ARGS=(--image "$IMAGE")
[ -n "$DEST" ] && ARGS+=(--dest "$DEST")

exec bash "$IMAGING" "${ARGS[@]}"
