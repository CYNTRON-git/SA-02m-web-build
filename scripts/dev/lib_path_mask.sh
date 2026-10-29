#!/bin/bash
# lib_path_mask.sh — sourced by scripts/dev harnesses; defines path_without.
#
# path_without MASKDIR CMD... — print $PATH with every directory that carries
# one of CMD replaced by a symlink mirror of itself (under MASKDIR, the
# caller's sandbox) minus those names. A harness that shims CMD first on PATH
# and then removes the shim to play «CMD is not installed» is otherwise judged
# by the HOST: the CI runner and the board ship util-linux fsfreeze and ntfs-3g,
# so `command -v` found the real binary there — a RED on Linux only, and on a
# root host a real `fsfreeze -f /`. With the mask the harness alone decides.
# Directories without CMD pass through untouched, so on a host lacking CMD the
# result is $PATH itself. On MSYS/git-bash a `CMD.exe` IS `CMD` (stat and
# command lookup both fall back to the .exe), so the mirror drops the Windows
# .exe twin as well — a mirror that kept `python3.exe` left `command -v
# python3` resolving through it (2026-10-28). MASKDIR is single-use: a second
# call into the same MASKDIR that needs a mirror fails (non-zero, empty output)
# rather than reuse it.
path_without() {
    local maskdir="$1" d c out="" n=0 hit
    shift
    local -a dirs
    IFS=: read -r -a dirs <<< "$PATH"
    for d in "${dirs[@]}"; do
        [ -n "$d" ] || continue
        hit=0
        for c in "$@"; do
            if [ -e "$d/$c" ] || [ -L "$d/$c" ]; then hit=1; break; fi
        done
        if [ "$hit" = 1 ]; then
            n=$((n + 1))
            mkdir -p -- "$maskdir" || return 1
            # Never reuse a slot: a mirror an earlier call built for another CMD
            # set still links that CMD, and ln below cannot replace an existing
            # link, so the host binary would stay visible (fail open). A taken
            # slot is refused (non-zero, empty output), never reused.
            mkdir -- "$maskdir/$n" 2>/dev/null || { echo "path_without: $maskdir/$n exists — refusing to reuse a mirror (one fresh MASKDIR per call)" >&2; return 1; }
            local -a skip=()
            local abs
            for c in "$@"; do skip+=( ! -name "$c" ! -name "$c.exe" ); done
            # Absolute, so a relative PATH entry's mirror links do not dangle.
            # CDPATH emptied for this cd: a relative entry would otherwise be
            # looked up in CDPATH first (mirroring the wrong directory) and cd
            # would print that directory into $abs.
            abs=$(CDPATH='' cd -- "$d" && pwd -P) || return 1
            find "$abs" -mindepth 1 -maxdepth 1 "${skip[@]}" \
                -exec ln -s -t "$maskdir/$n" {} + 2>/dev/null
            d="$maskdir/$n"
        fi
        out="${out:+$out:}$d"
    done
    printf '%s' "$out"
}
