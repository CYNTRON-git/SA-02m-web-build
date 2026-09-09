#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness — the helper's guarantee is asserted by RUNNING scripts/lib.sh sa02m_atomic_install in a sandbox with a failing `mv` / a torn `install` shim (files observed, not lines grepped), and the site pin delegates to the codemod's own `--check` sweep plus a drive-to-failure on a scratch copy; commenting a shipped install site out removes a call, which the codemod's non-vacuity floor (>= MIN_CONVERTED converted sites) and the module's own behaviour catch, not a needle grep here.
# test-install-atomic.sh — regression harness for the atomic live-path write
# (scripts/lib.sh sa02m_atomic_install) and for the rule that every live-path
# `install -m` site in scripts/*.sh goes through it. Quality row
# `install-atomic`. 8D bench-136 reset (.ai-dev/8d/bench-136-reset.md, D5 step A).
#
# Why this exists: a hard reset at 22:15 on bench 1.136 left
# /etc/systemd/system/sa02m-flasher.service as a 0-byte regular file — systemd
# reads an empty unit as MASKED — because `install -m` truncates the live path
# first and fills it later, and the writeback lagged minutes behind (ext4
# commit=600). The helper lands a file as tmp + fsync + rename-over, so any
# instant of a reset leaves the destination OLD or NEW, never empty.
#
# Method: source the SHIPPED lib.sh; run the helper against a scratch tree,
# once for real and twice under PATH-first shims that model the two failure
# instants (rename never happens: `mv` fails; the copy is torn: `install`
# writes half and dies). Assert the destination bytes and the absence of a
# leftover tmp. The site rule is measured by `python3
# scripts/dev/codemod-install-atomic.py --check` (its sweep is the one home of
# "which install sites are live-path"), floored by a converted-site count so an
# empty sweep cannot pass, and driven to failure on a scratch copy with one
# site reverted.
#
# Drive-to-failure: SVC_HELPERS_LIB=<(git show 1.0.6.41~N:scripts/lib.sh) has
# no sa02m_atomic_install — the source guard fails; revert one codemod site —
# case 7 reports it.
#
# Run: bash scripts/dev/test-install-atomic.sh   (bash + coreutils + python3)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
ROOT=$PWD
LIB=${SVC_HELPERS_LIB:-scripts/lib.sh}
CODEMOD=scripts/dev/codemod-install-atomic.py
MIN_CONVERTED=100   # 131 sites on 1.0.6.41; a sweep that sees fewer than this is broken, not clean
MIN_FILES=10

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

export LOG_FILE="$T/install.log"
# shellcheck disable=SC1090
source "$LIB" || { echo "FAIL  cannot source $LIB"; exit 1; }
declare -F sa02m_atomic_install >/dev/null \
    || { bad "$LIB does not define sa02m_atomic_install() — the atomic helper is missing"; echo "install-atomic: $fails FAILURE(S)"; exit 1; }
LOGCAP=""
log() { LOGCAP="${LOGCAP}[$1] ${2:-}"$'\n'; }

mkdir -p "$T/live" "$T/src"
printf 'OLD unit body\n' > "$T/live/x.service"
printf 'NEW unit body, longer than the old one\n' > "$T/src/x.service"

no_tmp_left() { ! ls "$T/live"/*.sa02m-tmp.* >/dev/null 2>&1; }

echo "── 1. success path ──"
sa02m_atomic_install -m 0644 "$T/src/x.service" "$T/live/x.service"; rc=$?
if [ "$rc" -eq 0 ] && cmp -s "$T/src/x.service" "$T/live/x.service" && no_tmp_left; then
    ok "1 destination carries the NEW bytes, rc=0, no tmp left behind"
else
    bad "1 success path: rc=$rc content=$(cat "$T/live/x.service" 2>/dev/null | head -c 40) tmp-left=$(ls "$T/live" | tr '\n' ' ')"
fi

echo "── 2. rename never happens (crash between tmp write and rename) ──"
printf 'OLD unit body\n' > "$T/live/x.service"
mkdir -p "$T/bin-nomv"
printf '#!/bin/bash\nexit 1\n' > "$T/bin-nomv/mv"; chmod +x "$T/bin-nomv/mv"
rc=0
( PATH="$T/bin-nomv:$PATH"; sa02m_atomic_install -m 0644 "$T/src/x.service" "$T/live/x.service" ) || rc=$?
if [ "$rc" -ne 0 ] && [ "$(cat "$T/live/x.service")" = "OLD unit body" ] && no_tmp_left; then
    ok "2 failed rename: destination still OLD, rc=$rc, tmp cleaned"
else
    bad "2 failed rename: rc=$rc content='$(cat "$T/live/x.service")' dir='$(ls "$T/live" | tr '\n' ' ')'"
fi

echo "── 3. torn copy (install writes half of the source and dies) ──"
printf 'OLD unit body\n' > "$T/live/x.service"
mkdir -p "$T/bin-torn"
cat > "$T/bin-torn/install" <<'SHIM'
#!/bin/bash
# model of a copy killed mid-body: the LAST argument is the tmp target
for last; do :; done
src=""
for a in "$@"; do [ -f "$a" ] && src=$a; done
head -c 8 "$src" > "$last"
exit 1
SHIM
chmod +x "$T/bin-torn/install"
rc=0
( PATH="$T/bin-torn:$PATH"; sa02m_atomic_install -m 0644 "$T/src/x.service" "$T/live/x.service" ) || rc=$?
if [ "$rc" -ne 0 ] && [ "$(cat "$T/live/x.service")" = "OLD unit body" ] && no_tmp_left; then
    ok "3 torn copy: destination still OLD (never the half-written body), rc=$rc, tmp cleaned"
else
    bad "3 torn copy: rc=$rc content='$(cat "$T/live/x.service")' dir='$(ls "$T/live" | tr '\n' ' ')'"
fi

echo "── 4. destination given as a directory (the 06-alice.sh form) ──"
mkdir -p "$T/live/dir"
sa02m_atomic_install -m 0644 "$T/src/x.service" "$T/live/dir/"; rc=$?
if [ "$rc" -eq 0 ] && cmp -s "$T/src/x.service" "$T/live/dir/x.service" && ! ls "$T/live/dir"/*.sa02m-tmp.* >/dev/null 2>&1; then
    ok "4 DST/ lands DST/basename(SRC), no tmp left"
else
    bad "4 directory destination: rc=$rc dir='$(ls "$T/live/dir" | tr '\n' ' ')'"
fi

echo "── 5. a stale tmp from an earlier crash is cleaned on the next run ──"
printf 'garbage' > "$T/live/x.service.sa02m-tmp.999"
sa02m_atomic_install -m 0644 "$T/src/x.service" "$T/live/x.service"; rc=$?
if [ "$rc" -eq 0 ] && no_tmp_left; then
    ok "5 stale x.service.sa02m-tmp.999 removed by the next install"
else
    bad "5 stale tmp: rc=$rc dir='$(ls "$T/live" | tr '\n' ' ')'"
fi

echo "── 6. argument floor ──"
rc=0; sa02m_atomic_install -m 0644 "$T/src/x.service" >/dev/null 2>&1 || rc=$?
[ "$rc" -ne 0 ] && ok "6a missing DST is refused (rc=$rc)" || bad "6a helper accepted a single argument"
rc=0; sa02m_atomic_install -d -m 0755 "$T/live/newdir" >/dev/null 2>&1 || rc=$?
[ "$rc" -ne 0 ] && [ ! -d "$T/live/newdir" ] && ok "6b -d is refused (a directory is not an atomic file write)" || bad "6b -d accepted"

echo "── 7. every live-path install site in scripts/*.sh uses the helper ──"
if ! command -v python3 >/dev/null 2>&1; then
    bad "7 python3 not found — the site sweep did NOT run (a skip is not a pass)"
else
    # Capture THEN transform: `python3 … | tr` would put tr's status in $? and
    # the case could never go RED (measured 2026-09-09 — reverting the flasher
    # unit site left 7a green; quality-gate-rigor.md shape (f), the pipeline
    # half). rc must come from the sweep itself.
    out=$(python3 "$CODEMOD" --check 2>&1); rc=$?
    out=$(printf '%s\n' "$out" | tr -d '\r')
    if [ "$rc" -eq 0 ]; then
        ok "7a codemod --check: no raw live-path install -m site"
    else
        bad "7a codemod --check rc=$rc:"; printf '        %s\n' "$out" | head -20
    fi
    listed=$(python3 "$CODEMOD" --list --all 2>/dev/null | tr -d '\r')
    n=$(printf '%s\n' "$listed" | grep -c '\[converted\]')
    nf=$(printf '%s\n' "$listed" | grep '\[converted\]' | cut -d: -f1 | sort -u | wc -l)
    if [ "$n" -ge "$MIN_CONVERTED" ] && [ "$nf" -ge "$MIN_FILES" ]; then
        ok "7b non-vacuity: the sweep sees $n converted sites across $nf files (floor $MIN_CONVERTED / $MIN_FILES)"
    else
        bad "7b non-vacuity: only $n converted sites in $nf files — the sweep stopped seeing the tree"
    fi
    # drive-to-failure: revert one converted site on a scratch copy → --check must go RED
    mkdir -p "$T/scratch/scripts"
    cp scripts/*.sh "$T/scratch/scripts/"
    first=$(printf '%s\n' "$listed" | grep '\[converted\]' | head -1)
    f=${first%%:*}; f=${f//\\//}; ln=${first#*:}; ln=${ln%%:*}
    if [ -n "$f" ] && [ -n "$ln" ] && sed -i "${ln}s/sa02m_atomic_install -m /install -m /" "$T/scratch/$f" \
       && ! (cd "$T/scratch" && python3 "$ROOT/$CODEMOD" --check >/dev/null 2>&1); then
        ok "7c drive-to-failure: reverting $f:$ln on a scratch copy turns --check RED"
    else
        bad "7c drive-to-failure: --check stayed GREEN with $f:$ln reverted — the sweep is hollow"
    fi
fi

echo ""
if [ "$fails" -eq 0 ]; then
    echo "install-atomic: ALL OK"
    exit 0
fi
echo "install-atomic: $fails FAILURE(S)"
exit 1
