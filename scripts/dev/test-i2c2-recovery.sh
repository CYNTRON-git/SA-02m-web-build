#!/usr/bin/env bash
# test-i2c2-recovery — the i2c-2 bus clear in etc/sa02m-pre-start.sh
# (sa02m_i2c2_gpio_recover), driven against a SIMULATED slave that holds SDA
# low. Bench 1.136, 2026-09-30: the old loop called `gpioset …=0` nine times,
# never let SCL rise, so no clock reached the wedged slave and every PCA9536
# transfer of the boot timed out («mv64xxx: I2C bus locked»).
#
# What it asserts, per scenario (slave releases after N clocks / never / SDA
# already high): SCL really rises (counted edges), the run stops clocking once
# SDA is free, a STOP is generated (SDA rising while SCL is high), no line is
# ever DRIVEN high (high = released, open-drain), both lines end released, the
# return status and the journal line say what happened.
#
# SAFETY: only the one function is extracted and eval'd — never the script's
# top level (sysfs unbind, MAC rewrite, reboot log). gpioset/gpioget/logp/sleep
# are shell functions, PATH is emptied inside the run, so a missing stub fails
# instead of reaching a real GPIO chip on a root host.
#
# Usage: bash scripts/dev/test-i2c2-recovery.sh [source-file] [function-name]
# (the arguments exist so the RED can be measured on another revision.)
#
# One STATIC pin besides the behaviour: the boot path must CALL the function —
# a tested function nobody runs protects nothing. It reads
# sa02m_i2c2_recover_and_init's body with comment lines dropped, so commenting
# the call out fails; that case is registered in comment-mutation-proof.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="${1:-$ROOT/etc/sa02m-pre-start.sh}"
FN="${2:-sa02m_i2c2_gpio_recover}"

fails=0
ok()  { printf 'test-i2c2-recovery: ok    %s\n' "$*"; }
bad() { printf 'test-i2c2-recovery: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

[ -f "$SRC" ] || { echo "test-i2c2-recovery: FAIL — $SRC not found"; exit 1; }
FN_TEXT=$(awk -v fn="$FN" '
    index($0, fn "() {") == 1 { f = 1 }
    f { print }
    f && /^}/ { exit }
' "$SRC")
if ! printf '%s\n' "$FN_TEXT" | grep -q '[^[:space:]]'; then
    echo "test-i2c2-recovery: FAIL — no ${FN}() in $SRC (the bus clear must be its own function)"
    exit 1
fi

STATE=$(mktemp -d) || { echo "test-i2c2-recovery: FAIL — mktemp"; exit 1; }
trap 'rm -rf "$STATE"' EXIT

# scenario RELEASE_AFTER: rising SCL edges before the slave lets SDA go
# (0 = SDA already high, -1 = never).
run_scenario() {
    local release_after=$1
    rm -rf "${STATE:?}"/*          # :? — never `rm -rf /*` on an empty var
    echo 0 > "$STATE/scl_low"; echo 0 > "$STATE/sda_low"     # master drive
    echo 0 > "$STATE/rises"; echo 0 > "$STATE/stop"
    : > "$STATE/events"; : > "$STATE/journal"; : > "$STATE/violations"
    if [ "$release_after" = 0 ]; then echo 0 > "$STATE/hold"; else echo 1 > "$STATE/hold"; fi
    echo "$release_after" > "$STATE/release_after"
    (
        S=$STATE
        # shellcheck disable=SC2034  # read by the eval'd function
        I2C2_SCL_LINE=256
        # shellcheck disable=SC2034
        I2C2_SDA_LINE=257
        level() {   # $1 = scl|sda → 0/1 on the wire
            if [ "$1" = scl ]; then
                [ "$(<"$S/scl_low")" = 1 ] && echo 0 || echo 1
            else
                { [ "$(<"$S/sda_low")" = 1 ] || [ "$(<"$S/hold")" = 1 ]; } && echo 0 || echo 1
            fi
        }
        drive() {   # $1 = scl|sda, $2 = 1 (drive low) | 0 (release)
            local before_scl before_sda after_scl after_sda r master_sda_was
            before_scl=$(level scl); before_sda=$(level sda)
            master_sda_was=$(<"$S/sda_low")
            echo "$2" > "$S/${1}_low"
            after_scl=$(level scl)
            if [ "$before_scl" = 0 ] && [ "$after_scl" = 1 ]; then
                r=$(( $(<"$S/rises") + 1 )); echo "$r" > "$S/rises"
                if [ "$(<"$S/hold")" = 1 ] && [ "$(<"$S/release_after")" != -1 ] \
                        && [ "$r" -ge "$(<"$S/release_after")" ]; then
                    echo 0 > "$S/hold"
                fi
            fi
            # A STOP is the MASTER releasing its own SDA drive while SCL is
            # high. The slave letting go of SDA mid-clock is not one — counting
            # it hid a missing STOP (round-3 mutation R-c).
            after_sda=$(level sda)
            if [ "$1" = sda ] && [ "$2" = 0 ] && [ "$master_sda_was" = 1 ] \
                    && [ "$before_sda" = 0 ] && [ "$after_sda" = 1 ] \
                    && [ "$(level scl)" = 1 ]; then
                echo 1 > "$S/stop"
            fi
        }
        name_of() { case "$1" in 256) echo scl ;; 257) echo sda ;; *) echo "?$1" ;; esac; }
        gpioset() {
            local arg line val n; shift    # $1 = the chip, one here
            for arg in "$@"; do
                line=${arg%%=*}; val=${arg#*=}; n=$(name_of "$line")
                echo "set $n=$val" >> "$S/events"
                if [ "$val" = 1 ]; then
                    echo "$n driven HIGH (push-pull fights the slave)" >> "$S/violations"
                    drive "$n" 0
                else
                    drive "$n" 1
                fi
            done
        }
        gpioget() {   # requests the line as INPUT: releases it, returns the level
            local n; n=$(name_of "$2")
            echo "get $n" >> "$S/events"
            drive "$n" 0
            level "$n"
        }
        logp() { echo "$*" >> "$S/journal"; }
        sleep() { :; }
        # shellcheck disable=SC2123  # deliberate: a missing stub must fail, not reach a real GPIO tool
        PATH=/nonexistent-test-i2c2-recovery
        eval "$FN_TEXT"
        "$FN"
        echo $? > "$S/rc"
    )
}

final() { cat "$STATE/$1"; }

# --- 1. the slave lets go after 3 clocks ---------------------------------------
run_scenario 3
rises=$(final rises); rc=$(final rc)
if [ "$rises" -ge 3 ]; then ok "stuck slave (releases after 3): SCL rose $rises time(s)"
else bad "stuck slave (releases after 3): SCL rose only $rises time(s) — no clock reached the slave"; fi
if [ "$rises" -le 5 ]; then ok "clocking stops once SDA is free ($rises edges incl. STOP, not 9+)"
else bad "clocked $rises times after the slave had already let SDA go"; fi
[ "$(final stop)" = 1 ] && ok "a STOP is generated (SDA rises while SCL is high)" \
    || bad "no STOP condition — the slave is left mid-transaction"
[ "$(final scl_low)" = 0 ] && [ "$(final sda_low)" = 0 ] && ok "both lines end released" \
    || bad "a line is left DRIVEN LOW (scl_low=$(final scl_low) sda_low=$(final sda_low)) — the bus stays blocked"
[ "$rc" = 0 ] && ok "returns 0 when the bus came free" || bad "returned $rc although SDA was released"
grep -q 'released after' "$STATE/journal" && ok "journal says the bus came free" \
    || bad "journal does not say the bus came free: $(tr '\n' '|' < "$STATE/journal")"
[ -s "$STATE/violations" ] && bad "$(head -1 "$STATE/violations")" || ok "no line was ever driven high"

# --- 2. the slave never lets go ---------------------------------------------
run_scenario -1
rises=$(final rises); rc=$(final rc)
if [ "$rises" -ge 9 ]; then ok "hard-stuck slave: all 9 clocks given ($rises edges incl. STOP)"
else bad "hard-stuck slave: only $rises SCL rising edge(s), fewer than the 9 a bus clear owes"; fi
[ "$(final scl_low)" = 0 ] && [ "$(final sda_low)" = 0 ] && ok "hard-stuck: both lines end released" \
    || bad "hard-stuck: a line is left DRIVEN LOW (scl_low=$(final scl_low) sda_low=$(final sda_low))"
[ "$rc" != 0 ] && ok "hard-stuck: returns non-zero" || bad "hard-stuck: returned 0 although SDA is still low"
grep -q 'still low' "$STATE/journal" && ok "hard-stuck: journal says the bus stays down" \
    || bad "hard-stuck: journal does not name the failure: $(tr '\n' '|' < "$STATE/journal")"
[ -s "$STATE/violations" ] && bad "hard-stuck: $(head -1 "$STATE/violations")" || ok "hard-stuck: no line was ever driven high"

# --- 3. a healthy bus is left alone ---------------------------------------------
run_scenario 0
if grep -q '^set ' "$STATE/events"; then bad "SDA was high, yet the bus was driven: $(grep '^set ' "$STATE/events" | head -2 | tr '\n' '|')"
else ok "healthy bus: nothing driven"; fi
[ "$(final rc)" = 0 ] && ok "healthy bus: returns 0" || bad "healthy bus: returned $(final rc)"

# --- 4. the boot path calls it (code only — a commented-out call fails) ----------
if [ "$FN" = sa02m_i2c2_gpio_recover ]; then
    CALLER=$(awk '
        index($0, "sa02m_i2c2_recover_and_init() {") == 1 { f = 1; next }
        f && /^}/ { exit }
        f && $0 !~ /^[[:space:]]*#/ { print }
    ' "$SRC")
    if ! printf '%s\n' "$CALLER" | grep -q '[^[:space:]]'; then
        bad "sa02m_i2c2_recover_and_init() not found — cannot tell whether the boot path runs the bus clear"
    elif printf '%s\n' "$CALLER" | grep -qE '^[[:space:]]*sa02m_i2c2_gpio_recover([[:space:]]|$)'; then
        ok "the boot path calls sa02m_i2c2_gpio_recover (in code, not a comment)"
    else
        bad "sa02m_i2c2_recover_and_init no longer calls sa02m_i2c2_gpio_recover — the bus clear is tested but never run"
    fi
fi

# --- non-vacuity: the stubs were reached ----------------------------------------
run_scenario 3
[ "$(grep -c . "$STATE/events")" -ge 4 ] && ok "non-vacuity: the function drove the simulated bus ($(grep -c . "$STATE/events") events)" \
    || bad "non-vacuity: the function barely touched the simulated bus — the harness tested nothing"

if [ "$fails" -eq 0 ]; then
    echo "test-i2c2-recovery: ALL OK"
    exit 0
fi
echo "test-i2c2-recovery: $fails FAILURE(S)"
exit 1
