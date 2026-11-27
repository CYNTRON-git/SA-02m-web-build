#!/bin/sh
# FEL mdev starts this file as /mnt/autorun.sh in the recovery initramfs.
set -eu
set -o pipefail
media=/mnt
target=/dev/mmcblk2
sys=/sys/class/block/mmcblk2
lock=/tmp/sa02m-sealed-flash
image=$media/sdcard.img
mounted=
die() { echo "FEL ERROR: $*" >&2; exit 1; }
cleanup() {
    [ -z "$mounted" ] || umount "$mounted" || return 1
    rmdir "$lock/view" "$lock"
}
digest() {
    value=$(sha256sum < "$1") || return 1
    printf '%s\n' "${value%% *}"
}
hex() {
    case "$1" in ''|*[!0-9a-f]*) return 1;; esac
    [ "${#1}" -eq 64 ]
}
whole_number() {
    case "$1" in ''|0*|*[!0-9]*) return 1;; esac
    [ "${#1}" -le 12 ]
}
# The only disk mounted by this RAM recovery must be its USB source. Refuse
# an installed OS, a mounted eMMC or an active swap before opening the target.
topology() {
    awk '$5=="/" && $3~/^0:/ {for(i=7;i<=NF;i++) if($i=="-" && $(i+1)~/^(rootfs|ramfs|tmpfs)$/) root=1}
         $5=="/mnt" {sources++; for(i=7;i<=NF;i++) if($i=="-" && $(i+1)=="exfat" && $(i+2)~/^\/dev\/sd[a-z][1-9][0-9]*$/ && $3!~/^0:/) usb++}
         $3!~/^0:/ && $5!="/mnt" {bad=1}
         END {exit !(root && sources==1 && usb==1 && !bad)}' /proc/self/mountinfo || return 1
    [ ! -e /proc/swaps ] || awk 'NR>1 {exit 1}' /proc/swaps || return 1
    [ -b "$target" ] && [ ! -L "$target" ] && [ "$(cat "$sys/device/type")" = MMC ] &&
        [ "$(cat "$sys/removable")" = 0 ]
}
mkdir "$lock" || die 'flash already running'
mkdir "$lock/view" || die 'cannot create verifier mount point'
trap 'cleanup' EXIT
trap 'exit 1' HUP INT TERM
topology || die 'unexpected recovery root, mounted disk, swap or eMMC identity'
receipt=$media/SEALED-FEL
[ -f "$receipt" ] && [ ! -L "$receipt" ] && [ -f "$image" ] && [ ! -L "$image" ] || die 'missing sealed payload'
IFS=' ' read -r format bytes raw boot seal selector extra < "$receipt" || die 'invalid receipt'
[ "$format" = SA02M-SEALED-FEL-1 ] && [ -z "$extra" ] && whole_number "$bytes" &&
    hex "$raw" && hex "$boot" && hex "$seal" && hex "$selector" || die 'invalid receipt fields'
[ "$(wc -l < "$receipt")" -eq 1 ] || die 'invalid receipt lines'
[ "$(stat -c '%s' "$image")" = "$bytes" ] || die 'source size mismatch'
sectors=$(cat "$sys/size") || die 'eMMC capacity unavailable'
whole_number "$sectors" && [ "$((bytes % 512))" -eq 0 ] && [ "$((bytes / 512))" -le "$sectors" ] || die 'image does not fit eMMC'
[ "$(digest "$image")" = "$raw" ] || die 'source checksum mismatch'
[ -f "$media/boot.scr" ] && [ "$(digest "$media/boot.scr")" = "$boot" ] || die 'source boot script mismatch'
if [ -d /sys/module/g_mass_storage ]; then
    rmmod g_mass_storage && [ ! -d /sys/module/g_mass_storage ] || die 'USB mass storage is still exporting eMMC'
fi
topology || die 'target became busy'
echo "FEL writing $bytes verified bytes to $target"
dd if="$image" of="$target" bs=4M iflag=count_bytes count="$bytes" conv=notrunc,fsync || die 'write failed'
sync || die 'sync failed'
# Direct I/O bypasses the Linux block cache. count_bytes includes the
# final partial 4 MiB block without hashing the unused eMMC tail.
readback=$(dd if="$target" bs=4M iflag=direct,count_bytes count="$bytes" status=none | sha256sum) || die 'eMMC readback failed'
[ "${readback%% *}" = "$raw" ] || die 'eMMC readback checksum mismatch'
partprobe "$target" || die 'partition-table reload failed'
view=$lock/view
for suffix in p1 p2; do
    n=0
    while [ ! -b "${target}$suffix" ] && [ "$n" -lt 5 ]; do sleep 1; n=$((n+1)); done
    [ -b "${target}$suffix" ] || die "missing eMMC partition $suffix"
done
mount -t vfat -o ro "${target}p1" "$view" || die 'failed to mount FAT read-only'
mounted=$view
[ -f "$view/boot.scr" ] && [ "$(digest "$view/boot.scr")" = "$boot" ] || die 'installed boot script mismatch'
umount "$view" || die 'FAT unmount failed'
mounted=
mount -t ext4 -o ro,noload "${target}p2" "$view" || die 'failed to mount sealed ext4 read-only'
mounted=$view
state=$view/var/lib/sa02m-runtime-transition/image-epoch.json
active=$view/var/lib/sa02m-python-activation/current.json
for file in "$state" "$active"; do [ -f "$file" ] && [ ! -L "$file" ] || die 'missing installed runtime authority'; done
[ "$(digest "$state")" = "$seal" ] && [ "$(digest "$active")" = "$selector" ] || die 'installed runtime seal or selector mismatch'
umount "$view" || die 'ext4 unmount failed'
mounted=
cleanup || die 'verification cleanup failed'
trap - EXIT HUP INT TERM
echo 'FEL verified complete eMMC range, FAT boot script and sealed ext4. Rebooting.'
reboot -f
