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
# FAT_MNT, CANON_DIRS, BACKUP_DIR, PROC_PHY, SYS_PHY, PATCHER, LOCK_DIR) are
# sed-retargeted into the sandbox; there is deliberately NO env seam in the
# shipped script. The sandbox FAT holds dts TEXT under the .dtb names, padded
# to a blob-like size, so the `dtc` shim is a copy both ways (compile pads,
# decompile strips the pad — and with DTC_NUL_FORM=1 re-joins every
# multi-string `compatible` into ONE literal with \0 escapes, the text dtc 1.7
# hands the patcher on the board), the REAL patcher runs through a `python3` shim
# that execs the first working host interpreter (python3 | python | py -3;
# none => exit 77 SKIP, never PASS), and `fdtget` answers from the text with
# awk (FDTGET_LIE=1 makes it lie about the EMAC `compatible` only). `mount`, `umount`,
# `mountpoint`, `sync` and `id` are recording shims. A host dtc/fdtget is
# hidden through scripts/dev/lib_path_mask.sh so the «not installed» case is
# the harness's call on every host. No root, no device, no systemd — bash +
# coreutils + one python; runs in Windows git-bash and on Linux CI.
#
# Cases:
#   T1  apply, one name, FAT already mounted: backup written, live file now
#       id-first, no `.new` leftover, canonical copy refreshed where pre-seeded
#       and NOT created where absent, «перезагрузка» printed, mount/umount not
#       called, exit 0.
#   T2  second apply: nothing written (bytes + no new backup), «уже исправлен»,
#       exit 0.
#   T3  three names with differing content: each patched independently, three
#       backups, the blobs still differ from each other afterwards.
#   T4  FDTGET_LIE=1 (verify sees a wrong first token): FAT bytes unchanged,
#       no backup, exit 5.                       <- the comment-mutation case
#   T5  DTC_FAIL=compile / =decompile / =short (an unpadded, too-small blob):
#       FAT unchanged, exit 5 each.
#   T6  no DTB name present: exit 4, nothing written, no backup.
#   T7  not root (id shim -> 1000): exit 3 before any mount or read; a stale
#       lock dir: exit 3.
#   T8  FAT not mounted: mount shim called once, umount once, exit 0, patched.
#   T9  --dry-run: «would-patch» printed, FAT unchanged, no backup, no
#       «перезагрузка», exit 0.
#   T10 check on proc/sysfs fixtures: Generic PHY -> exit 1 naming fix-eth;
#       ICPlus IP101G -> exit 0; node missing -> exit 2; `id` never consulted.
#   T11 dtc absent (shim removed, host masked): exit 3, no mount.
#   T12 harness non-vacuity: every shim invoked at least once across the run.
#   T13 (runs before T11) the real dtc 1.7 text form: FAT seeded with the bench
#       1.135 original blob trimmed (sun8i-a40i-sk.real.dts), decompile in the
#       one-literal `\0` form; apply #1 `patched` (EMAC only, both PHY lists in
#       canon through fdtget), apply #2 and #3 «уже исправлен», one backup, no
#       write. T2 never saw this form (copy-both-ways shim): the bench defect of
#       2026-10-29 — three `patched`, GMAC list x4, c22 lost — passed T1–T12.
#   T14 (before T11) verify refuses a duplicated string: a STUB patcher (the
#       harness writes it; DTB_STUB picks the shape) puts a second
#       `icplus,ip101a` in the GMAC list -> «failed: verify: GMAC compatible:
#       icplus,ip101a повторён», FAT unchanged, no backup, exit 5. Control
#       first: the stub's `canon` shape is `patched` (so the refusal is the
#       list check, not a broken stub). The real patcher is canonical now, so
#       only a misbehaving patcher can drive this guard.
#   T15 (before T11) the stub writes the EMAC list as the id only -> «failed:
#       verify: EMAC compatible: нет …c22», FAT unchanged, no backup, exit 5.
#   T16 check on NUL-separated proc lists: eth0 id-only + eth1 icplus x4 under
#       ICPlus -> a ВНИМАНИЕ line for each, exit 0; a duplicated eth0 string
#       under Generic PHY -> the warning, exit 1 (unchanged); canonical lists
#       on both nodes -> no warning. T1/T13 stay the canonical-blob controls.
#
# Non-vacuous: a failed or over-wide retarget, a leftover real root on an
# executable line, a missing tool/patcher/fixture, a real fixture that lost
# the `\0` form, or a shim never invoked FAILS rather than passing on zero
# matches.
#
# Drive-to-failure recipe (commit body + comment-mutation-proof): comment the
# `[ "$first" = "$EMAC_ID_COMPAT" ]` verify line out of a copy -> T4 writes the
# blob fdtget lied about (FAT changed, exit 0) -> RED; comment the
# `mv -f "$tmp" "$2"` line of write_atomic out -> T1 RED (live file not
# replaced, `.new` left behind); comment list_defect's duplicate line out ->
# T14 and T16 RED, its c22 line -> T15 and T16 RED; the tool before the list
# check (f70db73c) -> T14, T15, T16 RED (observed 2026-10-30). DTB_TOOL_SRC=<path> re-points the harness at
# such a copy, DTB_PATCHER_SRC=<path> at another patcher (the pre-fix patcher
# that split on quoted literals only — the defect found on bench 1.135 before
# the first merge of this change, BUGLOG 2026-10-30: T13 RED — apply #1 prepends
# a second `icplus,ip101a`, apply #2 and #3 `patched` again with a backup each);
# the real tree is never edited.
#
# Run: bash scripts/dev/test-dtb-patch-tool.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${DTB_TOOL_SRC:-usr/local/sbin/sa02m-dtb-patch.sh}
PATCHER_SRC=${DTB_PATCHER_SRC:-usr/local/sbin/sa02m-dtb-patch.py}
FIXTURE=scripts/dev/fixtures/dtb-patch/sun8i-a40i-sk.pre.dts
REAL_FIXTURE=scripts/dev/fixtures/dtb-patch/sun8i-a40i-sk.real.dts   # bench 1.135 decompile, dtc 1.7 form (T13)
ID_STR=ethernet-phy-id0243.0c54
C22=ethernet-phy-ieee802.3-c22

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -s "$SRC" ] || { echo "FAIL  missing or empty: $SRC"; exit 1; }
[ -s "$PATCHER_SRC" ] || { echo "FAIL  missing or empty: $PATCHER_SRC"; exit 1; }
[ -s "$FIXTURE" ] || { echo "FAIL  missing or empty: $FIXTURE"; exit 1; }
[ -s "$REAL_FIXTURE" ] || { echo "FAIL  missing or empty: $REAL_FIXTURE"; exit 1; }
grep -Fq "$ID_STR" "$FIXTURE" && { echo "FAIL  fixture already carries $ID_STR — it must be the PRE-patch text"; exit 1; }
grep -Fq "$ID_STR" "$REAL_FIXTURE" && { echo "FAIL  real fixture already carries $ID_STR — it must be the PRE-patch text"; exit 1; }
# T13 exists for the one-literal form; a fixture rewritten into `"a", "b"` would make it hollow.
grep -Fq 'compatible = "icplus,ip101a\0'"$C22"'";' "$REAL_FIXTURE" || { echo "FAIL  real fixture lost the dtc 1.7 one-literal form on the GMAC PHY node"; exit 1; }
# shellcheck source=scripts/dev/lib_path_mask.sh
. scripts/dev/lib_path_mask.sh || { echo "FAIL  scripts/dev/lib_path_mask.sh not loadable"; exit 1; }

# ── the real interpreter, resolved BEFORE the shims go on PATH ─────────────
# Absolute path, so a shim named python3 cannot exec itself on a host whose
# working interpreter IS python3 (Linux CI).
REAL_PY=""; REAL_PY_ARGS=""
for cand in python3 python py; do
    p=$(command -v "$cand" 2>/dev/null) || continue
    args=""; [ "$cand" = py ] && args="-3"
    # shellcheck disable=SC2086
    if "$p" $args -c 'import sys' >/dev/null 2>&1; then REAL_PY=$p; REAL_PY_ARGS=$args; break; fi
done
if [ -z "$REAL_PY" ]; then
    echo "SKIP  test-dtb-patch-tool.sh: no working python interpreter (python3 | python | py -3) — the real patcher cannot run"
    exit 77
fi

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
PATCHER="$T/sa02m-dtb-patch.py"
LOCK="$T/lockdir"
BIN="$T/bin"
OUT="$T/run.out"
CALLS="$T/calls.log"            # reset per run
CALLS_ALL="$T/calls-all.log"    # never reset — the non-vacuity witness
MOUNTED="$T/fat.mounted"        # exists = the mountpoint shim says «mounted»
mkdir -p "$BIN" "$FAT" "$CANON1" "$CANON2" "$BACKUP" "$BACKUP"
cp "$PATCHER_SRC" "$PATCHER"

# ── retarget the SHIPPED tool into the sandbox ─────────────────────────────
TOOL="$T/sa02m-dtb-patch.sh"
sed -e "s#^FAT_DEV=.*#FAT_DEV=$FATDEV#" \
    -e "s#^FAT_MNT=.*#FAT_MNT=$FAT#" \
    -e "s#^CANON_DIRS=.*#CANON_DIRS=\"$CANON1 $CANON2\"#" \
    -e "s#^BACKUP_DIR=.*#BACKUP_DIR=$BACKUP#" \
    -e "s#^PROC_PHY=.*#PROC_PHY=$PROC#" \
    -e "s#^SYS_PHY=.*#SYS_PHY=$SYSP#" \
    -e "s#^PATCHER=.*#PATCHER=$PATCHER#" \
    -e "s#^LOCK_DIR=.*#LOCK_DIR=$LOCK#" \
    "$SRC" > "$TOOL"
chmod +x "$TOOL"
for pat in "FAT_DEV=$FATDEV" "FAT_MNT=$FAT" "CANON_DIRS=\"$CANON1 $CANON2\"" "BACKUP_DIR=$BACKUP" \
           "PROC_PHY=$PROC" "SYS_PHY=$SYSP" "PATCHER=$PATCHER" "LOCK_DIR=$LOCK"; do
    grep -Fq "$pat" "$TOOL" || { echo "FAIL  retarget missed in the tool: $pat"; exit 1; }
done
# An un-retargeted copy would mount the real boot partition and write /root —
# the one failure this harness must never pass through. Comments are skipped
# (the header names the real paths as prose).
if grep -Eq '^[^#]*(/dev/mmcblk|/mnt/fat|/usr/local/share|/root([^a-zA-Z0-9_-]|$)|/proc/device-tree|/sys/bus|/usr/local/sbin|/run/lock)' "$TOOL"; then
    echo "FAIL  the tool copy still holds a real root on an executable line:"; grep -En '^[^#]*(/dev/mmcblk|/mnt/fat|/usr/local/share|/root([^a-zA-Z0-9_-]|$)|/proc/device-tree|/sys/bus|/usr/local/sbin|/run/lock)' "$TOOL" | head -3; exit 1
fi

# ── PATH shims ─────────────────────────────────────────────────────────────
# dtc: a copy both ways. «compile» pads the text with newlines to a blob-like
# size (the tool's SIZE_MIN floor is 16 KiB); «decompile» strips the pad, so a
# round trip is byte-exact on the fixture. DTC_FAIL selects a failure.
PAD_LINES=20000
cat > "$BIN/dtc" <<SHIM
#!/bin/bash
echo "dtc \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
out=""; in=""; ifmt=""
while [ \$# -gt 0 ]; do
  case "\$1" in
    -o) out=\$2; shift ;;
    -I) ifmt=\$2; shift ;;
    -O) shift ;;
    -q) ;;
    *)  in=\$1 ;;
  esac
  shift
done
case "\${DTC_FAIL:-}" in
  decompile) if [ "\$ifmt" = dtb ]; then echo "dtc: FATAL ERROR: shim decompile failure" >&2; exit 1; fi ;;
  compile)   if [ "\$ifmt" = dts ]; then echo "dtc: ERROR: shim compile failure" >&2; exit 1; fi ;;
esac
if [ "\$ifmt" = dtb ]; then
  awk '{ l[NR] = \$0 } END { n = NR; while (n > 0 && l[n] == "") n--; for (i = 1; i <= n; i++) print l[i] }' "\$in" > "\$out"
  # DTC_NUL_FORM=1: decompile as dtc >= 1.7 does — a multi-string compatible
  # comes back as ONE literal with \\0 between the strings (T13).
  if [ "\${DTC_NUL_FORM:-0}" = 1 ]; then
    sed -i -E -e '/^[[:space:]]*compatible = /{' -e ':a' -e 's/",[[:space:]]*"/\\\\0/' -e 'ta' -e '}' "\$out"
  fi
else
  cp "\$in" "\$out"
  if [ "\${DTC_FAIL:-}" != short ]; then yes '' | head -n $PAD_LINES >> "\$out"; fi
fi
exit 0
SHIM
# fdtget [-ts] FILE NODEPATH PROP — answers from the text: path components are
# matched as node headers in order, then the node's OWN property (children
# skipped). Strings print space-separated (what fdtget -ts prints), a cell
# prints as decimal (what a bare fdtget prints). FDTGET_LIE=1 lies about the
# EMAC node's `compatible` ONLY (every other answer stays true), so the
# first-token verify line is the one thing standing between a wrong blob and
# the FAT (T4 / the comment-mutation case) — a lie that also broke the GMAC
# answer would be caught by the GMAC check and leave that case hollow.
cat > "$BIN/fdtget" <<'SHIM'
#!/bin/bash
echo "fdtget $*" | tee -a "$CALLS_ALL_PATH" >> "$CALLS_PATH"
ts=0
if [ "${1:-}" = -ts ]; then ts=1; shift; fi
f=$1; node=$2; prop=$3
if [ "${FDTGET_LIE:-0}" = 1 ] && [ "$prop" = compatible ] && [ "$node" = /soc/mdio@1c0b080/ethernet-phy@0 ]; then
  echo "ethernet-phy-ieee802.3-c22"; exit 0
fi
val=$(awk -v path="$node" -v prop="$prop" '
BEGIN { n = split(substr(path, 2), comp, "/"); idx = 0; depth = 0; found = 0 }
{
  line = $0; sub(/^[ \t]+/, "", line); sub(/[ \t]+$/, "", line)
  if (idx < n) { if (line == comp[idx + 1] " {") idx++; next }
  if (line ~ /\{$/) { depth++; next }
  if (line == "};") { if (depth > 0) { depth--; next } exit }
  if (depth > 0) next
  if (index(line, prop " = ") == 1) { print substr(line, length(prop) + 4); found = 1; exit }
}
END { exit found ? 0 : 1 }' "$f") || exit 1
val=${val%;}
case "$val" in
  '"'*) echo "$val" | sed 's/", *"/ /g; s/\\0/ /g; s/"//g' ;;   # both text forms print as real fdtget -ts does
  '<'*) v=${val#<}; v=${v%>}; printf '%d\n' "$v" ;;
  *) echo "$val" ;;
esac
exit 0
SHIM
sed -i -e "s#\$CALLS_ALL_PATH#$CALLS_ALL#" -e "s#\$CALLS_PATH#$CALLS#" "$BIN/fdtget"
cat > "$BIN/python3" <<SHIM
#!/bin/bash
echo "python3 \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
exec "$REAL_PY" $REAL_PY_ARGS "\$@"
SHIM
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
# A STUB patcher for T14/T15 — the list shapes the pre-1.0.7.21 patcher wrote on
# bench 1.135 (BUGLOG 2026-10-30), on purpose. The real patcher is canonical
# now, so the verify guard can only be driven by a patcher that misbehaves —
# which is the independence the guard exists for. Input = the REAL_FIXTURE text
# (dtc 1.7 `\0` form); DTB_STUB selects the shape. A seed it does not recognise
# exits 1 (the tool then reports «patcher rc=1», which no T14/T15 assertion
# accepts), and `canon` is the stub's own positive control.
cat > "$T/stub-patcher.py" <<'STUB'
import os
import sys

args = sys.argv[1:]
src = args[args.index("--in") + 1]
dst = args[args.index("--out") + 1]
NUL = "\\0"   # the two-character escape dtc 1.7 writes between list strings
C22 = "ethernet-phy-ieee802.3-c22"
ID = "ethernet-phy-id0243.0c54"
GMAC = "icplus,ip101a"
emac_new = {"canon": ID + NUL + C22, "dup": ID + NUL + C22, "noc22": ID}
mode = os.environ.get("DTB_STUB", "")
with open(src, encoding="utf-8", newline="") as f:
    text = f.read()
emac_old = 'compatible = "' + C22 + '";'
gmac_old = 'compatible = "' + GMAC + NUL + C22 + '";'
if mode not in emac_new or text.count(emac_old) != 1 or text.count(gmac_old) != 1:
    sys.stderr.write("stub patcher: unknown DTB_STUB or unexpected seed\n")
    sys.exit(1)
text = text.replace(emac_old, 'compatible = "' + emac_new[mode] + '";')
if mode == "dup":   # the apply-#1 shape: a second icplus,ip101a in front
    text = text.replace(gmac_old, 'compatible = "' + GMAC + NUL + GMAC + NUL + C22 + '";')
with open(dst, "w", encoding="utf-8", newline="") as f:
    f.write(text)
print("stub " + mode)
STUB
MASKED=$(path_without "$T/mask" dtc fdtget) || { echo "FAIL  path_without could not mask a host dtc/fdtget"; exit 1; }
PATH="$BIN:$MASKED"; export PATH
command -v dtc | grep -Fq "$BIN/dtc" || { echo "FAIL  the dtc shim is not first on PATH"; exit 1; }
command -v python3 | grep -Fq "$BIN/python3" || { echo "FAIL  the python3 shim is not first on PATH"; exit 1; }

# ── fixtures and helpers ───────────────────────────────────────────────────
blob() {   # $1 = output path, [$2 = model tag], [$3 = source fixture] — the fixture text, padded like a compiled blob
    local src=${3:-$FIXTURE}
    if [ -n "${2:-}" ]; then
        sed "s/^\(\tmodel = \".*\)\";$/\1 [$2]\";/" "$src" > "$1"
        grep -Fq "[$2]" "$1" || { echo "FAIL  fixture model tag not applied: $2"; exit 1; }
    else
        cp "$src" "$1"
    fi
    yes '' | head -n "$PAD_LINES" >> "$1"
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
is_patched() { grep -Fq "compatible = \"$ID_STR\", \"ethernet-phy-ieee802.3-c22\";" "$1" && grep -Fq '"icplus,ip101a", "ethernet-phy-ieee802.3-c22"' "$1"; }
# The FAT file's EMAC node must be the one carrying the id string (not the decoy).
emac_id_first() { awk '/mdio@1c0b080 \{/ { in_mdio = 1 } in_mdio && /ethernet-phy@0 \{/ { in_phy = 1 } in_phy && /compatible = / { print; exit }' "$1" | grep -Fq "\"$ID_STR\", "; }

NAME=sun8i-a40i-sk.dtb

# ═══ T1: apply, one name, FAT already mounted ═════════════════════════════
reset_sandbox
blob "$FAT/$NAME"
blob "$CANON1/$NAME"
cp "$FAT/$NAME" "$T/seed.blob"
run_tool apply
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && cmp -s "$BACKUP"/dtb-backup-"$NAME"-* "$T/seed.blob" \
   && is_patched "$FAT/$NAME" && emac_id_first "$FAT/$NAME" && [ ! -e "$FAT/.$NAME.new" ] \
   && cmp -s "$FAT/$NAME" "$CANON1/$NAME" && [ ! -e "$CANON2/$NAME" ] \
   && grep -Fq "$NAME: patched:" "$OUT" && grep -Fq 'перезагрузка' "$OUT" \
   && [ "$(n_calls '^mount ')" = 0 ] && [ "$(n_calls '^umount ')" = 0 ] && [ "$(n_calls '^sync')" -ge 2 ]; then
    ok "T1: apply patches the live name atomically (backup, id-first, no .new, canonical refreshed where present only), exit 0"
else
    bad "T1: rc=$RC backups=$(n_backups) patched=$(is_patched "$FAT/$NAME" && echo y || echo n) new=$([ -e "$FAT/.$NAME.new" ] && echo y || echo n) canon2=$([ -e "$CANON2/$NAME" ] && echo y || echo n) mount=$(n_calls '^mount ') — $(head -3 "$OUT" | tr '\n' '|')"
fi
cp "$FAT/$NAME" "$T/after1.blob"

# ═══ T2: second apply is a no-op ═══════════════════════════════════════════
run_tool apply
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && cmp -s "$FAT/$NAME" "$T/after1.blob" \
   && grep -Fq 'уже исправлен' "$OUT" && ! grep -Fq 'перезагрузка' "$OUT" && [ ! -e "$FAT/.$NAME.new" ]; then
    ok "T2: second apply writes nothing, no new backup, «уже исправлен», exit 0"
else
    bad "T2: rc=$RC backups=$(n_backups) same=$(cmp -s "$FAT/$NAME" "$T/after1.blob" && echo y || echo n) — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T3: three names, differing content, each patched on its own text ═════
reset_sandbox
NAMES3="sun8i-a40i-sk.dtb sun8i-r40-sa02m.dtb sun8i-a40i-nano2e-none-sk.dtb"
for n in $NAMES3; do blob "$FAT/$n" "$n"; done
run_tool apply
t3=1
for n in $NAMES3; do
    is_patched "$FAT/$n" && grep -Fq "[$n]" "$FAT/$n" && grep -Fq "$n: patched:" "$OUT" || t3=0
done
cmp -s "$FAT/sun8i-a40i-sk.dtb" "$FAT/sun8i-r40-sa02m.dtb" && t3=0
if [ "$RC" = 0 ] && [ "$t3" = 1 ] && [ "$(n_backups)" = 3 ]; then
    ok "T3: three names patched independently, three backups, blobs keep their own differences"
else
    bad "T3: rc=$RC all=$t3 backups=$(n_backups) — $(grep -F 'failed' "$OUT" | head -2 | tr '\n' '|')"
fi

# ═══ T4: fdtget lies -> verify refuses, FAT untouched ══════════════════════
reset_sandbox
blob "$FAT/$NAME"; cp "$FAT/$NAME" "$T/seed.blob"
FDTGET_LIE=1 run_tool apply
if [ "$RC" = 5 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && [ "$(n_backups)" = 0 ] \
   && grep -Fq "$NAME: failed: verify" "$OUT" && [ ! -e "$FAT/.$NAME.new" ]; then
    ok "T4: a verify miss (fdtget lies about compatible) leaves FAT untouched, no backup, exit 5"
else
    bad "T4: rc=$RC unchanged=$(cmp -s "$FAT/$NAME" "$T/seed.blob" && echo y || echo n) backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T5: dtc failures -> FAT untouched, exit 5 ═════════════════════════════
for mode in compile decompile short; do
    reset_sandbox
    blob "$FAT/$NAME"; cp "$FAT/$NAME" "$T/seed.blob"
    DTC_FAIL=$mode run_tool apply
    if [ "$RC" = 5 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && [ "$(n_backups)" = 0 ] && grep -Fq "$NAME: failed:" "$OUT"; then
        ok "T5: DTC_FAIL=$mode leaves FAT untouched, exit 5"
    else
        bad "T5: DTC_FAIL=$mode rc=$RC unchanged=$(cmp -s "$FAT/$NAME" "$T/seed.blob" && echo y || echo n) backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
    fi
done

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
blob "$FAT/$NAME"; cp "$FAT/$NAME" "$T/seed.blob"
ID_UID=1000 run_tool apply
if [ "$RC" = 3 ] && [ "$(n_calls '^mount')" = 0 ] && [ "$(n_calls '^dtc ')" = 0 ] && [ "$(n_calls '^python3 ')" = 0 ] \
   && cmp -s "$FAT/$NAME" "$T/seed.blob" && grep -Fq 'root' "$OUT"; then
    ok "T7: not root: exit 3 before any mount or read"
else
    bad "T7: not root — rc=$RC mount=$(n_calls '^mount') dtc=$(n_calls '^dtc ') — $(head -2 "$OUT" | tr '\n' '|')"
fi
mkdir -p "$LOCK"
run_tool apply
if [ "$RC" = 3 ] && [ "$(n_calls '^dtc ')" = 0 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && grep -Fq 'lock' "$OUT" && [ -d "$LOCK" ]; then
    ok "T7: a held lock: exit 3, nothing read, the foreign lock is left in place"
else
    bad "T7: lock — rc=$RC dtc=$(n_calls '^dtc ') lock=$([ -d "$LOCK" ] && echo y || echo n) — $(head -2 "$OUT" | tr '\n' '|')"
fi
rmdir "$LOCK"

# ═══ T8: FAT not mounted: mount once, umount once ══════════════════════════
reset_sandbox
blob "$FAT/$NAME"
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
blob "$FAT/$NAME"; blob "$CANON1/$NAME"; cp "$FAT/$NAME" "$T/seed.blob"
run_tool apply --dry-run
if [ "$RC" = 0 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && cmp -s "$CANON1/$NAME" "$T/seed.blob" && [ "$(n_backups)" = 0 ] \
   && grep -Fq "$NAME: would-patch:" "$OUT" && grep -Fq "$ID_STR" "$OUT" && ! grep -Fq 'перезагрузка' "$OUT" && [ ! -e "$FAT/.$NAME.new" ]; then
    ok "T9: --dry-run names the change, writes nothing, no backup, exit 0"
else
    bad "T9: rc=$RC unchanged=$(cmp -s "$FAT/$NAME" "$T/seed.blob" && echo y || echo n) backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T13: the real dtc 1.7 text form — idempotent through the full cycle ═══
# Placed before T11 (which removes the dtc shim). Seed = the bench's original
# blob trimmed (REAL_FIXTURE: GMAC already `"icplus,ip101a\0…c22"`), and the
# dtc shim's decompile re-joins every multi-string compatible into ONE literal
# with \0 escapes, as dtc 1.7.0 does on the board. T2 never saw this form —
# its shim is a copy both ways, so the `"a", "b"` text the patcher wrote came
# straight back and the second apply was a no-op for the wrong reason; on the
# bench (2026-10-29) the same tool reported `patched` three times, grew the
# GMAC list to four `icplus,ip101a` and lost c22 on the EMAC node.
reset_sandbox
blob "$FAT/$NAME" "" "$REAL_FIXTURE"
cp "$FAT/$NAME" "$T/seed.blob"
DTC_NUL_FORM=1 run_tool apply
emac13=$(fdtget -ts "$FAT/$NAME" /soc/mdio@1c0b080/ethernet-phy@0 compatible)
gmac13=$(fdtget -ts "$FAT/$NAME" /soc/ethernet@1c50000/mdio/ethernet-phy@0 compatible)
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && grep -Fq "$NAME: patched:" "$OUT" && grep -Fq 'перезагрузка' "$OUT" \
   && [ "$emac13" = "$ID_STR $C22" ] && [ "$gmac13" = "icplus,ip101a $C22" ] \
   && ! grep -Fq 'ethernet@1c50000' "$OUT" && ! cmp -s "$FAT/$NAME" "$T/seed.blob"; then
    ok "T13: apply #1 on the dtc 1.7 form patches the EMAC node only (GMAC left as is), both lists in canon"
else
    bad "T13: apply #1 rc=$RC backups=$(n_backups) emac='$emac13' gmac='$gmac13' — $(head -2 "$OUT" | tr '\n' '|')"
fi
cp "$FAT/$NAME" "$T/after13.blob"
# The shim's own non-vacuity: what the decompile hands the patcher on passes 2/3
# must carry the two-byte `\0` sequence on the EMAC line (the patcher wrote the
# comma form). If the re-join sed stopped working, passes 2/3 would pass on the
# comma form for the wrong reason — so this check fails the case.
# Called by path, not through PATH lookup: a hashed `dtc` would survive T11's
# `rm -f "$BIN/dtc"` in this shell and make its "dtc absent" guard trip.
DTC_NUL_FORM=1 "$BIN/dtc" -I dtb -O dts -q -o "$T/t13.dts" "$FAT/$NAME"
if grep -Fq "compatible = \"$ID_STR\\0$C22\";" "$T/t13.dts"; then
    ok "T13: the shim's decompile re-joins the patched EMAC list into one \\0 literal"
else
    bad "T13: the shim's decompile does not carry the \\0 form — $(grep -F "$ID_STR" "$T/t13.dts" | head -1)"
fi
for pass in 2 3; do
    DTC_NUL_FORM=1 run_tool apply
    if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && cmp -s "$FAT/$NAME" "$T/after13.blob" \
       && grep -Fq 'уже исправлен' "$OUT" && ! grep -Fq 'перезагрузка' "$OUT" && [ ! -e "$FAT/.$NAME.new" ]; then
        ok "T13: apply #$pass on the dtc 1.7 form writes nothing, no new backup, «уже исправлен»"
    else
        bad "T13: apply #$pass rc=$RC backups=$(n_backups) same=$(cmp -s "$FAT/$NAME" "$T/after13.blob" && echo y || echo n) — $(head -2 "$OUT" | tr '\n' '|')"
    fi
done

# ═══ T14/T15: verify refuses a non-canonical list (stub patcher) ═══════════
# Placed before T11. The stub replaces the sandbox patcher; the real one is put
# back after T15. Control first: the stub's canonical shape must still be
# `patched`, so a T14/T15 refusal is the list check and not a broken stub.
cp "$T/stub-patcher.py" "$PATCHER"
reset_sandbox
blob "$FAT/$NAME" "" "$REAL_FIXTURE"
DTC_NUL_FORM=1 DTB_STUB=canon run_tool apply
emac14=$(fdtget -ts "$FAT/$NAME" /soc/mdio@1c0b080/ethernet-phy@0 compatible)
gmac14=$(fdtget -ts "$FAT/$NAME" /soc/ethernet@1c50000/mdio/ethernet-phy@0 compatible)
if [ "$RC" = 0 ] && [ "$(n_backups)" = 1 ] && grep -Fq "$NAME: patched: stub canon" "$OUT" && ! grep -Fq 'failed' "$OUT" \
   && [ "$emac14" = "$ID_STR $C22" ] && [ "$gmac14" = "icplus,ip101a $C22" ]; then
    ok "T14: control — the stub's canonical lists pass verify (\`patched\`, one backup)"
else
    bad "T14: control rc=$RC backups=$(n_backups) emac='$emac14' gmac='$gmac14' — $(head -2 "$OUT" | tr '\n' '|')"
fi
# T14: GMAC `icplus,ip101a` twice (the bench apply-#1 shape), EMAC canonical.
reset_sandbox
blob "$FAT/$NAME" "" "$REAL_FIXTURE"; cp "$FAT/$NAME" "$T/seed.blob"
DTC_NUL_FORM=1 DTB_STUB=dup run_tool apply
if [ "$RC" = 5 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && [ "$(n_backups)" = 0 ] && [ ! -e "$FAT/.$NAME.new" ] \
   && grep -Fq "$NAME: failed: verify: GMAC compatible: icplus,ip101a повторён" "$OUT" \
   && ! grep -Fq 'EMAC compatible' "$OUT" && ! grep -Fq 'перезагрузка' "$OUT"; then
    ok "T14: a duplicated icplus,ip101a in the GMAC list fails verify: named, FAT untouched, no backup, exit 5"
else
    bad "T14: rc=$RC unchanged=$(cmp -s "$FAT/$NAME" "$T/seed.blob" && echo y || echo n) backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi
# T15: EMAC list = the id only, c22 dropped (the bench apply-#2/#3 shape).
reset_sandbox
blob "$FAT/$NAME" "" "$REAL_FIXTURE"; cp "$FAT/$NAME" "$T/seed.blob"
DTC_NUL_FORM=1 DTB_STUB=noc22 run_tool apply
if [ "$RC" = 5 ] && cmp -s "$FAT/$NAME" "$T/seed.blob" && [ "$(n_backups)" = 0 ] && [ ! -e "$FAT/.$NAME.new" ] \
   && grep -Fq "$NAME: failed: verify: EMAC compatible: нет $C22" "$OUT" && ! grep -Fq 'перезагрузка' "$OUT"; then
    ok "T15: an EMAC list without $C22 fails verify: named, FAT untouched, no backup, exit 5"
else
    bad "T15: rc=$RC unchanged=$(cmp -s "$FAT/$NAME" "$T/seed.blob" && echo y || echo n) backups=$(n_backups) — $(head -2 "$OUT" | tr '\n' '|')"
fi
cp "$PATCHER_SRC" "$PATCHER"

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

# ═══ T11: dtc absent -> exit 3 before any mount ════════════════════════════
reset_sandbox
blob "$FAT/$NAME"
rm -f "$BIN/dtc"
command -v dtc >/dev/null 2>&1 && { echo "FAIL  T11: a dtc is still on PATH after the shim was removed ($(command -v dtc)) — the mask failed"; exit 1; }
run_tool apply
if [ "$RC" = 3 ] && [ "$(n_calls '^mount')" = 0 ] && grep -Fq 'device-tree-compiler' "$OUT" && ! is_patched "$FAT/$NAME"; then
    ok "T11: dtc absent: exit 3 with the apt hint, no mount, nothing written"
else
    bad "T11: rc=$RC mount=$(n_calls '^mount') — $(head -2 "$OUT" | tr '\n' '|')"
fi

# ═══ T12: harness non-vacuity ══════════════════════════════════════════════
for s in dtc fdtget python3 mount umount mountpoint sync id; do
    grep -q "^$s " "$CALLS_ALL" || bad "T12: the $s shim was never invoked — the cases are not exercising the tool"
done

printf '\n'
if [ "$fails" = 0 ]; then
    echo "PASS  test-dtb-patch-tool.sh"
    exit 0
fi
echo "FAIL  test-dtb-patch-tool.sh: $fails failing case(s)"
exit 1
