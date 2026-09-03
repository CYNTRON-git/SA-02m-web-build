#!/usr/bin/env bash
# carel-shared-home — the Carel register map has exactly one home, and every
# deploy path that needs it actually installs it.
#
# WHY THIS EXISTS. `opt/sa02m-carel/sa02m_carel/` is imported by two services
# that live in different trees and run as different users: the flasher daemon
# (`/opt/sa02m-flasher`, user sa02m-flasher, scan + config window) and the
# Modbus-MQTT bridge (`/opt/sa02m-modbus-mqtt`, root, poller + bus scan). Two
# failure modes follow, and neither shows up in any other row:
#
#   1. A DEPLOY PATH FORGETS IT. `scripts/update-www-only.sh` refreshes the
#      bridge but never the flasher tree, so a device updated that way would
#      run a new `bridge_carel.py` against an absent `sa02m_carel` and the
#      poller would die on import — green everywhere in CI, dead on the board.
#      All three paths (04-flasher.sh, 05-mqtt.sh, update-www-only.sh) must
#      call the one installer helper.
#   2. SOMEONE COPIES THE MAP. The cheapest way to "fix" an import error is to
#      paste carel_ahu.py into the consumer that cannot see it. Then the two
#      copies drift and the register a firmware bump moved is right in one and
#      wrong in the other. So the load-bearing addresses may appear in exactly
#      one place in the tree.
#
# METHOD. Read through lib_check.sh so a commented-out installer call cannot
# satisfy a pin (the comment-blindness class, docs/agent-rules/quality-gate-rigor.md).
# Duplicate detection greps the whole tree for the address constants and
# subtracts the legitimate homes; the count, not a name list, is what fails —
# a copy under any new path is caught.
#
# NON-VACUOUS: a missing package, a missing deploy script, an installer helper
# that vanished, or a sweep that stops seeing the constants FAILS the run. The
# duplicate sweep asserts the constants ARE found in the package first, so a
# broken grep reads as a failure and not as "no duplicates".
#
# Proven RED (1.0.6.31) by four mutations:
#   * commenting out the call in 05-mqtt.sh          -> case 2 FAIL
#   * commenting out the call in update-www-only.sh  -> case 3 FAIL
#   * deleting the helper body from lib.sh           -> case 4 FAIL
#   * copying carel_ahu.py into opt/sa02m-flasher/   -> case 5 FAIL
#
# Run: bash .ai-dev/quality/checks/carel-shared-home.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1

# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. "$ROOT/.ai-dev/quality/checks/lib_check.sh"

PKG=opt/sa02m-carel/sa02m_carel
LIB=scripts/lib.sh
HELPER=sa02m_install_carel_pkg

fails=0
ok()  { printf 'carel-shared-home: ok    %s\n' "$1"; }
bad() { printf 'carel-shared-home: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── non-vacuity: the things every case below reads must exist ───────────────
for f in "$PKG/carel_ahu.py" "$PKG/carel_ahu_map.py" "$PKG/controls.py" "$LIB" \
         scripts/04-flasher.sh scripts/05-mqtt.sh scripts/update-www-only.sh; do
    [ -f "$f" ] || { echo "carel-shared-home: FAIL — missing $f"; exit 1; }
done

# ── cases 1-3: every deploy path installs the package ──────────────────────
case_installer() {  # $1=script  $2=case label
    if stripped_has "$1" "$HELPER"; then
        ok "$2 installs the shared package"
    else
        bad "$2 never calls $HELPER — a device updated this way loses sa02m_carel"
    fi
}
case_installer scripts/04-flasher.sh      "1 flasher install"
case_installer scripts/05-mqtt.sh         "2 bridge install"
case_installer scripts/update-www-only.sh "3 www/bridge refresh"

# ── case 4: the helper itself is real, and installs into the shared path ────
helper_text=$(stripped_text "$LIB")
if text_has "$helper_text" "$HELPER() {" && text_has "$helper_text" "/opt/sa02m-carel"; then
    ok "4 helper defined in $LIB and targets /opt/sa02m-carel"
else
    bad "4 $LIB has no working $HELPER definition targeting /opt/sa02m-carel"
fi

# ── case 5: the map lives in exactly one place ─────────────────────────────
# Two addresses picked because they are meaningless outside the Carel map and
# would travel with any copy of it: the CRST BMS on/off coil and the uAria
# local-terminal coil we must never write.
dup=0
for needle in 'COIL_BMS_OFF_ON = 65' 'COIL_UARIA_LOCAL = 30'; do
    hits=$(grep -rlF "$needle" --include='*.py' opt/ www/ scripts/ tools/ etc/ usr/ firmware/ 2>/dev/null | sort || true)
    if ! printf '%s\n' "$hits" | grep -qx "$PKG/carel_ahu.py"; then
        bad "5 sweep for '$needle' no longer finds the package — the check reads nothing"
        dup=1
        continue
    fi
    extra=$(printf '%s\n' "$hits" | grep -vx "$PKG/carel_ahu.py" | grep -v '^$' || true)
    if [ -n "$extra" ]; then
        bad "5 second home of the Carel map: $(printf '%s' "$extra" | tr '\n' ' ')"
        dup=1
    fi
done
[ "$dup" -eq 0 ] && ok "5 register map has one home ($PKG)"

# ── case 6: the consumers import it rather than restating it ───────────────
# Skipped until the consumers land (they arrive in the same release); once a
# consumer file exists it must import the package, never define the addresses.
for consumer in opt/sa02m-modbus-mqtt/bridge_carel.py \
                opt/sa02m-flasher/sa02m_flasher/carel_poll.py; do
    [ -f "$consumer" ] || continue
    if stripped_has "$consumer" "sa02m_carel"; then
        ok "6 $(basename "$consumer") imports the shared package"
    else
        bad "6 $(basename "$consumer") does not import sa02m_carel"
    fi
done

if [ "$fails" -eq 0 ]; then
    printf 'carel-shared-home: ALL OK\n'
    exit 0
fi
printf 'carel-shared-home: %d FAILED\n' "$fails"
exit 1
