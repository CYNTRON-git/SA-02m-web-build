#!/bin/bash
# Actual ext4 policy test. Only a newly allocated loop backed by our own file.
# comment-mutation-proof-exempt: behavioural harness; real ext4 defaults, live remount and subsequent mount observed through the kernel's per-filesystem options.
set -euo pipefail
cd "$(dirname "$0")/../.."
[[ $(id -u) = 0 ]] || { echo 'NOT RUN: root/loop mount required'; exit 77; }
for tool in losetup mkfs.ext4 mount umount tune2fs findmnt python3; do
    command -v "$tool" >/dev/null || { echo "NOT RUN: missing $tool"; exit 77; }
done
task=$(mktemp -d)
loop=''
cleanup() {
    mountpoint -q "$task/root" && umount "$task/root"
    [[ -z "$loop" ]] || losetup -d "$loop"
    rm -rf -- "$task"
}
trap cleanup EXIT
truncate -s 48M "$task/ext4.img"
mkfs.ext4 -q -F "$task/ext4.img"
loop=$(losetup --find --show "$task/ext4.img")
[[ $(losetup -n -O BACK-FILE "$loop") = "$task/ext4.img" ]]
tune2fs -o journal_data_writeback,user_xattr,acl "$loop" >/dev/null
mkdir "$task/root"
mount -o commit=600 "$loop" "$task/root"
mkdir "$task/root/etc"
uuid=$(blkid -s UUID -o value "$loop")
printf 'UUID=%s / ext4 defaults,noatime,commit=600 0 1\n' "$uuid" > "$task/root/etc/fstab"
bash usr/local/sbin/sa02m-rootfs-policy.sh offline "$task/root" "$loop"
bash usr/local/sbin/sa02m-rootfs-policy.sh check "$task/root" "$loop"
before=$(cat "/proc/fs/ext4/${loop##*/}/options")
[[ $before = *data=writeback* && $before = *commit=600* ]]
# Execute live context with ONLY its root constant retargeted to our image.
python3 - usr/local/sbin/sa02m-rootfs-policy.sh "$task/live.sh" "$task/root" <<'PY'
from pathlib import Path
import sys
source, output, root = sys.argv[1:]
text = Path(source).read_text()
assert text.count('LIVE_ROOT = "/"') == 1
Path(output).write_text(text.replace('LIVE_ROOT = "/"', 'LIVE_ROOT = ' + repr(root)))
PY
bash "$task/live.sh" live
live=$(cat "/proc/fs/ext4/${loop##*/}/options")
[[ $live = *data=writeback* && $live = *commit=60* ]]
umount "$task/root"
mount -o commit=60 "$loop" "$task/root"
after=$(cat "/proc/fs/ext4/${loop##*/}/options")
[[ $after = *data=ordered* && $after = *commit=60* ]]
defaults=$(tune2fs -l "$loop" | sed -n 's/^Default mount options: *//p')
[[ $defaults = *user_xattr* && $defaults = *acl* && $defaults != *journal_data_writeback* ]]
printf 'PASS actual ext4: offline preserves writeback/commit600; live remount commit60 preserves writeback; next mount ordered/commit60; defaults=%s\n' "$defaults"
