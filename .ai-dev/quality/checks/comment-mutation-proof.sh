#!/usr/bin/env bash
# comment-mutation-proof — the standing proof that the static gates cannot be
# defeated by commenting a line out.
#
# WHY THIS EXISTS AS A ROW, not as a transcript in a commit message. The
# 2026-08-28 audit found five gates that went RED on DELETING a pinned line and
# GREEN on prefixing that same line with `#`: the offline pack silently dropping
# the MPLC RT plugin, CSRF gone from an endpoint that launches a root helper,
# the ~17 s multi-user boot hold back, an operator-disabled unit rolling an OTA
# back. Each gate was fixed and each fix was proven by a hand-run mutation —
# and a hand-run mutation protects nothing the day after. This row re-runs the
# proof on every review beat, so a gate that REGROWS the blindness (a new pin
# written with a plain `grep -q`) fails here instead of in the next audit.
#
# METHOD. A pristine copy of HEAD is extracted into a temp dir, the CURRENT
# working-tree `.ai-dev/quality/` is overlaid on it (so the proof covers the
# gates as they are right now, not as they were committed), and each case then:
#   1. runs its gate on the untouched copy               -> must be GREEN,
#   2. comments out every line carrying the pinned text  -> must go RED,
#   3. restores the file from the pristine copy.
# The real tree is NEVER mutated.
#
# ADDING A GATE: one row in CASES below — `gate|file|literal text`. A case whose
# text matches no line FAILS (non-vacuity): a pin that moved must be re-pinned
# here, never silently dropped.
#
# NOT COVERED, and why: a gate whose pins are all fail-IF-PRESENT sweeps
# (no-retired-session-token, the negative halves of installer-svc-policy-gate)
# cannot be defeated by commenting a line out — a comment removes a needle such
# a gate wants ABSENT. Those gates need the opposite mutation (re-introducing
# the banned pattern), which each already documents in its own header.
#
# Run: bash .ai-dev/quality/checks/comment-mutation-proof.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

fails=0
ok()  { printf 'comment-mutation-proof: ok    %s\n' "$*"; }
bad() { printf 'comment-mutation-proof: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

# gate id | file to mutate | literal text whose line gets commented out
CASES='
mplc-ota-deploy-contract|scripts/offline-update-allowlist.txt|firmware/mplc4/mplc_cyntron.so
mplc-project-deploy-contract|www/network_config/cgi-bin/mplc_project_deploy.cgi|web_csrf_require
kernel-policy-contract|opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua.py|sd_notify("READY=1")
kernel-policy-contract|scripts/01-system.sh|sa02m_svc_apply sa02m-kernel-service-guard.service infra
health-gate-operator-disabled|etc/sa02m-update-runner.sh|systemctl is-enabled "$u"
ota-deploy-mode-contract|etc/sa02m-update-runner.sh|mode = deploy_mode(rel, dst)
imaging-samefile-guard|tools/imaging/make-image.sh|-ef "$FINAL_IMG_KEEP"
installer-svc-policy-gate|install.sh|Установка завершена
iface-naming-contract|scripts/02-network.sh|install -m 755 "$ETC_DIR/sa02m-iface-canonical.sh"
nodered-pin-consistency|scripts/dev/build-nodered-payload.sh|--omit=optional
watchdog-cap|scripts/01-system.sh|install -m 644 "$ETC_REPO/systemd/sa02m-watchdog.conf"
telemetry-device-id-contract|opt/sa02m-modbus-mqtt/sa02m_telemetry.py|self._clear_legacy_retained()
telemetry-device-id-contract|opt/sa02m-modbus-mqtt/sa02m_telemetry.py|"HW not ready — %s command dropped"
sudoers-pin-contract|etc/sudoers.d/sa02m-www|/usr/local/sbin/sa02m-mplc-project-deploy.sh *
'

command -v git >/dev/null 2>&1 || { echo "comment-mutation-proof: FAIL — git is required to build the pristine copy"; exit 1; }
command -v tar >/dev/null 2>&1 || { echo "comment-mutation-proof: FAIL — tar is required to build the pristine copy"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
TREE="$TMP/tree"
PRISTINE="$TMP/pristine"
mkdir -p "$TREE" "$PRISTINE"

( cd "$ROOT" && git archive HEAD ) | tar -x -C "$TREE" 2>/dev/null || {
    echo "comment-mutation-proof: FAIL — could not extract a pristine copy of HEAD"; exit 1; }
n_extracted=$(find "$TREE" -type f | wc -l)
if [ "$n_extracted" -lt 200 ]; then
    echo "comment-mutation-proof: FAIL — pristine copy holds only $n_extracted files; the extraction is broken, not the tree small"
    exit 1
fi

# The gates under test are the WORKING-TREE ones, not HEAD's: this row must
# prove the gate a Builder is handing back, before it is ever committed.
rm -rf "$TREE/.ai-dev/quality"
mkdir -p "$TREE/.ai-dev"
cp -r "$ROOT/.ai-dev/quality" "$TREE/.ai-dev/quality"

# Several gates sweep via `git ls-files`; without an index they would see one
# file and (correctly) fail their own non-vacuity floor, which would read here
# as a mutation success it is not. Give the copy a real index.
( cd "$TREE" && git init -q && git add -A ) >/dev/null 2>&1 || {
    echo "comment-mutation-proof: FAIL — could not init a git index in the pristine copy"; exit 1; }

run_gate() {  # $1 = gate id ; returns the gate's exit status
    ( cd "$TREE" && bash ".ai-dev/quality/checks/$1.sh" ) >/dev/null 2>&1
}

# One green baseline per gate, cached: a gate that is already RED would make
# every mutation below look successful.
declare -A baseline_done=()
green_baseline() {  # $1 = gate id
    local g="$1"
    [ -n "${baseline_done[$g]:-}" ] && return "${baseline_done[$g]}"
    if run_gate "$g"; then
        baseline_done[$g]=0
    else
        baseline_done[$g]=1
        bad "$g is not green on an unmutated tree — its mutation results below prove nothing"
    fi
    return "${baseline_done[$g]}"
}

while IFS='|' read -r gate file needle; do
    [ -n "$gate" ] || continue
    if [ ! -f "$TREE/.ai-dev/quality/checks/$gate.sh" ]; then
        bad "$gate: no such check script — the case table names a gate that does not exist"
        continue
    fi
    if [ ! -f "$TREE/$file" ]; then
        bad "$gate: target file $file is absent from HEAD — the pin moved; re-point this case"
        continue
    fi
    green_baseline "$gate" || continue

    cp "$TREE/$file" "$PRISTINE/current"
    # Comment out every non-comment line carrying the pinned text. index() is a
    # literal match — the needles carry regex metacharacters ($ * " ( ) ).
    #
    # The `#` goes AFTER the indentation, not at column 0. That is how a person
    # actually comments a line out, and it is the harder mutation: a column-0
    # `#` inside a Python method also terminates several gates' own body
    # extractors (an unindented line ends the block), which turns them RED for
    # a reason that has nothing to do with the pin — a proof that would pass
    # while the pin stayed blind.
    awk -v needle="$needle" '
        index($0, needle) > 0 && $0 !~ /^[[:space:]]*#/ {
            sub(/^[[:space:]]*/, "&#"); hits++
        }
        { print }
        END { exit (hits > 0 ? 0 : 3) }
    ' "$PRISTINE/current" > "$TMP/mutated"
    rc=$?
    if [ "$rc" -eq 3 ]; then
        bad "$gate: '$needle' matches no live line in $file — the pin moved or was already commented (non-vacuity)"
        continue
    elif [ "$rc" -ne 0 ]; then
        bad "$gate: mutation of $file failed (awk rc=$rc)"
        continue
    fi
    cp "$TMP/mutated" "$TREE/$file"

    if run_gate "$gate"; then
        bad "$gate stays GREEN with '$needle' commented out in $file — the gate is hollow"
    else
        ok "$gate goes RED when '$needle' is commented out in $file"
    fi
    cp "$PRISTINE/current" "$TREE/$file"
done <<< "$CASES"

n_cases=$(printf '%s\n' "$CASES" | grep -c '|')
if [ "$n_cases" -lt 10 ]; then
    bad "only $n_cases mutation case(s) — the table was gutted (expected >=10)"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "comment-mutation-proof: ALL OK — $n_cases comment-out mutation(s) each turned their gate RED"
    exit 0
fi
echo "comment-mutation-proof: $fails FAILURE(S)"
exit 1
