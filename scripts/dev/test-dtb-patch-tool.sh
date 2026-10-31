#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-dtb-patch-tool.sh — behavioural regression for the on-board DTB tool
# usr/local/sbin/sa02m-dtb-patch.sh (quality row dtb-patch-tool).
#
# Why this exists: the tool rewrites the ONE file U-Boot boots the board from
# (boot.scr loads exactly sun8i-a40i-sk.dtb, no fallback). Its guarantees —
# backup before the write, fdtget verification BEFORE the live file is touched,
# tmp + sync + rename-over so the live name is never 0-byte, both canonical
# copies refreshed where they exist, idempotent second run, every failure
# leaving FAT untouched — are a BEHAVIOUR no syntax gate can see. They are
# pinned here, off the board.
#
# Method, in the idiom of test-fix-eth-link-cycle.sh: the SHIPPED tool is
# copied into a scratch tree and its single-assignment constants (FAT_DEV,
# FAT_MNT, CANON_DIRS, BACKUP_DIR, PROC_PHY, SYS_PHY, LOCK_DIR) are
# sed-retargeted into the sandbox; there is deliberately NO env seam in the
# shipped script. The sandbox FAT holds a STATE FILE under each .dtb name — one
# tab-separated line per node (`node <path>`) and per property (`prop <path>
# <name> <s|u|e> <value>`: strings / decimal cells space-separated, e = empty),
# padded with one line of spaces to a blob-like size — and `fdtget` / `fdtput` are
# shims over it: fdtget -l / -p / -t s|u / plain, fdtput -t s|u / empty / -d,
# a property replaced in place and a NEW one appended at the end of its node
# (what the real fdtput does), a missing node or property an error. The tool's
# real dtb_edit runs against them. FDTGET_LIE=1 lies about the EMAC
# `compatible` only; FDTPUT_FAIL=1 fails every write; FDTPUT_STUB (canon | dup
# | noc22) makes fdtput MISWRITE one list (T14/T15). `mount`, `umount`,
# `mountpoint`, `sync` and `id` are recording shims. A host fdtget/fdtput is
# hidden through scripts/dev/lib_path_mask.sh so the «not installed» case is
# the harness's call on every host. No root, no device, no python — bash +
# coreutils + awk; runs in Windows git-bash and on Linux CI.
#
# What it does NOT prove: that the real fdtget/fdtput behave like the shims.
# scripts/dev/test-dtb-fdtput.sh (row dtb-fdtput-real) runs dtb_edit on the
# real tools and the repo blob against the goldens — CI and the bench only.
#
# Seeds (seed_blob): `reverted` — all eight edits pending (the golden recipe
# (c) of test-dtb-fdtput.sh: dc1sw without regulator-always-on, i2c / GMAC /
# GMAC mdio disabled, syscon 1, GMAC PHY c22 only, both deasserts 100000, EMAC
# c22 only); `asis` — the bench original (only the EMAC list pending);
# `damaged` — the bench apply-#3 lists (GMAC icplus,ip101a x4 + c22, EMAC id
# only). Every seed carries a decoy `/decoy/ethernet-phy@0` (c22 only) that
# must never change: the edits go by DT path.
#
# Cases:
#   T1  apply on `reverted`, one name, FAT already mounted: the eight change
#       lines in order (the old text patcher's wording), every value in place
#       through fdtget (the EMPTY regulator-always-on included, the GMAC mdio
#       child okay), the decoy untouched, backup written, no `.new` leftover,
#       canonical copy refreshed where pre-seeded and NOT created where absent,
#       «перезагрузка» printed, mount/umount not called, exit 0.
#   T2  second apply: nothing written (bytes + no new backup), «уже исправлен»,
#       exit 0 — and no fdtput call at all (nothing to change => no write).
#   T3  three names with differing content: each patched independently, three
#       backups, the blobs still differ from each other afterwards.
#   T4  FDTGET_LIE=1 (verify sees a wrong first token): FAT bytes unchanged,
#       no backup, exit 5.                       <- the comment-mutation case
#   T5  FDTPUT_FAIL=1 (rc 2), an unpadded too-small blob (the size floor), a
#       blob without the GMAC node and one without the EMAC PHY node (rc 1,
#       «node not found»): FAT unchanged, no backup, exit 5 each.
#   T6  no DTB name present: exit 4, nothing written, no backup.
#   T7  not root (id shim -> 1000): exit 3 before any mount or read; a stale
#       lock dir: exit 3.
#   T8  FAT not mounted: mount shim called once, umount once, exit 0, patched.
#   T9  --dry-run: «would-patch» printed, FAT unchanged, no backup, no
#       «перезагрузка», exit 0.
#   T10 check on proc/sysfs fixtures: Generic PHY -> exit 1 naming fix-eth;
#       ICPlus IP101G -> exit 0; node missing -> exit 2; `id` never consulted.
#   T11 fdtput absent (shim removed, host masked): exit 3 with the apt hint,
#       no mount.
#   T12 harness non-vacuity: every shim invoked at least once across the run.
#   T13 (before T11) the bench shapes: `asis` -> apply #1 `patched` with the
#       ONE line «mdio@1c0b080/ethernet-phy@0: compatible
#       ethernet-phy-id0243.0c54 first» (GMAC untouched), apply #2 and #3
#       «уже исправлен», one backup; `damaged` -> the two lines (GMAC
#       «compatible icplus,ip101a first», EMAC «… c22 appended»), both lists
#       canonical, a second apply «уже исправлен».
#   T14 (before T11) verify refuses a duplicated string: FDTPUT_STUB=dup makes
#       fdtput write a second `icplus,ip101a` into the GMAC list -> «failed:
#       verify: GMAC compatible: icplus,ip101a повторён», FAT unchanged, no
#       backup, exit 5. Control first: FDTPUT_STUB=canon (the stub active,
#       writing what it was asked) is `patched`, so the refusal is the list
#       check, not a broken stub. dtb_edit writes canonical lists, so only a
#       misbehaving writer can drive this guard.
#   T15 (before T11) FDTPUT_STUB=noc22 writes the EMAC list as the id only ->
#       «failed: verify: EMAC compatible: нет …c22», FAT unchanged, no backup,
#       exit 5.
#   T16 check on NUL-separated proc lists: eth0 id-only + eth1 icplus x4 under
#       ICPlus -> a ВНИМАНИЕ line for each, exit 0; a duplicated eth0 string
#       under Generic PHY -> the warning, exit 1 (unchanged); canonical lists
#       on both nodes -> no warning.
#   T17 (before T11) a compatible string carrying a backslash followed by `0`
#       (`acme,phy\0x` as two literal characters, then c22) in the GMAC list:
#       apply keeps it as ONE string — `icplus,ip101a acme,phy\0x c22` —,
#       `patched`, exit 0. The text patcher split it on the dts `\0` escape
#       and emitted an invalid literal (observed on 1.0.7.23's
#       sa02m-dtb-patch.py, 2026-10-31: `"icplus,ip101a", "acme,phy\", "x",
#       …`, which dtc refuses — exit 5). The shims pass bytes through, so this
#       pins dtb_edit's own handling (read -r, quoted arrays); the real-tool
#       half is case F2 of test-dtb-fdtput.sh.
# Retired with the text path (1.0.7.26): the dtc/python3 shims, T5's dtc
# compile/decompile modes and T13's dtc 1.7 `\0`-form re-join check.
#
# Non-vacuous: a failed or over-wide retarget, a leftover real root on an
# executable line, a missing tool, a seed already carrying the id string, or a
# shim never invoked FAILS rather than passing on zero matches.
#
# Drive-to-failure recipe (commit body + comment-mutation-proof): comment the
# `[ "$first" = "$EMAC_ID_COMPAT" ]` verify line out of a copy -> T4 writes the
# blob fdtget lied about (FAT changed, exit 0) -> RED; comment the
# `mv -f "$tmp" "$2"` line of write_atomic out -> T1 RED (live file not
# replaced, `.new` left behind); comment list_defect's duplicate line out ->
# T14 and T16 RED, its c22 line -> T15 and T16 RED; comment canonical_list's
# `[ "$seen" = 1 ] || CANON+=("$s")` line -> T17 RED (the backslash-0 string
# dropped; the other seeds carry no extra string besides c22, which the
# fallback rule re-appends, so T1/T13 stay green); comment the EMAC `patch_phy` call -> T1/T13 RED. DTB_TOOL_SRC=<path>
# re-points the harness at such a copy; the real tree is never edited.
#
# Run: bash scripts/dev/test-dtb-patch-tool.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${DTB_TOOL_SRC:-usr/local/sbin/sa02m-dtb-patch.sh}
ID_STR=ethernet-phy-id0243.0c54
C22=ethernet-phy-ieee802.3-c22
GMAC_C=icplus,ip101a
P_DC=/soc/i2c@1c2ac00/pmic@34/regulators/dc1sw
P_I2C=/soc/i2c@1c2b800
P_G=/soc/ethernet@1c50000
P_GMDIO=/soc/ethernet@1c50000/mdio
P_GPHY=/soc/ethernet@1c50000/mdio/ethernet-phy@0
P_EMDIO=/soc/mdio@1c0b080
P_EPHY=/soc/mdio@1c0b080/ethernet-phy@0
P_DECOY=/decoy/ethernet-phy@0

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -s "$SRC" ] || { echo "FAIL  missing or empty: $SRC"; exit 1; }
# shellcheck source=scripts/dev/lib_path_mask.sh
. scripts/dev/lib_path_mask.sh || { echo "FAIL  scripts/dev/lib_path_mask.sh not loadable"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

FAT="$T/fat"                    # FAT_MNT
FATDEV="$T/fatdev"              # FAT_DEV (a token the mount shim records)
CANON1="$T/canon"               # /usr/local/share/sa02m
CANON2="$T/canon/kernel"        # /usr/local/share/sa02m/kernel
BACKUP="$T/backup"              # /root
# PROC_PHY keeps the real DT path as its tail: `check` finds the GMAC PHY node
# by swapping that tail for GMAC_PHY_PATH (GPROC below), as on the board.
PROC="$T/dt/soc/mdio@1c0b080/ethernet-phy@0"            # PROC_PHY
GPROC="$T/dt/soc/ethernet@1c50000/mdio/ethernet-phy@0"  # the GMAC PHY node check derives
SYSP="$T/sysphy/1c0b080.mdio-mii:00"   # SYS_PHY
LOCK="$T/lockdir"
BIN="$T/bin"
OUT="$T/run.out"
CALLS="$T/calls.log"            # reset per run
CALLS_ALL="$T/calls-all.log"    # never reset — the non-vacuity witness
MOUNTED="$T/fat.mounted"        # exists = the mountpoint shim says «mounted»
mkdir -p "$BIN" "$FAT" "$CANON1" "$CANON2" "$BACKUP"

# ── retarget the SHIPPED tool into the sandbox ─────────────────────────────
TOOL="$T/sa02m-dtb-patch.sh"
sed -e "s#^FAT_DEV=.*#FAT_DEV=$FATDEV#" \
    -e "s#^FAT_MNT=.*#FAT_MNT=$FAT#" \
    -e "s#^CANON_DIRS=.*#CANON_DIRS=\"$CANON1 $CANON2\"#" \
    -e "s#^BACKUP_DIR=.*#BACKUP_DIR=$BACKUP#" \
    -e "s#^PROC_PHY=.*#PROC_PHY=$PROC#" \
    -e "s#^SYS_PHY=.*#SYS_PHY=$SYSP#" \
    -e "s#^LOCK_DIR=.*#LOCK_DIR=$LOCK#" \
    "$SRC" > "$TOOL"
chmod +x "$TOOL"
for pat in "FAT_DEV=$FATDEV" "FAT_MNT=$FAT" "CANON_DIRS=\"$CANON1 $CANON2\"" "BACKUP_DIR=$BACKUP" \
           "PROC_PHY=$PROC" "SYS_PHY=$SYSP" "LOCK_DIR=$LOCK"; do
    grep -Fq "$pat" "$TOOL" || { echo "FAIL  retarget missed in the tool: $pat"; exit 1; }
done
# An un-retargeted copy would mount the real boot partition and write /root —
# the one failure this harness must never pass through. Comments are skipped
# (the header names the real paths as prose).
if grep -Eq '^[^#]*(/dev/mmcblk|/mnt/fat|/usr/local/share|/root([^a-zA-Z0-9_-]|$)|/proc/device-tree|/sys/bus|/usr/local/sbin|/run/lock)' "$TOOL"; then
    echo "FAIL  the tool copy still holds a real root on an executable line:"; grep -En '^[^#]*(/dev/mmcblk|/mnt/fat|/usr/local/share|/root([^a-zA-Z0-9_-]|$)|/proc/device-tree|/sys/bus|/usr/local/sbin|/run/lock)' "$TOOL" | head -3; exit 1
fi

# ── PATH shims ─────────────────────────────────────────────────────────────
# fdtget [-l | -p | -t s|u | -ts | -tu] FILE NODE [PROP] over the state file.
# Values come out of the file's own field (never through `awk -v`, which would
# read a backslash as an escape — T17). SHIM_NOLOG=1 keeps the harness's own
# reads out of the call logs.
# One awk per call and builtin logging: a fork costs ~50 ms on git-bash, and
# the cases make a few thousand of these calls.
cat > "$BIN/fdtget" <<'SHIM'
#!/bin/bash
if [ "${SHIM_NOLOG:-0}" != 1 ]; then printf 'fdtget %s\n' "$*" >> "$CALLS_ALL_PATH"; printf 'fdtget %s\n' "$*" >> "$CALLS_PATH"; fi
mode=val; type=""
while [ $# -gt 0 ]; do
  case "$1" in
    -l) mode=list ;;
    -p) mode=props ;;
    -t) type=$2; shift ;;
    -t?*) type=${1#-t} ;;
    -*) echo "fdtget shim: unsupported option $1" >&2; exit 2 ;;
    *) break ;;
  esac
  shift
done
f=$1; node=$2; prop=${3:-}
if [ "${FDTGET_LIE:-0}" = 1 ] && [ "$mode" = val ] && [ "$prop" = compatible ] && [ "$node" = /soc/mdio@1c0b080/ethernet-phy@0 ]; then
  echo "ethernet-phy-ieee802.3-c22"; exit 0
fi
N="$node" P="$prop" M="$mode" TY="$type" awk -F'\t' '
  BEGIN { n = ENVIRON["N"]; p = ENVIRON["P"]; m = ENVIRON["M"]; t = ENVIRON["TY"]; pre = (n == "/") ? "/" : n "/" }
  $1 == "node" && $2 == n { node = 1; next }
  m == "list" && $1 == "node" && index($2, pre) == 1 { r = substr($2, length(pre) + 1); if (r !~ /\//) out = out r "\n"; next }
  m == "props" && $1 == "prop" && $2 == n { out = out $3 "\n"; next }
  m == "val" && $1 == "prop" && $2 == n && $3 == p { kind = $4; val = $5; found = 1 }
  END {
    if (!node) { print "Error at " n ": FDT_ERR_NOTFOUND" > "/dev/stderr"; exit 1 }
    if (m != "val") { printf "%s", out; exit 0 }
    if (!found) { print "Error at " p ": FDT_ERR_NOTFOUND" > "/dev/stderr"; exit 1 }
    if ((t == "s" && kind == "u") || (t == "u" && kind != "u")) { print "fdtget shim: " p " is not of type " t > "/dev/stderr"; exit 1 }
    print val
  }' "$f"
SHIM
# fdtput [-t s|u] FILE NODE PROP [VALUE...] | fdtput -d FILE NODE PROP.
# No value = an EMPTY property; values without -t are refused (the tool must
# say which type it writes). Replace in place; a new property goes after the
# node's last line, as the real fdtput appends it at the end of the node.
cat > "$BIN/fdtput" <<'SHIM'
#!/bin/bash
if [ "${SHIM_NOLOG:-0}" != 1 ]; then printf 'fdtput %s\n' "$*" >> "$CALLS_ALL_PATH"; printf 'fdtput %s\n' "$*" >> "$CALLS_PATH"; fi
if [ "${FDTPUT_FAIL:-0}" = 1 ]; then echo "fdtput shim: write failure" >&2; exit 1; fi
type=""; del=0
while [ $# -gt 0 ]; do
  case "$1" in
    -t) type=$2; shift ;;
    -t?*) type=${1#-t} ;;
    -d) del=1 ;;
    -*) echo "fdtput shim: unsupported option $1" >&2; exit 2 ;;
    *) break ;;
  esac
  shift
done
f=$1; node=$2; prop=$3; shift 3
if [ "$del" = 1 ]; then
  kind=d; val=""
elif [ $# -eq 0 ]; then
  kind=e; val=""
else
  case "$type" in
    s) kind=s ;;
    u) kind=u
       for v in "$@"; do case "$v" in ''|*[!0-9]*) echo "fdtput shim: not a cell: $v" >&2; exit 1 ;; esac; done ;;
    *) echo "fdtput shim: values without -t s|u" >&2; exit 2 ;;
  esac
  val="$*"
fi
case "${FDTPUT_STUB:-}:$node:$prop" in
  dup:/soc/ethernet@1c50000/mdio/ethernet-phy@0:compatible) val="icplus,ip101a $val" ;;
  noc22:/soc/mdio@1c0b080/ethernet-phy@0:compatible) val="ethernet-phy-id0243.0c54" ;;
esac
# kind d = delete. A missing node writes nothing and fails, as the real one.
N="$node" P="$prop" K="$kind" V="$val" awk -F'\t' '
  { line[NR] = $0
    if ($1 == "node" && $2 == ENVIRON["N"]) node = 1
    if ($1 == "prop" && $2 == ENVIRON["N"] && $3 == ENVIRON["P"]) hit = NR
    if (($1 == "node" || $1 == "prop") && $2 == ENVIRON["N"]) last = NR }
  END {
    if (!node) { print "Error at " ENVIRON["N"] ": FDT_ERR_NOTFOUND" > "/dev/stderr"; exit 1 }
    del = (ENVIRON["K"] == "d")
    if (del && !hit) { print "Error at " ENVIRON["P"] ": FDT_ERR_NOTFOUND" > "/dev/stderr"; exit 1 }
    new = "prop\t" ENVIRON["N"] "\t" ENVIRON["P"] "\t" ENVIRON["K"] "\t" ENVIRON["V"]
    for (i = 1; i <= NR; i++) {
      if (i == hit) { if (!del) print new; continue }
      print line[i]
      if (!hit && i == last) print new
    }
  }' "$f" > "$f.shim" && mv -f "$f.shim" "$f" || { rm -f "$f.shim"; exit 1; }
SHIM
sed -i -e "s#\$CALLS_ALL_PATH#$CALLS_ALL#" -e "s#\$CALLS_PATH#$CALLS#" "$BIN/fdtget" "$BIN/fdtput"
cat > "$BIN/mount" <<SHIM
#!/bin/bash
echo "mount \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
: > "$MOUNTED"
exit 0
SHIM
cat > "$BIN/umount" <<SHIM
#!/bin/bash
echo "umount \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
rm -f "$MOUNTED"
exit 0
SHIM
cat > "$BIN/mountpoint" <<SHIM
#!/bin/bash
echo "mountpoint \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
[ -e "$MOUNTED" ]
SHIM
cat > "$BIN/sync" <<SHIM
#!/bin/bash
echo "sync \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
exit 0
SHIM
cat > "$BIN/id" <<SHIM
#!/bin/bash
echo "id \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
echo "\${ID_UID:-0}"
SHIM
chmod +x "$BIN"/*
MASKED=$(path_without "$T/mask" fdtget fdtput dtc) || { echo "FAIL  path_without could not mask a host fdtget/fdtput"; exit 1; }
PATH="$BIN:$MASKED"; export PATH
command -v fdtput | grep -Fq "$BIN/fdtput" || { echo "FAIL  the fdtput shim is not first on PATH"; exit 1; }
command -v fdtget | grep -Fq "$BIN/fdtget" || { echo "FAIL  the fdtget shim is not first on PATH"; exit 1; }

# ── seeds and helpers ──────────────────────────────────────────────────────
PAD_BYTES=20000   # one line of spaces: the size of a blob, without a per-line cost in every shim call
get()  { SHIM_NOLOG=1 "$BIN/fdtget" "$@" 2>/dev/null; }   # the harness's own read, never logged
put()  { SHIM_NOLOG=1 "$BIN/fdtput" "$@" || { echo "FAIL  seed edit: fdtput $*"; exit 1; }; }
TAB=$(printf '\t')
seed_blob() {   # $1 = output, $2 = asis|reverted|damaged|nogmac|noemac|backslash, [$3 = model tag], [$4 = nopad]
    local f=$1 v=$2
    {
        printf 'node%s/\n' "$TAB"
        printf 'prop%s/%smodel%ss%sSA-02m sandbox blob%s\n' "$TAB" "$TAB" "$TAB" "$TAB" "${3:+ [$3]}"
        printf 'node%s/decoy\n' "$TAB"
        printf 'node%s%s\n' "$TAB" "$P_DECOY"
        printf 'prop%s%s%scompatible%ss%s%s\n' "$TAB" "$P_DECOY" "$TAB" "$TAB" "$TAB" "$C22"
        printf 'prop%s%s%sreset-deassert-us%su%s1000\n' "$TAB" "$P_DECOY" "$TAB" "$TAB" "$TAB"
        printf 'node%s/soc\n' "$TAB"
        printf 'node%s/soc/i2c@1c2ac00\n' "$TAB"
        printf 'node%s/soc/i2c@1c2ac00/pmic@34\n' "$TAB"
        printf 'node%s/soc/i2c@1c2ac00/pmic@34/regulators\n' "$TAB"
        printf 'node%s%s\n' "$TAB" "$P_DC"
        printf 'prop%s%s%sregulator-always-on%se%s\n' "$TAB" "$P_DC" "$TAB" "$TAB" "$TAB"
        printf 'prop%s%s%sregulator-name%ss%svcc-gmac-phy\n' "$TAB" "$P_DC" "$TAB" "$TAB" "$TAB"
        printf 'prop%s%s%sregulator-min-microvolt%su%s3300000\n' "$TAB" "$P_DC" "$TAB" "$TAB" "$TAB"
        printf 'node%s%s\n' "$TAB" "$P_I2C"
        printf 'prop%s%s%sstatus%ss%sokay\n' "$TAB" "$P_I2C" "$TAB" "$TAB" "$TAB"
        if [ "$v" != nogmac ]; then
            printf 'node%s%s\n' "$TAB" "$P_G"
            printf 'prop%s%s%scompatible%ss%sallwinner,sun8i-r40-gmac\n' "$TAB" "$P_G" "$TAB" "$TAB" "$TAB"
            printf 'prop%s%s%ssyscon%su%s2\n' "$TAB" "$P_G" "$TAB" "$TAB" "$TAB"
            printf 'prop%s%s%sstatus%ss%sokay\n' "$TAB" "$P_G" "$TAB" "$TAB" "$TAB"
            printf 'node%s%s\n' "$TAB" "$P_GMDIO"
            printf 'prop%s%s%scompatible%ss%ssnps,dwmac-mdio\n' "$TAB" "$P_GMDIO" "$TAB" "$TAB" "$TAB"
            printf 'node%s%s\n' "$TAB" "$P_GPHY"
            printf 'prop%s%s%scompatible%ss%s%s %s\n' "$TAB" "$P_GPHY" "$TAB" "$TAB" "$TAB" "$GMAC_C" "$C22"
            printf 'prop%s%s%sreset-deassert-us%su%s500000\n' "$TAB" "$P_GPHY" "$TAB" "$TAB" "$TAB"
            printf 'prop%s%s%sstatus%ss%sokay\n' "$TAB" "$P_GPHY" "$TAB" "$TAB" "$TAB"
        fi
        printf 'node%s/soc/ethernet@1c0b000\n' "$TAB"
        printf 'node%s%s\n' "$TAB" "$P_EMDIO"
        printf 'prop%s%s%scompatible%ss%sallwinner,sun4i-a10-mdio\n' "$TAB" "$P_EMDIO" "$TAB" "$TAB" "$TAB"
        if [ "$v" != noemac ]; then
            printf 'node%s%s\n' "$TAB" "$P_EPHY"
            printf 'prop%s%s%scompatible%ss%s%s\n' "$TAB" "$P_EPHY" "$TAB" "$TAB" "$TAB" "$C22"
            printf 'prop%s%s%sreset-deassert-us%su%s500000\n' "$TAB" "$P_EPHY" "$TAB" "$TAB" "$TAB"
            printf 'prop%s%s%sstatus%ss%sokay\n' "$TAB" "$P_EPHY" "$TAB" "$TAB" "$TAB"
        fi
    } > "$f"
    case "$v" in
        reverted)   # golden recipe (c) of test-dtb-fdtput.sh, on the state file
            put -d "$f" "$P_DC" regulator-always-on
            put -t s "$f" "$P_I2C" status disabled
            put -t s "$f" "$P_G" status disabled
            put -t s "$f" "$P_GMDIO" status disabled
            put -t u "$f" "$P_G" syscon 1
            put -t s "$f" "$P_GPHY" compatible "$C22"
            put -t u "$f" "$P_GPHY" reset-deassert-us 100000
            put -t u "$f" "$P_EPHY" reset-deassert-us 100000 ;;
        damaged)    # golden recipe (b): the bench apply-#3 lists
            put -t s "$f" "$P_GPHY" compatible "$GMAC_C" "$GMAC_C" "$GMAC_C" "$GMAC_C" "$C22"
            put -t s "$f" "$P_EPHY" compatible "$ID_STR" ;;
        backslash)  # T17: a backslash and a 0 as two literal characters inside one string
            put -t s "$f" "$P_GPHY" compatible 'acme,phy\0x' "$C22" ;;
    esac
    grep -Fq "$ID_STR" "$f" && [ "$v" != damaged ] && { echo "FAIL  seed $v already carries $ID_STR — it must be a PRE-patch blob"; exit 1; }
    [ "${4:-}" = nopad ] || printf "%${PAD_BYTES}s\n" '' >> "$f"
}
reset_sandbox() {
    rm -rf "$FAT" "$CANON1" "$BACKUP" "$LOCK"
    mkdir -p "$FAT" "$CANON1" "$CANON2" "$BACKUP"
    : > "$MOUNTED"
}
run_tool() {   # one invocation, stdout+stderr in $OUT, status in RC
    : > "$CALLS"
    "$TOOL" "$@" > "$OUT" 2>&1
    RC=$?
}
n_calls()   { grep -c -E "$1" "$CALLS" 2>/dev/null || true; }
n_backups() { find "$BACKUP" -name 'dtb-backup-*' | wc -l | tr -d ' '; }
is_patched() {   # both PHY lists canonical, the deasserts pinned
    [ "$(get -ts "$1" "$P_EPHY" compatible)" = "$ID_STR $C22" ] \
    && [ "$(get -ts "$1" "$P_GPHY" compatible)" = "$GMAC_C $C22" ] \
    && [ "$(get -tu "$1" "$P_EPHY" reset-deassert-us)" = 500000 ] \
    && [ "$(get -tu "$1" "$P_GPHY" reset-deassert-us)" = 500000 ]
}
all_eight() {   # every edit of the header in place, the decoy untouched
    is_patched "$1" \
    && get -p "$1" "$P_DC" | grep -qx regulator-always-on \
    && [ -z "$(get -ts "$1" "$P_DC" regulator-always-on)" ] \
    && [ "$(get -ts "$1" "$P_I2C" status)" = okay ] \
    && [ "$(get -ts "$1" "$P_G" status)" = okay ] \
    && [ "$(get -ts "$1" "$P_GMDIO" status)" != disabled ] \
    && [ "$(get -ts "$1" "$P_GPHY" status)" = okay ] \
    && [ "$(get -tu "$1" "$P_G" syscon)" = 2 ] \
    && [ "$(get -ts "$1" "$P_DECOY" compatible)" = "$C22" ] \
    && [ "$(get -tu "$1" "$P_DECOY" reset-deassert-us)" = 1000 ]
}
# The eight change lines of the `reverted` seed, as `patched:` joins them —
# the old text patcher's lines verbatim (golden reverted.changes.txt).
EIGHT="dc1sw: regulator-always-on;i2c@1c2b800: status okay;ethernet@1c50000: status okay;ethernet@1c50000: syscon 0x02;ethernet@1c50000/mdio/ethernet-phy@0: compatible $GMAC_C first;ethernet@1c50000/mdio/ethernet-phy@0: reset-deassert-us 500000;mdio@1c0b080/ethernet-phy@0: compatible $ID_STR first;mdio@1c0b080/ethernet-phy@0: reset-deassert-us 500000;"

NAME=sun8i-a40i-sk.dtb

# The seeds' own non-vacuity: `reverted` really has every edit pending.
seed_blob "$T/probe.blob" reverted
if [ "$(get -ts "$T/probe.blob" "$P_GMDIO" status)" = disabled ] && ! get -p "$T/probe.blob" "$P_DC" | grep -qx regulator-always-on \
   && [ "$(get -tu "$T/probe.blob" "$P_G" syscon)" = 1 ] && [ "$(get -ts "$T/probe.blob" "$P_GPHY" compatible)" = "$C22" ]; then
    ok "seed: \`reverted\` carries all eight edits undone"
else
    bad "seed: \`reverted\` is not the all-pending blob — the cases below would be hollow"
fi

# ═══ T1: apply, one name, FAT already mounted ═════════════════════════════
reset_sandbox
seed_blob "$FAT/$NAME" reverted
cp "$FAT/$NAME" "$CANON1/$NAME"
cp "$FAT/$NAME" "$T/seed.blob"
run_tool apply
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && cmp -s "$BACKUP"/dtb-backup-"$NAME"-* "$T/seed.blob" \
   && all_eight "$FAT/$NAME" && [ "$(get -ts "$FAT/$NAME" "$P_GMDIO" status)" = okay ] && [ ! -e "$FAT/.$NAME.new" ] \
   && cmp -s "$FAT/$NAME" "$CANON1/$NAME" && [ ! -e "$CANON2/$NAME" ] \
   && grep -Fq "$NAME: patched: $EIGHT; backup $BACKUP/dtb-backup-$NAME-" "$OUT" && grep -Fq 'перезагрузка' "$OUT" \
   && [ "$(n_calls '^mount ')" = 0 ] && [ "$(n_calls '^umount ')" = 0 ] && [ "$(n_calls '^sync')" -ge 2 ]; then
    ok "T1: apply makes the eight edits (old patcher's lines, empty regulator-always-on, decoy untouched) and lands them atomically, exit 0"
else
    bad "T1: rc=$RC backups=$(n_backups) eight=$(all_eight "$FAT/$NAME" && echo y || echo n) new=$([ -e "$FAT/.$NAME.new" ] && echo y || echo n) canon2=$([ -e "$CANON2/$NAME" ] && echo y || echo n) mount=$(n_calls '^mount ') — $(head -3 "$OUT" | tr '\n' '|')"
fi
cp "$FAT/$NAME" "$T/after1.blob"

# ═══ T2: second apply is a no-op ═══════════════════════════════════════════
run_tool apply
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && cmp -s "$FAT/$NAME" "$T/after1.blob" && [ "$(n_calls '^fdtput ')" = 0 ] \
   && grep -Fq 'уже исправлен' "$OUT" && ! grep -Fq 'перезагрузка' "$OUT" && [ ! -e "$FAT/.$NAME.new" ]; then
    ok "T2: second apply writes nothing (no fdtput call), no new backup, «уже исправлен», exit 0"
else
    bad "T2: rc=$RC backups=$(n_backups) fdtput=$(n_calls '^fdtput ') same=$(cmp -s "$FAT/$NAME" "$T/after1.blob" && echo y || echo n) — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T3: three names, differing content, each patched on its own copy ═════
reset_sandbox
NAMES3="sun8i-a40i-sk.dtb sun8i-r40-sa02m.dtb sun8i-a40i-nano2e-none-sk.dtb"
for n in $NAMES3; do seed_blob "$FAT/$n" reverted "$n"; done
run_tool apply
t3=1
for n in $NAMES3; do
    all_eight "$FAT/$n" && [ "$(get -ts "$FAT/$n" / model)" = "SA-02m sandbox blob [$n]" ] && grep -Fq "$n: patched:" "$OUT" || t3=0
done
cmp -s "$FAT/sun8i-a40i-sk.dtb" "$FAT/sun8i-r40-sa02m.dtb" && t3=0
if [ "$RC" = 0 ] && [ "$t3" = 1 ] && [ "$(n_backups)" = 3 ]; then
    ok "T3: three names patched independently, three backups, blobs keep their own differences"
else
    bad "T3: rc=$RC all=$t3 backups=$(n_backups) — $(grep -F 'failed' "$OUT" | head -2 | tr '\n' '|')"
fi

# ═══ T4: fdtget lies -> verify refuses, FAT untouched ══════════════════════
reset_sandbox
seed_blob "$FAT/$NAME" reverted; cp "$FAT/$NAME" "$T/seed.blob"
FDTGET_LIE=1 run_tool apply
if [ "$RC" = 5 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && [ "$(n_backups)" = 0 ] \
   && grep -Fq "$NAME: failed: verify: EMAC compatible[0]" "$OUT" && [ ! -e "$FAT/.$NAME.new" ]; then
    ok "T4: a verify miss (fdtget lies about compatible) leaves FAT untouched, no backup, exit 5"
else
    bad "T4: rc=$RC unchanged=$(cmp -s "$FAT/$NAME" "$T/seed.blob" && echo y || echo n) backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T5: edit failures -> FAT untouched, exit 5 ════════════════════════════
t5_case() {   # $1 = label, $2 = the failed-line prefix, [$3 = a further fragment]; the call's env selects the failure
    if [ "$RC" = 5 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && [ "$(n_backups)" = 0 ] && [ ! -e "$FAT/.$NAME.new" ] \
       && grep -Fq "$NAME: failed: $2" "$OUT" && grep -Fq "${3:-$2}" "$OUT" && ! grep -Fq 'перезагрузка' "$OUT"; then
        ok "T5: $1 leaves FAT untouched, no backup, exit 5"
    else
        bad "T5: $1 rc=$RC unchanged=$(cmp -s "$FAT/$NAME" "$T/seed.blob" && echo y || echo n) backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
    fi
}
reset_sandbox
seed_blob "$FAT/$NAME" reverted; cp "$FAT/$NAME" "$T/seed.blob"
FDTPUT_FAIL=1 run_tool apply
t5_case "an fdtput failure (rc 2)" "edit rc=2:" "sa02m-dtb-patch: fdtput $P_DC regulator-always-on"
reset_sandbox
seed_blob "$FAT/$NAME" reverted "" nopad; cp "$FAT/$NAME" "$T/seed.blob"
run_tool apply
t5_case "a too-small blob (the size floor)" "verify: размер"
reset_sandbox
seed_blob "$FAT/$NAME" nogmac; cp "$FAT/$NAME" "$T/seed.blob"
run_tool apply
t5_case "a blob without the GMAC node (rc 1)" "edit rc=1: sa02m-dtb-patch: node not found: $P_G (GMAC)"
reset_sandbox
seed_blob "$FAT/$NAME" noemac; cp "$FAT/$NAME" "$T/seed.blob"
run_tool apply
t5_case "a blob without the EMAC PHY node (rc 1)" "edit rc=1: sa02m-dtb-patch: node not found: $P_EPHY (EMAC PHY)"

# ═══ T6: no DTB name on FAT ════════════════════════════════════════════════
reset_sandbox
run_tool apply
if [ "$RC" = 4 ] && [ -z "$(ls -A "$FAT")" ] && [ "$(n_backups)" = 0 ]; then
    ok "T6: no DTB name present: exit 4, nothing written"
else
    bad "T6: rc=$RC fat='$(ls -A "$FAT" | tr '\n' ' ')' backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T7: not root; stale lock ══════════════════════════════════════════════
reset_sandbox
seed_blob "$FAT/$NAME" reverted; cp "$FAT/$NAME" "$T/seed.blob"
ID_UID=1000 run_tool apply
if [ "$RC" = 3 ] && [ "$(n_calls '^mount')" = 0 ] && [ "$(n_calls '^fdt')" = 0 ] \
   && cmp -s "$FAT/$NAME" "$T/seed.blob" && grep -Fq 'root' "$OUT"; then
    ok "T7: not root: exit 3 before any mount or read"
else
    bad "T7: not root — rc=$RC mount=$(n_calls '^mount') fdt=$(n_calls '^fdt') — $(head -2 "$OUT" | tr '\n' '|')"
fi
mkdir -p "$LOCK"
run_tool apply
if [ "$RC" = 3 ] && [ "$(n_calls '^fdt')" = 0 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && grep -Fq 'lock' "$OUT" && [ -d "$LOCK" ]; then
    ok "T7: a held lock: exit 3, nothing read, the foreign lock is left in place"
else
    bad "T7: lock — rc=$RC fdt=$(n_calls '^fdt') lock=$([ -d "$LOCK" ] && echo y || echo n) — $(head -2 "$OUT" | tr '\n' '|')"
fi
rmdir "$LOCK"

# ═══ T8: FAT not mounted: mount once, umount once ══════════════════════════
reset_sandbox
seed_blob "$FAT/$NAME" reverted
rm -f "$MOUNTED"
run_tool apply
if [ "$RC" = 0 ] && [ "$(n_calls "^mount $FATDEV $FAT\$")" = 1 ] && [ "$(n_calls "^umount $FAT\$")" = 1 ] \
   && is_patched "$FAT/$NAME" && [ ! -e "$MOUNTED" ] && [ ! -d "$LOCK" ]; then
    ok "T8: FAT not mounted: mounted once, patched, unmounted once, lock released, exit 0"
else
    bad "T8: rc=$RC mount=$(n_calls '^mount ') umount=$(n_calls '^umount ') patched=$(is_patched "$FAT/$NAME" && echo y || echo n) lock=$([ -d "$LOCK" ] && echo y || echo n) — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T9: --dry-run ═════════════════════════════════════════════════════════
reset_sandbox
seed_blob "$FAT/$NAME" reverted; cp "$FAT/$NAME" "$CANON1/$NAME"; cp "$FAT/$NAME" "$T/seed.blob"
run_tool apply --dry-run
if [ "$RC" = 0 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && cmp -s "$CANON1/$NAME" "$T/seed.blob" && [ "$(n_backups)" = 0 ] \
   && grep -Fq "$NAME: would-patch: $EIGHT" "$OUT" && ! grep -Fq 'перезагрузка' "$OUT" && [ ! -e "$FAT/.$NAME.new" ]; then
    ok "T9: --dry-run names the change, writes nothing, no backup, exit 0"
else
    bad "T9: rc=$RC unchanged=$(cmp -s "$FAT/$NAME" "$T/seed.blob" && echo y || echo n) backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T13: the bench shapes (asis, damaged) — idempotent ════════════════════
reset_sandbox
seed_blob "$FAT/$NAME" asis
cp "$FAT/$NAME" "$T/seed.blob"
run_tool apply
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && grep -Fq 'перезагрузка' "$OUT" \
   && grep -Fxq "$NAME: patched: mdio@1c0b080/ethernet-phy@0: compatible $ID_STR first;; backup $(echo "$BACKUP"/dtb-backup-"$NAME"-*)" "$OUT" \
   && all_eight "$FAT/$NAME" && ! grep -Fq 'ethernet@1c50000' "$OUT" && ! cmp -s "$FAT/$NAME" "$T/seed.blob"; then
    ok "T13: apply #1 on the bench original patches the EMAC list only (one line, GMAC left as is)"
else
    bad "T13: asis apply #1 rc=$RC backups=$(n_backups) emac='$(get -ts "$FAT/$NAME" "$P_EPHY" compatible)' — $(head -2 "$OUT" | tr '\n' '|')"
fi
cp "$FAT/$NAME" "$T/after13.blob"
for pass in 2 3; do
    run_tool apply
    if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && cmp -s "$FAT/$NAME" "$T/after13.blob" \
       && grep -Fq 'уже исправлен' "$OUT" && ! grep -Fq 'перезагрузка' "$OUT" && [ ! -e "$FAT/.$NAME.new" ]; then
        ok "T13: apply #$pass on the bench original writes nothing, no new backup, «уже исправлен»"
    else
        bad "T13: asis apply #$pass rc=$RC backups=$(n_backups) same=$(cmp -s "$FAT/$NAME" "$T/after13.blob" && echo y || echo n) — $(head -2 "$OUT" | tr '\n' '|')"
    fi
done
reset_sandbox
seed_blob "$FAT/$NAME" damaged
run_tool apply
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && all_eight "$FAT/$NAME" \
   && grep -Fq "$NAME: patched: ethernet@1c50000/mdio/ethernet-phy@0: compatible $GMAC_C first;mdio@1c0b080/ethernet-phy@0: compatible $C22 appended;; backup" "$OUT"; then
    ok "T13: the bench-damaged lists (GMAC x4, EMAC id only) come back canonical, the old patcher's two lines"
else
    bad "T13: damaged rc=$RC gmac='$(get -ts "$FAT/$NAME" "$P_GPHY" compatible)' emac='$(get -ts "$FAT/$NAME" "$P_EPHY" compatible)' — $(head -2 "$OUT" | tr '\n' '|')"
fi
run_tool apply
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && grep -Fq 'уже исправлен' "$OUT"; then
    ok "T13: a second apply on the healed blob: «уже исправлен», no new backup"
else
    bad "T13: damaged pass 2 rc=$RC backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T14/T15: verify refuses a non-canonical list (miswriting fdtput) ══════
# Control first: the stub active in its pass-through mode must still be
# `patched`, so a T14/T15 refusal is the list check and not a broken stub.
reset_sandbox
seed_blob "$FAT/$NAME" reverted
FDTPUT_STUB=canon run_tool apply
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && grep -Fq "$NAME: patched:" "$OUT" && ! grep -Fq 'failed' "$OUT" && is_patched "$FAT/$NAME"; then
    ok "T14: control — the stub in pass-through mode: \`patched\`, one backup, canonical lists"
else
    bad "T14: control rc=$RC backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi
# T14: GMAC `icplus,ip101a` twice (the bench apply-#1 shape), EMAC canonical.
reset_sandbox
seed_blob "$FAT/$NAME" reverted; cp "$FAT/$NAME" "$T/seed.blob"
FDTPUT_STUB=dup run_tool apply
if [ "$RC" = 5 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && [ "$(n_backups)" = 0 ] && [ ! -e "$FAT/.$NAME.new" ] \
   && grep -Fq "$NAME: failed: verify: GMAC compatible: icplus,ip101a повторён" "$OUT" \
   && ! grep -Fq 'EMAC compatible' "$OUT" && ! grep -Fq 'перезагрузка' "$OUT"; then
    ok "T14: a duplicated icplus,ip101a in the GMAC list fails verify: named, FAT untouched, no backup, exit 5"
else
    bad "T14: rc=$RC unchanged=$(cmp -s "$FAT/$NAME" "$T/seed.blob" && echo y || echo n) backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi
# T15: EMAC list = the id only, c22 dropped (the bench apply-#2/#3 shape).
reset_sandbox
seed_blob "$FAT/$NAME" reverted; cp "$FAT/$NAME" "$T/seed.blob"
FDTPUT_STUB=noc22 run_tool apply
if [ "$RC" = 5 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && [ "$(n_backups)" = 0 ] && [ ! -e "$FAT/.$NAME.new" ] \
   && grep -Fq "$NAME: failed: verify: EMAC compatible: нет $C22" "$OUT" && ! grep -Fq 'перезагрузка' "$OUT"; then
    ok "T15: an EMAC list without $C22 fails verify: named, FAT untouched, no backup, exit 5"
else
    bad "T15: rc=$RC unchanged=$(cmp -s "$FAT/$NAME" "$T/seed.blob" && echo y || echo n) backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T17: a backslash + 0 inside one string survives as ONE string ═════════
reset_sandbox
seed_blob "$FAT/$NAME" backslash
run_tool apply
g17=$(get -ts "$FAT/$NAME" "$P_GPHY" compatible)
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && [ "$g17" = 'icplus,ip101a acme,phy\0x ethernet-phy-ieee802.3-c22' ] \
   && grep -Fq "ethernet@1c50000/mdio/ethernet-phy@0: compatible $GMAC_C first" "$OUT"; then
    ok "T17: a string with a literal backslash + 0 stays one string (icplus,ip101a first, then it, then c22), patched"
else
    bad "T17: rc=$RC gmac='$g17' — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T10: check on proc/sysfs fixtures ═════════════════════════════════════
seed_phy() {   # $1 driver, $2 phy_id
    mkdir -p "$PROC" "$SYSP"
    printf 'ethernet-phy-ieee802.3-c22\0' > "$PROC/compatible"
    printf '%s\n' "$2" > "$SYSP/phy_id"
    printf 'DRIVER=%s\nOF_NAME=ethernet-phy\n' "$1" > "$SYSP/uevent"
}
seed_phy "Generic PHY" 0x00000000
run_tool check
if [ "$RC" = 1 ] && grep -Fq 'Generic PHY' "$OUT" && grep -Fq 'fix-eth' "$OUT" && grep -Fq 'phy_id = 0x00000000' "$OUT" \
   && [ "$(n_calls '^id')" = 0 ] && [ "$(n_calls '^mount')" = 0 ]; then
    ok "T10: check under Generic PHY: exit 1, names fix-eth as what raises the link, no root/mount consulted"
else
    bad "T10: generic — rc=$RC id=$(n_calls '^id') — $(head -3 "$OUT" | tr '\n' '|')"
fi
seed_phy "ICPlus IP101G" 0x02430c54
printf 'ethernet-phy-id0243.0c54\0ethernet-phy-ieee802.3-c22\0' > "$PROC/compatible"
mkdir -p "$(dirname "$SYSP")/stmmac-1:00"
printf '0x02430c54\n' > "$(dirname "$SYSP")/stmmac-1:00/phy_id"
printf 'DRIVER=ICPlus IP101A\n' > "$(dirname "$SYSP")/stmmac-1:00/uevent"
run_tool check
if [ "$RC" = 0 ] && grep -Fq 'driver = ICPlus IP101G' "$OUT" && grep -Fq "compatible = $ID_STR ethernet-phy-ieee802.3-c22" "$OUT" \
   && grep -Fq 'eth1 PHY: phy_id = 0x02430c54, driver = ICPlus IP101A' "$OUT"; then
    ok "T10: check under ICPlus IP101G: exit 0, both PHYs reported"
else
    bad "T10: icplus — rc=$RC — $(head -4 "$OUT" | tr '\n' '|')"
fi
rm -rf "$PROC"
run_tool check
if [ "$RC" = 2 ]; then
    ok "T10: check with the DT node missing: exit 2"
else
    bad "T10: missing node — rc=$RC — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T16: check warns on a non-canonical LIVE list, exit code unchanged ════
# The proc files carry what the kernel exposes: strings NUL-separated.
seed_phy "ICPlus IP101A" 0x02430c54
mkdir -p "$GPROC"
printf 'ethernet-phy-id0243.0c54\0' > "$PROC/compatible"
printf 'icplus,ip101a\0icplus,ip101a\0icplus,ip101a\0icplus,ip101a\0ethernet-phy-ieee802.3-c22\0' > "$GPROC/compatible"
run_tool check
if [ "$RC" = 0 ] && grep -Fq 'под драйвером icplus — OK' "$OUT" \
   && grep -Fq "eth0 PHY: ВНИМАНИЕ: список compatible не канонический: нет $C22" "$OUT" \
   && grep -Fq 'eth1 PHY: ВНИМАНИЕ: список compatible не канонический: icplus,ip101a повторён' "$OUT"; then
    ok "T16: check names the bench-damaged live lists (eth0 without c22, eth1 icplus x4), exit 0 under icplus"
else
    bad "T16: damaged/icplus — rc=$RC — $(tr '\n' '|' < "$OUT")"
fi
seed_phy "Generic PHY" 0x00000000
printf 'ethernet-phy-ieee802.3-c22\0ethernet-phy-ieee802.3-c22\0' > "$PROC/compatible"
printf 'icplus,ip101a\0ethernet-phy-ieee802.3-c22\0' > "$GPROC/compatible"
run_tool check
if [ "$RC" = 1 ] && grep -Fq 'fix-eth' "$OUT" \
   && grep -Fq "eth0 PHY: ВНИМАНИЕ: список compatible не канонический: $C22 повторён" "$OUT" \
   && ! grep -Fq 'eth1 PHY: ВНИМАНИЕ' "$OUT"; then
    ok "T16: check under Generic PHY with a duplicated eth0 token: the warning, exit 1 as before"
else
    bad "T16: damaged/generic — rc=$RC — $(tr '\n' '|' < "$OUT")"
fi
seed_phy "ICPlus IP101A" 0x02430c54
printf 'ethernet-phy-id0243.0c54\0ethernet-phy-ieee802.3-c22\0' > "$PROC/compatible"
run_tool check
if [ "$RC" = 0 ] && [ -s "$GPROC/compatible" ] && ! grep -Fq 'ВНИМАНИЕ' "$OUT" && grep -Fq 'под драйвером icplus — OK' "$OUT"; then
    ok "T16: control — canonical live lists on both PHY nodes: no warning, exit 0"
else
    bad "T16: canonical — rc=$RC — $(tr '\n' '|' < "$OUT")"
fi
rm -rf "$T/dt"

# ═══ T11: fdtput absent -> exit 3 before any mount ═════════════════════════
reset_sandbox
seed_blob "$FAT/$NAME" reverted; cp "$FAT/$NAME" "$T/seed.blob"
rm -f "$BIN/fdtput"
hash -r
command -v fdtput >/dev/null 2>&1 && { echo "FAIL  T11: an fdtput is still on PATH after the shim was removed ($(command -v fdtput)) — the mask failed"; exit 1; }
run_tool apply
if [ "$RC" = 3 ] && [ "$(n_calls '^mount')" = 0 ] && grep -Fq 'fdtput' "$OUT" && grep -Fq 'device-tree-compiler' "$OUT" \
   && cmp -s "$FAT/$NAME" "$T/seed.blob"; then
    ok "T11: fdtput absent: exit 3 with the apt hint, no mount, nothing written"
else
    bad "T11: rc=$RC mount=$(n_calls '^mount') — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T12: harness non-vacuity ══════════════════════════════════════════════
for s in fdtget fdtput mount umount mountpoint sync id; do
    grep -q "^$s " "$CALLS_ALL" || bad "T12: the $s shim was never invoked — the cases are not exercising the tool"
done

printf '\n'
if [ "$fails" = 0 ]; then
    echo "PASS  test-dtb-patch-tool.sh"
    exit 0
fi
echo "FAIL  test-dtb-patch-tool.sh: $fails failing case(s)"
exit 1
