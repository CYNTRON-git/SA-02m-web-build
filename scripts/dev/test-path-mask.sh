#!/bin/bash
# test-path-mask.sh — self-test of scripts/dev/lib_path_mask.sh (path_without).
#
# Why this exists: path_without is what stands between a harness that plays
# «CMD is not installed» and the HOST's own CMD — on a root host the difference
# was a real `fsfreeze -f /` (test-firstboot-sb-csum.sh) and a real ntfs-3g
# mount path (test-storage-automount.sh). Those harnesses only prove that THEIR
# case went the right way on the host they ran on; a lib bug that leaves the
# host binary visible reads GREEN on every host without that binary (Windows,
# most dev boxes) and bites only where the binary exists. This row pins the
# lib's own contract on synthetic host dirs, so it is measured on every host.
#
# Cases (all against fixture dirs under $T; nothing outside $T is touched):
#   P1 a dir carrying CMD is mirrored without it: `command -v CMD` fails, a
#      sibling in the same dir still resolves AND runs (non-vacuity: CMD is
#      found on the unmasked PATH first).
#   P2 order and pass-through: dirs without CMD stay verbatim, the mirror takes
#      the masked dir's slot.
#   P3 a PATH carrying no CMD comes back byte-identical.
#   P4 several CMDs in one call, in different dirs.
#   P5 a dangling symlink named CMD is masked too.
#   P6 a relative PATH entry becomes an absolute mirror: its sibling still runs
#      after a cd elsewhere.
#   P7 CDPATH cannot redirect the mirror: a CDPATH holding a decoy dir of the
#      same relative name must not be mirrored instead (bash's cd searches
#      CDPATH before the cwd for a bare relative name, and prints the hit —
#      so `>/dev/null` alone would still mirror the decoy).
#
# Comment-mutation: the lib's load-bearing lines (the mirror substitution and
# the absolute-dir resolution) are cased in comment-mutation-proof under this
# row's id, path-mask-lib — commenting either out turns P1 RED.
# RED, observed 2026-09-30 against the pre-fix lib (43be80d7, PATH_MASK_LIB=<git
# show copy>): P7 FAILs — under CDPATH the bare `cd` resolved the decoy and
# printed it into the captured dir, so the mirror came out empty; with only
# `>/dev/null` added P7 still FAILs, the mirror now built from the decoy.
#
# Run: bash scripts/dev/test-path-mask.sh   (bash + coreutils + findutils)
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

LIB=${PATH_MASK_LIB:-scripts/dev/lib_path_mask.sh}   # PATH_MASK_LIB=<copy> drives another copy (the RED run)
[ -s "$LIB" ] || { echo "FAIL  $LIB missing or empty"; exit 1; }
# shellcheck source=scripts/dev/lib_path_mask.sh
. "$LIB" || { echo "FAIL  $LIB not loadable"; exit 1; }
declare -F path_without >/dev/null || { echo "FAIL  $LIB does not define path_without"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
# The host PATH with empty entries dropped (path_without drops them too), so the
# byte-identity checks below compare like with like on any host.
REAL_PATH=$(printf '%s' "$PATH" | tr -s ':' | sed 's/^://; s/:$//')
PATH="$REAL_PATH"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# mkcmd DIR NAME MARK — an executable that prints MARK.
mkcmd() { mkdir -p "$1"; printf '#!/bin/sh\necho %s\n' "$3" > "$1/$2"; chmod +x "$1/$2"; }
# resolves PATHVAL NAME — exit 0 if NAME resolves on PATHVAL (fresh lookup).
resolves() { ( PATH="$1"; hash -r; command -v "$2" >/dev/null 2>&1 ); }
# runs PATHVAL NAME — NAME's stdout when run from $T/elsewhere on PATHVAL.
runs() { ( PATH="$1"; hash -r; cd "$T/elsewhere" && "$2" 2>/dev/null ); }
mkdir -p "$T/elsewhere"

C1=sa02m_pm_cmd1; C2=sa02m_pm_cmd2; SIB=sa02m_pm_sib

# ── P1 ──
mkcmd "$T/host1" "$C1" host-cmd1
mkcmd "$T/host1" "$SIB" host1-sibling
IN="$T/host1:$REAL_PATH"
if resolves "$IN" "$C1"; then
    OUT=$(PATH="$IN"; path_without "$T/m1" "$C1") || bad "P1 path_without returned non-zero"
    if resolves "$OUT" "$C1"; then bad "P1 masked CMD still resolves: $(PATH="$OUT"; command -v "$C1")"
    else ok "P1 CMD in a host dir no longer resolves"; fi
    if [ "$(runs "$OUT" "$SIB")" = host1-sibling ]; then ok "P1 its sibling still resolves and runs from the mirror"
    else bad "P1 sibling lost with the masked dir (got '$(runs "$OUT" "$SIB")')"; fi
else
    bad "P1 fixture CMD does not resolve on the UNMASKED path — the mask case would be vacuous"
fi

# ── P2 ──
mkdir -p "$T/plainA" "$T/plainB"
IN="$T/plainA:$T/host1:$T/plainB:$REAL_PATH"
OUT=$(PATH="$IN"; path_without "$T/m2" "$C1")
IFS=: read -r -a o <<< "$OUT"
if [ "${o[0]}" = "$T/plainA" ] && [ "${o[2]}" = "$T/plainB" ] \
   && [ "${o[1]}" != "$T/host1" ] && [ "${o[1]#"$T/m2/"}" != "${o[1]}" ] \
   && [ "${OUT#"${o[0]}:${o[1]}:${o[2]}:"}" = "$REAL_PATH" ]; then
    ok "P2 pass-through dirs verbatim, the mirror in the masked dir's slot"
else
    bad "P2 order/pass-through broken: '$OUT'"
fi

# ── P3 ──
IN="$T/plainA:$T/plainB:$REAL_PATH"
OUT=$(PATH="$IN"; path_without "$T/m3" "$C1" "$C2")
[ "$OUT" = "$IN" ] && ok "P3 PATH without CMD returned byte-identical" || bad "P3 PATH changed with nothing to mask: '$OUT'"
[ -e "$T/m3" ] && bad "P3 created a mirror with nothing to mask" || ok "P3 no mirror created"

# ── P4 ──
mkcmd "$T/host2" "$C2" host-cmd2
IN="$T/host1:$T/host2:$REAL_PATH"
OUT=$(PATH="$IN"; path_without "$T/m4" "$C1" "$C2")
if ! resolves "$OUT" "$C1" && ! resolves "$OUT" "$C2" && [ "$(runs "$OUT" "$SIB")" = host1-sibling ]; then
    ok "P4 two CMDs in two dirs masked in one call, sibling kept"
else
    bad "P4 multi-CMD mask incomplete: C1 $(resolves "$OUT" "$C1" && echo visible || echo hidden), C2 $(resolves "$OUT" "$C2" && echo visible || echo hidden)"
fi

# ── P5 ──
mkdir -p "$T/host5"
mkcmd "$T/host5" "$SIB" host5-sibling
if ln -s "$T/nonexistent-target" "$T/host5/$C1" 2>/dev/null && [ -L "$T/host5/$C1" ]; then
    OUT=$(PATH="$T/host5:$REAL_PATH"; path_without "$T/m5" "$C1")
    IFS=: read -r -a o <<< "$OUT"
    if [ "${o[0]}" != "$T/host5" ] && [ ! -L "${o[0]}/$C1" ] && [ ! -e "${o[0]}/$C1" ]; then
        ok "P5 a dangling symlink named CMD is masked (its dir mirrored without it)"
    else
        bad "P5 dangling-symlink CMD left in place: '${o[0]}'"
    fi
else
    echo "SKIP  P5 this host cannot create a dangling symlink (git-bash without native symlinks)"
fi

# ── P6 ──
mkdir -p "$T/rel6/relbin"
mkcmd "$T/rel6/relbin" "$C1" rel-cmd1
mkcmd "$T/rel6/relbin" "$SIB" rel-sibling
OUT=$(cd "$T/rel6" && PATH="relbin:$REAL_PATH" && path_without "$T/m6" "$C1")
IFS=: read -r -a o <<< "$OUT"
case "${o[0]}" in
    /*) ok "P6 relative entry replaced by an absolute mirror (${o[0]#"$T/"})" ;;
    *)  bad "P6 relative entry kept relative: '${o[0]}'" ;;
esac
if [ "$(runs "$OUT" "$SIB")" = rel-sibling ] && ! resolves "$OUT" "$C1"; then
    ok "P6 the relative dir's sibling runs from elsewhere, its CMD stays hidden"
else
    bad "P6 relative mirror dangles or leaks CMD (sibling: '$(runs "$OUT" "$SIB")')"
fi

# ── P7 ──
mkdir -p "$T/rel7/relbin" "$T/decoy/relbin"
mkcmd "$T/rel7/relbin" "$C1" real-cmd1
mkcmd "$T/rel7/relbin" "$SIB" real-sibling
mkcmd "$T/decoy/relbin" "$C1" decoy-cmd1
mkcmd "$T/decoy/relbin" "$SIB" decoy-sibling
OUT=$(cd "$T/rel7" && export CDPATH="$T/decoy" && PATH="relbin:$REAL_PATH" && path_without "$T/m7" "$C1")
got=$(runs "$OUT" "$SIB")
if [ "$got" = real-sibling ] && ! resolves "$OUT" "$C1"; then ok "P7 CDPATH decoy not mirrored — the PATH entry's own dir is"
else bad "P7 mirror built from the wrong dir under CDPATH (sibling ran '${got:-nothing}', want real-sibling)"; fi

echo "---"
if [ "$fails" = 0 ]; then echo "path-mask-lib: all checks passed"; else echo "path-mask-lib: $fails check(s) FAILED"; fi
exit $(( fails > 0 ))  # never the count: 77 reads SKIP, 256 reads PASS (run.mjs SKIP_EXIT)
