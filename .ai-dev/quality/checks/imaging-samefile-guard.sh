#!/usr/bin/env bash
# Static gate: tools/imaging/make-image.sh must not `cp`/`rm` a file onto/of itself
# in the KEEP_RAW_IMG publish path. FINAL_IMG_WORK and FINAL_IMG_KEEP resolve to the
# SAME $WORK/<name>.img path, and after a keep/rescue reassignment FINAL_IMG can too —
# an unguarded `cp` errors "are the same file" (aborting under set -e, as observed on
# the 2026-08-20 golden capture: the .img.xz published but the raw .img cp crashed),
# and an unguarded trailing `rm "$FINAL_IMG_WORK"` would delete the kept raw image.
# Asserts the three -ef guards are present.
set -u
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"
MK="$HERE/tools/imaging/make-image.sh"
fails=0
ok(){ printf '  ok    %s\n' "$1"; }
bad(){ printf '  FAIL  %s\n' "$1"; fails=$((fails+1)); }
[ -r "$MK" ] || { echo "imaging-samefile-guard: cannot read $MK"; exit 1; }

# (a) safe_publish_to_out early-returns when src -ef dst
fn="$(sed -n '/^safe_publish_to_out() {/,/^}/p' "$MK")"
printf '%s\n' "$fn" | grep -qE '\[ "\$src" -ef "\$dst" \]' \
    && printf '%s\n' "$fn" | grep -A2 '\-ef "\$dst"' | grep -qw return \
    && ok "(a) safe_publish_to_out guards src -ef dst (early return)" \
    || bad "(a) safe_publish_to_out lacks the src -ef dst guard"

# (b) the KEEP_RAW_IMG raw-copy is guarded against copying onto itself
grep -qE '\[ "\$FINAL_IMG_WORK" -ef "\$FINAL_IMG_KEEP" \] \|\| cp ' "$MK" \
    && ok "(b) FINAL_IMG_WORK->FINAL_IMG_KEEP cp guarded by -ef" \
    || bad "(b) the keep-raw self-copy is not -ef guarded"

# (c) the trailing rm of FINAL_IMG_WORK is guarded so it never deletes the kept image
grep -qE '\[ "\$FINAL_IMG_WORK" -ef "\$FINAL_IMG" \] \|\| rm -f "\$FINAL_IMG_WORK"' "$MK" \
    && ok "(c) trailing rm of FINAL_IMG_WORK guarded by -ef FINAL_IMG" \
    || bad "(c) the trailing FINAL_IMG_WORK rm is not -ef guarded"

# (d) non-vacuity: the KEEP_RAW_IMG publish block still exists
grep -qE 'if \[ "\$KEEP_RAW_IMG" -eq 1 \]; then' "$MK" \
    && ok "(d) KEEP_RAW_IMG publish block present (anchor)" \
    || bad "(d) KEEP_RAW_IMG block gone — guards may be dead"

echo
[ "$fails" -eq 0 ] && { echo "imaging-samefile-guard: ALL OK"; exit 0; }
echo "imaging-samefile-guard: $fails FAILURE(S)"; exit 1
