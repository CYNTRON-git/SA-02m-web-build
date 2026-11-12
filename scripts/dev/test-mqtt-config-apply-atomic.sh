#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-mqtt-config-apply-atomic.sh — behavioural regression for the root helper
# usr/local/sbin/sa02m-mqtt-config-apply.sh: the live bridge config
# /etc/sa02m-modbus-mqtt.yaml is replaced by RENAME, never truncated in place.
#
# Why this exists (audit 2026-11-11 A5-06): the helper ran
# `install -m 0660 … "$SAFE_SRC" "$DST"` straight over the live file. `install`
# truncates the destination and then fills it, so a power cut in between left
# a 0-byte or half-written bridge config — the bridge then starts with no
# devices. The live-path rule (docs/agent-rules/web-code-rigor.md, «A live-path
# file is written atomically») wants tmp in the SAME directory + fsync +
# rename-over. The helper deliberately does not source scripts/lib.sh (its
# header), so it repeats the sa02m_atomic_install idiom; this harness measures
# the behaviour, not the lines.
#
# Method (the test-dtb-patch-tool.sh idiom): the SHIPPED helper is copied into
# a scratch tree and its fixed roots (`DST=`, `/tmp`, `/etc`) are sed-retargeted
# into the sandbox — there is deliberately NO env seam in a privileged helper.
# `install`, `sync` and `mv` are PATH-first shims that append to a call log and
# then do the real work (`install` drops -o/-g, which need root). The real
# helper then runs on a valid caller file.
#
# Cases:
#   1  exit 0 and the destination carries the new bytes;
#   2  the destination inode CHANGED (rename-over, not truncate-in-place);
#   3  `sync` was called on the tmp file BEFORE the `mv` that renames it, and
#      the tmp lived in the destination's directory (same filesystem);
#   4  no tmp file is left in the destination directory;
#   5  mode 0660 (Linux only — NTFS under git-bash has no POSIX modes: SKIP);
#   6  a refused source (wrong name) leaves the destination byte-identical and
#      no tmp behind (exit 2).
# Non-vacuous: a retarget that misses, or a call log that never saw `mv`, FAILS.
#
# RED on the pre-fix helper (`git show 42418b57:usr/local/sbin/sa02m-mqtt-config-apply.sh`
# via MQTT_APPLY_HELPER=<file>): case 3 (no tmp + fsync + rename). Case 2 alone
# does NOT catch the old helper: GNU and Cygwin `install` unlink the
# destination before writing (new inode, but the live name is missing or
# partial meanwhile) — it guards against a future truncate-in-place
# (`cat > "$DST"`), which keeps the inode. Commenting out the helper's
# `mv -f -- "$TMP_DST" "$DST"` turns cases 1-4 and 6 RED (comment-mutation case).
# Run: bash scripts/dev/test-mqtt-config-apply-atomic.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
SRC_TOOL=${MQTT_APPLY_HELPER:-usr/local/sbin/sa02m-mqtt-config-apply.sh}
[ -f "$SRC_TOOL" ] || { echo "FAIL  helper not found: $SRC_TOOL"; exit 1; }
. .ai-dev/quality/checks/lib_skip.sh || { echo "FAIL  cannot source .ai-dev/quality/checks/lib_skip.sh"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

SB_TMP="$T/tmp"; SB_ETC="$T/etc"; BIN="$T/bin"; CALLS="$T/calls.log"
mkdir -p "$SB_TMP" "$SB_ETC" "$BIN"
DST="$SB_ETC/sa02m-modbus-mqtt.yaml"

# ── retarget the SHIPPED helper into the sandbox ───────────────────────────
TOOL="$T/sa02m-mqtt-config-apply.sh"
# The DST line is set last and kept out of the path rules: the sandbox itself
# lives under the host's /tmp.
sed -e '/^DST=/!s#/tmp#'"$SB_TMP"'#g' \
    -e '/^DST=/!s#/etc\([/[:space:]]\)#'"$SB_ETC"'\1#g' \
    -e '/^DST=/!s#/etc$#'"$SB_ETC"'#' \
    -e "s#^DST=.*#DST=$DST#" \
    "$SRC_TOOL" > "$TOOL"
chmod +x "$TOOL"
grep -Fq "DST=$DST" "$TOOL" || { echo "FAIL  retarget missed: DST"; exit 1; }
# Non-vacuity of the retarget: with the sandbox prefix removed, no executable
# line may still name the real /etc or /tmp.
real=$(sed "s#$T#SANDBOX#g" "$TOOL" | grep -En '^[^#]*(^|[^A-Za-z0-9_.-])/(etc|tmp)(/|[[:space:]"]|$)')
if [ -n "$real" ]; then
    echo "FAIL  the helper copy still names a real /etc or /tmp on an executable line:"
    printf '%s\n' "$real" | head -3
    exit 1
fi

# ── PATH shims: log, then do the real work ─────────────────────────────────
REAL_INSTALL=$(command -v install) || { echo "FAIL  no install(1)"; exit 1; }
REAL_MV=$(command -v mv) || { echo "FAIL  no mv(1)"; exit 1; }
cat > "$BIN/install" <<SHIM
#!/bin/bash
printf 'install %s\n' "\$*" >> "$CALLS"
args=()
while [ \$# -gt 0 ]; do
  case "\$1" in -o|-g) shift 2 ;; *) args+=("\$1"); shift ;; esac
done
exec "$REAL_INSTALL" "\${args[@]}"
SHIM
cat > "$BIN/mv" <<SHIM
#!/bin/bash
printf 'mv %s\n' "\$*" >> "$CALLS"
exec "$REAL_MV" "\$@"
SHIM
cat > "$BIN/sync" <<SHIM
#!/bin/bash
printf 'sync %s\n' "\$*" >> "$CALLS"
exit 0
SHIM
chmod +x "$BIN/install" "$BIN/mv" "$BIN/sync"

inode() { stat -c %i "$1" 2>/dev/null; }
leftovers() { find "$SB_ETC" -mindepth 1 -maxdepth 1 ! -name "$(basename "$DST")" | head -5; }

# ── case 1-5: a valid write over an existing live file ─────────────────────
printf 'old: config\n' > "$DST"
before=$(inode "$DST")
SRC="$SB_TMP/sa02m-mqcfg-out.Ab12Cd"
printf 'devices: []\nnew: config\n' > "$SRC"
: > "$CALLS"
PATH="$BIN:$PATH" bash "$TOOL" "$SRC" > "$T/out" 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ "$(cat "$DST")" = "$(printf 'devices: []\nnew: config')" ]; then
    ok "1 exit 0, destination carries the new bytes"
else
    bad "1 rc=$rc, destination: $(head -c 80 "$DST" 2>/dev/null) / $(head -c 200 "$T/out")"
fi
after=$(inode "$DST")
if [ -n "$before" ] && [ -n "$after" ] && [ "$before" != "$after" ]; then
    ok "2 destination replaced by rename (inode $before -> $after)"
else
    bad "2 destination inode unchanged ($before -> $after): written by truncate-in-place"
fi
mv_line=$(grep -n '^mv ' "$CALLS" | head -1)
if [ -z "$mv_line" ]; then
    bad "3 no mv in the call log — nothing renamed the tmp over the destination"
else
    mv_n=${mv_line%%:*}
    tmp=$(printf '%s\n' "${mv_line#*:}" | awk '{print $(NF-1)}')
    sync_n=$(grep -nF "sync -- $tmp" "$CALLS" | head -1 | cut -d: -f1)
    if [ "$(dirname "$tmp")" != "$SB_ETC" ]; then
        bad "3 tmp $tmp is not in the destination directory (cross-filesystem rename is not atomic)"
    elif [ -n "$sync_n" ] && [ "$sync_n" -lt "$mv_n" ]; then
        ok "3 tmp fsynced (call $sync_n) before the rename (call $mv_n), same directory"
    else
        bad "3 no 'sync -- $tmp' before the mv: $(tr '\n' '|' < "$CALLS")"
    fi
fi
left=$(leftovers)
if [ -z "$left" ]; then ok "4 no tmp left in the destination directory"; else bad "4 leftover: $left"; fi
mode=$(stat -c %a "$DST" 2>/dev/null)
case "$(uname -s)" in
    Linux) if [ "$mode" = 660 ]; then ok "5 mode 0660"; else bad "5 mode $mode, want 660"; fi ;;
    *) quality_skip "mqtt-config-apply-atomic case 5: POSIX mode not observable on $(uname -s)" ;;
esac

# ── case 6: a refused source touches nothing ───────────────────────────────
snap=$(cat "$DST")
BAD="$SB_TMP/not-ours.yaml"
printf 'evil: 1\n' > "$BAD"
PATH="$BIN:$PATH" bash "$TOOL" "$BAD" > "$T/out6" 2>&1
rc=$?
if [ "$rc" -eq 2 ] && [ "$(cat "$DST")" = "$snap" ] && [ -z "$(leftovers)" ]; then
    ok "6 refused source: exit 2, destination unchanged, no tmp"
else
    bad "6 rc=$rc, destination changed or tmp left: $(leftovers)"
fi

if [ "$fails" -eq 0 ]; then
    echo "mqtt-config-apply-atomic: ALL OK"
    exit 0
fi
echo "mqtt-config-apply-atomic: $fails FAILURE(S)"
exit 1
