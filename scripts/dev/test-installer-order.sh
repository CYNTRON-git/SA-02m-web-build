#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: every pin here is an ORDER between two live lines (a shared package before the first consumer copy; the module-runner's `sync` right after its `bash` line) read comment-stripped through lib_check.sh — an order has no comment-out form: commenting either line out is already RED here (the pin then finds no line and FAILS on non-vacuity) and RED under installer-svc-policy-gate (f)/(g) for install.sh; the one fail-IF-PRESENT pin (the retired "self-contained" claim) wants the line ABSENT, so a comment-out cannot defeat it.
# test-installer-order.sh — regression harness for the install ORDER the
# 8D bench-136 reset requires (.ai-dev/8d/bench-136-reset.md, D5 step B).
# Quality row `installer-order`.
#
# Why this exists: after the 1.136 reset the board ran a 1.0.6.40 bridge
# (bridge_led.py, which imports sa02m_led.MB2WS_TEXT_BASE at module level)
# against a torn /opt/sa02m-led package — 119 crash-loop restarts. The bridge
# files landed at 05-mqtt.sh:160 and the package they import at :166, so a
# tear between the two leaves a consumer NEWER than its dependency. The
# guarantee: a shared package (Carel, LED) lands BEFORE the first file that
# imports it, in every module that installs both; and install.sh syncs after
# every module so a tear is bounded to one module, not five minutes of
# writeback (the board's ext4 commit=600).
#
# Method: comment-stripped line numbers via .ai-dev/quality/checks/lib_check.sh
# (stripped_first_line — comments blanked, not deleted, so numbers line up);
# every pin FAILS when a line is missing (non-vacuity), never passes on zero.
#
# Drive-to-failure: swap the sa02m_install_led_pkg call below the bridge loop
# in scripts/05-mqtt.sh (the 1.0.6.40 order) — case 1 goes RED; replace a
# `sa02m_run_module 01-system.sh` call in install.sh with the bare
# `bash "$SCRIPT_DIR/scripts/01-system.sh"` — case 3 goes RED.
#
# Run: bash scripts/dev/test-installer-order.sh   (bash + sed + grep)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
# shellcheck source=/dev/null
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  cannot source lib_check.sh"; exit 1; }
declare -F stripped_first_line >/dev/null || { echo "FAIL  lib_check.sh lacks stripped_first_line"; exit 1; }

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
for f in scripts/05-mqtt.sh scripts/04-flasher.sh install.sh; do
    [ -f "$f" ] || { echo "FAIL  missing $f"; exit 1; }
done

# order_pin <file> <label> <dependency ERE> <consumer ERE>
# The dependency's FIRST line must precede the consumer's FIRST line; either
# line missing is a FAIL (a pin that anchors on nothing proves nothing).
order_pin() {
    local f=$1 label=$2 dep cons
    dep=$(stripped_first_line "$f" "$3")
    cons=$(stripped_first_line "$f" "$4")
    if [ -z "$dep" ]; then
        bad "$label: dependency line not found in $f (ERE: $3)"
    elif [ -z "$cons" ]; then
        bad "$label: consumer line not found in $f (ERE: $4)"
    elif [ "$dep" -lt "$cons" ]; then
        ok "$label: $f dependency at line $dep precedes consumer at line $cons"
    else
        bad "$label: $f dependency at line $dep comes AFTER consumer at line $cons — a tear leaves a consumer newer than its package"
    fi
}

echo "── 1. 05-mqtt.sh: shared packages land before the bridge modules ──"
order_pin scripts/05-mqtt.sh "1a LED pkg → bridge copy"   '^[[:space:]]*sa02m_install_led_pkg '   'install -m [0-9]+ .*"\$BRIDGE_DIR/'
order_pin scripts/05-mqtt.sh "1b Carel pkg → bridge copy" '^[[:space:]]*sa02m_install_carel_pkg ' 'install -m [0-9]+ .*"\$BRIDGE_DIR/'
# The retired claim: the old comment said the entry is self-contained, which
# stopped being true in 1.0.6.33 (modbus_mqtt_bridge.py imports bridge_led).
if grep -qi 'self-contained' scripts/05-mqtt.sh; then
    bad "1c scripts/05-mqtt.sh still claims the bridge entry is self-contained (false since 1.0.6.33 — it imports bridge_led)"
else
    ok "1c the stale 'self-contained' claim is gone from scripts/05-mqtt.sh"
fi

echo "── 2. 04-flasher.sh: shared packages land before the daemon tree ──"
order_pin scripts/04-flasher.sh "2a LED pkg → rsync"   '^[[:space:]]*sa02m_install_led_pkg '   '^[[:space:]]*rsync '
order_pin scripts/04-flasher.sh "2b Carel pkg → rsync" '^[[:space:]]*sa02m_install_carel_pkg ' '^[[:space:]]*rsync '

echo "── 3. install.sh: every module runs through sa02m_run_module, which syncs ──"
raw_bash=$(stripped_count install.sh '^[[:space:]]*bash "\$SCRIPT_DIR/scripts/')
fn_line=$(stripped_first_line install.sh '^sa02m_run_module\(\)')
bash_line=$(stripped_first_line install.sh '^[[:space:]]*bash "\$SCRIPT_DIR/scripts/')
sync_line=$(stripped_first_line install.sh '^[[:space:]]*sync$')
calls=$(stripped_count install.sh '^[[:space:]]*sa02m_run_module [0-9]')
if [ -z "$fn_line" ]; then
    bad "3a install.sh has no sa02m_run_module() — modules run with no sync between them"
elif [ "$raw_bash" -ne 1 ] || [ -z "$bash_line" ] || [ "$bash_line" -lt "$fn_line" ]; then
    bad "3a install.sh runs a module outside sa02m_run_module ($raw_bash raw bash lines; function at $fn_line, first bash line at ${bash_line:-none})"
elif [ -z "$sync_line" ] || [ "$sync_line" -le "$bash_line" ] || [ $((sync_line - bash_line)) -gt 3 ]; then
    bad "3a sa02m_run_module: no sync within 3 lines after its bash line (bash at $bash_line, sync at ${sync_line:-none})"
else
    ok "3a install.sh: one bash line (l.$bash_line) inside sa02m_run_module (l.$fn_line), sync at l.$sync_line"
fi
if [ "$calls" -ge 12 ]; then
    ok "3b non-vacuity: $calls sa02m_run_module call lines (floor 12)"
else
    bad "3b non-vacuity: only $calls sa02m_run_module call lines — the module list stopped going through the runner"
fi

echo ""
if [ "$fails" -eq 0 ]; then
    echo "installer-order: ALL OK"
    exit 0
fi
echo "installer-order: $fails FAILURE(S)"
exit 1
