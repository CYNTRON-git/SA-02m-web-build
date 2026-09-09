#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-watchdog-hold.sh — regression harness for the runtime-watchdog hold
# shared by install.sh (scripts/lib.sh), etc/sa02m-update-runner.sh and
# etc/sa02m-factory-reset-runner.sh. Quality row `watchdog-hold`.
# 8D bench-136 reset (.ai-dev/8d/bench-136-reset.md, D5 G).
#
# Why this exists: the runners' precedent guard was
# `systemctl set-property --runtime Manager RuntimeWatchdogSec=0 … || true`
# followed by `log "… RuntimeWatchdogSec=0"` — it logged the guarantee without
# ever checking it, the exact overclaim shape quality-gate-rigor.md is about.
# The board's manager exposes the value as the D-Bus property
# RuntimeWatchdogUSec (policy home: etc/systemd/sa02m-watchdog.conf
# `[Manager] RuntimeWatchdogSec=15s` → /etc/systemd/system.conf.d/), and the
# property is writable only on systemd >= 250 — so the ONE thing this harness
# exists to prove is that the helper READS THE VALUE BACK and reports a
# manager that ignored the write instead of claiming success.
#
# Method: source the SHIPPED scripts/lib.sh and drive it through PATH-first
# `busctl` / `systemctl` shims backed by a state file, in three manager
# flavours — writable (systemd >= 250), hollow (a write is accepted and
# changes nothing — systemd < 250 / a refused polkit), and unreadable (no
# busctl at all: a chroot / rootfs build). Then the wiring pins, read
# COMMENT-STRIPPED through .ai-dev/quality/checks/lib_check.sh: the shared
# block is byte-identical in ALL THREE homes, install.sh holds and restores
# it, neither runner keeps a hollow one-liner, and the factory-reset runner
# releases the hold on an abort.
#
# Three homes, not two: the block cannot be sourced from one file — install.sh
# reads scripts/lib.sh out of an extracted tree, while both runners execute
# standalone on the device, where scripts/ is not deployed. Duplication by
# construction, which is why case 7 pins the copies byte-for-byte.
#
# Drive-to-failure, measured 2026-09-09: delete the two read-back lines from
# sa02m_runtime_watchdog_set on a scratch copy passed as SVC_HELPERS_LIB=,
# and case 3 goes GREEN-on-a-lie → the harness reports it as a FAIL (it
# asserts rc≠0 for the hollow manager); revert either runner to
# `systemctl set-property --runtime Manager RuntimeWatchdogSec=0` and case 9
# resp. 10 reports it; change one byte of any copy of the block and case 7
# reports the diff.
#
# The 1.0.6.41 factory-reset pins, measured the same day on a scratch copy of
# the tree (each mutation applied alone, the rest pristine): the unfixed
# factory runner → 7c + 10a/10b + 11a–11d RED (7 failures); one byte changed in
# its copy of the block → 7c; the `RuntimeWatchdogSec=0` one-liner put back →
# 10a; `#trap on_exit EXIT` → 11a + 11c; `#trap 'exit 143' INT TERM` → 11b.
#
# Comment-mutation: cases 7–11 pin live lines and are registered in
# comment-mutation-proof (commenting the install.sh hold, its trap, or any
# BEGIN marker out turns 7/8 RED — each pin FAILS when its line is missing).
#
# Run: bash scripts/dev/test-watchdog-hold.sh   (bash + sed + coreutils)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
LIB=${SVC_HELPERS_LIB:-scripts/lib.sh}
RUNNER=etc/sa02m-update-runner.sh
FACTORY=etc/sa02m-factory-reset-runner.sh
BEGIN='# ── BEGIN sa02m-runtime-watchdog'
END='# ── END sa02m-runtime-watchdog'

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

export LOG_FILE="$T/install.log"
# shellcheck disable=SC1090
source "$LIB" || { echo "FAIL  cannot source $LIB"; exit 1; }
for fn in sa02m_runtime_watchdog_usec sa02m_runtime_watchdog_set; do
    declare -F "$fn" >/dev/null || { echo "FAIL  $LIB does not define $fn() — the shared watchdog block is missing"; exit 1; }
done
log() { :; }

# ── Shims: a manager whose behaviour is set by $T/mode ─────────────────────
mkdir -p "$T/bin"
printf '15000000\n' > "$T/value"
printf 'writable\n' > "$T/mode"
: > "$T/calls"
cat > "$T/bin/busctl" <<SHIM
#!/bin/bash
T="$T"
SHIM
cat >> "$T/bin/busctl" <<'SHIM'
printf 'busctl %s\n' "$1" >> "$T/calls"
case "${1:-}" in
    get-property) printf 't %s\n' "$(cat "$T/value")" ;;
    set-property)
        # the requested value is the last argument
        for want; do :; done
        [ "$(cat "$T/mode")" = writable ] && printf '%s\n' "$want" > "$T/value"
        # a hollow manager accepts the call and changes nothing — the systemd
        # < 250 shape this harness exists for
        ;;
    *) exit 1 ;;
esac
exit 0
SHIM
cat > "$T/bin/systemctl" <<SHIM
#!/bin/bash
T="$T"
SHIM
cat >> "$T/bin/systemctl" <<'SHIM'
printf 'systemctl %s %s\n' "${1:-}" "${2:-}" >> "$T/calls"
exit 0
SHIM
chmod +x "$T/bin"/*
PATH="$T/bin:$PATH"

echo "── 1. read the manager's current value ──"
now=$(sa02m_runtime_watchdog_usec); rc=$?
[ "$rc" = 0 ] && [ "$now" = 15000000 ] \
    && ok "1 RuntimeWatchdogUSec read back as $now µs" \
    || bad "1 read: rc=$rc value='$now' (expected rc=0 / 15000000)"

echo "── 2. writable manager: the hold takes effect ──"
: > "$T/calls"
out=$(sa02m_runtime_watchdog_set 0); rc=$?
if [ "$rc" = 0 ] && [ "$out" = 0 ] && [ "$(cat "$T/value")" = 0 ] && grep -q 'busctl set-property' "$T/calls"; then
    ok "2 set 0 → in force 0 µs, rc=0, via the bus write"
else
    bad "2 set 0: rc=$rc printed='$out' value=$(cat "$T/value") calls='$(tr '\n' ' ' < "$T/calls")'"
fi

echo "── 3. restore puts the previous value back ──"
out=$(sa02m_runtime_watchdog_set 15000000); rc=$?
[ "$rc" = 0 ] && [ "$out" = 15000000 ] && [ "$(cat "$T/value")" = 15000000 ] \
    && ok "3 restore 15000000 → in force 15000000 µs, rc=0" \
    || bad "3 restore: rc=$rc printed='$out' value=$(cat "$T/value")"

echo "── 4. HOLLOW manager (write accepted, nothing changes) is REPORTED ──"
printf 'hollow\n' > "$T/mode"; : > "$T/calls"
out=$(sa02m_runtime_watchdog_set 0); rc=$?
if [ "$rc" != 0 ] && [ "$out" = 15000000 ]; then
    ok "4a rc=$rc and the value ACTUALLY in force ($out µs) is what the caller is told — not a claimed 0"
else
    bad "4a hollow manager: rc=$rc printed='$out' — a no-op write was reported as success (the 1.0.6.40 guard's defect)"
fi
if grep -q 'systemctl set-property' "$T/calls"; then
    ok "4b the systemctl fallback was tried before giving up"
else
    bad "4b no systemctl set-property attempt after the bus write failed: '$(tr '\n' ' ' < "$T/calls")'"
fi
printf 'writable\n' > "$T/mode"

echo "── 5. unreadable manager (no busctl: chroot / rootfs build) ──"
# An EMPTY PATH, not a real one: on a systemd host /usr/bin/busctl exists and
# `set 0` would reach the REAL service manager. The functions are already
# defined in this shell, so a subshell with nothing on PATH exercises exactly
# the "no busctl, no systemctl" branch and can touch nothing live.
mkdir -p "$T/bin-none"
out=$(PATH="$T/bin-none"; sa02m_runtime_watchdog_usec; echo "rc=$?")
[ "$out" = "rc=1" ] && ok "5a read with no busctl: rc=1, nothing printed" \
                    || bad "5a read with no busctl: got '$out' (expected rc=1)"
out=$(PATH="$T/bin-none"; sa02m_runtime_watchdog_set 0; echo "rc=$?")
[ "$out" = "rc=1" ] && ok "5b set with no busctl: rc=1 (never claims a hold it could not make)" \
                    || bad "5b set with no busctl: got '$out' (expected rc=1)"

echo "── 6. argument floor ──"
rc=0; sa02m_runtime_watchdog_set >/dev/null 2>&1 || rc=$?
[ "$rc" = 2 ] && ok "6a no argument → rc=2" || bad "6a no argument: rc=$rc (expected 2)"
rc=0; sa02m_runtime_watchdog_set 15s >/dev/null 2>&1 || rc=$?
[ "$rc" = 2 ] && ok "6b a non-µs argument (15s) → rc=2, never silently mis-set" || bad "6b '15s': rc=$rc (expected 2)"

echo "── 7. one home: the shared block is byte-identical in all THREE callers ──"
block_of() {  # block_of <file> <out>
    awk -v b="$BEGIN" -v e="$END" 'index($0,b)==1{f=1} f{print} f&&index($0,e)==1{exit}' "$1" > "$2"
    [ -s "$2" ]
}
if ! block_of "$LIB" "$T/a"; then
    bad "7a no shared watchdog block in $LIB — the reference copy is gone"
else
    n=$(wc -l < "$T/a")
    [ "$n" -ge 40 ] && ok "7a reference block in $LIB ($n lines)" \
                    || bad "7a block is only $n lines — the extraction stopped seeing the body"
    for pair in "7b:$RUNNER" "7c:$FACTORY"; do
        id=${pair%%:*}; f=${pair#*:}
        if ! block_of "$f" "$T/b"; then
            bad "$id no shared watchdog block in $f — it keeps its own guard"
        elif ! cmp -s "$T/a" "$T/b"; then
            bad "$id the copy in $f has drifted from $LIB:"; diff "$T/a" "$T/b" | head -10
        else
            ok "$id $f carries the block byte-identically"
        fi
    done
fi

echo "── 8. install.sh holds it and restores on EXIT ──"
# shellcheck source=/dev/null
if . .ai-dev/quality/checks/lib_check.sh 2>/dev/null && declare -F stripped_first_line >/dev/null; then
    hold=$(stripped_first_line install.sh '^[[:space:]]*if _wdt_now=\$\(sa02m_runtime_watchdog_set 0\)')
    trapl=$(stripped_first_line install.sh '^trap sa02m_restore_runtime_watchdog EXIT$')
    restore=$(stripped_first_line install.sh 'sa02m_runtime_watchdog_set "\$SA02M_WDT_PREV"')
    mod=$(stripped_first_line install.sh '^sa02m_run_module 01-system\.sh$')
    [ -n "$hold" ]    && ok "8a install.sh takes the hold (l.$hold)"            || bad "8a install.sh never calls sa02m_runtime_watchdog_set 0"
    [ -n "$trapl" ]   && ok "8b EXIT trap restores it (l.$trapl)"               || bad '8b install.sh has no `trap sa02m_restore_runtime_watchdog EXIT` line'
    [ -n "$restore" ] && ok "8c the restore path writes the READ-BACK previous value (l.$restore)" \
                      || bad "8c install.sh restores something other than the captured value"
    if [ -n "$hold" ] && [ -n "$mod" ] && [ "$hold" -lt "$mod" ]; then
        ok "8d the hold (l.$hold) is taken BEFORE the first module runs (l.$mod)"
    else
        bad "8d the hold is not taken before the first module (hold=${hold:-none}, first module=${mod:-none})"
    fi

    echo "── 9/10. neither runner keeps a hollow guard ──"
    # EXACTLY ONE live `set-property --runtime Manager` line may exist in a
    # runner: the shared helper's own `RuntimeWatchdogSec=${want}us`, whose
    # result is read back on the next line. Any other value — the literal 0,
    # the old `${RUNTIME_WDT_RESTORE}`, a hardcoded 15s — is an unchecked
    # write and fails here. (A first cut banned only `=0` / `=${UPPER}` and a
    # re-introduced `=15s` slipped through it: measured 2026-09-09.) The
    # factory-reset runner carried exactly that unchecked `=0` / `=15s` pair —
    # on the longest single operation the board runs — until 1.0.6.41.
    runner_pins() {   # $1=case number  $2=file
        local id=$1 f=$2 sp_all sp_helper hollow r_read r_hold r_rest
        sp_all=$(stripped_count "$f" 'set-property --runtime Manager')
        sp_helper=$(stripped_count "$f" 'set-property --runtime Manager "RuntimeWatchdogSec=\$\{want\}us"')
        hollow=$((sp_all - sp_helper))
        [ "$sp_helper" = 1 ] || hollow=$((hollow + 1))   # the helper's own line must be there
        r_read=$(stripped_count "$f" 'RUNTIME_WDT_PREV=\$\(sa02m_runtime_watchdog_usec')
        r_hold=$(stripped_count "$f" 'sa02m_runtime_watchdog_set 0')
        r_rest=$(stripped_count "$f" 'sa02m_runtime_watchdog_set "\$RUNTIME_WDT_PREV"')
        [ "$hollow" = 0 ] && ok "${id}a $f: the only live set-property is the helper's read-back-checked one" \
                          || bad "${id}a $f still carries $hollow unchecked set-property line(s) — the hollow guard is back"
        if [ "$r_read" = 1 ] && [ "$r_hold" = 1 ] && [ "$r_rest" = 1 ]; then
            ok "${id}b $f reads back, holds and restores through the shared helper (1 site each)"
        else
            bad "${id}b $f wiring: read=$r_read hold=$r_hold restore=$r_rest (expected 1/1/1)"
        fi
    }
    runner_pins 9 "$RUNNER"
    runner_pins 10 "$FACTORY"

    echo "── 11. the factory reset releases the hold on an ABORT ──"
    # A factory reset is the longest single-purpose operation on the board, so
    # the path that matters most is the one that does NOT reach the end: the
    # pre-1.0.6.41 script restored the watchdog only from cleanup_imaging_lock,
    # reached by the ERR trap and by fail() — a SIGTERM (systemd stopping the
    # job, an operator abort) left the manager's watchdog off with no owner.
    f_trap=$(stripped_first_line "$FACTORY" '^trap on_exit EXIT$')
    f_sig=$(stripped_first_line "$FACTORY" '^trap .* INT TERM$')
    f_disp=$(stripped_first_line "$FACTORY" '^case "\$CMD" in$')
    f_held=$(stripped_count "$FACTORY" '^[[:space:]]*IMAGING_HELD=1$')
    [ -n "$f_trap" ] && ok "11a the factory runner arms an EXIT trap (l.$f_trap)" \
                     || bad "11a no 'trap on_exit EXIT' in $FACTORY — an abort keeps the watchdog held off"
    [ -n "$f_sig" ]  && ok "11b INT/TERM are turned into an exit so the EXIT trap runs (l.$f_sig)" \
                     || bad "11b no INT/TERM trap in $FACTORY — bash kills the shell without running the EXIT trap"
    if [ -n "$f_trap" ] && [ -n "$f_disp" ] && [ "$f_trap" -lt "$f_disp" ]; then
        ok "11c the trap is armed (l.$f_trap) BEFORE the command dispatch (l.$f_disp)"
    else
        bad "11c the trap is not armed before the dispatch (trap=${f_trap:-none}, dispatch=${f_disp:-none})"
    fi
    [ "$f_held" = 1 ] && ok "11d the hold is bookkept (IMAGING_HELD=1) so the trap restores only what it took" \
                      || bad "11d IMAGING_HELD=1 appears $f_held time(s) in $FACTORY (expected 1)"
else
    bad "8–11 cannot source .ai-dev/quality/checks/lib_check.sh — the wiring pins did NOT run (a skip is not a pass)"
fi

echo ""
if [ "$fails" -eq 0 ]; then
    echo "watchdog-hold: ALL OK"
    exit 0
fi
echo "watchdog-hold: $fails FAILURE(S)"
exit 1
