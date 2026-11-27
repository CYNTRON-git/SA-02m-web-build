#!/bin/bash
# Mount only the private acquisition and its writable derivative for sealing.
set -euo pipefail

[ "$#" -eq 4 ] && [ "$EUID" -eq 0 ] || {
    echo 'usage (as root): seal-captured-runtime.sh RAW COPY RECEIPT PASSPORT' >&2
    exit 2
}
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
raw=$1
copy=$2
receipt=$3
passport=$4
[[ "${raw%/*}" =~ ^/var/lib/sa02m-image-captures/acquisition-[a-zA-Z0-9]{8}$ ]] || exit 1
for parent in /var/lib/sa02m-image-captures "${raw%/*}"; do
    [ -d "$parent" ] && [ ! -L "$parent" ] \
        && [ "$(stat -c '%u:%g:%a' -- "$parent")" = 0:0:700 ] || exit 1
done
for input in "$raw" "$copy" "$receipt" "$passport"; do
    [[ "$input" == /var/lib/sa02m-image-captures/* && -f "$input" && ! -L "$input" ]] \
        && [ "$(stat -c '%u' -- "$input")" = 0 ] || exit 1
done
[ ! "$raw" -ef "$copy" ] || exit 1
[ "${raw%/*}" = "${copy%/*}" ] && [ "${raw%/*}" = "${receipt%/*}" ] \
    && [ "${raw%/*}" = "${passport%/*}" ] || exit 1

mounts=$(mktemp -d -- "${raw%/*}/mounts-XXXXXXXX")
raw_loop='' copy_loop='' raw_mounted=0 copy_mounted=0
cleanup() {
    local rc=$?
    trap - EXIT
    if [ "$copy_mounted" = 1 ]; then
        if umount -- "$mounts/copy"; then copy_mounted=0; else rc=1; fi
    fi
    if [ "$raw_mounted" = 1 ]; then
        if umount -- "$mounts/raw"; then raw_mounted=0; else rc=1; fi
    fi
    if [ -n "$copy_loop" ] && [ "$copy_mounted" = 0 ]; then losetup -d "$copy_loop" || rc=1; fi
    if [ -n "$raw_loop" ] && [ "$raw_mounted" = 0 ]; then losetup -d "$raw_loop" || rc=1; fi
    if [ "$copy_mounted" = 0 ] && [ "$raw_mounted" = 0 ]; then
        rmdir -- "$mounts/copy" "$mounts/raw" "$mounts" || rc=1
    fi
    exit "$rc"
}
trap cleanup EXIT
mkdir -- "$mounts/raw" "$mounts/copy"
raw_loop=$(losetup --read-only --partscan --find --show "$raw")
copy_loop=$(losetup --partscan --find --show "$copy")
for _ in 1 2 3 4 5 6 7 8; do
    [ -b "${raw_loop}p2" ] && [ -b "${copy_loop}p2" ] && break
    sleep 1
done
[ -b "${raw_loop}p2" ] && [ -b "${copy_loop}p2" ] || {
    echo 'runtime seal requires both partition 2 loop devices' >&2
    exit 1
}
mount -t ext4 -o ro,noload -- "${raw_loop}p2" "$mounts/raw"
raw_mounted=1
mount -t ext4 -o rw -- "${copy_loop}p2" "$mounts/copy"
copy_mounted=1
/usr/bin/python3 -I -B "$here/seal-runtime-image.py" --capture-root "$mounts/raw" \
    --image-root "$mounts/copy" --raw "$raw" --preflight "$receipt" --passport "$passport"
