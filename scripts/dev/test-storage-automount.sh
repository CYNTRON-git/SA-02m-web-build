#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-storage-automount.sh — regression test for the STORAGE_AUTO_FORMAT flag:
# the decision it drives (etc/storage-mount.sh do_mount, cases 1-15), the two
# readers that must agree on what it means (do_mount + status.cgi, cases 16-17),
# and the writer that owns it (etc/sa02m-set-storage-auto-format, case 18).
#
# One harness rather than three, because all three surfaces pin ONE fact — what
# this flag means and who may change it — and every defect found so far was a
# disagreement between them, which only a test that reads more than one file
# can see.
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
# STATUS_CGI_SRC and STORAGE_SETTER_SRC do the same for the other two surfaces.
# Proven RED there: the panel defaulting to ON again (`:-1`); the panel's
# allow-list drifting from the mounter's (both shape and value level); BOTH
# readers agreeing on the UNSAFE default (agreement alone must not pass); a new
# unclassified file naming the flag; a stale ledger entry; the writer
# regenerating the config from scratch, keeping a stale duplicate of its own
# key, carrying an unsafe value, losing the 0|1 argument guard, losing the
# rescan-on-enable, or losing the status-cache invalidation.
#
# Deliberate limit: pointing STORAGE_SETTER_SRC at a writer that hardcodes its
# paths (the pre-fix version) FAILS on the retarget guard rather than running —
# the harness refuses to execute a writer it cannot keep inside the sandbox.
# The 0644 config-mode assertion runs on Linux only (printed as a skip
# elsewhere); CI is the enforcing run.
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

echo "── 16. every READER normalises the flag identically, and fail-safe OFF ──"
# The mounter decides whether to format; the panel tells the operator what the
# mounter will do. When they disagree on the default for an absent/unreadable
# config the panel lies (it showed ON while nothing was ever formatted), so the
# two shipped normalisers are run over ONE value table and must agree — and the
# absent case must be OFF on both, since agreement alone would also be
# satisfied by both defaulting to ON.
STATUS_SRC=${STATUS_CGI_SRC:-www/network_config/cgi-bin/status.cgi}
[ -f "$STATUS_SRC" ] || bad "status.cgi not found: $STATUS_SRC"

extract_norm() { sed -n '/STORAGE_AUTO_FORMAT:-/,/esac/p' "$1"; }

norm_ok=1
for f in "$SRC" "$STATUS_SRC"; do
    blk=$(extract_norm "$f")
    n_esac=$(printf '%s\n' "$blk" | grep -c '^[[:space:]]*esac')
    n_line=$(printf '%s\n' "$blk" | grep -c .)
    if [ -z "$blk" ] || [ "$n_esac" != 1 ] || [ "$n_line" -gt 8 ]; then
        bad "could not extract exactly one normaliser from $f (lines=$n_line esac=$n_esac)"; norm_ok=0
    fi
    printf '%s\n' "$blk" | grep -q '1|yes|true|on|ON|Y' || { bad "$f: normaliser allow-list changed shape"; norm_ok=0; }
done

norm_verdict() { # $1=file $2=outvar $3=__unset__|raw value
    {
        [ "$3" = "__unset__" ] || printf "STORAGE_AUTO_FORMAT='%s'\n" "$3"
        extract_norm "$1"
        printf 'printf "%%s" "${%s-UNSET}"\n' "$2"
    } > "$T/norm.sh"
    bash "$T/norm.sh" 2>/dev/null
}

if [ "$norm_ok" = 1 ]; then
    for v in __unset__ "" 0 1 yes true on ON Y y 2 0n maybe "1 "; do
        m=$(norm_verdict "$SRC" STORAGE_AUTO_FORMAT "$v")
        u=$(norm_verdict "$STATUS_SRC" STORAGE_AUTO_FORMAT_UI "$v")
        label=$([ "$v" = "__unset__" ] && echo "<absent>" || echo "'$v'")
        case "$m" in 0|1) ;; *) bad "mounter verdict for $label is not 0/1: '$m'" ;; esac
        [ "$m" = "$u" ] && ok "$label -> both readers say $m" \
                        || bad "$label -> mounter says '$m', panel says '$u' (the panel would lie)"
    done
    [ "$(norm_verdict "$SRC" STORAGE_AUTO_FORMAT __unset__)" = 0 ] \
        && ok "absent config -> mounter OFF (fail-safe)" || bad "absent config is not OFF in the mounter"
    [ "$(norm_verdict "$STATUS_SRC" STORAGE_AUTO_FORMAT_UI __unset__)" = 0 ] \
        && ok "absent config -> panel OFF (fail-safe)" || bad "absent config is not OFF in the panel"
fi

echo "── 17. no UNCLASSIFIED file touches the flag (open-world sweep + ledger) ──"
# A third reader with its own default would just move the bug, so every shipped
# file naming the key is classified here. Sweep scope = what the repo ships;
# docs/.ai-dev/CHANGELOG (prose about the defect) and the gitignored private/
# deploy artifact are deliberately outside it.
ledger_role() {
    case "$1" in
        etc/storage-mount.sh)                      echo "reader-normaliser (agreement table, case 16)" ;;
        www/network_config/cgi-bin/status.cgi)     echo "reader-normaliser (agreement table, case 16)" ;;
        etc/sa02m-set-storage-auto-format)         echo "the panel's writer (case 18)" ;;
        etc/sa02m_storage.conf)                    echo "shipped default config" ;;
        etc/sa02m-prepare-working-board.sh)        echo "writer + raw reporter (prints the value, interprets no default)" ;;
        etc/udev/99-storage.rules)                 echo "comment only" ;;
        scripts/01-system.sh)                      echo "installer: install + repair of the corrupted 0n shape" ;;
        scripts/dev/test-storage-automount.sh)     echo "this harness" ;;
        *) return 1 ;;
    esac
}
LEDGER="etc/storage-mount.sh www/network_config/cgi-bin/status.cgi etc/sa02m-set-storage-auto-format etc/sa02m_storage.conf etc/sa02m-prepare-working-board.sh etc/udev/99-storage.rules scripts/01-system.sh scripts/dev/test-storage-automount.sh"
FOUND=$(grep -rIl "STORAGE_AUTO_FORMAT" etc www opt usr scripts tools install.sh 2>/dev/null | tr '\\' '/' | sort -u)
n_found=$(printf '%s\n' "$FOUND" | grep -c .)
if [ "$n_found" -lt 7 ]; then
    bad "sweep found only $n_found files naming the flag — the sweep itself is broken (expected >= 7)"
else
    ok "sweep is alive ($n_found shipped files name the flag)"
    for f in $FOUND; do
        ledger_role "$f" >/dev/null || bad "UNCLASSIFIED file names STORAGE_AUTO_FORMAT: $f — classify it here, and if it interprets a default add it to the case-16 table"
    done
    for f in $LEDGER; do
        printf '%s\n' "$FOUND" | grep -qx "$f" || bad "stale ledger entry: $f no longer names the flag"
    done
    [ "$fails" = 0 ] && ok "every file naming the flag is classified" || true
fi

echo "── 18. the panel's writer keeps keys it does not own ──"
SETTER_SRC=${STORAGE_SETTER_SRC:-etc/sa02m-set-storage-auto-format}
SCONF="$T/set-storage.conf"; SCACHE="$T/status-cache"; SMOUNT="$T/scan-shim.sh"
sed -e "s|^CONF=.*|CONF=$SCONF|" \
    -e "s|^STORAGE_MOUNT=.*|STORAGE_MOUNT=$SMOUNT|" \
    -e "s|^STATUS_CACHE=.*|STATUS_CACHE=$SCACHE|" "$SETTER_SRC" > "$T/setter.sh"
printf '#!/bin/bash\nprintf "scan %%s\\n" "$*" >> "%s/scan.log"\nexit 0\n' "$T" > "$SMOUNT"; chmod +x "$SMOUNT"

setter_ok=1
for p in /etc/sa02m_storage.conf /usr/local/bin/storage-mount.sh /tmp/sa02m_status_cache; do
    if grep -qF "$p" "$T/setter.sh"; then
        bad "writer not retargetable ($p still present) — refusing to run it against the real system"
        setter_ok=0
    fi
done

write_conf() { printf '%s\n' "$@" > "$SCONF"; }
setter_run() { rm -f "$T/scan.log"; mkdir -p "$SCACHE"; : > "$SCACHE/system.json"; : > "$SCACHE/storage.json"
               bash "$T/setter.sh" "$1" >/dev/null 2>&1; SRC_RC=$?; }
conf_has() { grep -qx "$1" "$SCONF" 2>/dev/null; }

if [ "$setter_ok" = 1 ]; then
    # a. the reported defect: toggling autoformat must not wipe the autorun opt-in
    write_conf "# комментарий оператора" "STORAGE_AUTO_FORMAT=0" "STORAGE_ALLOW_AUTORUN=1"
    setter_run 1
    [ "$SRC_RC" = 0 ] && ok "exit 0" || bad "exit $SRC_RC, expected 0"
    conf_has "STORAGE_AUTO_FORMAT=1" && ok "owned key written" || bad "owned key not written: $(cat "$SCONF")"
    conf_has "STORAGE_ALLOW_AUTORUN=1" \
        && ok "foreign key STORAGE_ALLOW_AUTORUN=1 preserved" \
        || bad "toggling autoformat silently reset the operator's autorun opt-in"
    # The readers SOURCE this file, so a duplicate stale assignment would win
    # over ours: assert the EFFECTIVE value, not just that our line is present.
    n_own=$(grep -c '^STORAGE_AUTO_FORMAT=' "$SCONF")
    [ "$n_own" = 1 ] && ok "exactly one STORAGE_AUTO_FORMAT assignment" \
                     || bad "$n_own assignments of the owned key — a stale one would win on source"
    eff=$(bash -c ". '$SCONF' >/dev/null 2>&1; printf '%s' \"\${STORAGE_AUTO_FORMAT-UNSET}:\${STORAGE_ALLOW_AUTORUN-UNSET}\"")
    [ "$eff" = "1:1" ] && ok "sourcing the result yields autoformat=1, autorun=1" \
                       || bad "sourced result is '$eff', expected '1:1'"

    # b. byte-identical re-run (the file must not grow or reorder)
    cp "$SCONF" "$T/conf.snap"; setter_run 1
    cmp -s "$SCONF" "$T/conf.snap" && ok "re-run is byte-identical" || bad "re-run changed the file: $(diff "$T/conf.snap" "$SCONF" | head -3)"

    # c. toggling back off keeps it too
    setter_run 0
    conf_has "STORAGE_AUTO_FORMAT=0" && ok "toggled back off" || bad "off not written"
    conf_has "STORAGE_ALLOW_AUTORUN=1" && ok "foreign key survives the off-toggle" || bad "foreign key lost on the off-toggle"

    # d. no config yet -> created, and a key that was never there is NOT invented
    rm -f "$SCONF"; setter_run 1
    conf_has "STORAGE_AUTO_FORMAT=1" && ok "config created from nothing" || bad "config not created"
    grep -q "STORAGE_ALLOW_AUTORUN" "$SCONF" && bad "invented an autorun key that was never set" || ok "absent key stays absent (autorun stays OFF)"

    # e. an unsafe value is dropped, never carried into a root-sourced file
    # `$(id)` has no whitespace, so ONLY the unsafe-character rule can reject it
    write_conf "STORAGE_AUTO_FORMAT=0" 'STORAGE_ALLOW_AUTORUN=$(id)' "OTHER_KEY=ok"
    setter_run 1
    grep -q '\$(' "$SCONF" && bad "carried a command substitution into a file sourced as root" || ok "unsafe value dropped"
    grep -q "STORAGE_ALLOW_AUTORUN" "$SCONF" && bad "unparseable autorun value survived (must fail safe to absent=OFF)" || ok "unparseable autorun value -> absent -> OFF"
    conf_has "OTHER_KEY=ok" && ok "an unrelated well-formed key still survives" || bad "dropped a well-formed foreign key"

    # f. a trailing comment is not a parseable assignment -> dropped (fail-safe OFF)
    write_conf "STORAGE_AUTO_FORMAT=0" "STORAGE_ALLOW_AUTORUN=1 # приёмник"
    setter_run 1
    grep -q "STORAGE_ALLOW_AUTORUN" "$SCONF" && bad "carried a line with a trailing comment" || ok "trailing-comment line dropped (fail-safe OFF)"

    # g. a bad argument changes nothing at all
    write_conf "STORAGE_AUTO_FORMAT=1" "STORAGE_ALLOW_AUTORUN=1"
    cp "$SCONF" "$T/conf.snap"
    for badarg in 2 "" "1; rm -rf /" yes; do
        setter_run "$badarg"
        [ "$SRC_RC" != 0 ] || bad "bad argument '$badarg' accepted"
        cmp -s "$SCONF" "$T/conf.snap" || bad "bad argument '$badarg' modified the config"
    done
    ok "every bad argument refused with the config untouched"

    # h. the enable path still rescans; the disable path must not
    write_conf "STORAGE_AUTO_FORMAT=0"
    setter_run 1; [ -s "$T/scan.log" ] && ok "enable triggers a rescan" || bad "enable no longer rescans"
    setter_run 0; [ -s "$T/scan.log" ] && bad "disable triggered a rescan" || ok "disable does not rescan"

    # i. the panel's status cache is still invalidated
    setter_run 1
    { [ ! -f "$SCACHE/system.json" ] && [ ! -f "$SCACHE/storage.json" ]; } \
        && ok "status cache invalidated" || bad "stale status cache left behind (panel would show the old value)"

    # j. mode: www-data must still be able to read the config (Linux only)
    if [ "$(uname -s 2>/dev/null)" = "Linux" ]; then
        mode=$(stat -c %a "$SCONF" 2>/dev/null)
        [ "$mode" = "644" ] && ok "config mode 644" || bad "config mode is $mode, expected 644 (the panel reads it as www-data)"
    else
        echo "skip  config-mode check (not Linux; CI enforces)"
    fi
fi

echo "---"
if [ "$fails" = 0 ]; then
    echo "storage-automount-decision: all checks passed"
else
    echo "storage-automount-decision: $fails check(s) FAILED"
fi
exit "$fails"
