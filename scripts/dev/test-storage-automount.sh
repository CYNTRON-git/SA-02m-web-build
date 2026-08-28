#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-storage-automount.sh — regression test for the auto-format DECISION
# in etc/storage-mount.sh do_mount().
#
# Why this exists: the decision has two opposite failure modes and BOTH are
# silent on a board.
#   * Too strict — an extra `-z "${FSTYPE}"` term in the skip test made an
#     empty (no-filesystem) partition ALWAYS skip mkfs, so with
#     STORAGE_AUTO_FORMAT=1 only NTFS was ever reformatted. Half the shipped
#     feature was dead, the panel toggle and /etc/sa02m_storage.conf both
#     still promised «пустой или NTFS раздел перезаписывается в exFAT», and
#     the log line claimed the flag was 0 while it was 1 — which pointed
#     diagnosis at the config instead of the code.
#   * Too loose — formatting a partition that ntfs3/ntfs-3g could still have
#     mounted DESTROYS the operator's data. The try-mount-before-mkfs order
#     and the flag-off skip are the only things standing between a plugged-in
#     USB stick and mkfs.exfat.
# Neither is reachable by a syntax gate or a grep: the bug is which branch
# runs for which (FSTYPE, flag, mount-outcome) triple.
#
# Method: the SHIPPED script is copied, its dispatcher tail (`case "${ACTION}"`)
# dropped so sourcing runs no action, and its two absolute roots
# (/etc/sa02m_storage.conf, /proc/mounts) sed-retargeted into a scratch tree.
# udevadm/blkid/mount/mkfs.exfat/umount/lsblk/fsck/logger/sync/sleep are
# recording PATH shims. No root, no device, no mkfs, nothing touches the real
# system.
#
# Non-vacuous: a failed or over-wide extraction, an un-retargeted absolute
# path, a missing function, or a shim that was never invoked FAILS rather than
# passing on zero matches.
#
# Drive-to-failure recipe (how this harness was proven to catch the break):
#   cp etc/storage-mount.sh /tmp/broken.sh
#   # reinstate the defect:
#   sed -i 's/if (( STORAGE_AUTO_FORMAT != 1 )); then/if [[ -z "${FSTYPE}" ]] || (( STORAGE_AUTO_FORMAT != 1 )); then/' /tmp/broken.sh
#   STORAGE_MOUNT_SRC=/tmp/broken.sh bash scripts/dev/test-storage-automount.sh
#   # -> FAILS (empty-partition format cases + the log-truth case)
# Other proven-RED mutations: hardcoding the value back into the skip log line;
# dropping the ntfs3 attempt; formatting before try_mount_ntfs; returning 1
# from the intentional skip; defaulting STORAGE_AUTO_FORMAT to 1.
#
# Run: bash scripts/dev/test-storage-automount.sh   (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${STORAGE_MOUNT_SRC:-etc/storage-mount.sh}
[ -f "$SRC" ] || { echo "FAIL  source script not found: $SRC"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
export T_DIR="$T"
BIN="$T/bin"; mkdir -p "$BIN" "$T/dev" "$T/media"
CONF="$T/storage.conf"
MOUNTS="$T/mounts"
: > "$MOUNTS"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── extract + retarget ─────────────────────────────────────────────────────
# Drop the dispatcher tail so sourcing performs no action.
sed '/^case .*ACTION/,$d' "$SRC" > "$T/mount-fns.sh"
sed -i -e "s|/etc/sa02m_storage.conf|$CONF|g" -e "s|/proc/mounts|$MOUNTS|g" "$T/mount-fns.sh"

for fn in do_mount format_exfat probe_fstype try_mount_ntfs is_disk_with_partitions log; do
    grep -q "^${fn}() {" "$T/mount-fns.sh" \
        || { echo "FAIL  could not extract ${fn}() from $SRC (did the file shape change?)"; exit 1; }
done
grep -q '^case .*ACTION' "$T/mount-fns.sh" \
    && { echo "FAIL  dispatcher tail still present — sourcing would run an action"; exit 1; }
grep -q '/etc/sa02m_storage.conf' "$T/mount-fns.sh" \
    && { echo "FAIL  conf path not retargeted — the test would read the real /etc"; exit 1; }
grep -q '/proc/mounts' "$T/mount-fns.sh" \
    && { echo "FAIL  /proc/mounts not retargeted — the test would read the real host"; exit 1; }
grep -q 'mkfs.exfat' "$T/mount-fns.sh" \
    || { echo "FAIL  no mkfs.exfat call left in the extracted body"; exit 1; }

# ── PATH shims ─────────────────────────────────────────────────────────────
cat > "$BIN/udevadm" <<'SHIM'
#!/bin/bash
case "${1:-}" in settle) exit 0 ;; esac
printf 'udevadm %s\n' "$*" >> "$T_DIR/probe.log"
[ -n "${SHIM_FSTYPE:-}" ] && printf 'ID_FS_TYPE=%s\n' "$SHIM_FSTYPE"
exit 0
SHIM
cat > "$BIN/blkid" <<'SHIM'
#!/bin/bash
printf 'blkid %s\n' "$*" >> "$T_DIR/probe.log"
[ -n "${SHIM_FSTYPE:-}" ] && printf '%s\n' "$SHIM_FSTYPE"
exit 0
SHIM
cat > "$BIN/mount" <<'SHIM'
#!/bin/bash
printf '%s\n' "$*" >> "$T_DIR/mount.log"
fstype=""; prev=""
for a in "$@"; do [ "$prev" = "-t" ] && fstype="$a"; prev="$a"; done
case "$fstype" in
  ntfs3)   [ "${SHIM_NTFS3_OK:-0}" = "1" ]  && exit 0 || exit 1 ;;
  ntfs-3g) [ "${SHIM_NTFS3G_OK:-0}" = "1" ] && exit 0 || exit 1 ;;
  *)       [ "${SHIM_FSMOUNT_OK:-1}" = "1" ] && exit 0 || exit 1 ;;
esac
SHIM
cat > "$BIN/mkfs.exfat" <<'SHIM'
#!/bin/bash
printf '%s\n' "$*" >> "$T_DIR/mkfs.log"
exit "${SHIM_MKFS_RC:-0}"
SHIM
cat > "$BIN/lsblk" <<'SHIM'
#!/bin/bash
printf '%s\n' "$*" >> "$T_DIR/lsblk.log"
echo "disk"
i=0
while [ "$i" -lt "${SHIM_LSBLK_PARTS:-0}" ]; do echo "part$i"; i=$((i + 1)); done
exit 0
SHIM
for noop in logger sync sleep umount fsck; do
    printf '#!/bin/bash\nprintf "%s %%s\\n" "$*" >> "$T_DIR/%s.log"\nexit 0\n' "$noop" "$noop" > "$BIN/$noop"
done
chmod +x "$BIN"/*
PATH="$BIN:$PATH"

ntfs3g_present() { printf '#!/bin/bash\nexit 0\n' > "$BIN/mount.ntfs-3g"; chmod +x "$BIN/mount.ntfs-3g"; }
ntfs3g_absent()  { rm -f "$BIN/mount.ntfs-3g"; }

# ── harness helpers ────────────────────────────────────────────────────────
load_src() {   # $1 = conf body ("" = no conf file at all)
    if [ "${1-__none__}" = "__none__" ]; then rm -f "$CONF"; else printf '%s\n' "$1" > "$CONF"; fi
    # shellcheck disable=SC1090
    . "$T/mount-fns.sh"
}

reset_case() { # $1 = device basename, $2 = TYPE (usb|sdcard)
    rm -f "$T"/{mount,mkfs,probe,lsblk,umount,fsck,logger,sync,sleep}.log
    : > "$MOUNTS"
    rm -rf "$T/media"; mkdir -p "$T/media"
    rm -f "$T/dev"/*
    : > "$T/dev/$1"
    DEV_PATH="$T/dev/$1"
    MOUNT_POINT="$T/media/usb"
    TYPE="$2"
    FSTYPE=""
    export SHIM_FSTYPE="" SHIM_NTFS3_OK=0 SHIM_NTFS3G_OK=0 SHIM_FSMOUNT_OK=1 SHIM_MKFS_RC=0 SHIM_LSBLK_PARTS=0
    ntfs3g_present
}

run_mount() { OUT=$(do_mount 2>&1); RC=$?; }
mkfs_ran()  { [ -s "$T/mkfs.log" ]; }
mounted_as() { grep -q -- "-t $1 " "$T/mount.log" 2>/dev/null; }

load_src "STORAGE_AUTO_FORMAT=0"

echo "── 1. THE regression: empty FSTYPE + flag ON -> the partition IS formatted ──"
reset_case sda1 usb
STORAGE_AUTO_FORMAT=1
run_mount
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
[ -s "$T/probe.log" ] && ok "fs probe really ran (shim invoked)" || bad "probe shim never called — the harness proved nothing"
if mkfs_ran; then ok "mkfs.exfat called on the empty partition"; else bad "empty partition NOT formatted with STORAGE_AUTO_FORMAT=1 (the regression)"; fi
grep -q -- "-n USB_EXFAT" "$T/mkfs.log" 2>/dev/null && ok "USB label used" || bad "wrong/absent mkfs label: $(cat "$T/mkfs.log" 2>/dev/null)"
mounted_as exfat && ok "mounted as exfat after the format" || bad "no exfat mount after the format"

echo "── 2. empty FSTYPE + flag OFF -> no mkfs, intentional skip is exit 0 ──"
reset_case sda1 usb
STORAGE_AUTO_FORMAT=0
run_mount
[ "$RC" = 0 ] && ok "exit 0 (systemd must not show the unit failed)" || bad "exit $RC, expected 0"
mkfs_ran && bad "mkfs ran with the flag OFF — destructive" || ok "no mkfs with the flag OFF"
printf '%s\n' "$OUT" | grep -q "STORAGE_AUTO_FORMAT=0" && ok "log states the flag value" || bad "log does not state the flag value: $OUT"
printf '%s\n' "$OUT" | grep -q "без распознанной ФС" && ok "log states the real reason (no filesystem)" || bad "log does not name the real reason: $OUT"

echo "── 3. the log line is DERIVED, not hardcoded (flag=7 must print 7) ──"
reset_case sda1 usb
STORAGE_AUTO_FORMAT=7
run_mount
mkfs_ran && bad "mkfs ran for a non-1 flag value" || ok "non-1 flag value does not format"
printf '%s\n' "$OUT" | grep -q "STORAGE_AUTO_FORMAT=7" \
    && ok "log printed the REAL value (7)" \
    || bad "log hardcodes a value instead of printing the real one: $OUT"

echo "── 4. NTFS + flag ON, no ntfs driver mounts it -> formatted (the half that worked) ──"
reset_case sda1 usb
export SHIM_FSTYPE=ntfs
STORAGE_AUTO_FORMAT=1
run_mount
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
mkfs_ran && ok "unmountable NTFS reformatted" || bad "unmountable NTFS not formatted with the flag ON"

echo "── 5. NTFS + flag OFF -> no mkfs, reason names the filesystem ──"
reset_case sda1 usb
export SHIM_FSTYPE=ntfs
STORAGE_AUTO_FORMAT=0
run_mount
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
mkfs_ran && bad "mkfs ran with the flag OFF" || ok "no mkfs with the flag OFF"
printf '%s\n' "$OUT" | grep -q "ntfs" && ok "log names the ntfs case" || bad "log does not name the ntfs case: $OUT"

echo "── 6. DATA-SAFETY FLOOR: a mountable NTFS is never reformatted, flag ON ──"
reset_case sda1 usb
export SHIM_FSTYPE=ntfs SHIM_NTFS3_OK=1
STORAGE_AUTO_FORMAT=1
run_mount
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
mkfs_ran && bad "a MOUNTABLE NTFS was reformatted — operator data destroyed" || ok "mountable NTFS kept"
head -1 "$T/mount.log" | grep -q -- "-t ntfs3 " && ok "ntfs3 kernel driver tried FIRST" || bad "ntfs3 not the first mount attempt: $(head -1 "$T/mount.log")"
printf '%s\n' "$OUT" | grep -q "ntfs3 kernel driver" && ok "ntfs3 success logged" || bad "ntfs3 success not logged"

echo "── 7. ntfs-3g FUSE fallback still guards the data (ntfs3 fails, ntfs-3g mounts) ──"
reset_case sda1 usb
export SHIM_FSTYPE=ntfs SHIM_NTFS3_OK=0 SHIM_NTFS3G_OK=1
STORAGE_AUTO_FORMAT=1
run_mount
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
mkfs_ran && bad "formatted despite a successful ntfs-3g mount" || ok "ntfs-3g mount preempts the format"
mounted_as ntfs-3g && ok "ntfs-3g attempted after ntfs3" || bad "no ntfs-3g attempt"

echo "── 8. ntfs-3g binary absent -> no ntfs-3g attempt, format proceeds ──"
reset_case sda1 usb
ntfs3g_absent
export SHIM_FSTYPE=ntfs SHIM_NTFS3G_OK=1
STORAGE_AUTO_FORMAT=1
run_mount
mounted_as ntfs-3g && bad "ntfs-3g mount attempted without mount.ntfs-3g installed" || ok "command -v guard honoured"
mkfs_ran && ok "falls through to the format" || bad "no format after both ntfs paths were unavailable"
ntfs3g_present

echo "── 9. a healthy filesystem is never touched by the format branch (vfat, flag ON) ──"
reset_case sda1 usb
export SHIM_FSTYPE=vfat
STORAGE_AUTO_FORMAT=1
run_mount
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
mkfs_ran && bad "a healthy vfat partition was reformatted" || ok "vfat left alone"
mounted_as vfat && ok "mounted as vfat" || bad "not mounted as vfat"
grep -q "umask=000,dmask=000,fmask=000" "$T/mount.log" && ok "vfat/exfat option set preserved" || bad "vfat option set changed"

echo "── 10. a failing mkfs is a real failure -> exit 1 ──"
reset_case sda1 usb
export SHIM_MKFS_RC=1
STORAGE_AUTO_FORMAT=1
run_mount
[ "$RC" = 1 ] && ok "exit 1 on a failed format" || bad "exit $RC after a failed mkfs, expected 1"

echo "── 11. sdcard label + empty FSTYPE + flag ON ──"
reset_case mmcblk1p1 sdcard
STORAGE_AUTO_FORMAT=1
run_mount
grep -q -- "-n SDCARD_EXFAT" "$T/mkfs.log" 2>/dev/null && ok "SDCARD label used" || bad "wrong/absent sdcard label: $(cat "$T/mkfs.log" 2>/dev/null)"

echo "── 12. whole disk with partitions -> quiet skip, no mkfs, exit 0 ──"
reset_case sda usb
export SHIM_LSBLK_PARTS=2
STORAGE_AUTO_FORMAT=1
run_mount
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
mkfs_ran && bad "a partitioned disk was formatted whole" || ok "partitioned disk skipped"
[ -s "$T/mount.log" ] && bad "mount attempted on a partitioned disk" || ok "no mount attempted"

echo "── 13. mount point already mounted -> no re-mount, no mkfs, exit 0 ──"
reset_case sda1 usb
STORAGE_AUTO_FORMAT=1
printf '/dev/sda1 %s vfat rw 0 0\n' "$MOUNT_POINT" > "$MOUNTS"
run_mount
[ "$RC" = 0 ] && ok "exit 0" || bad "exit $RC, expected 0"
mkfs_ran && bad "formatted a device whose mount point is already in use" || ok "no mkfs"
[ -s "$T/mount.log" ] && bad "re-mounted an already mounted point" || ok "no re-mount"

echo "── 14. absent device -> exit 1, nothing formatted ──"
reset_case sda1 usb
rm -f "$DEV_PATH"
STORAGE_AUTO_FORMAT=1
run_mount
[ "$RC" = 1 ] && ok "exit 1" || bad "exit $RC on an absent device, expected 1"
mkfs_ran && bad "formatted an absent device" || ok "no mkfs"

echo "── 15. config parsing: fail-safe OFF, and only the documented ON forms ──"
check_conf() { # $1 = conf body or __none__, $2 = expected normalised value, $3 = label
    reset_case sda1 usb
    load_src "$1"
    [ "${STORAGE_AUTO_FORMAT}" = "$2" ] && ok "$3 -> $2" || bad "$3 -> ${STORAGE_AUTO_FORMAT}, expected $2"
}
check_conf __none__                    0 "no config file (fail-safe)"
check_conf "STORAGE_AUTO_FORMAT=0"     0 "explicit 0"
check_conf "STORAGE_AUTO_FORMAT=1"     1 "explicit 1"
check_conf "STORAGE_AUTO_FORMAT=yes"   1 "yes"
check_conf "STORAGE_AUTO_FORMAT=0n"    0 "corrupted 0n (the 01-system.sh repair case)"
check_conf "STORAGE_AUTO_FORMAT=maybe" 0 "unrecognised value"

echo "---"
if [ "$fails" = 0 ]; then
    echo "storage-automount-decision: all checks passed"
else
    echo "storage-automount-decision: $fails check(s) FAILED"
fi
exit "$fails"
