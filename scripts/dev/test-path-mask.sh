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
#   P8 a MASKDIR reused for a different CMD fails closed: the first call's
#      mirror still links the second CMD and ln cannot replace an existing link,
#      so reusing the slot left the host binary visible. The second call must
#      refuse (non-zero, empty output) or return a PATH where that CMD is hidden.
#   P9 the Windows .exe twin: a dir carrying CMD and CMD.exe is mirrored with
#      NEITHER (host-independent, RED on the unfixed lib on Linux too); P9b the
#      .exe-only dir — mirrored without the twin where stat falls back to .exe
#      (MSYS), passed through verbatim elsewhere; CMD never resolves.
#
# Comment-mutation: the lib's load-bearing lines (the mirror substitution, the
# single-use slot creation and the absolute-dir resolution) are cased in
# comment-mutation-proof under this row's id, path-mask-lib — commenting any of
# them out turns P1 RED.
# RED, observed 2026-09-30 against the pre-fix lib (43be80d7, PATH_MASK_LIB=<git
# show copy>): P7 FAILs — under CDPATH the bare `cd` resolved the decoy and
# printed it into the captured dir, so the mirror came out empty; with only
# `>/dev/null` added P7 still FAILs, the mirror now built from the decoy.
# RED, observed 2026-10-14 against the lib before the single-use slot (mkdir -p
# on the mirror slot): P8 FAILs — the second call returned 0 and the CMD it was
# asked to hide resolved through the first call's mirror.
# RED, observed 2026-10-28 (git-bash) against the lib before the .exe-twin skip:
# P9 FAILs (mirror/CMD.exe PRESENT, CMD VISIBLE through the mirror) and P9b
# FAILs (CMD VISIBLE); the symptom it came from: fix-eth-link-cycle's python3
# mask assertion RED with a real Python312 first on PATH.
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
skips=0; skipped=""
skip() { printf 'SKIP  %s
' "$1"; skips=$((skips + 1)); skipped="${skipped:+$skipped; }$1"; }
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
    skip "P5 this host cannot create a dangling symlink (git-bash without native symlinks)"
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

# ── P8 ──
mkcmd "$T/host8" "$C1" host8-cmd1
mkcmd "$T/host8" "$C2" host8-cmd2
mkcmd "$T/host8" "$SIB" host8-sibling
IN="$T/host8:$REAL_PATH"
if resolves "$IN" "$C2"; then
    (PATH="$IN"; path_without "$T/m8" "$C1") >/dev/null 2>&1
    OUT=$(PATH="$IN"; path_without "$T/m8" "$C2" 2>/dev/null); rc=$?
    if [ "$rc" != 0 ] && [ -z "$OUT" ]; then
        ok "P8 a reused MASKDIR is refused (non-zero, empty output)"
    elif [ "$rc" = 0 ] && ! resolves "$OUT" "$C2" && [ "$(runs "$OUT" "$SIB")" = host8-sibling ]; then
        ok "P8 a reused MASKDIR still hides the second CMD"
    else
        bad "P8 reused MASKDIR failed open: rc=$rc, $C2 $(resolves "$OUT" "$C2" && echo "VISIBLE ($(PATH="$OUT"; command -v "$C2"))" || echo hidden), output '${OUT:0:80}'"
    fi
else
    bad "P8 fixture CMD does not resolve on the UNMASKED path — the reuse case would be vacuous"
fi

# ── P9 ──
# The Windows .exe twin. On MSYS/git-bash `[ -e dir/CMD ]` and `command -v CMD`
# both fall back to `CMD.exe`, so a mirror built with only `! -name CMD` still
# carried `python3.exe` and the masked python3 resolved through it
# (2026-10-28, a real Python312 first on PATH). The fixture carries BOTH names
# so the dir is a hit on every host: the no-.exe-in-mirror assertion is then
# host-independent and RED on the unfixed lib on Linux too, where `command -v`
# would never resolve the .exe and could not see the hole.
mkcmd "$T/host9" "$C1" host9-cmd1
mkcmd "$T/host9" "$C1.exe" host9-exe
mkcmd "$T/host9" "$SIB" host9-sibling
IN="$T/host9:$REAL_PATH"
OUT=$(PATH="$IN"; path_without "$T/m9" "$C1"); rc=$?
IFS=: read -r -a o <<< "$OUT"
if [ "$rc" = 0 ] && [ "${o[0]}" != "$T/host9" ] && [ ! -e "${o[0]}/$C1.exe" ] && [ ! -L "${o[0]}/$C1.exe" ] \
   && ! resolves "$OUT" "$C1" && [ "$(runs "$OUT" "$SIB")" = host9-sibling ]; then
    ok "P9 the .exe twin of CMD is dropped from the mirror too (sibling kept)"
else
    bad "P9 .exe twin leaked: rc=$rc, mirror/$C1.exe $([ -e "${o[0]}/$C1.exe" ] && echo PRESENT || echo absent), $C1 $(resolves "$OUT" "$C1" && echo "VISIBLE ($(PATH="$OUT"; command -v "$C1"))" || echo hidden)"
fi
# P9b — the real shape (python3.exe with no `python3`): a hit only where the
# shell's stat falls back to .exe (MSYS); elsewhere the dir is not a hit and
# passes through verbatim, and CMD does not resolve either way.
mkcmd "$T/host9b" "$C1.exe" host9b-exe
IN="$T/host9b:$REAL_PATH"
OUT=$(PATH="$IN"; path_without "$T/m9b" "$C1"); rc=$?
IFS=: read -r -a o <<< "$OUT"
if [ "$rc" != 0 ] || resolves "$OUT" "$C1"; then
    bad "P9b .exe-only dir: rc=$rc, $C1 $(resolves "$OUT" "$C1" && echo "VISIBLE ($(PATH="$OUT"; command -v "$C1"))" || echo hidden)"
elif [ -e "$T/host9b/$C1" ]; then   # MSYS: stat sees CMD through the .exe → the dir must be mirrored without it
    if [ "${o[0]}" != "$T/host9b" ] && [ ! -e "${o[0]}/$C1.exe" ]; then ok "P9b (.exe fallback host) the .exe-only dir is mirrored without the twin"
    else bad "P9b (.exe fallback host) .exe-only dir not masked: entry '${o[0]}'"; fi
else                                 # Linux: no fallback, nothing to hide — verbatim
    [ "${o[0]}" = "$T/host9b" ] && ok "P9b (no .exe fallback on this host) the .exe-only dir passes through verbatim, CMD unresolvable" \
                                || bad "P9b (no .exe fallback) dir without CMD was rewritten: '${o[0]}'"
fi

echo "---"
# A skip is reported as a skip (quality-gate-rigor.md): never «all checks passed»
# when a case did not run here.
if [ "$fails" != 0 ]; then echo "path-mask-lib: $fails check(s) FAILED${skipped:+, $skips skipped ($skipped)}"
elif [ "$skips" != 0 ]; then echo "path-mask-lib: no check failed, $skips skipped ($skipped)"
else echo "path-mask-lib: all checks passed"; fi
exit $(( fails > 0 ))  # never the count: 77 reads SKIP, 256 reads PASS (run.mjs SKIP_EXIT)
