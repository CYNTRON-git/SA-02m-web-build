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
# result is $PATH itself.
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
            mkdir -p "$maskdir/$n" || return 1
            local -a skip=()
            for c in "$@"; do skip+=( ! -name "$c" ); done
            find "$d" -mindepth 1 -maxdepth 1 "${skip[@]}" \
                -exec ln -s -t "$maskdir/$n" {} + 2>/dev/null
            d="$maskdir/$n"
        fi
        out="${out:+$out:}$d"
    done
    printf '%s' "$out"
}
