#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped code in a sandbox (files written, shim invocations, exit codes), so a commented-out line changes the measured behaviour instead of hiding behind a needle grep. Exception, stated honestly: section 1f also carries ONE load-bearing static pin (the mplc4 probe must stay wrapper-free) - it reads the extracted block COMMENT-STRIPPED, so a commented-out wrapper line cannot satisfy it, and it is non-vacuous (an extraction that misses the block FAILS). The remaining source-text greps are extraction/retarget sanity guards on its own scratch copy, which abort the run when the shipped block moves.
# test-service-ctl-policy.sh — regression for the stack-policy writes in
# etc/sa02m-web-service-ctl.sh (cmd_install / cmd_uninstall / stack_key_for_id)
# against etc/sa02m-stacks-policy.sh. Quality row `service-ctl-policy-write`.
# Contract: docs/contracts/installer-refresh-policy.md.
#
# Why: «удалено оператором остаётся удалённым» rests entirely on these two
# hooks — a silent regression (hook dropped, wrong ID mapped, a FAILED install
# recorded as present) has no on-device symptom until the next installer run
# quietly reinstalls the stack the operator removed.
#
# Method: extract the SHIPPED cmd_install/cmd_uninstall/stack_key_for_id +
# emit-plumbing from the ctl (everything above the arg dispatch, like
# test-port-lease-gate.sh), stub the six per-stack installers/uninstallers with
# scripted rc, source the REAL policy lib pointed into a scratch conf, and
# assert the policy file after each call. Also asserts the ctl works with the
# policy lib ABSENT (older board): same JSON, installer's rc, no write.
#
# Drive-to-failure: revert the `stack_policy_record` hooks in the ctl — the
# present/disabled cases go red; break stack_key_for_id's map — the wrong-key
# case goes red.
#
# Run: bash scripts/dev/test-service-ctl-policy.sh   (stdlib bash only)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=etc/sa02m-web-service-ctl.sh
POLICY=etc/sa02m-stacks-policy.sh
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── Extraction (everything above the dispatch; markers must hold) ──────────
sed '/^ACTION=/,$d' "$SRC" > "$T/raw.sh"
for f in cmd_install cmd_uninstall stack_key_for_id stack_policy_record emit_result validate_id svc_is_installable; do
    grep -q "^${f}() {" "$T/raw.sh" \
        || { echo "FAIL  could not extract ${f}() from $SRC — the dispatch marker moved; fix this harness, do not delete it"; exit 1; }
done
# The nodered-ctl harness extracts by these two block markers — a move breaks
# ITS extraction silently only if this pin is deleted too (belt+braces; the
# nodered-ctl-install row is the real re-run of those expectations).
grep -q '^# ── Node-RED: staged payload' "$SRC" && grep -q '^# ── CODESYS uninstall' "$SRC" \
    || { echo "FAIL  the Node-RED block markers moved — test-nodered-ctl.sh extraction range broke"; exit 1; }

# Neutralise the ctl's own soft lib source (points at /usr/local/lib on a real
# board); the harness decides per-case whether the policy lib is loaded.
sed 's#^SA02M_STACKS_LIB=/usr/local/lib/sa02m-stacks-policy.sh#SA02M_STACKS_LIB=/nonexistent-in-test#' \
    "$T/raw.sh" > "$T/fn.sh"
grep -q 'SA02M_STACKS_LIB=/nonexistent-in-test' "$T/fn.sh" \
    || { echo "FAIL  soft-source retarget matched nothing — the ctl would source the real /usr/local/lib"; exit 1; }

export SA02M_STACKS_CONF="$T/etc/sa02m_stacks.conf"
mkdir -p "$T/etc"

# shellcheck disable=SC1090
. "$T/fn.sh"

# Stubs (defined AFTER sourcing so they win over any extracted definition):
# shellcheck disable=SC2034  # read by the SHIPPED code sourced above
LOG="$T/install.log"; : >"$LOG"
# shellcheck disable=SC2034  # read by the SHIPPED code sourced above
RESULT_DIR="$T/svcctl"
_STUB_RC=0
codesys_install()   { echo '{"ok":true,"id":"codesys","action":"install"}';   return "$_STUB_RC"; }
mplc4_install()     { echo '{"ok":true,"id":"mplc4","action":"install"}';     return "$_STUB_RC"; }
nodered_install()   { echo '{"ok":true,"id":"node-red","action":"install"}';  return "$_STUB_RC"; }
codesys_uninstall() { echo '{"ok":true,"id":"codesys","action":"uninstall"}'; return "$_STUB_RC"; }
mplc4_uninstall()   { echo '{"ok":true,"id":"mplc4","action":"uninstall"}';   return "$_STUB_RC"; }
nodered_uninstall() { echo '{"ok":true,"id":"node-red","action":"uninstall"}'; return "$_STUB_RC"; }

policy_of() { sa02m_stack_policy_get "$1"; }

echo "── 1. with the policy lib PRESENT ──"
# shellcheck disable=SC1090
. "$POLICY" || { echo "FAIL  cannot source $POLICY"; exit 1; }

# 1a. install rc 0 ⇒ present (per-ID map)
for pair in "codesys CODESYS" "mplc4 MPLC" "node-red NODERED"; do
    set -- $pair
    rm -f "$SA02M_STACKS_CONF"
    _STUB_RC=0
    out=$(cmd_install "$1"); rc=$?
    if [ "$rc" -eq 0 ] && [ "$(policy_of "$2")" = present ] && [[ "$out" == *'"ok":true'* ]]; then
        ok "install $1 rc0: policy $2=present, JSON ok, rc 0"
    else
        bad "install $1 rc0: rc=$rc policy=$(policy_of "$2") out=$out"
    fi
done

# 1b. uninstall rc 0 ⇒ disabled
for pair in "codesys CODESYS" "mplc4 MPLC" "node-red NODERED"; do
    set -- $pair
    rm -f "$SA02M_STACKS_CONF"
    sa02m_stack_policy_set "$2" present
    _STUB_RC=0
    out=$(cmd_uninstall "$1"); rc=$?
    if [ "$rc" -eq 0 ] && [ "$(policy_of "$2")" = disabled ]; then
        ok "uninstall $1 rc0: policy $2=disabled"
    else
        bad "uninstall $1 rc0: rc=$rc policy=$(policy_of "$2")"
    fi
done

# 1c. FAILED install/uninstall (rc 1) ⇒ nothing written, rc passes through
rm -f "$SA02M_STACKS_CONF"
_STUB_RC=1
out=$(cmd_install mplc4); rc=$?
if [ "$rc" -eq 1 ] && [ ! -f "$SA02M_STACKS_CONF" ]; then
    ok "install rc1: no policy write, rc 1 passed through"
else
    bad "install rc1: rc=$rc file=$([ -f "$SA02M_STACKS_CONF" ] && echo written || echo absent)"
fi
sa02m_stack_policy_set NODERED present
before=$(cat "$SA02M_STACKS_CONF")
out=$(cmd_uninstall node-red); rc=$?
if [ "$rc" -eq 1 ] && [ "$before" = "$(cat "$SA02M_STACKS_CONF")" ]; then
    ok "uninstall rc1: policy untouched, rc 1 passed through"
else
    bad "uninstall rc1: rc=$rc changed=$([ "$before" != "$(cat "$SA02M_STACKS_CONF")" ] && echo yes || echo no)"
fi
_STUB_RC=0

# 1d. a non-installable id never reaches the hooks
rm -f "$SA02M_STACKS_CONF"
out=$(cmd_install mosquitto); rc=$?
if [ "$rc" -eq 1 ] && [ ! -f "$SA02M_STACKS_CONF" ] && [[ "$out" == *not_installable* ]]; then
    ok "non-installable id: refused, no policy write"
else
    bad "non-installable id: rc=$rc out=$out"
fi

# 1e. stack_key_for_id maps exactly the three ids
[ "$(stack_key_for_id codesys)" = CODESYS ] && [ "$(stack_key_for_id mplc4)" = MPLC ] \
    && [ "$(stack_key_for_id node-red)" = NODERED ] && [ -z "$(stack_key_for_id docker)" ] \
    && ok "stack_key_for_id: codesys→CODESYS mplc4→MPLC node-red→NODERED, docker unmapped" \
    || bad "stack_key_for_id map wrong"

echo "── 1f. service_present mplc4: runtime payload, not wrappers ──"
# A half-removed install (leftover /etc/init.d/mplc4 + unit file, /opt/mplc4
# gone — bench 1.135, 2026-08-29) must read NOT installed, so the panel offers
# «Установить» instead of a dead «Пуск». The probe goes through the
# MPLC4_RUNTIME seam (sudoers env_reset keeps it unreachable from the web).
#
# The behavioural cases below cover the payload/process axes; the WRAPPER axis
# (no falling back to init.d / unit files) cannot be exercised behaviourally
# here — those paths are host-absolute and absent on every dev/CI host, so a
# regression re-adding them would stay green. Pin it statically instead, on
# the comment-stripped extracted block: any init.d / unit_file_installed
# token inside the mplc4 case is a FAIL. Non-vacuous: an extraction that
# misses the block (no MPLC4_RUNTIME line) FAILS rather than checking nothing.
_mplc4_block=$(awk '
    /^service_present\(\) \{/ { f = 1 }
    f && /^\}/ { f = 0 }
    f && /^[[:space:]]*mplc4\)/ { g = 1 }
    g { print }
    g && /;;/ { g = 0 }
' "$T/raw.sh" | sed 's/[[:space:]]*#.*$//')
if printf '%s' "$_mplc4_block" | grep -q 'MPLC4_RUNTIME'; then
    ok "service_present mplc4: block extracted (MPLC4_RUNTIME probe present)"
else
    bad "service_present mplc4: extraction missed the block — the wrapper pin below checks nothing"
fi
if printf '%s' "$_mplc4_block" | grep -Eq 'init\.d|unit_file_installed'; then
    bad "service_present mplc4: wrapper fallback (init.d/unit file) re-added to the probe"
else
    ok "service_present mplc4: no wrapper fallback in the probe (static pin)"
fi
MPLC4_RUNTIME="$T/opt/mplc4/start_mplc4.sh"
mplc4_process_active() { return 1; }
rm -rf "$T/opt/mplc4"
if service_present mplc4 "mplc4.service"; then
    bad "service_present mplc4: wrapper-only (no runtime payload) read as installed"
else
    ok "service_present mplc4: no runtime payload ⇒ not installed"
fi
mkdir -p "$T/opt/mplc4"
if service_present mplc4 "mplc4.service"; then
    bad "service_present mplc4: bare /opt/mplc4 dir (no start_mplc4.sh) read as installed"
else
    ok "service_present mplc4: bare runtime dir ⇒ not installed"
fi
# A shebang, not an empty file: on Windows git-bash `test -x` ignores the
# mode bits and derives executability from content (#!) — an empty +x file
# reads non-executable there and only there (CI Linux honours the chmod).
printf '#!/bin/sh\n' > "$T/opt/mplc4/start_mplc4.sh"; chmod +x "$T/opt/mplc4/start_mplc4.sh"
if service_present mplc4 "mplc4.service"; then
    ok "service_present mplc4: executable start_mplc4.sh ⇒ installed"
else
    bad "service_present mplc4: runtime payload present but read as not installed"
fi
rm -rf "$T/opt/mplc4"
mplc4_process_active() { return 0; }
if service_present mplc4 "mplc4.service"; then
    ok "service_present mplc4: live process without files ⇒ installed (Стоп must work)"
else
    bad "service_present mplc4: live process read as not installed"
fi
mplc4_process_active() { return 1; }

echo "── 2. with the policy lib ABSENT (older board) ──"
# Simulate: drop the policy functions; the ctl's `command -v` guard must skip.
unset -f sa02m_stack_policy_set sa02m_stack_policy_get sa02m_stack_id_valid 2>/dev/null || true
rm -f "$SA02M_STACKS_CONF"
_STUB_RC=0
out=$(cmd_install mplc4); rc=$?
if [ "$rc" -eq 0 ] && [[ "$out" == *'"ok":true'* ]] && [ ! -f "$SA02M_STACKS_CONF" ]; then
    ok "lib absent: install still emits JSON, rc 0, no policy file"
else
    bad "lib absent install: rc=$rc out=$out file=$([ -f "$SA02M_STACKS_CONF" ] && echo written || echo absent)"
fi
_STUB_RC=1
out=$(cmd_uninstall codesys); rc=$?
if [ "$rc" -eq 1 ]; then
    ok "lib absent: uninstall passes the installer's rc through"
else
    bad "lib absent uninstall: rc=$rc"
fi

echo ""
if [ "$fails" -eq 0 ]; then
    echo "service-ctl-policy-write: ALL OK"
    exit 0
fi
echo "service-ctl-policy-write: $fails FAILURE(S)"
exit 1
