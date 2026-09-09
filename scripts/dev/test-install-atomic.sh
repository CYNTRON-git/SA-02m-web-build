#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness — the helper's guarantee is asserted by RUNNING scripts/lib.sh sa02m_atomic_install in a sandbox with a failing `mv` / a torn `install` shim (files observed, not lines grepped), and the site pin delegates to the codemod's own `--check` sweep plus a drive-to-failure on a scratch copy; commenting a shipped install site out removes a call, which the codemod's non-vacuity floor (>= MIN_CONVERTED converted sites) and the module's own behaviour catch, not a needle grep here; sections 8-9 hold the same property for the OTA apply path - the helper is EXTRACTED from the shipped file and RUN under the same shims, and its call sites are floored by a converted-call count (>= MIN_APPLY_CALLS) that a comment-out drops below, with 8g a negative control that requires the pre-fix shape to really break.
# test-install-atomic.sh — regression harness for the atomic live-path write
# (scripts/lib.sh sa02m_atomic_install) and for the rule that every live-path
# `install -m` site in scripts/*.sh AND install.sh goes through it. Quality row
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
# site reverted (case 7c) plus a planted raw unit write in install.sh (case 7d
# - install.sh carries zero install -m sites today, so 7c alone would leave the
# "and install.sh" half of the claim unmeasured).
#
# Sections 8-9 do the same for the OTA apply path — etc/sa02m-web-update-apply.sh,
# how a FIELD board updates itself. It cannot source scripts/lib.sh (scripts/ is
# not deployed), so it carries its own atomic_install_script; the function is
# EXTRACTED from the shipped file and run in the sandbox (the file itself cannot
# be sourced — it locks, truncates its log and exec's at load). Its seven live
# destinations are shell variables, which the codemod's literal-destination
# sweep cannot classify, so the sites are pinned by an ALLOW-LIST of the two
# raw destinations there that are NOT live paths, floored by the converted-call
# count and driven to failure on a scratch copy.
#
# Drive-to-failure: SVC_HELPERS_LIB=<(git show 1.0.6.41~N:scripts/lib.sh) has
# no sa02m_atomic_install — the source guard fails; revert one codemod site —
# case 7 reports it. RED for the OTA half, observed 2026-09-09 against the
# pre-fix etc/sa02m-web-update-apply.sh: 4 FAILURES — "8 … defines no
# atomic_install_script()", "9a 7 raw live-path 'install -m' site(s) still in
# …" naming :274 :277 :280 :284 :288 :290 :306, "9b … only 0
# atomic_install_script sites", "9c … no atomic_install_script site found to
# revert"; and, on the converted tree with tmp="$dst" spliced into the extracted
# helper (a scratch mutation, never shipped), 8c and 8d both RED with the live
# file GONE — the incident class itself.
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
fi

echo "── 8. the OTA apply path's own atomic helper (etc/sa02m-web-update-apply.sh) ──"
# etc/ scripts run standalone on the board, where scripts/ is not deployed, so
# sa02m_atomic_install is unreachable there and the OTA apply path carries its
# own copy (atomic_install_script). The helper is EXTRACTED from the SHIPPED
# file and run for real — the file itself cannot be sourced (it takes a lock,
# truncates its log and exec's the shared runner at load).
APPLY=etc/sa02m-web-update-apply.sh
sed -n '/^atomic_install_script() {$/,/^}$/p' "$APPLY" > "$T/apply-helper.sh"
if [ ! -s "$T/apply-helper.sh" ]; then
    bad "8 $APPLY defines no atomic_install_script() — the fleet's self-update path still writes live paths with a bare 'install -m' (nothing to run)"
else
    ok "8 extracted atomic_install_script() from $APPLY ($(wc -l < "$T/apply-helper.sh") lines)"
    # shellcheck disable=SC1090
    source "$T/apply-helper.sh"
    # the shipped helper appends tool stderr to the OTA log the script owns
    export LOGFILE="$T/apply.log"; : > "$LOGFILE"
    mkdir -p "$T/ota"
    printf 'OLD auth lib\n' > "$T/ota/sa02m-web-auth-lib.sh"
    # The repo copy a CRLF checkout hands the board — the strip is what the
    # pre-fix shape ran as a SECOND write over the LIVE file.
    printf '#!/bin/bash\r\nweb_auth_check() { :; }\r\n' > "$T/src/lib-crlf.sh"
    printf '#!/bin/bash\nweb_auth_check() { :; }\n' > "$T/src/lib-lf.sh"
    ota_tmp_left() { ls "$T/ota"/*.sa02m-tmp.* >/dev/null 2>&1; }

    rc=0
    atomic_install_script -m 644 "$T/src/lib-crlf.sh" "$T/ota/sa02m-web-auth-lib.sh" || rc=$?
    if [ "$rc" -eq 0 ] && cmp -s "$T/src/lib-lf.sh" "$T/ota/sa02m-web-auth-lib.sh" && ! ota_tmp_left; then
        ok "8a success: destination carries the NEW bytes with CRLF stripped, rc=0, no tmp left"
    else
        bad "8a success path: rc=$rc first-line='$(head -1 "$T/ota/sa02m-web-auth-lib.sh" 2>/dev/null | tr -d '\r')' bytes=$(wc -c < "$T/ota/sa02m-web-auth-lib.sh" 2>/dev/null) dir='$(ls "$T/ota" | tr '\n' ' ')'"
    fi

    # Mode. MSYS/Cygwin derives a file's mode from a heuristic (a `#!` body reads
    # 755, anything else 644) and ignores chmod outright, so -m 644 and -m 755
    # are indistinguishable on a Windows dev box — an assertion there could not
    # fail, and decoration is not evidence (quality-gate-rigor.md). The host is
    # PROBED and the case reports a SKIP rather than a pass when it cannot tell
    # 0600 from 0644, the same call test-installer-svc-helpers.sh 14b makes
    # (.ai-dev/notes/quality-gate-environment.md). Where modes are real, the
    # assertion is both absolute (644/755 land) and equivalent to install(1),
    # which is what the seven converted sites did before.
    printf 'plain
' > "$T/modeprobe"; chmod 600 "$T/modeprobe" 2>/dev/null
    if [ "$(stat -c '%a' "$T/modeprobe" 2>/dev/null)" = "600" ]; then fidelity=real; else fidelity=heuristic-host; fi
    if [ "$fidelity" != "real" ]; then
        echo "SKIP  8b mode assertions (POSIX modes are not representable on $(uname -s): MSYS derives a mode from the file body and ignores chmod, so -m 644 and -m 755 are indistinguishable here) — CI is the authority"
    else
        mode_ok=1
        for m in 644 755; do
            install -m "$m" "$T/src/lib-crlf.sh" "$T/ota/ref-$m.sh" 2>/dev/null
            atomic_install_script -m "$m" "$T/src/lib-crlf.sh" "$T/ota/new-$m.sh"
            ref=$(stat -c '%a' "$T/ota/ref-$m.sh" 2>/dev/null || echo '?')
            got=$(stat -c '%a' "$T/ota/new-$m.sh" 2>/dev/null || echo '??')
            [ "$got" = "$m" ] && [ "$got" = "$ref" ]                 || { mode_ok=0; bad "8b mode -m $m: helper landed $got, install(1) lands $ref"; }
        done
        [ "$mode_ok" -eq 1 ] && ok "8b mode: -m 644 and -m 755 land 644/755, exactly what install(1) lands"
    fi

    printf 'OLD auth lib\n' > "$T/ota/sa02m-web-auth-lib.sh"
    rc=0
    ( PATH="$T/bin-nomv:$PATH"; atomic_install_script -m 644 "$T/src/lib-crlf.sh" "$T/ota/sa02m-web-auth-lib.sh" ) || rc=$?
    if [ "$rc" -ne 0 ] && [ "$(cat "$T/ota/sa02m-web-auth-lib.sh")" = "OLD auth lib" ] && ! ota_tmp_left; then
        ok "8c failed rename: the live auth lib is still OLD, rc=$rc, tmp cleaned"
    else
        bad "8c failed rename: rc=$rc content='$(cat "$T/ota/sa02m-web-auth-lib.sh")' dir='$(ls "$T/ota" | tr '\n' ' ')'"
    fi

    # Torn STAGING copy: the helper stages its tmp with `sed` (the CRLF strip is
    # folded into the copy), so the shim modelling "the copy is killed mid-body"
    # is a sed that emits a prefix and dies.
    mkdir -p "$T/bin-tornsed"
    cat > "$T/bin-tornsed/sed" <<'SHIM'
#!/bin/bash
# model of a staging copy killed mid-body: emit 8 bytes of the source and die
for last; do :; done
head -c 8 "$last"
exit 1
SHIM
    chmod +x "$T/bin-tornsed/sed"
    printf 'OLD auth lib\n' > "$T/ota/sa02m-web-auth-lib.sh"
    rc=0
    ( PATH="$T/bin-tornsed:$PATH"; atomic_install_script -m 644 "$T/src/lib-crlf.sh" "$T/ota/sa02m-web-auth-lib.sh" ) || rc=$?
    if [ "$rc" -ne 0 ] && [ "$(cat "$T/ota/sa02m-web-auth-lib.sh")" = "OLD auth lib" ] && ! ota_tmp_left; then
        ok "8d torn staging copy: the live auth lib is still OLD (never empty, never half-converted), rc=$rc, tmp cleaned"
    else
        bad "8d torn staging copy: rc=$rc content='$(cat "$T/ota/sa02m-web-auth-lib.sh")' dir='$(ls "$T/ota" | tr '\n' ' ')'"
    fi

    printf 'OLD auth lib\n' > "$T/ota/sa02m-web-auth-lib.sh"
    printf 'garbage' > "$T/ota/sa02m-web-auth-lib.sh.sa02m-tmp.999"
    rc=0
    atomic_install_script -m 644 "$T/src/lib-crlf.sh" "$T/ota/sa02m-web-auth-lib.sh" || rc=$?
    if [ "$rc" -eq 0 ] && ! ota_tmp_left; then
        ok "8e stale sa02m-tmp.999 from an earlier crash is removed by the next install"
    else
        bad "8e stale tmp: rc=$rc dir='$(ls "$T/ota" | tr '\n' ' ')'"
    fi

    rc=0; atomic_install_script -m 644 "$T/src/lib-crlf.sh" >/dev/null 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then ok "8f argument floor: missing DST is refused (rc=$rc)"; else bad "8f helper accepted a single argument"; fi

    # NEGATIVE CONTROL — the shims above are not decoration. Replay the PRE-FIX
    # shape (`install -m` then a follow-up `sed -i` over the LIVE path) under the
    # torn-copy shim of section 3 and require the live file to come out BROKEN.
    # If this ever passes, 8c/8d assert nothing (quality-gate-rigor.md: an
    # assertion that cannot fail is decoration, not evidence).
    printf 'OLD auth lib\n' > "$T/ota/prefix.sh"
    ( PATH="$T/bin-torn:$PATH"; install -m 644 "$T/src/lib-crlf.sh" "$T/ota/prefix.sh" && sed -i 's/\r$//' "$T/ota/prefix.sh" ) >/dev/null 2>&1
    pfx_bytes=$(wc -c < "$T/ota/prefix.sh" 2>/dev/null || echo -1)
    if [ "$(cat "$T/ota/prefix.sh" 2>/dev/null | tr -d '\r\n')" != "OLD auth lib" ] && [ "$pfx_bytes" -lt 20 ]; then
        ok "8g negative control: the PRE-FIX 'install -m' + 'sed -i' shape leaves the live file truncated to $pfx_bytes bytes under the same shim — the failure 8c/8d assert against is real"
    else
        bad "8g negative control: the pre-fix shape survived the torn-copy shim ($pfx_bytes bytes) — 8c/8d model nothing"
    fi
fi

echo "── 9. every live-path install site in the OTA apply path goes through the helper ──"
# The rule is an ALLOW-LIST of the sanctioned raw sites, not a denylist of the
# ones we thought of (quality-gate-rigor.md shape (b) — the mqtt-install-secret
# lesson). Only TWO destinations in this file are not live paths:
#   /etc/tmpfiles.d/*   — read by systemd-tmpfiles on demand, never mid-flight
#   /etc/sudoers.d/*    — staged and `visudo -c`-validated first, re-read per sudo
# Any other `install -m` here writes a live path on the fleet's self-update
# path. Those destinations are SHELL VARIABLES ($tgt), which is why the
# codemod's literal-destination sweep (section 7) cannot classify them and this
# file is not in its FILES set — the sites are pinned here instead.
MIN_APPLY_CALLS=7      # 7 live-path sites on 1.0.6.41
MIN_APPLY_ALLOWED=2    # the two sanctioned raw sites above
apply_raw_sites() {
    local out
    out=$(grep -n -E '(^|[^[:alnum:]_/.-])install[[:space:]]+-m[[:space:]]' "$1" 2>/dev/null || true)
    printf '%s\n' "$out" | tr -d '\r' \
        | grep -v -E '^[[:space:]]*[0-9]+:[[:space:]]*#' \
        | grep -v -E '/etc/tmpfiles\.d/|/etc/sudoers\.d/' || true
}
apply_allowed_count() {
    local out
    out=$(grep -n -E '(^|[^[:alnum:]_/.-])install[[:space:]]+-m[[:space:]]' "$1" 2>/dev/null || true)
    printf '%s\n' "$out" | tr -d '\r' | grep -c -E '/etc/tmpfiles\.d/|/etc/sudoers\.d/'
}

raw=$(apply_raw_sites "$APPLY" | sed '/^$/d')
if [ -z "$raw" ]; then
    ok "9a no unsanctioned raw 'install -m' site left in $APPLY"
else
    bad "9a $(printf '%s\n' "$raw" | wc -l) raw live-path 'install -m' site(s) still in $APPLY:"
    printf '%s\n' "$raw" | head -20 | sed "s|^|        $APPLY:|"
fi

calls=$(grep -c -E '^[[:space:]]*atomic_install_script[[:space:]]+-m[[:space:]]' "$APPLY" || true)
allowed=$(apply_allowed_count "$APPLY")
if [ "$calls" -ge "$MIN_APPLY_CALLS" ] && [ "$allowed" -ge "$MIN_APPLY_ALLOWED" ]; then
    ok "9b non-vacuity: $calls atomic_install_script sites and $allowed sanctioned raw sites are really there (floors $MIN_APPLY_CALLS / $MIN_APPLY_ALLOWED)"
else
    bad "9b non-vacuity: only $calls atomic_install_script sites and $allowed sanctioned raw sites (floors $MIN_APPLY_CALLS / $MIN_APPLY_ALLOWED) — the file was restructured and 9a has stopped watching the sites it names; update the floors, do not delete the check"
fi

# drive-to-failure: revert ONE converted site on a scratch copy → 9a's rule must
# go RED naming that line. Without it, 9a is a sweep that has never seen a defect.
mkdir -p "$T/scratch/etc"
cp "$APPLY" "$T/scratch/etc/"
aln=$(grep -n -E '^[[:space:]]*atomic_install_script[[:space:]]+-m[[:space:]]' "$APPLY" | head -1 | cut -d: -f1)
if [ -n "${aln:-}" ] && sed -i "${aln}s/atomic_install_script -m /install -m /" "$T/scratch/etc/$(basename "$APPLY")"; then
    red=$(apply_raw_sites "$T/scratch/etc/$(basename "$APPLY")" | sed '/^$/d')
    if printf '%s\n' "$red" | grep -q "^${aln}:"; then
        ok "9c drive-to-failure: reverting line $aln to a raw 'install -m' on a scratch copy turns 9a RED at that line"
    else
        bad "9c drive-to-failure: reverting line $aln was NOT caught — 9a is hollow (got: $(printf '%s\n' "$red" | head -3 | tr '\n' ' '))"
    fi
else
    bad "9c drive-to-failure could not run: no atomic_install_script site found to revert"
fi

echo ""
if [ "$fails" -eq 0 ]; then
    echo "install-atomic: ALL OK"
    exit 0
fi
echo "install-atomic: $fails FAILURE(S)"
exit 1
