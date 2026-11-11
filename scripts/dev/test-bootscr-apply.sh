#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-bootscr-apply.sh — behavioural regression for the two writers of the
# board's boot.scr: the hand-run tool usr/local/sbin/sa02m-bootscr-apply.sh
# and the boot-time restorer etc/sa02m-restore-dtb.sh.
#
# Why this exists (audit 2026-11-11 A1-01 / A3-01): the kernel parameter
# watchdog.stop_on_reboot=0 — the half of the hung-reboot fix that keeps the
# kernel from disarming the hardware watchdog in kernel_restart_prepare() —
# lived in a comment, not in boot.scr. Nothing on an installed board rewrites
# boot.scr (installer, OTA, offline pack and www-only all leave the FAT alone),
# and a FAT copy edited by hand is put back from the canonical copy by
# sa02m-restore-dtb.sh at the next boot — with a plain `cp` over the live file,
# so a power cut mid-copy left U-Boot no script at all (recovery = FEL). The
# Operator's decision (В2 (a)): a hand-run tool that rebuilds BOTH copies from
# the shipped payload, canonical first, each by tmp + sync + rename; and the
# restorer made atomic so it can finish an interrupted apply.
#
# Method (idiom of test-dtb-patch-tool.sh): the SHIPPED scripts are copied into
# a scratch tree with their single-assignment path constants sed-retargeted
# (FAT_DEV, FAT_MNT, the canonical boot.scr / DTB, the payload, uboot-script.py,
# BACKUP_DIR, /proc/cmdline, the watchdog sysfs parameter, LOCK_DIR, LOG) and
# proven to differ from the originals in nothing else. FAT is a directory;
# `mountpoint` says «mounted», `mount`/`umount` record, `id -u` answers 0.
# uboot-script.py is the repo's own (tools/imaging/, the format's one home).
# A `mv` shim can fail any rename INTO the FAT directory (the interrupted case).
#
# Cases:
#   R1  restore-dtb: FAT boot.scr differs from the canonical -> FAT replaced by
#       rename (inode changes), byte-equal to the canonical, no temp left.
#                                                   RED: `cp` keeps the inode
#   A1  apply: both copies rebuilt from the payload (validated legacy image,
#       carries watchdog.stop_on_reboot=0), equal md5, backup of both old copies
#       under BACKUP_DIR/bootscr-backup-<ts>/, no temp files, exit 0.
#                                                   RED: no tool
#   A2  second apply: «уже применено», nothing written, no new backup.
#   A3  rename onto FAT fails: exit 5, FAT still the OLD script, canonical NEW;
#       restore-dtb then makes FAT new (rename, inode changes).
#   A4  refusals, nothing written: not root (exit 3); a payload without the
#       parameter (exit 3).
#   C1  check: exit 0 when /proc/cmdline carries the parameter AND
#       stop_on_reboot = 0; exit 1 when the cmdline lacks it; md5 of both copies
#       printed.
# Non-vacuous: a missing tool/restorer/payload/uboot-script.py or a failed
# retarget FAILS. Needs bash + coreutils + python3.
# Run: bash scripts/dev/test-bootscr-apply.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

TOOL_SRC=${BOOTSCR_APPLY_SRC:-usr/local/sbin/sa02m-bootscr-apply.sh}
RESTORE_SRC=${RESTORE_DTB_SRC:-etc/sa02m-restore-dtb.sh}
PAYLOAD_SRC=etc/boot.cmd.sa02m.min
UBOOT_PY=tools/imaging/uboot-script.py
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

for f in "$RESTORE_SRC" "$PAYLOAD_SRC" "$UBOOT_PY"; do
    [ -s "$f" ] || { echo "FAIL  missing or empty: $f"; exit 1; }
done
PY=""
for p in python3 python; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys" >/dev/null 2>&1; then PY=$p; break; fi
done
[ -n "$PY" ] || { echo "FAIL  no working python3 — uboot-script.py cannot run"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
FAT="$T/fat"; SHARE="$T/share"; LIB="$T/lib"; ROOT="$T/root"; SH="$T/shim"
mkdir -p "$FAT" "$SHARE" "$LIB" "$ROOT" "$SH" "$T/sys"
cp "$UBOOT_PY" "$LIB/uboot-script.py"
cp "$PAYLOAD_SRC" "$SHARE/boot.cmd.sa02m.min"
printf 'dtb-bytes\n' > "$SHARE/sun8i-a40i-sk.dtb"; cp "$SHARE/sun8i-a40i-sk.dtb" "$FAT/sun8i-a40i-sk.dtb"

# ── retarget the restorer ─────────────────────────────────────────────────
R="$T/restore-dtb.sh"
sed -e "s#^CANONICAL_DTB=\"/usr/local/share/sa02m/sun8i-a40i-sk.dtb\"\$#CANONICAL_DTB=\"$SHARE/sun8i-a40i-sk.dtb\"#" \
    -e "s#^CANONICAL_BOOTSCR=\"/usr/local/share/sa02m/boot.scr\"\$#CANONICAL_BOOTSCR=\"$SHARE/boot.scr\"#" \
    -e "s#^FAT_DEV=\"/dev/mmcblk2p1\"\$#FAT_DEV=\"$T/fatdev\"#" \
    -e "s#^FAT_MNT=\"/mnt/fat\"\$#FAT_MNT=\"$FAT\"#" \
    -e "s#^LOG=\"/var/log/sa02m_install.log\"\$#LOG=\"$T/install.log\"#" "$RESTORE_SRC" > "$R"
n_r=$(grep -cF "$T/" "$R")
[ "$n_r" -ge 5 ] || { echo "FAIL  restore-dtb retarget hit $n_r constants, expected 5"; exit 1; }
if ! sed -e "s#^CANONICAL_DTB=\"$SHARE/#CANONICAL_DTB=\"/usr/local/share/sa02m/#" \
         -e "s#^CANONICAL_BOOTSCR=\"$SHARE/#CANONICAL_BOOTSCR=\"/usr/local/share/sa02m/#" \
         -e "s#^FAT_DEV=\"$T/fatdev\"#FAT_DEV=\"/dev/mmcblk2p1\"#" -e "s#^FAT_MNT=\"$FAT\"#FAT_MNT=\"/mnt/fat\"#" \
         -e "s#^LOG=\"$T/install.log\"#LOG=\"/var/log/sa02m_install.log\"#" "$R" | cmp -s - "$RESTORE_SRC"; then
    echo "FAIL  the restore-dtb copy differs from $RESTORE_SRC beyond the retargeted constants"; exit 1
fi

# ── shims ─────────────────────────────────────────────────────────────────
export SHIM_LOG="$T/shim.log" SHIM_FAT="$FAT" REAL_MV
REAL_MV=$(command -v mv)
printf '#!/bin/bash\nexit 0\n' > "$SH/mountpoint"
printf '#!/bin/bash\necho "mount $*" >> "$SHIM_LOG"\nexit 0\n' > "$SH/mount"
printf '#!/bin/bash\necho "umount $*" >> "$SHIM_LOG"\nexit 0\n' > "$SH/umount"
printf '#!/bin/bash\nif [ "${1:-}" = -u ]; then echo "${SHIM_UID:-0}"; exit 0; fi\nexec /usr/bin/id "$@"\n' > "$SH/id"
cat > "$SH/mv" <<'EOF'
#!/bin/bash
last=${*: -1}
if [ -n "${SHIM_MV_FAIL_FAT:-}" ]; then
    case "$last" in "$SHIM_FAT"/*) echo "mv shim: refusing rename onto FAT" >&2; exit 1 ;; esac
fi
exec "$REAL_MV" "$@"
EOF
chmod +x "$SH"/*

# Build a boot.scr from a payload with the repo writer (the "old" copies).
mkscr() { "$PY" "$LIB/uboot-script.py" build "$1" "$2" --name SA-02m >/dev/null 2>&1; }
sed '1s/ watchdog\.stop_on_reboot=0//' "$PAYLOAD_SRC" > "$T/old.cmd"
mkscr "$T/old.cmd" "$T/old.scr" || { echo "FAIL  cannot build the old boot.scr fixture"; exit 1; }
mkscr "$PAYLOAD_SRC" "$T/new.scr" || { echo "FAIL  cannot build boot.scr from $PAYLOAD_SRC"; exit 1; }

# ── R1: restore-dtb replaces a mismatched FAT boot.scr by rename ──────────
cp "$T/new.scr" "$SHARE/boot.scr"; cp "$T/old.scr" "$FAT/boot.scr"
ino0=$(stat -c %i "$FAT/boot.scr"); ln "$FAT/boot.scr" "$T/pin-old" 2>/dev/null
PATH="$SH:$PATH" bash "$R" > /dev/null 2>&1; rc=$?
ino1=$(stat -c %i "$FAT/boot.scr")
if [ "$rc" = 0 ] && cmp -s "$FAT/boot.scr" "$SHARE/boot.scr" && [ "$ino0" != "$ino1" ] \
   && [ -z "$(find "$FAT" -name '.*.new*')" ]; then
    ok "R1: restore-dtb put the canonical boot.scr on FAT by rename (inode $ino0 -> $ino1), no temp left"
else
    bad "R1: restore-dtb rc=$rc equal=$(cmp -s "$FAT/boot.scr" "$SHARE/boot.scr" && echo yes || echo NO) inode $ino0 -> $ino1 (same inode = written in place)"
fi
rm -f "$T/pin-old"

# ── the tool ──────────────────────────────────────────────────────────────
if [ ! -s "$TOOL_SRC" ]; then
    bad "A1-C1: $TOOL_SRC missing — there is no way to put the parameter on an installed board"
else
    TL="$T/bootscr-apply.sh"
    sed -e "s#^FAT_DEV=/dev/mmcblk2p1\$#FAT_DEV=$T/fatdev#" \
        -e "s#^FAT_MNT=/mnt/fat\$#FAT_MNT=$FAT#" \
        -e "s#^CANON_BOOTSCR=/usr/local/share/sa02m/boot.scr\$#CANON_BOOTSCR=$SHARE/boot.scr#" \
        -e "s#^PAYLOAD=/usr/local/share/sa02m/boot.cmd.sa02m.min\$#PAYLOAD=$SHARE/boot.cmd.sa02m.min#" \
        -e "s#^UBOOT_PY=/usr/local/lib/sa02m/uboot-script.py\$#UBOOT_PY=$LIB/uboot-script.py#" \
        -e "s#^BACKUP_DIR=/root\$#BACKUP_DIR=$ROOT#" \
        -e "s#^PROC_CMDLINE=/proc/cmdline\$#PROC_CMDLINE=$T/cmdline#" \
        -e "s#^WDT_PARAM=/sys/module/watchdog/parameters/stop_on_reboot\$#WDT_PARAM=$T/sys/stop_on_reboot#" \
        -e "s#^LOCK_DIR=/run/lock/sa02m-bootscr-apply.d\$#LOCK_DIR=$T/lock.d#" \
        -e "s#^LOG=/var/log/sa02m_install.log\$#LOG=$T/install.log#" "$TOOL_SRC" > "$TL"
    n_t=$(grep -cF "$T/" "$TL")
    [ "$n_t" -ge 10 ] || { echo "FAIL  tool retarget hit $n_t constants, expected 10"; exit 1; }
    grep -Eq '^(FAT_DEV|FAT_MNT|CANON_BOOTSCR|PAYLOAD|UBOOT_PY|BACKUP_DIR|PROC_CMDLINE|WDT_PARAM|LOCK_DIR|LOG)=/(dev|mnt|usr|root|proc|sys|run|var)' "$TL" \
        && { echo "FAIL  tool retarget left a real path"; exit 1; }
    tl() { out=$(PATH="$SH:$PATH" bash "$TL" "$@" 2>&1); rc=$?; }
    nback() { find "$ROOT" -maxdepth 1 -name 'bootscr-backup-*' -type d | wc -l; }

    # A1
    cp "$T/old.scr" "$SHARE/boot.scr"; cp "$T/old.scr" "$FAT/boot.scr"
    tl apply
    # The writer stamps the build time into the 64-byte header, so a copy built
    # by the tool equals new.scr from byte 64 on; the two board copies must be
    # byte-identical to each other (restore-dtb compares them by md5).
    m_fat=$(md5sum < "$FAT/boot.scr"); m_can=$(md5sum < "$SHARE/boot.scr")
    m_new=$m_can; cmp -s -i 64 "$SHARE/boot.scr" "$T/new.scr" || m_new=differs
    bk=$(find "$ROOT" -maxdepth 1 -name 'bootscr-backup-*' -type d | head -n 1)
    if [ "$rc" = 0 ] && [ "$m_fat" = "$m_new" ] && [ "$m_can" = "$m_new" ] \
       && "$PY" "$LIB/uboot-script.py" validate "$FAT/boot.scr" >/dev/null 2>&1 \
       && grep -aq 'watchdog.stop_on_reboot=0' "$FAT/boot.scr" \
       && [ -n "$bk" ] && cmp -s "$bk/boot.scr.fat" "$T/old.scr" && cmp -s "$bk/boot.scr.canonical" "$T/old.scr" \
       && [ -z "$(find "$FAT" "$SHARE" -name '.*.new*')" ]; then
        ok "A1: apply rebuilt FAT and canonical boot.scr from the payload (validated, carries the parameter, md5 equal), both old copies backed up, no temp"
    else
        bad "A1: rc=$rc fat==new:$([ "$m_fat" = "$m_new" ] && echo y || echo n) canon==new:$([ "$m_can" = "$m_new" ] && echo y || echo n) backup='${bk:-none}' out: $(printf '%s' "$out" | tr '\n' '|' | cut -c1-300)"
    fi

    # A2 — one second later on purpose: a fresh build then carries another
    # header timestamp, and the tool must still see «already applied».
    sleep 1.1
    nb0=$(nback); tl apply
    if [ "$rc" = 0 ] && [ "$(nback)" = "$nb0" ] && printf '%s' "$out" | grep -q 'уже применено'; then
        ok "A2: second apply -> «уже применено», no write, no new backup"
    else
        bad "A2: second apply rc=$rc backups $nb0 -> $(nback): $(printf '%s' "$out" | tr '\n' '|' | cut -c1-200)"
    fi

    # A3
    rm -rf "$ROOT"/bootscr-backup-*
    cp "$T/old.scr" "$SHARE/boot.scr"; cp "$T/old.scr" "$FAT/boot.scr"
    SHIM_MV_FAIL_FAT=1 tl apply; rc_a3=$rc
    fat_old=0; cmp -s "$FAT/boot.scr" "$T/old.scr" && fat_old=1
    can_new=0; cmp -s -i 64 "$SHARE/boot.scr" "$T/new.scr" && can_new=1
    ino0=$(stat -c %i "$FAT/boot.scr"); ln "$FAT/boot.scr" "$T/pin-old" 2>/dev/null
    PATH="$SH:$PATH" bash "$R" > /dev/null 2>&1
    ino1=$(stat -c %i "$FAT/boot.scr")
    if [ "$rc_a3" = 5 ] && [ "$fat_old" = 1 ] && [ "$can_new" = 1 ] \
       && cmp -s "$FAT/boot.scr" "$SHARE/boot.scr" && [ "$ino0" != "$ino1" ]; then
        ok "A3: rename onto FAT fails -> exit 5, FAT still old, canonical new; restore-dtb then completes FAT by rename"
    else
        bad "A3: rc=$rc_a3 FAT-old=$fat_old canonical-new=$can_new after-restore FAT==canonical:$(cmp -s "$FAT/boot.scr" "$SHARE/boot.scr" && echo y || echo n) inode $ino0 -> $ino1"
    fi
    rm -f "$T/pin-old"

    # A4
    cp "$T/old.scr" "$SHARE/boot.scr"; cp "$T/old.scr" "$FAT/boot.scr"; rm -rf "$ROOT"/bootscr-backup-*
    SHIM_UID=1000 tl apply; rc_nr=$rc
    cp "$T/old.cmd" "$SHARE/boot.cmd.sa02m.min"; tl apply; rc_np=$rc
    cp "$PAYLOAD_SRC" "$SHARE/boot.cmd.sa02m.min"
    if [ "$rc_nr" = 3 ] && [ "$rc_np" = 3 ] && cmp -s "$FAT/boot.scr" "$T/old.scr" && cmp -s "$SHARE/boot.scr" "$T/old.scr" \
       && [ "$(nback)" = 0 ]; then
        ok "A4: not root -> exit 3; payload without watchdog.stop_on_reboot=0 -> exit 3; nothing written, no backup"
    else
        bad "A4: not-root rc=$rc_nr, no-param payload rc=$rc_np, FAT/canonical untouched: $(cmp -s "$FAT/boot.scr" "$T/old.scr" && cmp -s "$SHARE/boot.scr" "$T/old.scr" && echo y || echo n)"
    fi

    # C1
    cp "$T/new.scr" "$SHARE/boot.scr"; cp "$T/new.scr" "$FAT/boot.scr"
    echo 'root=/dev/mmcblk2p2 rootwait threadirqs watchdog.stop_on_reboot=0' > "$T/cmdline"; echo 0 > "$T/sys/stop_on_reboot"
    SHIM_UID=1000 tl check; rc_on=$rc; out_on=$out
    echo 'root=/dev/mmcblk2p2 rootwait threadirqs' > "$T/cmdline"; echo 1 > "$T/sys/stop_on_reboot"
    SHIM_UID=1000 tl check; rc_off=$rc
    m=$(md5sum < "$T/new.scr" | cut -c1-32)
    if [ "$rc_on" = 0 ] && [ "$rc_off" = 1 ] && [ "$(printf '%s' "$out_on" | grep -c "$m")" -ge 2 ]; then
        ok "C1: check -> 0 with the parameter active (cmdline + stop_on_reboot=0), 1 without; md5 of FAT and canonical printed"
    else
        bad "C1: check rc on=$rc_on off=$rc_off; out: $(printf '%s' "$out_on" | tr '\n' '|' | cut -c1-300)"
    fi
fi

echo
if [ "$fails" = 0 ]; then
    echo "test-bootscr-apply: all cases passed"
    exit 0
fi
echo "test-bootscr-apply: $fails case(s) FAILED"
exit 1
