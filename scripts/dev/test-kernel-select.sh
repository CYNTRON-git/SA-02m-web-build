#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-kernel-select.sh — behavioural regression for the RT/SMP kernel switch
# etc/sa02m-kernel-select.sh (installed as /usr/local/sbin/sa02m-kernel-select.sh,
# driven by kernel_ctrl.cgi).
#
# Why this exists (audit 2026-11-11 A4-02, A4-03, A3-03 neighbour):
#   * A4-02 — running RT, «SMP» writes zImage.smp to the FAT boot slot; the
#     Operator changes their mind and picks «RT» again: the old `run == target`
#     branch answered «already active» and left the SMP image on FAT, so the
#     next reboot came up on the kernel the Operator had just cancelled;
#   * A4-03 — the canonical image was accepted on SIZE alone; a damaged image of
#     the right size went onto the boot slot. tools/buildroot/sa02m-kernel-deploy.sh
#     writes manifest.json with each image's md5, and nothing read it;
#   * write_conf truncated /etc/sa02m_kernel.conf in place.
#
# Method (idiom of test-dtb-patch-tool.sh): the SHIPPED script is copied into a
# scratch tree and its single-assignment path constants (CONF, LOG, LOCK,
# FAT_DEV, FAT_MNT, CANON_DIR) plus the literal /lib/modules/ are sed-retargeted
# into the sandbox — no env seam in the shipped script; the copy is proven to
# differ from the original ONLY in those paths. FAT is a directory; `mountpoint`
# says «mounted», `mount`/`umount` record and do nothing, `uname -r` answers the
# kernel the case is «running», `pgrep` finds no CODESYS. Canonical images are
# 6 MB of random bytes; the manifest is written in sa02m-kernel-deploy.sh's
# shape ({"images":{"<prof>":{"md5":…}}}).
#
# Cases:
#   K1  running RT: `set smp` -> FAT = smp; `set rt` -> {"noop":true,
#       "restored":1}, FAT byte-identical to zImage.rt.   RED: FAT stays smp
#   K1b running RT with FAT already rt: `set rt` -> noop, restored:0, FAT untouched.
#   K2  canonical zImage.smp damaged (same size, md5 differs from the manifest):
#       `set smp` -> {"ok":false,"error":"zimage_mismatch"}, FAT untouched.
#                                                         RED: damaged image written
#   K3  no manifest: `set smp` proceeds (size check only) and logs
#       `no manifest md5 for smp`.                         RED: no log line
#   K4  write_conf replaces the conf by rename (inode changes), no *.tmp.* left.
#                                                         RED: inode kept
#
# Needs Linux (flock, GNU stat/cmp, python3 for the manifest read) — anything
# else is SKIP via lib_skip.sh, never PASS. Run: bash scripts/dev/test-kernel-select.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${KERNEL_SELECT_SRC:-etc/sa02m-kernel-select.sh}
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
. .ai-dev/quality/checks/lib_skip.sh || { echo "FAIL  lib_skip.sh not loadable"; exit 1; }

[ -s "$SRC" ] || { echo "FAIL  missing or empty: $SRC"; exit 1; }
if [ "$(uname -s)" != Linux ] || ! command -v flock >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    quality_skip "K1-K4 kernel-select sandbox: needs Linux + flock + python3 ($(uname -s)) — CI is the authoritative run"
    echo; echo "test-kernel-select: skipped (not Linux)"; exit 77
fi

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
FAT="$T/fat"; CANON="$T/canon"; MODS="$T/modules"; SH="$T/shim"
mkdir -p "$FAT" "$CANON" "$MODS" "$SH" "$T/etc"

S="$T/kernel-select.sh"
sed -e "s#^CONF=/etc/sa02m_kernel\.conf\$#CONF=$T/etc/sa02m_kernel.conf#" \
    -e "s#^LOG=/var/log/sa02m_install\.log\$#LOG=$T/install.log#" \
    -e "s#^LOCK=/run/sa02m-kernel-switch\.lock\$#LOCK=$T/kernel.lock#" \
    -e "s#^FAT_DEV=/dev/mmcblk2p1\$#FAT_DEV=$T/fatdev#" \
    -e "s#^FAT_MNT=/mnt/fat\$#FAT_MNT=$FAT#" \
    -e "s#^CANON_DIR=/usr/local/share/sa02m/kernel\$#CANON_DIR=$CANON#" \
    -e "s#/lib/modules/#$MODS/#g" \
    "$SRC" > "$S"
for pat in "CONF=$T/" "LOG=$T/" "LOCK=$T/" "FAT_DEV=$T/" "FAT_MNT=$FAT" "CANON_DIR=$CANON" "$MODS/"; do
    grep -qF "$pat" "$S" || { echo "FAIL  retarget missed: $pat"; exit 1; }
done
grep -Eq '^(CONF|LOG|LOCK|FAT_DEV|FAT_MNT|CANON_DIR)=/(etc|var|run|dev|mnt|usr)/|/lib/modules/' "$S" \
    && { echo "FAIL  retarget left a real root in the copy"; exit 1; }
if ! sed -e "s#^CONF=$T/etc/sa02m_kernel\.conf\$#CONF=/etc/sa02m_kernel.conf#" \
         -e "s#^LOG=$T/install\.log\$#LOG=/var/log/sa02m_install.log#" \
         -e "s#^LOCK=$T/kernel\.lock\$#LOCK=/run/sa02m-kernel-switch.lock#" \
         -e "s#^FAT_DEV=$T/fatdev\$#FAT_DEV=/dev/mmcblk2p1#" \
         -e "s#^FAT_MNT=$FAT\$#FAT_MNT=/mnt/fat#" \
         -e "s#^CANON_DIR=$CANON\$#CANON_DIR=/usr/local/share/sa02m/kernel#" \
         -e "s#$MODS/#/lib/modules/#g" "$S" | cmp -s - "$SRC"; then
    echo "FAIL  the copy differs from $SRC beyond the retargeted paths"; exit 1
fi

export SHIM_LOG="$T/shim.log" SHIM_KREL="6.1.0-rc6-rt4"
for c in mount umount; do printf '#!/bin/bash\nprintf "%s %%s\\n" "$*" >> "$SHIM_LOG"\nexit 0\n' "$c" > "$SH/$c"; done
printf '#!/bin/bash\nexit 0\n' > "$SH/mountpoint"
printf '#!/bin/bash\nexit 1\n' > "$SH/pgrep"
printf '#!/bin/bash\ncase "${1:-}" in -r) echo "$SHIM_KREL" ;; *) echo Linux ;; esac\n' > "$SH/uname"
chmod +x "$SH"/*
mkdir -p "$MODS/6.1.0-rc6" "$MODS/6.1.0-rc6-rt4"
: > "$MODS/6.1.0-rc6/modules.dep"; : > "$MODS/6.1.0-rc6-rt4/modules.dep"
head -c 6000000 /dev/urandom > "$T/img.smp"
head -c 6100000 /dev/urandom > "$T/img.rt"

manifest() {   # writes manifest.json for the CURRENT canonical files
    python3 - "$CANON" <<'PY'
import hashlib, json, os, sys
base = sys.argv[1]
out = {"images": {}}
for prof in ("smp", "rt"):
    p = os.path.join(base, "zImage." + prof)
    if os.path.isfile(p):
        out["images"][prof] = {"path": p, "size": os.path.getsize(p),
                               "md5": hashlib.md5(open(p, "rb").read()).hexdigest()}
open(os.path.join(base, "manifest.json"), "w").write(json.dumps(out, indent=2))
PY
}
reset() {      # $1 = what FAT/zImage holds (smp|rt); canonical both, manifest on
    rm -rf "$FAT"/* "$FAT"/.[!.]* "$CANON"/* "$T/etc"/* 2>/dev/null
    cp "$T/img.smp" "$CANON/zImage.smp"; cp "$T/img.rt" "$CANON/zImage.rt"
    cp "$T/img.$1" "$FAT/zImage"
    manifest
    : > "$T/install.log"; : > "$SHIM_LOG"
}
ks() { out=$(PATH="$SH:$PATH" bash "$S" "$@" 2>"$T/err"); rc=$?; }

# ── K1: cancel SMP by choosing RT again -> the RT image goes back on FAT ──
reset rt
ks set smp; rc1=$rc; out1=$out
fat_smp=0; cmp -s "$FAT/zImage" "$T/img.smp" && fat_smp=1
ks set rt
if [ "$rc1" = 0 ] && [ "$fat_smp" = 1 ] && [ "$rc" = 0 ] \
   && printf '%s' "$out" | grep -q '"noop":true' && printf '%s' "$out" | grep -q '"restored":1' \
   && cmp -s "$FAT/zImage" "$T/img.rt"; then
    ok "K1: running RT, set smp (FAT=smp) then set rt -> noop + restored:1, FAT byte-identical to zImage.rt"
else
    bad "K1: set smp rc=$rc1 FAT-smp=$fat_smp ($out1); set rt rc=$rc -> $out; FAT==rt: $(cmp -s "$FAT/zImage" "$T/img.rt" && echo yes || echo NO)"
fi

# ── K1b: FAT already rt -> noop, restored:0, nothing written ──────────────
reset rt
touch -d '2020-01-01' "$FAT/zImage"; m0=$(stat -c %Y "$FAT/zImage")
ks set rt
if [ "$rc" = 0 ] && printf '%s' "$out" | grep -q '"noop":true' && printf '%s' "$out" | grep -q '"restored":0' \
   && [ "$(stat -c %Y "$FAT/zImage")" = "$m0" ]; then
    ok "K1b: running RT, FAT already rt -> noop, restored:0, FAT untouched"
else
    bad "K1b: rc=$rc -> $out (mtime $m0 -> $(stat -c %Y "$FAT/zImage"))"
fi

# ── K2: damaged canonical image of the right size -> refused ──────────────
reset rt
printf 'XXXXXXXX' | dd of="$CANON/zImage.smp" bs=1 seek=4096 conv=notrunc status=none
[ "$(stat -c %s "$CANON/zImage.smp")" = "$(stat -c %s "$T/img.smp")" ] || bad "K2 fixture: size changed"
ks set smp
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -q '"error":"zimage_mismatch"' && cmp -s "$FAT/zImage" "$T/img.rt" \
   && [ -z "$(find "$FAT" -name '.*.new')" ]; then
    ok "K2: canonical zImage.smp md5 != manifest -> zimage_mismatch, FAT still rt, no temp left"
else
    bad "K2: damaged image — rc=$rc -> $out; FAT still rt: $(cmp -s "$FAT/zImage" "$T/img.rt" && echo yes || echo NO)"
fi

# ── K3: no manifest -> size check only, logged ────────────────────────────
reset rt; rm -f "$CANON/manifest.json"
ks set smp
if [ "$rc" = 0 ] && cmp -s "$FAT/zImage" "$T/img.smp" && grep -q 'no manifest md5 for smp' "$T/install.log"; then
    ok "K3: no manifest -> set smp writes FAT (size check only) and logs 'no manifest md5 for smp'"
else
    bad "K3: rc=$rc -> $out; log: $(tr '\n' '|' < "$T/install.log")"
fi

# ── K4: write_conf is a rename, not a truncate ────────────────────────────
reset rt
ks set rt; ino0=$(stat -c %i "$T/etc/sa02m_kernel.conf" 2>/dev/null || echo none)
ln "$T/etc/sa02m_kernel.conf" "$T/conf-old-link" 2>/dev/null
ks set smp; ino1=$(stat -c %i "$T/etc/sa02m_kernel.conf" 2>/dev/null || echo none)
n_tmp=$(find "$T/etc" -name '*.tmp.*' | wc -l)
if [ "$ino0" != none ] && [ "$ino0" != "$ino1" ] && [ "$n_tmp" -eq 0 ] \
   && grep -qx 'SA02M_KERNEL_DESIRED=smp' "$T/etc/sa02m_kernel.conf"; then
    ok "K4: /etc/sa02m_kernel.conf replaced by rename (inode $ino0 -> $ino1), no *.tmp.* left"
else
    bad "K4: conf inode $ino0 -> $ino1, tmp files $n_tmp, desired line: $(grep DESIRED "$T/etc/sa02m_kernel.conf" 2>/dev/null)"
fi

echo
if [ "$fails" = 0 ]; then
    echo "test-kernel-select: all cases passed"
    exit 0
fi
echo "test-kernel-select: $fails case(s) FAILED"
exit 1
