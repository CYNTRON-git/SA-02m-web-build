#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-link-monitor.sh — behaviour harness for usr/local/sbin/sa02m-link-monitor.sh
# (the netlink link-up reaction, 1.0.7.27; analysis
# docs/decisions/network-recovery-standard-linux.md, recommendation 5).
#
# Why this exists: a carrier change on the board's eth0 emits no udev event
# (BUGLOG 1.0.7.22 / 1.0.7.25), so between net-watchdog passes (30 s; the
# first one 60 s after its unit starts) nothing reacted to a plugged cable or
# to a link cycle that ended outside fix-eth's own re-check — routes, DNS and
# the default route waited up to 30 s (60+30 s at boot). Netlink does report
# the change (bench 1.135, 2026-11-02: `ip -o -ts monitor link` printed
# LOWER_UP 2.2 s after `ip link set eth0 up`). The monitor turns a carrier
# RISING EDGE into `systemctl start fix-eth@<if>.service`; which lines are an
# edge is a behaviour no syntax gate can see, so it is pinned here.
#
# Method, in the idiom of test-fix-eth-link-cycle.sh: copy the SHIPPED script
# into a scratch tree and sed-retarget its two roots (/sys/class/net,
# /etc/network/interfaces.d) into it; `ip`, `systemctl` and `sleep` are
# recording PATH shims. The `ip` shim prints the case's canned
# `ip -o monitor link` lines and exits — so the monitor reaches EOF, which on a
# board means `ip monitor` died; the script must then exit non-zero (systemd
# restarts it). `systemctl is-active` answers from a per-unit busy counter
# (`activating` while it is > 0, decremented per call; `inactive` after);
# `sleep` is a recorded shim that costs no real second, so the bounded
# busy-wait runs without real waiting;
# /proc/uptime is retargeted to a sandbox file that the `systemctl is-active`
# shim advances by a per-case step on every call (default 10000 s, step 0) —
# the monitor asks is-active once per rising edge, before it reads the clock,
# so the start interval is decided on a monotonic stand-in without real
# waiting. `date` is a trap: it steps the WALL clock by its own per-case step,
# and the monitor must never call it (asserted over the whole run). No
# root, no device, no systemd, no python — bash + coreutils only, so it runs
# in Windows git-bash as well as on Linux CI. There is deliberately NO env
# seam in the shipped script — the retarget is the test's job.
#
# What synchronises what (the busy cases L6, L9, L10 run the monitor's
# foreground and its background waiter concurrently; nothing here may depend
# on which of the two the host schedules first):
#   - is-active calls are serialised by an O_EXCL lock file in the shim, so
#     the busy counter and the uptime stand-in are never read half-written
#     and never lose a decrement (the spin is bounded at LOCK_MAX=3 s of the
#     shim's clock, under the monitor's `timeout 5` per call);
#   - the waiter's first `sleep` blocks until the foreground has logged
#     «ip monitor завершился» (all canned lines handled), so every edge of a
#     case meets the pass still busy and the waiter still alive;
#   - the busy-wait is bounded by COUNT: the sleep shim kills the waiter past
#     SLEEP_CAP=120 waits (2 × BUSY_WAIT_MAX). The `timeout` wall bound
#     (RUN_CAP=300 s) is only a last resort for a hang that passes through no
#     shim. A gate, count or lock fault is recorded (SHIM-FAULT) and FAILS the
#     case. Wall bounds that remain: GATE_MAX=60 s and the monitor's own
#     `timeout 5` per is-active call — host load can still turn a busy case
#     RED, only past those far higher thresholds.
#
# Cases (each = one run of the monitor over one canned line set):
#   L1  eth0 has a conf, carrier=0 at start; a NO-CARRIER line then a
#       LOWER_UP line -> exactly one `start fix-eth@eth0.service`, one
#       «линк поднялся» journal line.
#   L2  repeated LOWER_UP lines -> still exactly one start (not an edge).
#   L3  up, down, up -> two starts (the second rising edge counts).
#   L4  no conf for eth1; eth1, docker0, lo, a veth@ peer and a garbage
#       line all go up -> nothing started, the script survives the parse miss.
#   L5  eth1 with a conf -> `start fix-eth@eth1.service`, nothing for eth0.
#   L6  fix-eth@eth0 is busy (activating) on the edge -> NO immediate start;
#       the start is issued only after is-active has answered `inactive`
#       (the deferred, bounded wait — an edge during a running pass is not
#       lost), exactly once.
#   L7  carrier=1 already at start and LOWER_UP lines -> no start (the
#       initial state is read from sysfs; an up line is not an edge).
#   L9  the instance stays busy past BUSY_WAIT_MAX -> exactly 60 one-second
#       waits, the «занят дольше 60с» give-up line, NO start (the contract's
#       «до 60 с»).
#   L10 five rising edges during one busy pass (clock stepping 61 s, so the
#       start interval below never decides it) -> one deferred job, four
#       «уже ожидает» lines, exactly one start (no pile-up of waiters).
#   L11 the per-interface start interval (MIN_START_INTERVAL=60 s of uptime):
#       two rising edges 10 s apart -> one start and the «пропуск (интервал
#       60с)» line; 61 s apart -> two starts. Breaks the self-trigger loop of a
#       failed ifdown/ifup in fix-eth.sh (its cooldown lock is released after
#       every completed pass, so it does not space them).
#   L12 clock steps: the wall clock stepped back 3600 s between two edges 61 s
#       of uptime apart -> two starts (RED on the wall-clock version: one start,
#       «запускался -3600с назад»); the uptime stand-in itself going back
#       3600 s (the `0 <=` belt) -> two starts; the first edge at uptime 15 s
#       (boot) -> one start (the never-started guard). Whole run: `date` never
#       called.
#   Every case: the script exits non-zero when `ip monitor` ends.
#   L8  wiring (static, comment-stripped): scripts/02-network.sh lands the
#       script and the unit through sa02m_atomic_install and enables+starts
#       the unit (infra class, `start restart` so a re-run refreshes a running
#       monitor); the unit's ExecStart names the script, Restart=always,
#       WantedBy=multi-user.target, TimeoutStopSec=5; fix-eth@.service keeps
#       StartLimitBurst=10 (the monitor adds starts — a boot-time burst over the
#       old 3/60 s left the unit failed); the factory-reset runner's
#       imaging hold stops the monitor and gives it back only where it is
#       enabled (its own harness, factory-reset-runner, needs a POSIX host and
#       skips on Windows — this pin is the part every host measures; the
#       update runner's and the remedy's halves are behavioural cases N1-N3,
#       R6, Mb, Mf in test-update-recover-boot.sh).
#
# Non-vacuous: a missing script, a failed retarget, a real root left on an
# executable line, a shim never invoked across the run, `ip` invoked with
# anything but `-o monitor link`, a run that hangs (bounded by `timeout`), or
# a shim fault (the sleep gate never opened, the wait count cap hit, the
# is-active lock never freed) FAILS rather than passing on zero matches.
#
# Drive-to-failure (how this harness was proven a real ratchet):
#   RED on the 1.0.7.26 tree (script absent): «FAIL missing or empty».
#   Commenting the `systemctl --no-block start "fix-eth@${1}.service"` line
#   out of a copy turns L1, L2, L3, L5, L6 RED; commenting the one-job guard
#   (`[ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && {`) turns L10 RED
#   (start=5 queued=5); commenting `BUSY_WAIT_MAX=60` turns L6, L9, L10 RED
#   (unbound under set -u, the waiter dies); commenting the start-interval
#   check (`-lt "$MIN_START_INTERVAL" ] && {`) turns L11 RED (two starts 10 s
#   apart). L12 was RED on the round-2 monitor that measured the interval with
#   `date +%s` (start=1 with the wall clock stepped back 3600 s); removing only
#   the `[ "$d" -ge 0 ]` belt from that line turns L12 RED (negative difference
#   skipped, start=1); replacing its never-started guard
#   `[ -n "${LAST_START[$ifc]:-}" ] && d=$((now - LAST_START[$ifc]))` with
#   `d=$((now - ${LAST_START[$ifc]:-0}))` turns L12c RED (first edge at uptime
#   15 s skipped, start=0 — observed by hand after round 4, re-observed in review round 5). Not expressible as a comment-out, recorded by hand: with
#   BUSY_WAIT_MAX=999999 review round 1 saw the harness GREEN (L9 did not
#   exist — its finding F1); after L9 was added the Builder observed L9 RED
#   (then: hang into the old 60 s wall bound, 143 waits, no give-up line) and
#   review round 2 re-observed it; since the count bound it is RED on the count
#   (the waiter killed at wait 121, no give-up line). Each L8
#   pin turns L8 RED when commented out (the installer's `infra start
#   restart`, the unit's TimeoutStopSec=5, fix-eth@'s StartLimitBurst=10, the
#   factory-reset runner's two lines). The comment-out cases are registered in
#   comment-mutation-proof. LINK_MONITOR_SRC re-runs it against another copy;
#   the override never edits the real tree.
#
# Run: bash scripts/dev/test-link-monitor.sh   (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${LINK_MONITOR_SRC:-usr/local/sbin/sa02m-link-monitor.sh}

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -s "$SRC" ] || { echo "FAIL  missing or empty: $SRC"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

CD="$T/ifd"                  # /etc/network/interfaces.d
SYS="$T/sysnet"              # /sys/class/net (no such substring in the name, so the leftover scan cannot match the sandbox)
ST="$T/state"                # shim state: busy counters
BIN="$T/bin"
LINES="$T/lines"             # the canned `ip -o monitor link` output of the current case
OUT="$T/run.out"             # stdout+stderr of the last run
CALLS="$T/calls.log"         # reset per case
CALLS_ALL="$T/calls-all.log" # never reset — the harness non-vacuity witness
mkdir -p "$CD" "$SYS" "$ST" "$BIN"

# ── retarget the SHIPPED script into the sandbox ───────────────────────────
MON="$T/sa02m-link-monitor.sh"
sed -e "s#/etc/network/interfaces\.d#$CD#g" \
    -e "s#/sys/class/net#$SYS#g" \
    -e "s#/proc/uptime#$ST/uptime#g" \
    "$SRC" > "$MON"
chmod +x "$MON"
for pat in "CONF_DIR=\"$CD\"" "SYS_NET=\"$SYS\"" "UPTIME=\"$ST/uptime\""; do
    grep -Fq "$pat" "$MON" || { echo "FAIL  retarget missed in the monitor: $pat"; exit 1; }
done
if grep -Eq '^[^#]*(/sys/class/net|/etc/network|/proc/)' "$MON"; then
    echo "FAIL  monitor copy still holds a real root on an executable line"; exit 1
fi

# ── PATH shims ─────────────────────────────────────────────────────────────
cat > "$BIN/ip" <<SHIM
#!/bin/bash
echo "ip \$*" | tee -a "$CALLS_ALL" >> "$CALLS"
[ -f "$LINES" ] && cat "$LINES"
exit 0
SHIM
# is-active: `activating` while $ST/busy.<unit> holds a positive count (one
# answer consumed per call), `inactive` (rc 3, as systemd) otherwise. The
# answer is recorded, so L6 can prove the start came after an `inactive`.
# Every is-active first advances the sandbox uptime by $ST/uptime.step.
# The foreground (edge-time check) and the deferred waiter both call it, so
# the read-modify-write runs under an O_EXCL lock file. Unserialised (the shim
# before 2026-11-04), a two-caller stress on one counter lost decrements in
# every run (reproduced in review); in two of its five runs (Windows
# git-bash, 2026-11-04) it also answered one spurious `inactive` with the
# counter still positive — a read landing in the other call's truncate saw ""
# (NOT reproduced in review's sample). In L10 a spurious `inactive` is a fresh
# start where «уже ожидает» is due (foreground) or a waiter that starts early
# so the next edge queues a second one (waiter): consistent with the flakes
# seen (CI start=3 and start=2, one Windows run start=2 queued=2), which the
# sleep gate below closes by the other route as well. The lock spin is bounded
# by the shim's own clock at LOCK_MAX=3 s, under the monitor's `timeout 5` on
# every is-active call — so a lock that is never freed is recorded as
# SHIM-FAULT and fails the case, instead of the call being killed silently
# and read as «not busy». State is read with builtins, records are plain
# appends — every fork here is paid on each busy-wait iteration.
LOCK_MAX=3
cat > "$BIN/systemctl" <<SHIM
#!/bin/bash
rec() { echo "\$1" >> "$CALLS"; echo "\$1" >> "$CALLS_ALL"; }
if [ "\${1:-}" = is-active ]; then
    owned=0; trap '[ "\$owned" = 1 ] && rm -f "$ST/is-active.lock"' EXIT; trap 'exit 143' TERM INT HUP
    set -C
    until { : > "$ST/is-active.lock"; } 2>/dev/null && owned=1; do
        [ "\$SECONDS" -lt $LOCK_MAX ] || { rec "SHIM-FAULT is-active lock not freed within ${LOCK_MAX}s"; break; }
    done
    set +C
    _u=0; _st=0; n=0
    { read -r _u _ < "$ST/uptime"; } 2>/dev/null
    { read -r _st < "$ST/uptime.step"; } 2>/dev/null
    printf '%s.00 0.00\n' "\$(( \${_u%%.*} + _st ))" > "$ST/uptime"
    f="$ST/busy.\$2"
    { read -r n < "\$f"; } 2>/dev/null
    if [ "\${n:-0}" -gt 0 ]; then
        echo "\$((n - 1))" > "\$f"
        rec "systemctl \$* -> activating"
        echo activating; exit 0
    fi
    rec "systemctl \$* -> inactive"
    echo inactive; exit 3
fi
rec "systemctl \$*"
exit 0
SHIM
# sleep: no real time, but two synchronisation duties (only the deferred
# waiter calls it). GATE — it does not return until the monitor's foreground
# has drained every canned line, observed as its «ip monitor завершился» line
# in $OUT: every edge of a case is then handled while the pass is still busy,
# unless the host stalls the foreground past GATE_MAX (without it a waiter that runs ahead drains
# the busy counter before the foreground reaches the later edges, and those
# start fresh — reproduced by feeding L10's last edges late: start=3 queued=1
# waiting=2, the CI signature). The hold is bounded by GATE_MAX seconds of
# the shim's own clock (bash SECONDS), well under RUN_CAP: the foreground needs
# a few seconds for its handful of lines even on a loaded host.
# COUNT BOUND — past $SLEEP_CAP waits (2 × the monitor's BUSY_WAIT_MAX) it
# kills its caller, the waiter: a runaway busy-wait is cut by its iteration
# count, not by the wall clock. Either fault is recorded as SHIM-FAULT and
# FAILS the case (exit_ok).
REAL_SLEEP=$(command -v sleep) || { echo "FAIL  no real sleep for the gate poll"; exit 1; }
SLEEP_CAP=120
GATE_MAX=60
cat > "$BIN/sleep" <<SHIM
#!/bin/bash
rec() { echo "\$1" >> "$CALLS"; echo "\$1" >> "$CALLS_ALL"; }
rec "sleep \$*"
while [ ! -f "$ST/fg-eof" ]; do
    while IFS= read -r l; do
        [[ \$l == "ip monitor завершился"* ]] && { : > "$ST/fg-eof"; break; }
    done 2>/dev/null < "$OUT"
    [ -f "$ST/fg-eof" ] && break
    [ "\$SECONDS" -lt $GATE_MAX ] || { rec "SHIM-FAULT sleep gate: the foreground never logged the end of ip monitor within ${GATE_MAX}s"; : > "$ST/fg-eof"; break; }
    "$REAL_SLEEP" 0.05
done
n=0; { read -r n < "$ST/sleeps"; } 2>/dev/null; n=\$((n + 1)); echo "\$n" > "$ST/sleeps"
if [ "\$n" -gt $SLEEP_CAP ]; then
    rec "SHIM-FAULT sleep cap: the waiter was killed at wait \$n (> $SLEEP_CAP)"
    kill -TERM "\$PPID" 2>/dev/null
fi
exit 0
SHIM
# `date` — a TRAP, not a clock: it answers `+%s` from a sandbox wall clock
# ($ST/clock, default 1000) stepped by $ST/clock.step after every read, so a
# monitor that measured its interval on the wall clock would see L12's step
# back; every call is recorded and the run FAILS if there is any.
cat > "$BIN/date" <<SHIM
#!/bin/bash
[ "\${1:-}" = "+%s" ] || { echo "date shim: unexpected \$*" >&2; exit 2; }
c=\$(cat "$ST/clock" 2>/dev/null || echo 1000); st=\$(cat "$ST/clock.step" 2>/dev/null || echo 0)
printf '%s\n' "\$((c + st))" > "$ST/clock"
echo "date +%s -> \$c" | tee -a "$CALLS_ALL" >> "$CALLS"
echo "\$c"
SHIM
chmod +x "$BIN"/*
PATH="$BIN:$PATH"; export PATH
command -v ip | grep -Fq "$BIN/ip" || { echo "FAIL  the ip shim is not first on PATH"; exit 1; }

# ── fixtures ───────────────────────────────────────────────────────────────
# Line shapes as `ip -o monitor link` prints them (iproute2, one record per line).
up()   { printf '%s: %s: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default \\    link/ether 02:00:00:00:00:0%s brd ff:ff:ff:ff:ff:ff\n' "$2" "$1" "$2"; }
down() { printf '%s: %s: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500 qdisc mq state DOWN mode DEFAULT group default \\    link/ether 02:00:00:00:00:0%s brd ff:ff:ff:ff:ff:ff\n' "$2" "$1" "$2"; }
reset_case() {   # wipe confs, sysfs stand-ins, busy counters, clocks, lines, per-case calls
    rm -rf "$CD"/* "$SYS"/* "$ST"/*
    printf '10000.00 0.00\n' > "$ST/uptime"
    : > "$LINES"; : > "$CALLS"
}
conf()    { printf 'auto %s\niface %s inet static\n    address 192.168.1.135/24\n' "$1" "$1" > "$CD/$1.conf"; }
carrier() { mkdir -p "$SYS/$1"; printf '%s\n' "$2" > "$SYS/$1/carrier"; }
clock_step() { printf '%s\n' "$1" > "$ST/uptime.step"; }   # seconds the sandbox uptime advances per is-active
wall_step()  { printf '%s\n' "$1" > "$ST/clock.step"; }    # seconds the date trap's wall clock moves per read
# Last-resort wall bound for a hang that passes through no shim. The busy-wait
# is bounded by count (the sleep shim's SLEEP_CAP), not by this, so L9/L10 no
# longer race a wall bound with their 60 iterations. Two wall bounds remain in
# the busy cases, both far above what the work needs: GATE_MAX=60 s for the
# foreground's few lines, and the monitor's own `timeout 5` on each is-active
# call (a call the host stalls that long reads as «not busy» and changes the
# counts). Host load can still turn them RED, only past those thresholds.
RUN_CAP=300
run_mon() {   # the monitor over $LINES; bounded so a hang FAILS instead of blocking
    timeout "$RUN_CAP" "$MON" < /dev/null > "$OUT" 2>&1
    RC=$?
}
# `grep -c` prints 0 AND exits 1 on no match; `|| true` keeps that single 0.
n_calls() { grep -c -E "$1" "$CALLS" 2>/dev/null || true; }
n_start() { n_calls "^systemctl .*start fix-eth@$1\\.service\$"; }
n_start_any() { n_calls '^systemctl .*start fix-eth@'; }
exit_ok() {   # $1 case: the monitor must exit non-zero (not 124 = hung) when ip monitor ends; no shim fault
    if grep -q '^SHIM-FAULT' "$CALLS"; then bad "$1: $(grep '^SHIM-FAULT' "$CALLS" | tr '\n' ' ')"; fi
    if [ "$RC" = 124 ]; then bad "$1: the monitor hung (killed by timeout $RUN_CAP) — $(tail -2 "$OUT" | tr '\n' ' ')"
    elif [ "$RC" = 0 ]; then bad "$1: the monitor exited 0 when \`ip monitor\` ended — systemd would not restart it"
    fi
}

# ═══ L1: a down→up edge starts fix-eth@eth0 once ═══════════════════════════
reset_case; conf eth0; carrier eth0 0
{ down eth0 2; up eth0 2; } > "$LINES"
run_mon; exit_ok L1
if [ "$(n_start eth0)" = 1 ] && [ "$(n_start_any)" = 1 ] \
   && [ "$(grep -c 'eth0: линк поднялся' "$OUT")" = 1 ]; then
    ok "L1: eth0 carrier 0→1 starts fix-eth@eth0.service once, one journal line (exit rc=$RC)"
else
    bad "L1: start(eth0)=$(n_start eth0) start(any)=$(n_start_any): $(tr '\n' ' ' < "$OUT")"
fi

# ═══ L2: repeated up lines are one edge ════════════════════════════════════
reset_case; conf eth0; carrier eth0 0
{ up eth0 2; up eth0 2; up eth0 2; } > "$LINES"
run_mon; exit_ok L2
if [ "$(n_start eth0)" = 1 ] && [ "$(n_start_any)" = 1 ]; then
    ok "L2: three LOWER_UP lines in a row start fix-eth@eth0 once"
else
    bad "L2: start(eth0)=$(n_start eth0) start(any)=$(n_start_any): $(tr '\n' ' ' < "$OUT")"
fi

# ═══ L3: up, down, up — the second rising edge counts ══════════════════════
# (clock stepping 61 s: the two edges are outside each other's start interval;
#  inside it the second edge is skipped — that is L11)
reset_case; conf eth0; carrier eth0 0; clock_step 61
{ up eth0 2; down eth0 2; up eth0 2; } > "$LINES"
run_mon; exit_ok L3
if [ "$(n_start eth0)" = 2 ] && grep -q 'eth0: линк пропал' "$OUT"; then
    ok "L3: up, down, up starts fix-eth@eth0 twice (falling edge only logged)"
else
    bad "L3: start(eth0)=$(n_start eth0): $(tr '\n' ' ' < "$OUT")"
fi

# ═══ L4: no conf / not ethernet / garbage — nothing started ════════════════
reset_case; conf eth0; carrier eth0 1
{ down eth1 3; up eth1 3
  up docker0 4; up lo 1
  printf '7: veth1a2b@if6: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue master docker0 state UP\n'
  printf 'garbage line without the record shape\n'
  printf 'Deleted 9: eth5: <BROADCAST,MULTICAST> mtu 1500\n'
} > "$LINES"
run_mon; exit_ok L4
if [ "$(n_start_any)" = 0 ] && [ "$(grep -cE 'линк (поднялся|пропал)' "$OUT")" = 0 ]; then
    ok "L4: eth1 without a conf, docker0, lo, a veth@ peer, a garbage line — nothing started, nothing logged as a link"
else
    bad "L4: start(any)=$(n_start_any): $(tr '\n' ' ' < "$OUT")"
fi

# ═══ L5: eth1 with a conf ══════════════════════════════════════════════════
reset_case; conf eth0; conf eth1; carrier eth0 0; carrier eth1 0
{ down eth1 3; up eth1 3; } > "$LINES"
run_mon; exit_ok L5
if [ "$(n_start eth1)" = 1 ] && [ "$(n_start eth0)" = 0 ]; then
    ok "L5: eth1 carrier 0→1 starts fix-eth@eth1.service, nothing for eth0"
else
    bad "L5: start(eth1)=$(n_start eth1) start(eth0)=$(n_start eth0): $(tr '\n' ' ' < "$OUT")"
fi

# ═══ L6: instance busy on the edge — deferred start after it finishes ══════
reset_case; conf eth0; carrier eth0 0
printf '3\n' > "$ST/busy.fix-eth@eth0.service"     # activating for the next 3 is-active answers
up eth0 2 > "$LINES"
run_mon; exit_ok L6
# Line numbers in the per-case call log: the first is-active (must answer
# activating — the edge-time check), the last `inactive` and the start.
first_busy=$(grep -n -m1 '^systemctl is-active fix-eth@eth0\.service' "$CALLS" | cut -d: -f1)
first_busy_ans=$(grep -m1 '^systemctl is-active fix-eth@eth0\.service' "$CALLS")
inactive_at=$(grep -n 'is-active fix-eth@eth0\.service -> inactive' "$CALLS" | tail -1 | cut -d: -f1)
start_at=$(grep -n -E '^systemctl .*start fix-eth@eth0\.service$' "$CALLS" | tail -1 | cut -d: -f1)
if [ "$(n_start eth0)" = 1 ] && [ -n "$first_busy" ] && [ -n "$inactive_at" ] && [ -n "$start_at" ] \
   && [ "${first_busy_ans##* -> }" = activating ] && [ "$inactive_at" -lt "$start_at" ] \
   && [ "$(n_calls '^sleep ')" -ge 1 ]; then
    ok "L6: busy fix-eth@eth0 on the edge — no immediate start; one start issued after is-active answered inactive (call $inactive_at < $start_at)"
else
    bad "L6: start(eth0)=$(n_start eth0) first_is_active='${first_busy_ans:-none}' inactive_at=${inactive_at:-none} start_at=${start_at:-none} sleep=$(n_calls '^sleep '): $(tr '\n' ' ' < "$OUT")"
fi

# ═══ L7: carrier already 1 at start — an up line is not an edge ════════════
reset_case; conf eth0; carrier eth0 1
{ up eth0 2; up eth0 2; } > "$LINES"
run_mon; exit_ok L7
if [ "$(n_start_any)" = 0 ]; then
    ok "L7: carrier=1 read from sysfs at start, LOWER_UP lines start nothing"
else
    bad "L7: start(any)=$(n_start_any): $(tr '\n' ' ' < "$OUT")"
fi

# ═══ L9: busy past the wait limit — give up, never start ═══════════════════
# The contract's «до 60 с»: the deferred start waits at most BUSY_WAIT_MAX
# one-second steps, then logs the give-up and starts nothing (net-watchdog's
# pass is the fallback). 200 busy answers outlast the limit; an unbounded wait
# runs into the sleep shim's count bound (killed at wait 121, no give-up line)
# — it FAILS on the count, not on a wall-clock race.
reset_case; conf eth0; carrier eth0 0
printf '200\n' > "$ST/busy.fix-eth@eth0.service"
up eth0 2 > "$LINES"
run_mon; exit_ok L9
n_sl=$(n_calls '^sleep ')
if [ "$(n_start_any)" = 0 ] && grep -q 'eth0: fix-eth@eth0 занят дольше 60с' "$OUT" && [ "$n_sl" = 60 ]; then
    ok "L9: instance busy past 60 s — 60 one-second waits, the give-up line, no start"
else
    bad "L9: start(any)=$(n_start_any) sleeps=$n_sl give_up=$(grep -c 'занят дольше' "$OUT"): $(tail -2 "$OUT" | tr '\n' ' ')"
fi

# ═══ L10: a flapping link during one busy pass — one waiter, one start ════
# Five rising edges while fix-eth@eth0 runs: the first queues the deferred
# start, the next four find it waiting and only log; exactly one start
# follows. Without the one-job-per-interface guard every edge spawns its own
# waiter and each of them starts a pass.
# 58 busy answers (≤ BUSY_WAIT_MAX, so the waiter still starts). The waiter
# cannot run ahead: the sleep shim's gate holds its first wait until the
# foreground has logged the end of the canned lines, so all five edges see the
# pass still busy and the waiter alive — by construction, not by a margin.
reset_case; conf eth0; carrier eth0 0; clock_step 61
printf '58\n' > "$ST/busy.fix-eth@eth0.service"
{ up eth0 2; down eth0 2; up eth0 2; down eth0 2; up eth0 2; down eth0 2; up eth0 2; down eth0 2; up eth0 2; } > "$LINES"
run_mon; exit_ok L10
n_queued=$(grep -c 'повторный запуск после его завершения' "$OUT")
n_waiting=$(grep -c 'уже ожидает' "$OUT")
if [ "$(n_start eth0)" = 1 ] && [ "$n_queued" = 1 ] && [ "$n_waiting" = 4 ]; then
    ok "L10: five edges during one busy pass — one deferred job, four «уже ожидает», one start"
else
    bad "L10: start(eth0)=$(n_start eth0) queued=$n_queued waiting=$n_waiting: $(tr '\n' ' ' < "$OUT")"
fi

# ═══ L11: at most one start per MIN_START_INTERVAL per interface ═════════
# A failed ifdown/ifup bounces the link: the next edge arrives seconds after the
# pass the monitor started. Inside 60 s it is logged and skipped (net-watchdog
# picks it up); past 60 s it starts again.
reset_case; conf eth0; carrier eth0 0; clock_step 10
{ up eth0 2; down eth0 2; up eth0 2; } > "$LINES"
run_mon; exit_ok L11a
if [ "$(n_start eth0)" = 1 ] && [ "$(grep -c 'eth0: линк поднялся (netlink), fix-eth@eth0 запускался 10с назад — пропуск (интервал 60с)' "$OUT")" = 1 ]; then
    ok "L11: second edge 10 s after the start — skipped with the interval line, one start"
else
    bad "L11: 10 s apart — start(eth0)=$(n_start eth0): $(tr '\n' ' ' < "$OUT")"
fi
reset_case; conf eth0; carrier eth0 0; clock_step 61
{ up eth0 2; down eth0 2; up eth0 2; } > "$LINES"
run_mon; exit_ok L11b
if [ "$(n_start eth0)" = 2 ] && ! grep -q 'пропуск (интервал' "$OUT"; then
    ok "L11: second edge 61 s after the start — started again"
else
    bad "L11: 61 s apart — start(eth0)=$(n_start eth0): $(tr '\n' ' ' < "$OUT")"
fi

# ═══ L12: a wall-clock step back between two edges does not silence it ════
# The «Время» form (date -s), the boot RTC load or NTP can step the wall clock
# back by hours; the interval must not read that as «just started».
reset_case; conf eth0; carrier eth0 0; clock_step 61; wall_step -3600
{ up eth0 2; down eth0 2; up eth0 2; } > "$LINES"
run_mon; exit_ok L12a
if [ "$(n_start eth0)" = 2 ] && ! grep -q 'пропуск (интервал' "$OUT"; then
    ok "L12: wall clock stepped back 3600 s between two edges 61 s of uptime apart — both start"
else
    bad "L12: wall clock back 3600 s — start(eth0)=$(n_start eth0): $(tr '\n' ' ' < "$OUT")"
fi
# The belt: a difference below zero counts as elapsed, never as «just started».
reset_case; conf eth0; carrier eth0 0; clock_step -3600
{ up eth0 2; down eth0 2; up eth0 2; } > "$LINES"
run_mon; exit_ok L12b
if [ "$(n_start eth0)" = 2 ] && ! grep -q 'пропуск (интервал' "$OUT"; then
    ok "L12: the clock read 3600 s lower on the second edge — a negative difference counts as elapsed, both start"
else
    bad "L12: negative difference — start(eth0)=$(n_start eth0): $(tr '\n' ' ' < "$OUT")"
fi
# The first start per interface: at boot uptime is below the interval, and an
# interface the monitor never started for must start, not be measured from 0.
reset_case; conf eth0; carrier eth0 0
printf '15.00 0.00\n' > "$ST/uptime"
up eth0 2 > "$LINES"
run_mon; exit_ok L12c
if [ "$(n_start eth0)" = 1 ] && ! grep -q 'пропуск (интервал' "$OUT"; then
    ok "L12: first edge at uptime 15 s (boot) — started, not skipped"
else
    bad "L12: first edge at uptime 15 s — start(eth0)=$(n_start eth0): $(tr '\n' ' ' < "$OUT")"
fi

# ═══ L8: wiring — the installer lands and enables it, the unit runs it ═════
# Read comment-stripped (lib_check.sh, the one home for the stripping), so a
# `#` in front of a pinned line FAILS instead of satisfying the grep.
INST=scripts/02-network.sh
UNIT=etc/systemd/sa02m-link-monitor.service
if . .ai-dev/quality/checks/lib_check.sh 2>/dev/null && declare -F stripped_has >/dev/null; then
    for f in "$INST" "$UNIT" etc/sa02m-factory-reset-runner.sh etc/fix-eth@.service; do [ -s "$f" ] || bad "L8: missing or empty: $f"; done
    while IFS='|' read -r f needle; do
        [ -n "$f" ] || continue
        if stripped_has "$f" "$needle"; then ok "L8: $f carries «$needle»"
        else bad "L8: $f has no live line «$needle»"; fi
    done <<'PINS'
scripts/02-network.sh|sa02m_atomic_install -m 755 "$ETC_DIR/../usr/local/sbin/sa02m-link-monitor.sh" \
scripts/02-network.sh|sa02m_atomic_install -m 644 "$ETC_DIR/systemd/sa02m-link-monitor.service" \
scripts/02-network.sh|sa02m_svc_apply sa02m-link-monitor.service infra start restart
etc/systemd/sa02m-link-monitor.service|ExecStart=/usr/local/sbin/sa02m-link-monitor.sh
etc/systemd/sa02m-link-monitor.service|Restart=always
etc/systemd/sa02m-link-monitor.service|WantedBy=multi-user.target
etc/systemd/sa02m-link-monitor.service|TimeoutStopSec=5
etc/fix-eth@.service|StartLimitBurst=10
etc/sa02m-factory-reset-runner.sh|timeout 10 systemctl stop sa02m-link-monitor 2>/dev/null || true
etc/sa02m-factory-reset-runner.sh|systemctl is-enabled --quiet sa02m-link-monitor 2>/dev/null && systemctl start sa02m-link-monitor 2>/dev/null || true
PINS
else
    bad "L8: .ai-dev/quality/checks/lib_check.sh could not be sourced — the wiring was NOT verified (a skip is not a pass)"
fi

# ── harness non-vacuity ────────────────────────────────────────────────────
for s in ip systemctl sleep; do
    grep -q "^$s " "$CALLS_ALL" || bad "harness: the $s shim was never invoked — the cases are not exercising the script"
done
if grep -q '^date ' "$CALLS_ALL"; then bad "harness: the monitor read the wall clock ($(grep -c '^date ' "$CALLS_ALL") date call(s)) — the start interval must be measured on uptime"
else ok "harness: the monitor never read the wall clock (no date call in the whole run)"; fi
bad_ip=$(grep '^ip ' "$CALLS_ALL" | grep -v -x 'ip -o monitor link' | head -1)
[ -z "$bad_ip" ] || bad "harness: ip was invoked as «$bad_ip», expected only \`ip -o monitor link\`"

printf '\n'
if [ "$fails" = 0 ]; then
    echo "PASS  test-link-monitor.sh"
    exit 0
fi
echo "FAIL  test-link-monitor.sh: $fails failing case(s)"
exit 1
