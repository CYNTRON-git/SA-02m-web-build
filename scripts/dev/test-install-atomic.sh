#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness — the helper's guarantee is asserted by RUNNING scripts/lib.sh sa02m_atomic_install in a sandbox with a failing `mv` / a torn `install` shim (files observed, not lines grepped), and the site pin delegates to the codemod's own `--check` sweep plus a drive-to-failure on a scratch copy; commenting a shipped install site out removes a call, which the codemod's non-vacuity floor (>= MIN_CONVERTED converted sites) and the module's own behaviour catch, not a needle grep here; section 8 holds the inverse for the OTA launcher (1.0.7.5: it delivers nothing itself) - the sweep must find NO raw write and the handoff line must be live, so commenting the handoff out is caught by 8a and a planted raw site by 8d, not by a needle grep here.
# test-install-atomic.sh — regression harness for the atomic live-path write
# (scripts/lib.sh sa02m_atomic_install) and for the rule that every live-path
# `install -m` site in scripts/*.sh AND install.sh goes through it. Quality row
# `install-atomic`. 8D bench-136 reset (docs/bugs/bench-136-reset.md, D5 step A).
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
# site reverted (case 7c) plus a planted raw unit write in install.sh (case 7d
# - install.sh carries zero install -m sites today, so 7c alone would leave the
# "and install.sh" half of the claim unmeasured). Cases 7e-7h (1.0.7.0) plant
# the three shapes the sweep used to miss — `cp` through a literal dir
# variable, /lib/systemd/system/, a `"$VAR"; }` one-line body — plus a
# negative control for an ambiguous variable. RED observed 2026-09-27 on
# 9c4355d (1.0.6.54) with the widened codemod: --check rc=1 naming
# scripts/05-cloud-agent.sh:57, :58, :113 and scripts/06-gateway.sh:93, while
# the pre-widening codemod reported «ok» over the same tree.
#
# Section 8 covers the OTA launcher — etc/sa02m-web-update-apply.sh, how a FIELD
# board updates itself. Until 1.0.7.4 it carried its own atomic_install_script()
# and seven live-path sites (its legacy rsync delivery); sections 8-9 extracted
# that helper, ran it under the same shims and floored the converted-call count
# (RED 2026-09-09 on the pre-fix launcher: 4 FAILURES; 8c/8d RED with tmp="$dst"
# spliced in). Since 1.0.7.5 (UPD-03) the launcher DELIVERS NOTHING: it clones
# the allow-listed source and exec's the shared runner (`exec "$RUNNER" apply`),
# which lands every file through its own atomic_install_file(); without a
# capable runner it refuses with E_BOOTSTRAP_REQUIRED before any write. The
# dead helper copy went with it (one home less), so the pin inverted: the
# launcher must hold NO raw install -m / rsync write and NO private helper,
# while the handoff line must still be there (delegation, not mere absence).
# Driven to failure on a scratch copy with one planted raw site (8d).
#
# Drive-to-failure: SVC_HELPERS_LIB=<(git show 1.0.6.41~N:scripts/lib.sh) has
# no sa02m_atomic_install — the source guard fails; revert one codemod site —
# case 7 reports it. Section 8 RED on the 1.0.7.4 launcher
# (WEB_UPDATE_LAUNCHER=<file of `git show 1.0.7.4:etc/sa02m-web-update-apply.sh`>):
# 8b names its raw sites and 8c its helper copy (observed 2026-10-21).
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
# A case this host cannot run (6c, 8b, 8h) prints SKIP and is handed to the
# runner, which then reports the row PARTIAL instead of PASS.
. .ai-dev/quality/checks/lib_skip.sh || { echo "FAIL  cannot source .ai-dev/quality/checks/lib_skip.sh"; exit 1; }

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

# 6c A VALUE-TAKING FLAG WITH NO VALUE. `-m` last on the line makes `shift 2`
# fail; $# never decreases and `while [ $# -gt 0 ]` spins forever (ship review
# 1.0.6.41 finding 8 — the same shape shipped in the OTA helper, case 8h). Run
# under `timeout` so a regression FAILS here instead of hanging the suite.
if ! command -v timeout >/dev/null 2>&1; then
    quality_skip "6c dangling flag (no coreutils 'timeout' on this host to bound a hang) — CI is the authority"
else
    for flag in -m -o -g; do
        rc=0
        timeout 5 bash -c '. "$1" >/dev/null 2>&1; sa02m_atomic_install "$2"' _ "$LIB" "$flag" >/dev/null 2>&1 || rc=$?
        if [ "$rc" -eq 124 ]; then
            bad "6c dangling '$flag' HANGS the helper (rc=124, killed by timeout) — 'shift 2' fails and the argument loop never terminates"
        elif [ "$rc" -eq 0 ]; then
            bad "6c dangling '$flag' returned 0 — a flag with no value was accepted"
        else
            ok "6c dangling '$flag' (no value) is refused, not spun on (rc=$rc)"
        fi
    done
fi

echo "── 7. every live-path install site in scripts/*.sh + install.sh uses the helper ──"
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
        ok "7a codemod --check: no raw live-path install -m / cp site"
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
    # the sweep set is scripts/*.sh + install.sh; a missing member is rc=2 (an
    # incomplete sweep), which must never be mistaken for a site RED below.
    cp install.sh "$T/scratch/"
    first=$(printf '%s\n' "$listed" | grep '\[converted\]' | head -1)
    f=${first%%:*}; f=${f//\\//}; ln=${first#*:}; ln=${ln%%:*}
    red=""; rrc=0
    if [ -n "$f" ] && [ -n "$ln" ] && sed -i "${ln}s/sa02m_atomic_install -m /install -m /" "$T/scratch/$f"; then
        red=$(cd "$T/scratch" && python3 "$ROOT/$CODEMOD" --check 2>&1); rrc=$?
    fi
    if [ "$rrc" -eq 1 ] && printf '%s\n' "$red" | tr -d '\r' | grep -q "^$f:$ln:"; then
        ok "7c drive-to-failure: reverting $f:$ln on a scratch copy turns --check RED at that line"
    else
        bad "7c drive-to-failure: rc=$rrc with $f:$ln reverted (expected rc=1 naming it) — the sweep is hollow"
    fi
    # 7d proves the install.sh HALF of the row's claim. install.sh carries zero
    # install -m sites today, so 7c can only ever exercise scripts/*.sh: without
    # a planted site, "and install.sh" would be an unmeasured sentence again
    # (quality-gate-rigor.md shape (b) — the very finding this widening closes).
    sed -i "${ln}s/install -m /sa02m_atomic_install -m /" "$T/scratch/$f"
    printf '%s\n' 'install -m 0644 "$s" /etc/systemd/system/planted.service' >> "$T/scratch/install.sh"
    pln=$(wc -l < "$T/scratch/install.sh")
    red=$(cd "$T/scratch" && python3 "$ROOT/$CODEMOD" --check 2>&1); rrc=$?
    if [ "$rrc" -eq 1 ] && printf '%s\n' "$red" | tr -d '\r' | grep -q "^install.sh:$pln:"; then
        ok "7d install.sh is really swept: a planted /etc/systemd/system/ write there turns --check RED (install.sh:$pln)"
    else
        bad "7d a raw unit write planted in install.sh was NOT caught (rc=$rrc): $(printf '%s\n' "$red" | head -3 | tr '\n' ' ')"
    fi
    # 7e-7h: the three shapes the sweep was blind to until 1.0.7.0, each
    # planted in the byte shape that actually shipped (scripts/05-cloud-agent.sh
    # and scripts/06-gateway.sh on 1.0.6.54, where this row was GREEN over four
    # raw unit/binary writes). A fresh scratch copy per case so one plant can
    # never make another case's RED. 7h is the negative control: a variable
    # assigned two different values must stay unresolved, never guessed.
    plant_case() {  # <label> <expected-line-offset|none> <line>...
        local label=$1 want=$2; shift 2
        rm -rf "$T/scratch7"; mkdir -p "$T/scratch7/scripts"
        cp scripts/*.sh "$T/scratch7/scripts/"; cp install.sh "$T/scratch7/"
        local base; base=$(wc -l < "$T/scratch7/install.sh" | tr -d ' ')
        printf '%s\n' "$@" >> "$T/scratch7/install.sh"
        local out7 rc7
        out7=$(cd "$T/scratch7" && python3 "$ROOT/$CODEMOD" --check 2>&1); rc7=$?
        # Captured, then matched in-shell — never a producer piped into
        # `grep -q` / `head` (quality-gate-rigor.md shape f).
        out7=${out7//$'\r'/}
        local brief=${out7//$'\n'/ }; brief=${brief:0:300}
        if [ "$want" = none ]; then
            if [ "$rc7" -eq 0 ]; then
                ok "$label"
            else
                bad "$label (rc=$rc7): $brief"
            fi
            return
        fi
        local wl=$((base + want))
        if [ "$rc7" -eq 1 ] && [[ $'\n'"$out7" == *$'\n'"install.sh:$wl:"* ]]; then
            ok "$label (install.sh:$wl)"
        else
            bad "$label NOT caught (rc=$rc7, expected install.sh:$wl): $brief"
        fi
    }
    plant_case "7e a raw 'cp' of a unit through a literal dir variable (the 05-cloud-agent.sh shape) turns --check RED" 2 \
        'SYSTEMD_DIR="/etc/systemd/system"' \
        'cp "$AGENT_SRC/sa02m-cloud-agent.service" "$SYSTEMD_DIR/"'
    plant_case "7f a raw 'install -m' into /lib/systemd/system/ (the 06-gateway.sh shape) turns --check RED" 1 \
        'install -m 0644 -o root -g root \' \
        '    "$ETC_DIR/sa02m-serial-gateway.service" \' \
        '    "/lib/systemd/system/$SVC_NAME.service"'
    plant_case "7g a one-line function body ending '\"\$VAR\"; }' into /usr/local/bin (the frpc shape) turns --check RED" 2 \
        'FRPC_BIN="/usr/local/bin/frpc"' \
        '_frpc_install_bin() { install -m 0755 -o root -g root "$1" "$FRPC_BIN"; }'
    plant_case "7h negative control: a variable assigned two different literals is not resolved (no guessed destination)" none \
        'PLANT_DIR="/etc/systemd/system"' \
        'PLANT_DIR="/opt/sa02m-plant"' \
        'cp "$s" "$PLANT_DIR/"'
fi

echo "── 8. the OTA launcher (etc/sa02m-web-update-apply.sh) delivers nothing itself ──"
# Header: why the pin inverted in 1.0.7.5. ANY raw `install -m` / `rsync` line
# in the launcher is a second delivery path outside the shared runner
# (comment-stripped; the two sanctioned tmpfiles/sudoers sites went with the
# delivery). `cp` is not swept: the launcher copies nothing today and this is a
# short file — widen the ERE if a cp site ever appears.
APPLY=${WEB_UPDATE_LAUNCHER:-etc/sa02m-web-update-apply.sh}
apply_raw_sites() {
    grep -n -E '(^|[^[:alnum:]_/.-])(install[[:space:]]+-m[[:space:]]|rsync[[:space:]])' "$1" 2>/dev/null \
        | tr -d '\r' | grep -v -E '^[[:space:]]*[0-9]+:[[:space:]]*#' || true
}
if [ ! -s "$APPLY" ]; then
    bad "8a $APPLY missing or empty — nothing to sweep"
else
    if grep -q -E '^[[:space:]]*exec[[:space:]]+"\$RUNNER"[[:space:]]+apply' "$APPLY"; then
        ok "8a the launcher hands the overlay to the shared runner (exec \"\$RUNNER\" apply) — delegation, not absence"
    else
        bad "8a no 'exec \"\$RUNNER\" apply' handoff in $APPLY — the delivery moved again; re-anchor this section consciously"
    fi
    raw=$(apply_raw_sites "$APPLY" | sed '/^$/d')
    if [ -z "$raw" ]; then
        ok "8b no raw install -m / rsync write in $APPLY — the shared runner is the only delivery path"
    else
        bad "8b $(printf '%s\n' "$raw" | grep -c .) raw write site(s) in $APPLY — a delivery path outside the shared runner:"
        printf '%s\n' "$raw" | head -10 | sed "s|^|        $APPLY:|"
    fi
    if grep -q -E '^atomic_install_script\(\) \{' "$APPLY"; then
        bad "8c $APPLY carries a private atomic_install_script() again — the helper's homes are scripts/lib.sh and the runner's atomic_install_file()"
    else
        ok "8c no private atomic helper copy in $APPLY"
    fi
    # drive-to-failure: a planted raw live-path site on a scratch copy must be
    # the ONE site 8b's sweep reports (non-vacuity of the sweep itself).
    mkdir -p "$T/scratch/etc"
    plant="$T/scratch/etc/plant-$(basename "$APPLY")"
    cp "$APPLY" "$plant"
    printf '    install -m 755 "$CLONE_TMP/repo/etc/x.sh" /usr/local/lib/x.sh\n' >> "$plant"
    pln=$(wc -l < "$plant" | tr -d ' ')
    planted=$(apply_raw_sites "$plant" | sed '/^$/d')
    if [ "$(printf '%s\n' "$planted" | grep -c .)" = "1" ] && printf '%s\n' "$planted" | grep -q "^${pln}:"; then
        ok "8d drive-to-failure: a planted raw 'install -m' into /usr/local/lib on a scratch copy is the one site reported, at line $pln"
    else
        bad "8d drive-to-failure: the planted raw site at line $pln was NOT the single site reported — 8b is hollow (got: $(printf '%s\n' "$planted" | head -3 | tr '\n' ' '))"
    fi
fi

echo ""
if [ "$fails" -eq 0 ]; then
    echo "install-atomic: ALL OK"
    exit 0
fi
echo "install-atomic: $fails FAILURE(S)"
exit 1
