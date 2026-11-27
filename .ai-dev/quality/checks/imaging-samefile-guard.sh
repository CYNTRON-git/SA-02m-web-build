#!/usr/bin/env bash
# Static gate: tools/imaging/make-image.sh must not `cp`/`rm` a file onto/of itself
# in the KEEP_RAW_IMG publish path. FINAL_IMG_WORK and FINAL_IMG_KEEP resolve to the
# SAME $WORK/<name>.img path, and after a keep/rescue reassignment FINAL_IMG can too —
# an unguarded `cp` errors "are the same file" (aborting under set -e, as observed on
# the 2026-08-20 golden capture: the .img.xz published but the raw .img cp crashed),
# and an unguarded trailing `rm "$FINAL_IMG_WORK"` would delete the kept raw image.
# Asserts the three -ef guards and exercises failed-copy publication in a
# private local directory. The early refusal check runs without SSH on PATH.
# Every pin reads COMMENT-STRIPPED text (lib_check.sh): a `#` in front of a
# guard line leaves the needle in place, so an unstripped grep would report the
# guard present while the publish path was crashing again on `cp` onto itself.
set -u
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"
MK="$HERE/tools/imaging/make-image.sh"
# shellcheck source=/dev/null
. "$(cd "$(dirname "$0")" && pwd)/lib_check.sh" || { echo "imaging-samefile-guard: cannot source lib_check.sh"; exit 1; }
fails=0
ok(){ printf '  ok    %s\n' "$1"; }
bad(){ printf '  FAIL  %s\n' "$1"; fails=$((fails+1)); }
[ -r "$MK" ] || { echo "imaging-samefile-guard: cannot read $MK"; exit 1; }

# (a) safe_publish_to_out early-returns when src -ef dst
fn="$(sed -n '/^safe_publish_to_out() {/,/^}/p' "$MK" | strip_comments)"
grep -q '[^[:space:]]' <<<"$fn" || { echo "imaging-samefile-guard: safe_publish_to_out() not found (or wholly commented out)"; exit 1; }
text_matches "$fn" '\[ "\$src" -ef "\$dst" \]' \
    && grep -A2 '\-ef "\$dst"' <<<"$fn" | grep -qw return \
    && ok "(a) safe_publish_to_out guards src -ef dst (early return)" \
    || bad "(a) safe_publish_to_out lacks the src -ef dst guard"

# (b) the KEEP_RAW_IMG raw-copy is guarded against copying onto itself
stripped_matches "$MK" '\[ "\$FINAL_IMG_WORK" -ef "\$FINAL_IMG_KEEP" \] \|\| cp ' \
    && ok "(b) FINAL_IMG_WORK->FINAL_IMG_KEEP cp guarded by -ef" \
    || bad "(b) the keep-raw self-copy is not -ef guarded"

# (c) the trailing rm of FINAL_IMG_WORK is guarded so it never deletes the kept image
stripped_matches "$MK" '\[ "\$FINAL_IMG_WORK" -ef "\$FINAL_IMG" \] \|\| rm -f "\$FINAL_IMG_WORK"' \
    && ok "(c) trailing rm of FINAL_IMG_WORK guarded by -ef FINAL_IMG" \
    || bad "(c) the trailing FINAL_IMG_WORK rm is not -ef guarded"

# (d) non-vacuity: the KEEP_RAW_IMG publish block still exists
stripped_matches "$MK" 'if \[ "\$KEEP_RAW_IMG" -eq 1 \]; then' \
    && ok "(d) KEEP_RAW_IMG publish block present (anchor)" \
    || bad "(d) KEEP_RAW_IMG block gone — guards may be dead"

# Publication is exercised under the caller's conditional, where set -e does
# not make a failed copy fatal. The failures are injected on real local files.
scratch=$(mktemp -d) || exit 1
trap 'rm -rf -- "$scratch"' EXIT
mkdir "$scratch/work" "$scratch/out" || exit 1
src="$scratch/work/board.img.xz"
dst="$scratch/out/board.img.xz"
printf 'complete image\n' > "$src" || exit 1
printf 'checksum\n' > "$src.sha256" || exit 1
printf 'manifest\n' > "$scratch/work/board.manifest.json" || exit 1
out_dir_has_space() { return 0; }
log() { :; }
eval "$fn"
if (
    cp() {
        if [ "$2" = "$src" ]; then printf 'partial' > "${@: -1}"; return 1; fi
        command cp "$@"
    }
    if safe_publish_to_out "$src" "$dst" "$src.sha256" "$scratch/work/board.manifest.json"; then exit 1; fi
    [ ! -e "$dst" ] && [ ! -e "$dst.sha256" ] \
        && [ ! -e "$scratch/out/board.manifest.json" ] \
        && [ "$(cat "$src")" = 'complete image' ] \
        && [ "$(cat "$src.sha256")" = 'checksum' ]
); then
    ok "(e) a partial copy leaves the complete WORK bundle and no final output"
else
    bad "(e) a partial copy was published or lost the WORK bundle"
fi

if (
    cp() {
        if [ "$2" = "$src" ]; then printf 'tampered image\n' > "${@: -1}"; return 0; fi
        command cp "$@"
    }
    if safe_publish_to_out "$src" "$dst" "$src.sha256"; then exit 1; fi
    [ ! -e "$dst" ] && [ ! -e "$dst.sha256" ]
); then
    ok "(f) a successful copy with different bytes is refused"
else
    bad "(f) a successful but corrupt copy was accepted"
fi

if (
    mv() {
        if [ "${@: -1}" = "$dst" ]; then printf 'old image\n' > "$dst" || return 1; fi
        command mv "$@"
    }
    if safe_publish_to_out "$src" "$dst" "$src.sha256" "$scratch/work/board.manifest.json"; then exit 1; fi
    [ "$(cat "$dst")" = 'old image' ] && [ ! -e "$dst.sha256" ] \
        && [ ! -e "$scratch/out/board.manifest.json" ]
); then
    ok "(g) a late collision preserves the prior image and rolls back new sidecars"
else
    bad "(g) a late collision overwrote the image or stranded its new sidecars"
fi

if ! safe_publish_to_out "$src" "$dst" "$src.sha256" \
    && [ "$(cat "$dst")" = 'old image' ] && [ ! -e "$dst.sha256" ]; then
    ok "(h) a retry keeps prior output without adding mismatched metadata"
else
    bad "(h) a retry changed prior output or its metadata"
fi

mkdir "$scratch/fresh" || exit 1
if safe_publish_to_out "$src" "$scratch/fresh/board.img.xz" "$src.sha256" "$scratch/work/board.manifest.json" \
    && cmp -s "$src" "$scratch/fresh/board.img.xz" && cmp -s "$src.sha256" "$scratch/fresh/board.img.xz.sha256" \
    && cmp -s "$scratch/work/board.manifest.json" "$scratch/fresh/board.manifest.json"; then
    ok "(i) successful publication preserves image and sidecar bytes"
else
    bad "(i) verified image and sidecars did not reach output"
fi

mkdir "$scratch/offline-bin" || exit 1
for utility in date dirname mkdir mktemp rm rmdir; do
    executable=$(command -v "$utility") && ln -s "$executable" "$scratch/offline-bin/$utility" || exit 1
done
: > "$scratch/empty"
if refusal=$(SA02M_DEVICE_ENV="$scratch/empty" TMPDIR="$scratch" PATH="$scratch/offline-bin" \
    "$BASH" "$MK" --name board --key "$scratch/empty" --out-dir "$scratch/out" 2>&1); then
    bad "(j) the capture accepted a collision before checking the donor"
elif [[ "$refusal" == *"артефакт уже существует: $dst"* ]] \
    && [ "$(cat "$dst")" = 'old image' ] && [ ! -e "$scratch/out/.board.capture-lock" ]; then
    ok "(j) the capture refuses an existing image before accessing SSH"
else
    bad "(j) expected early collision refusal, got $refusal"
fi

if (
    eval "$(sed -n '/^rescue_work_artifacts() {/,/^}$/p; /^rescue_remaining_artifacts() {/,/^}$/p' "$MK")"
    WORK="$scratch/work"
    SHRUNK_XZ="$src"
    FINAL_IMG_KEEP="$WORK/board.img"
    FINAL_IMG="$FINAL_IMG_KEEP"
    KEEP_RAW_IMG=1
    printf 'raw image\n' > "$FINAL_IMG" || exit 1
    rescue_remaining_artifacts || exit 1
    [[ "$SHRUNK_XZ" == "$scratch"/sa02m-image-rescue-*/board.img.xz ]] \
        && [ "$(cat "$SHRUNK_XZ")" = 'complete image' ] \
        && [ "$(cat "$SHRUNK_XZ.sha256")" = 'checksum' ] \
        && [ "$(cat "${SHRUNK_XZ%.img.xz}.manifest.json")" = 'manifest' ] \
        && [ "$FINAL_IMG" = "${SHRUNK_XZ%.xz}" ] \
        && [ "$(cat "$FINAL_IMG")" = 'raw image' ]
); then
    ok "(k) rescue retains the complete image set and updates reported paths"
else
    bad "(k) rescue lost a finished artifact or its final path"
fi

mkdir -p "$scratch/failure/work" "$scratch/failure/output-lock" || exit 1
if failed_rescue=$(
    eval "$(sed -n '/^rescue_work_artifacts() {/,/^}$/p; /^rescue_remaining_artifacts() {/,/^}$/p; /^cleanup_work() {/,/^}$/p' "$MK")"
    WORK="$scratch/failure/work"
    OUT_LOCK="$scratch/failure/output-lock"
    RESCUE_INCOMPLETE=0
    SHRUNK_XZ="$WORK/board.img.xz"
    FINAL_IMG_KEEP="$WORK/board.img"
    KEEP_RAW_IMG=0
    printf 'complete image\n' > "$SHRUNK_XZ" || exit 2
    printf 'checksum\n' > "$SHRUNK_XZ.sha256" || exit 2
    printf 'manifest\n' > "$WORK/board.manifest.json" || exit 2
    mv() {
        if [ "$2" = "$SHRUNK_XZ.sha256" ]; then return 1; fi
        command mv "$@"
    }
    log() { printf '%s\n' "$*"; }
    trap cleanup_work EXIT
    rescue_remaining_artifacts || exit 1
); then
    bad "(l) failed metadata move unexpectedly completed the rescue"
else
    failed_dirs=("$scratch/failure"/sa02m-image-rescue-*)
    if [ "${#failed_dirs[@]}" -eq 1 ] \
        && [ "$(cat "${failed_dirs[0]}/board.img.xz" 2>/dev/null)" = 'complete image' ] \
        && [ "$(cat "$scratch/failure/work/board.img.xz.sha256" 2>/dev/null)" = 'checksum' ] \
        && [ "$(cat "$scratch/failure/work/board.manifest.json" 2>/dev/null)" = 'manifest' ] \
        && [ ! -e "$scratch/failure/output-lock" ] \
        && [[ "$failed_rescue" == *"${failed_dirs[0]}"*"$scratch/failure/work"* ]]; then
        ok "(l) EXIT after failed rescue preserves both sides and releases the output lock"
    else
        bad "(l) EXIT repeated the failed rescue or discarded artifacts"
    fi
fi

echo
[ "$fails" -eq 0 ] && { echo "imaging-samefile-guard: ALL OK"; exit 0; }
echo "imaging-samefile-guard: $fails FAILURE(S)"; exit 1
