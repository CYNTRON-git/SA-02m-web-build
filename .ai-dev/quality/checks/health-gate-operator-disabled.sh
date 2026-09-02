#!/usr/bin/env bash
# Static gate: the update runner's health check must SKIP an operator-disabled
# required unit (masked / masked-runtime / disabled) instead of rolling the update
# back — the never-widen rule (a shared HardPy stand masks sa02m-devices-api because
# its own app serves :8765). An ENABLED-but-down unit must still FAIL. Verifies the
# skip logic is present, correctly gated, and positioned before the fail path in
# etc/sa02m-update-runner.sh's restart_services_and_health health loop.
#
# Every pin below reads the COMMENT-STRIPPED function body: commenting out the
# `systemctl is-enabled` line left this gate green while `_en_state` went empty
# and an operator-disabled unit rolled the OTA back again (audit 2026-08-28,
# finding C3). Comments are blanked rather than deleted (lib_check.sh), so the
# ordering pin (d) keeps its line arithmetic.
set -u
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"
RUNNER="$HERE/etc/sa02m-update-runner.sh"
# shellcheck source=/dev/null
. "$(cd "$(dirname "$0")" && pwd)/lib_check.sh" || { echo "health-gate-operator-disabled: cannot source lib_check.sh"; exit 1; }
fails=0
ok(){ printf '  ok    %s\n' "$1"; }
bad(){ printf '  FAIL  %s\n' "$1"; fails=$((fails+1)); }

[ -r "$RUNNER" ] || { echo "health-gate-operator-disabled: cannot read $RUNNER"; exit 1; }

# The health loop lives inside restart_services_and_health, between the units_active
# read and the http/version checks. Extract that function body.
fn="$(sed -n '/^restart_services_and_health() {/,/^}/p' "$RUNNER" | strip_comments)"
# Non-vacuity: an empty extraction (function renamed, or its body entirely
# commented out) FAILS the gate, it never passes silently.
grep -q '[^[:space:]]' <<<"$fn" || { echo "restart_services_and_health not found (or its body is entirely commented out)"; exit 1; }

# (a) the is-active failure branch still exists (non-vacuous anchor)
text_matches "$fn" 'if ! systemctl is-active --quiet "\$u"; then' \
    && ok "(a) health loop still guards each units_active with is-active" \
    || bad "(a) the is-active failure branch is gone — health loop changed shape"

# (b) an is-enabled read of the same unit inside that branch
text_matches "$fn" 'systemctl is-enabled "\$u"' \
    && ok "(b) the branch reads is-enabled to classify operator intent" \
    || bad "(b) no is-enabled check — the skip cannot distinguish operator-disabled"

# (c) a case skipping masked / masked-runtime / disabled with continue
text_matches "$fn" 'masked\|masked-runtime\|disabled\)' \
    && grep -A2 'masked\|masked-runtime\|disabled)' <<<"$fn" | grep -qw continue \
    && ok "(c) masked/masked-runtime/disabled -> continue (skip)" \
    || bad "(c) no masked/disabled skip case with continue"

# (d) the skip 'continue' appears BEFORE the 'unit not active' + return 1 (fail path)
#     within the branch — an ENABLED-but-down unit must still fail.
blk="$(printf '%s\n' "$fn" | sed -n '/if ! systemctl is-active --quiet "\$u"; then/,/^        fi$/p')"
c_line=$(printf '%s\n' "$blk" | grep -nE 'operator-disabled.*|[[:space:]]continue$' | grep -w continue | head -1 | cut -d: -f1)
f_line=$(printf '%s\n' "$blk" | grep -nE 'log "health: unit not active' | head -1 | cut -d: -f1)
r_line=$(printf '%s\n' "$blk" | grep -nE '^[[:space:]]*return 1$' | tail -1 | cut -d: -f1)
if [ -n "$c_line" ] && [ -n "$f_line" ] && [ -n "$r_line" ] && [ "$c_line" -lt "$f_line" ] && [ "$f_line" -lt "$r_line" ]; then
    ok "(d) skip(continue) precedes the fail path (log + return 1)"
else
    bad "(d) ordering wrong or missing: continue@$c_line fail@$f_line return1@$r_line"
fi

# (e) the fail path (return 1 on a genuinely-down enabled unit) still exists
text_matches "$fn" 'log "health: unit not active: \$u"' \
    && ok "(e) the enabled-but-down FAIL path is preserved (still rolls back a real regression)" \
    || bad "(e) the fail path log is gone — the gate no longer catches a down required unit"

echo
[ "$fails" -eq 0 ] && { echo "health-gate-operator-disabled: ALL OK"; exit 0; }
echo "health-gate-operator-disabled: $fails FAILURE(S)"; exit 1
