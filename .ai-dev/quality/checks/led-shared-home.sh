#!/usr/bin/env bash
# led-shared-home — the LED (RGBW_WS2812 / MB2WS) register map has exactly one
# home, and every deploy path that needs it actually installs it.
#
# WHY THIS EXISTS. `opt/sa02m-led/sa02m_led/` is imported by two services that
# live in different trees and run as different users: the flasher daemon
# (`/opt/sa02m-flasher`, user sa02m-flasher, scan + config window) and the
# Modbus-MQTT bridge (`/opt/sa02m-modbus-mqtt`, root, `type: led` poller). Two
# failure modes follow, and neither shows up in any other row:
#
#   1. A DEPLOY PATH FORGETS IT. `scripts/update-www-only.sh` refreshes the
#      bridge but never the flasher tree, so a device updated that way would run
#      a new LED poller against an absent `sa02m_led` and the poller would die on
#      import — green everywhere in CI, dead on the board. All three paths
#      (04-flasher.sh, 05-mqtt.sh, update-www-only.sh) must call the one
#      installer helper.
#   2. SOMEONE COPIES THE MAP. The cheapest way to "fix" an import error is to
#      paste led_mb2ws.py into the consumer that cannot see it. Then the two
#      copies drift and the register a firmware bump moved is right in one and
#      wrong in the other. So the load-bearing addresses may appear in exactly
#      one place in the tree.
#
# METHOD. Read through lib_check.sh so a commented-out installer call cannot
# satisfy a pin (the comment-blindness class, docs/agent-rules/quality-gate-rigor.md).
# Duplicate detection greps the whole tree for the address constants and
# subtracts the legitimate homes; the count, not a name list, is what fails — a
# copy under any new path is caught.
#
# NON-VACUOUS: a missing package file, a missing deploy script, an installer
# helper that vanished, or a sweep that stops seeing the constants FAILS the run.
# The duplicate sweep asserts the constants ARE found in the package first, so a
# broken grep reads as a failure and not as "no duplicates".
#
# Proven RED (1.0.6.33) by five mutations — the observed failure text is in the
# commit body:
#   * commenting out the call in 04-flasher.sh        -> case 1 FAIL
#   * commenting out the call in 05-mqtt.sh           -> case 2 FAIL
#   * commenting out the call in update-www-only.sh   -> case 3 FAIL
#   * deleting the helper body from lib.sh            -> case 4 FAIL
#   * copying led_mb2ws.py into opt/sa02m-flasher/    -> case 5 FAIL
# Case 7 was reworked when the first consumer landed (led_poll.py, 1.0.6.33): it
# swept the bare word `sa02m_led`, which case 6 requires every consumer to carry,
# so the two cases could not both pass. Proven RED after the rework by adding a
# real `from sa02m_led import led_mb2ws` to led_poll.py, and proven not to fire
# on the package name inside its "not installed" message.
#
# Run: bash .ai-dev/quality/checks/led-shared-home.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1

# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. "$ROOT/.ai-dev/quality/checks/lib_check.sh"

PKG=opt/sa02m-led/sa02m_led
LIB=scripts/lib.sh
HELPER=sa02m_install_led_pkg

fails=0
ok()  { printf 'led-shared-home: ok    %s\n' "$1"; }
bad() { printf 'led-shared-home: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── non-vacuity: the things every case below reads must exist ───────────────
for f in "$PKG/led_mb2ws.py" "$PKG/led_mb2ws_map.py" "$PKG/controls.py" "$LIB" \
         scripts/04-flasher.sh scripts/05-mqtt.sh scripts/update-www-only.sh; do
    [ -f "$f" ] || { echo "led-shared-home: FAIL — missing $f"; exit 1; }
done

# ── cases 1-3: every deploy path installs the package ──────────────────────
case_installer() {  # $1=script  $2=case label
    if stripped_has "$1" "$HELPER"; then
        ok "$2 installs the shared package"
    else
        bad "$2 never calls $HELPER — a device updated this way loses sa02m_led"
    fi
}
case_installer scripts/04-flasher.sh      "1 flasher install"
case_installer scripts/05-mqtt.sh         "2 bridge install"
case_installer scripts/update-www-only.sh "3 www/bridge refresh"

# ── case 4: the helper itself is real, and installs into the shared path ────
helper_text=$(stripped_text "$LIB")
if text_has "$helper_text" "$HELPER() {" && text_has "$helper_text" "/opt/sa02m-led"; then
    ok "4 helper defined in $LIB and targets /opt/sa02m-led"
else
    bad "4 $LIB has no working $HELPER definition targeting /opt/sa02m-led"
fi

# ── case 5: the map lives in exactly one place ─────────────────────────────
# Two constants picked because they are meaningless outside the MB2WS map and
# would travel with any copy of it: the settings-block unlock key and the
# marquee window base that had to be moved off the safe-AO block.
dup=0
for needle in 'MB2WS_UNLOCK_KEY = 0x10C8' 'MB2WS_TEXT_BASE = 516'; do
    hits=$(grep -rlF "$needle" --include='*.py' opt/ www/ scripts/ tools/ etc/ usr/ firmware/ 2>/dev/null | sort || true)
    if ! printf '%s\n' "$hits" | grep -qx "$PKG/led_mb2ws.py"; then
        bad "5 sweep for '$needle' no longer finds the package — the check reads nothing"
        dup=1
        continue
    fi
    extra=$(printf '%s\n' "$hits" | grep -vx "$PKG/led_mb2ws.py" | grep -v '^$' || true)
    if [ -n "$extra" ]; then
        bad "5 second home of the LED map: $(printf '%s' "$extra" | tr '\n' ' ')"
        dup=1
    fi
done
[ "$dup" -eq 0 ] && ok "5 register map has one home ($PKG)"

# ── case 6: the consumers import it rather than restating it ───────────────
# Skipped until the consumers land (they arrive later in the same release); once
# a consumer file exists it must import the package, never define the addresses.
for consumer in opt/sa02m-modbus-mqtt/bridge_led.py \
                opt/sa02m-flasher/sa02m_flasher/led_poll.py; do
    [ -f "$consumer" ] || continue
    if stripped_has "$consumer" "sa02m_led"; then
        ok "6 $(basename "$consumer") imports the shared package"
    else
        bad "6 $(basename "$consumer") does not import sa02m_led"
    fi
done

# ── case 7: the flasher reaches the map through the import seam only ───────
# module_profiles.py owns the seam (`led_mb2ws()`), and the scan predicates read
# it through `signature_is_led`. A module that imports `sa02m_led` directly would
# bypass the "package not deployed → None" fallback and crash the whole scan on a
# device where the package is missing.
seam=opt/sa02m-flasher/sa02m_flasher/module_profiles.py
if stripped_has "$seam" "def led_mb2ws()" && stripped_has "$seam" "def signature_is_led("; then
    ok "7 the flasher import seam is in module_profiles.py"
else
    bad "7 $seam lost the led_mb2ws() / signature_is_led() import seam"
fi
# The defect is an IMPORT, not a mention. Case 6 above REQUIRES each consumer to
# name the package (its "not installed" message says which package to deploy), so
# a sweep for the bare word makes case 6 and case 7 unsatisfiable at once — it
# did, the moment led_poll.py landed. The sweep therefore reads import
# STATEMENTS, comment-stripped, so a commented-out import is not a finding and a
# real one cannot hide behind a `#`.
sweep_hits=$(grep -rlF 'sa02m_led' --include='*.py' opt/sa02m-flasher/ 2>/dev/null | sort || true)
if ! printf '%s\n' "$sweep_hits" | grep -qx "$seam"; then
    bad "7 the direct-import sweep no longer sees $seam — it is reading nothing"
else
    direct=""
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        case "$f" in
            "$seam") continue ;;
            opt/sa02m-flasher/tests/*) continue ;;
        esac
        if stripped_matches "$f" '(^|[[:space:]])(import[[:space:]]+sa02m_led|from[[:space:]]+sa02m_led)'; then
            direct="$direct $f"
        fi
    done <<EOF
$sweep_hits
EOF
    if [ -n "$direct" ]; then
        bad "7 sa02m_led imported outside the seam:$direct"
    else
        ok "7 no flasher module bypasses the seam"
    fi
fi

if [ "$fails" -eq 0 ]; then
    printf 'led-shared-home: ALL OK\n'
    exit 0
fi
printf 'led-shared-home: %d FAILED\n' "$fails"
exit 1
