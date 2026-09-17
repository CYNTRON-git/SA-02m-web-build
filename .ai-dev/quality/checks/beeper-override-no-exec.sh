#!/usr/bin/env bash
# beeper-override-no-exec — /run/sa02m-hw-override/beeper.env is DATA, and the
# worker that reads it must never execute a byte of it.
#
# WHY THIS ROW EXISTS. Until 1.0.6.45 etc/sa02m-beeper-override.sh parsed the
# override file by SOURCING it (`. "$OVERRIDE_FILE"`), and its `case`
# validation of `value` ran AFTER the file had already executed. Three facts
# turned that into arbitrary code execution as root:
#   * the file's directory ships as `d /run/sa02m-hw-override 0775 www-data
#     www-data` (scripts/03-webserver.sh), so www-data holds the directory
#     write bit and can rename any content over beeper.env whatever the file's
#     own mode says;
#   * sa02m-modbus-mqtt.service runs `User=root`, and since 1.0.6.43
#     sa02m_telemetry.py spawns this worker from that root daemon with no
#     privilege drop — before 1.0.6.43 the only starter was lib_hw.sh, running
#     as www-data, where www-data sourcing a www-data file escalated nothing;
#   * the worker re-reads the file every INTERVAL (0.2 s) for the whole TTL, so
#     the window is ~35 executions, not one.
# It was reachable from an MQTT publish to /devices/<id>/controls/beeper/on
# while MPLC4 held the expander — the normal state — and the local broker is
# loopback with allow_anonymous, so one www-data foothold reached both halves.
# No board ever ran 1.0.6.43; the hole lived in main and was deployed nowhere.
#
# WHAT IT MEASURES — behaviour, not text. The worker is really RUN, in a temp
# sandbox, over a real override file carrying a payload, and the check asserts
# the payload's marker file was NOT created. That is the mutation
# docs/agent-rules/quality-gate-rigor.md demands: the RED it was written
# against is the defect executing, observed, not a grep for `source`.
#
# NOTHING PRIVILEGED IS TOUCHED, and not by accident. The sandbox supplies its
# own SA02M_BEEPER_OVERRIDE_FILE, SA02M_I2C_LOCK_FILE and an absent HW_CONF, so
# no real /run path and no real config is read. The bus is neutralised
# EXPLICITLY rather than by hoping i2c-tools is absent — see run_worker: this
# row must stay safe to run ON a board, where /usr/sbin/i2cget is present and
# an accepted case would otherwise drive the live buzzer. The planted payload
# is a marker-file write inside the same temp dir; it runs with the privileges
# of whoever runs the quality suite, never root's, and its whole job is to be
# observable.
#
# HOW ACCEPT/REJECT IS OBSERVED WITHOUT AN I2C BUS. The worker's tool paths are
# absolute (/usr/sbin/i2cget), so they cannot be shimmed on PATH — and making
# them overridable would be a feature change in a security fix. Instead the
# worker runs under `sh -x` and the trace is read: `flock` appears in it if and
# only if read_override returned 0 and the loop entered its locked subshell.
# A rejected file leaves the trace with no `flock` at all, because the string
# occurs nowhere else in the script. Every accept case asserts the token IS
# there, so a change that breaks this observation fails loudly instead of
# turning the reject cases vacuously green.
#
# THE PRODUCER HALF. `covers` names lib_hw.sh and sa02m_telemetry.py because
# they can BREAK this guarantee from outside the worker
# (docs/agent-rules/quality-gate-rigor.md (c)): the parser now accepts exactly
# two keys, so a producer that starts emitting a third would have its beep
# silently refused. Section D reads both producers' write functions and fails
# when their emitted key set is not exactly {value, expires_at} — in either
# direction, and non-vacuously (a function whose keys cannot be extracted at
# all FAILS rather than passing on an empty set).
#
# COMMENT-OUT MUTATION: registered in comment-mutation-proof (case
# `beeper-override-no-exec|etc/sa02m-beeper-override.sh|# any other line: refuse the file`).
# Commenting that arm out leaves a `case` with no default, so a planted line is
# silently ignored instead of refused and section B goes RED. The static half
# (section C) is a fail-IF-PRESENT sweep and needs the opposite mutation —
# re-introducing execution — which section A already performs behaviourally.
# Section C is also the WEAKER half by construction and says so at its own
# site: it pins the literal line that shipped, so an indirect revival slips
# past it and only the running worker in section A catches that.
#
# Run: bash .ai-dev/quality/checks/beeper-override-no-exec.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
WORKER="$ROOT/etc/sa02m-beeper-override.sh"
LIB_HW="$ROOT/www/network_config/cgi-bin/lib_hw.sh"
TELEMETRY="$ROOT/opt/sa02m-modbus-mqtt/sa02m_telemetry.py"

fails=0
ok()  { printf 'beeper-override-no-exec: ok    %s\n' "$*"; }
bad() { printf 'beeper-override-no-exec: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

for f in "$WORKER" "$LIB_HW" "$TELEMETRY"; do
    [ -f "$f" ] || { printf 'beeper-override-no-exec: FAIL  %s is absent — the check has nothing to measure\n' "$f"; exit 1; }
done
command -v timeout >/dev/null 2>&1 || {
    printf 'beeper-override-no-exec: FAIL  timeout(1) is required to bound the worker run\n'; exit 1; }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

caseno=0
CASE_DIR=""
MARKER=""
CASE_RC=0

# Allocates the next sandbox BEFORE the fixture is built, so a payload can name
# its own marker path inside it.
new_case() {
    caseno=$((caseno + 1))
    CASE_DIR="$SANDBOX/case$caseno"
    MARKER="$CASE_DIR/pwned"
    mkdir -p "$CASE_DIR/run" "$CASE_DIR/lock"
}

# Runs the worker over $1 as its override file, in the sandbox new_case just
# allocated. `timeout 20` is the ceiling (see live() for why every fixture TTL
# is short); a run that hits it is reported, because every assertion after a
# kill is unreliable.
run_worker() {  # $1 = override-file content
    printf '%s' "$1" > "$CASE_DIR/run/beeper.env"
    # SA02M_I2C_EXP_BUS is the reason an ACCEPTED case cannot reach hardware.
    # The worker's i2cget/i2cset paths are absolute and unshimmable, so on a
    # host that HAS i2c-tools — a board, or a dev machine with them installed —
    # an accepted case would otherwise run `i2cget -y 2 0x41 0x01` against the
    # real PCA9536 and, on success, `i2cset` the output port: the live buzzer,
    # alarm LED and discrete output share that byte. Pointing the bus at a name
    # no adapter can carry makes the READ fail, and apply_once returns on a
    # failed read (`reg=$(i2c_get 0x01) || return 1`) — so no write is ever
    # attempted. The reserved 0x7f address is the second belt.
    HW_CONF="$CASE_DIR/no-such-hw.conf" \
    SA02M_BEEPER_OVERRIDE_FILE="$CASE_DIR/run/beeper.env" \
    SA02M_I2C_LOCK_FILE="$CASE_DIR/lock/pca9536.lock" \
    SA02M_I2C_EXP_BUS=sa02m-quality-gate-no-such-bus \
    SA02M_I2C_EXP_ADDR=0x7f \
    SA02M_BEEPER_OVERRIDE_INTERVAL_SEC=0.05 \
        timeout 20 sh -x "$WORKER" >"$CASE_DIR/out" 2>"$CASE_DIR/trace"
    CASE_RC=$?
    if [ "$CASE_RC" -eq 124 ]; then
        bad "the worker did not finish within 20 s — every assertion on this case is unreliable"
    fi
    # The self-guard on the paragraph above: no case, accepted or refused, may
    # ever reach a BUS WRITE. `i2cset -y` is the invocation form — the bare
    # `I2CSET=/usr/sbin/i2cset` assignment lines carry no `-y`, so this does
    # not match them. Honesty about where it bites: on a host without flock(1)
    # (Windows git-bash) apply_once is unreachable and this guard cannot fire;
    # it is load-bearing exactly where it matters — CI and a board, where flock
    # and i2c-tools both exist and only the neutralised bus stands between an
    # accepted case and the live PCA9536 output port.
    bus_writes=$(grep -c 'i2cset .*-y' "$CASE_DIR/trace" || true)
    if [ "${bus_writes:-0}" -ne 0 ]; then
        bad "a worker run attempted an I2C WRITE ($bus_writes) — the sandbox's bus neutralisation is broken and this row is unsafe to run on a board"
    fi
}

assert_no_marker() {  # $1 = label
    if [ -e "$MARKER" ]; then
        bad "$1: the planted payload EXECUTED — $MARKER exists"
    else
        ok "$1: the planted payload did not execute"
    fi
}

# `flock` is in the trace iff read_override returned 0 (see the header).
assert_accepted() {  # $1 = label
    if grep -q 'flock' "$CASE_DIR/trace"; then
        ok "$1: accepted — the worker reached its locked apply"
    else
        bad "$1: REFUSED a well-formed override file — the panel's beep would not sound"
    fi
}

assert_refused() {  # $1 = label
    if grep -q 'flock' "$CASE_DIR/trace"; then
        bad "$1: the worker ACCEPTED a file it must refuse"
    else
        ok "$1: refused"
    fi
}

# Every live TTL in this fixture is SHORT on purpose. An accepted file makes
# the worker loop until the TTL passes, and on the PRE-fix worker the payload
# cases are accepted too — an hour-long TTL would turn each of them into a 20 s
# `timeout` kill and report the defect as a hung harness instead of as executed
# code. Two seconds is ~40 iterations at the 0.05 s interval and comfortably
# clear of the worker's own sub-100 ms startup.
live() { printf '%s\n' "$(( $(date +%s) + 2 ))"; }
past() { printf '%s\n' "$(( $(date +%s) - 1 ))"; }

# ── A. execution: a payload in the file must not run ────────────────────────
# The shape the exploit takes: two well-formed keys plus one extra shell line.
# On the PRE-fix worker every one of these is accepted, because the `case`
# validation ran only after the sourcing had already executed the payload.
new_case
run_worker "$(printf 'value=1\nexpires_at=%s\n: > %s\n' "$(live)" "$MARKER")"
assert_no_marker "A1 extra command line"
assert_refused   "A1 extra command line"

# Command substitution inside an otherwise plausible value — proves the reader
# does no expansion, not merely that it reads line by line.
new_case
run_worker "$(printf 'value=$(: > %s)\nexpires_at=%s\n' "$MARKER" "$(live)")"
assert_no_marker "A2 \$( ) in value"
assert_refused   "A2 \$( ) in value"

# Backticks in expires_at — the same class through the other substitution form.
new_case
run_worker "$(printf 'value=1\nexpires_at=`: > %s`\n' "$MARKER")"
assert_no_marker "A3 backticks in expires_at"
assert_refused   "A3 backticks in expires_at"

# A payload before any well-formed key: nothing may run ahead of the first
# validation either.
new_case
run_worker "$(printf ': > %s\nvalue=1\nexpires_at=%s\n' "$MARKER" "$(live)")"
assert_no_marker "A4 payload on the first line"
assert_refused   "A4 payload on the first line"

# Assignment plus command on one line — the form that reads most like config.
new_case
run_worker "$(printf 'value=1\nexpires_at=%s\nPATH=/tmp; : > %s\n' "$(live)" "$MARKER")"
assert_no_marker "A5 assignment + command on one line"
assert_refused   "A5 assignment + command on one line"

# ── B. shape: exactly the two contracted keys, nothing else ─────────────────
new_case; run_worker "$(printf 'value=1\nexpires_at=%s\n' "$(live)")"
assert_accepted "B1 value=1 with a live TTL"

new_case; run_worker "$(printf 'value=0\nexpires_at=%s\n' "$(live)")"
assert_accepted "B2 value=0 with a live TTL"

# No trailing newline — a reader must not depend on one.
new_case; run_worker "$(printf 'value=1\nexpires_at=%s' "$(live)")"
assert_accepted "B3 no trailing newline"

new_case; run_worker "$(printf 'value=2\nexpires_at=%s\n' "$(live)")"
assert_refused "B4 value out of {0,1}"

new_case; run_worker "$(printf 'value=1\nexpires_at=later\n')"
assert_refused "B5 non-numeric expires_at"

new_case; run_worker "$(printf 'value=1\n')"
assert_refused "B6 expires_at missing"

new_case; run_worker "$(printf 'expires_at=%s\n' "$(live)")"
assert_refused "B7 value missing"

new_case; run_worker "$(printf 'value=1\nexpires_at=%s\n' "$(past)")"
assert_refused "B8 TTL already expired"

# An unknown key is a foreign line like any other — this is the arm the
# registered comment-out mutation removes.
new_case; run_worker "$(printf 'value=1\nexpires_at=%s\nowner=root\n' "$(live)")"
assert_refused "B9 unknown key"

# A quoted value was accepted while the file was SOURCED; it is not the shape
# either producer writes, and the parser refuses it rather than unquoting.
new_case; run_worker "$(printf 'value="1"\nexpires_at=%s\n' "$(live)")"
assert_refused "B10 shell-quoted value"

# ── C. the read path holds no `.` / `source` at all ─────────────────────────
# A fail-IF-PRESENT sweep over the literal regression (the exact line that
# shipped). Its LIMIT, stated rather than papered over: an indirect revival
# (`f=$OVERRIDE_FILE; . "$f"`) reads past C1 — section A is what catches that,
# by running the worker over a payload, and it is why this row is behavioural
# and not a grep. The non-vacuity floor here is the function's own existence.
#
# Captured, then matched: a producer piped into `head`/`grep -q` under
# `pipefail` is shape (f) of docs/agent-rules/quality-gate-rigor.md, and the
# rule is not "unless the status happens to be unused here".
worker_text=$(cat "$WORKER")
if printf '%s\n' "$worker_text" | grep -q '^read_override()'; then
    ok "C0 read_override() is where the file is parsed (non-vacuity)"
else
    bad "C0 read_override() not found in $WORKER — the sweep below would scan nothing"
fi
c1_hits=$(printf '%s\n' "$worker_text" | grep -nE '^[[:space:]]*(\.|source)[[:space:]]+.*OVERRIDE_FILE')
if [ -n "$c1_hits" ]; then
    bad "C1 $WORKER still sources the override file: $(printf '%s\n' "$c1_hits" | sed -n 1p)"
else
    ok "C1 the override file is never sourced"
fi
c2_hits=$(printf '%s\n' "$worker_text" | grep -nE '\b(eval|source)\b' | grep -v '^[0-9]*:[[:space:]]*#')
if [ -n "$c2_hits" ]; then
    bad "C2 $WORKER carries eval/source in live code: $(printf '%s\n' "$c2_hits" | sed -n 1p)"
else
    ok "C2 no eval/source anywhere in the worker's live code"
fi

# ── D. the producers still write exactly the two keys ───────────────────────
# Non-vacuous both ways: an extraction that yields nothing FAILS.
lib_keys=$(awk '/^sa02m_hw_beeper_override_write\(\)/{inf=1} inf&&/^}/{exit} inf' "$LIB_HW" \
    | grep -oE "printf '[a-z_]+=" | sed "s/printf '//; s/=$//" | sort -u | tr '\n' ' ' | sed 's/ $//')
if [ "$lib_keys" = "expires_at value" ]; then
    ok "D1 lib_hw.sh writes exactly {value, expires_at}"
else
    bad "D1 lib_hw.sh's override writer emits '$lib_keys', not 'expires_at value' — the worker's parser refuses anything else"
fi

tel_keys=$(awk '/^def _beeper_override_write\(/{inf=1;next} inf&&/^def /{exit} inf' "$TELEMETRY" \
    | grep -oE 'fh\.write\("[a-z_]+=' | sed 's/fh\.write("//; s/=$//' | sort -u | tr '\n' ' ' | sed 's/ $//')
if [ "$tel_keys" = "expires_at value" ]; then
    ok "D2 sa02m_telemetry.py writes exactly {value, expires_at}"
else
    bad "D2 the daemon's override writer emits '$tel_keys', not 'expires_at value' — the worker's parser refuses anything else"
fi

if [ "$caseno" -lt 15 ]; then
    bad "only $caseno worker run(s) — the fixture table was gutted (expected >=15)"
fi

if [ "$fails" -ne 0 ]; then
    printf 'beeper-override-no-exec: %d FAILED\n' "$fails"
    exit 1
fi
printf 'beeper-override-no-exec: all ok (%d worker runs)\n' "$caseno"
