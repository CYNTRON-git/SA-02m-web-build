#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-initd-bounded.sh — every SysV start/stop the service helper runs is
# bounded in time (A4-10, 1.0.7.36).
#
# etc/sa02m-web-service-ctl.sh starts and stops CODESYS, CodeMeter and MPLC4
# through their vendor /etc/init.d scripts. Before 1.0.7.36 those calls had no
# time limit: a wedged vendor script kept the coordinator's service guard, the
# helper and the panel's «port_busy» for as long as it hung.
#
# (A) Allow-list, open world: on the helper's text with comments blanked
#     (.ai-dev/quality/checks/lib_check.sh), EVERY occurrence of
#     `/etc/init.d/<name> start|stop` must be the argument of `initd_run`. A bare
#     call anywhere FAILS, whatever the script name. Non-vacuous: fewer than 6
#     occurrences (8 on 2026-11-11) FAILS — the sweep would have stopped seeing
#     the calls.
# (B) Behaviour: initd_run is EXTRACTED from the shipped helper and run with
#     SA02M_INITD_TIMEOUT_S=1 on `sleep 30` — it must return 124 within 8 s and
#     write its «timed out» line to the log; a command that finishes returns its
#     own status. A missing or unextractable initd_run FAILS.
# The guard side (service_guard.run's 300 s bound and the guard_timeout code)
# is opt/sa02m-port-coordinator/tests/test_service_guard_timeout.py.
#
# RED (2026-11-11, pre-change helper via SVC_CTL_SRC=<copy of HEAD>): (A) 8 bare
# calls listed, (B) "initd_run() not found". Commenting out the `initd_run() {`
# line turns (B) RED (registered case in comment-mutation-proof, step C).
#
# Run: bash scripts/dev/test-initd-bounded.sh   (bash, coreutils `timeout`)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

CTL=${SVC_CTL_SRC:-etc/sa02m-web-service-ctl.sh}
[ -f "$CTL" ] || { echo "FAIL  service helper not found: $CTL"; exit 1; }
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  cannot source lib_check.sh"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── (A) every init.d start/stop goes through initd_run ─────────────────────
text=$(stripped_text "$CTL")
n=0; bare=""
while IFS= read -r line; do
    rest=$line
    while [[ "$rest" =~ /etc/init\.d/[A-Za-z0-9_.-]+[[:space:]]+(start|stop)([^A-Za-z0-9_-]|$) ]]; do
        n=$((n + 1))
        m=${BASH_REMATCH[0]}
        pre=${rest%%"$m"*}
        [[ "$pre" =~ (^|[^A-Za-z0-9_])initd_run[[:space:]]+$ ]] || bare="$bare
  $line"
        rest=${rest#*"$m"}
    done
done <<< "$text"
if [ "$n" -lt 6 ]; then
    bad "A  only $n init.d start/stop call(s) found in $CTL — the sweep is vacuous"
elif [ -n "$bare" ]; then
    bad "A  init.d start/stop without initd_run (unbounded):$bare"
else
    ok "A  all $n init.d start/stop calls go through initd_run"
fi

# ── (B) initd_run bounds a hung command ────────────────────────────────────
{
    grep -E '^INITD_TIMEOUT_S=|^case "\$\{SA02M_INITD_TIMEOUT_S' "$CTL"
    awk '/^initd_run\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$CTL"
} > "$T/initd.sh"
if ! grep -q '^initd_run() {' "$T/initd.sh"; then
    bad "B  initd_run() not found in $CTL"
else
    LOG="$T/install.log"
    out=$(LOG="$LOG" SA02M_INITD_TIMEOUT_S=1 sh -c '. "$1"; start=$(date +%s); initd_run sleep 30; rc=$?; echo "$rc $(( $(date +%s) - start ))"' _ "$T/initd.sh")
    rc=${out%% *}; took=${out##* }
    if [ "$rc" = 124 ] && [ "${took:-99}" -lt 8 ]; then
        ok "B  a hung command is stopped: rc=124 after ${took}s (limit 1 s + 5 s kill grace)"
    else
        bad "B  initd_run did not bound a hung command: rc=$rc after ${took}s"
    fi
    if grep -q 'init.d sleep 30 timed out after 1s' "$LOG" 2>/dev/null; then
        ok "B  the timeout is written to the install log"
    else
        bad "B  no «timed out» line in the log: $(cat "$LOG" 2>/dev/null)"
    fi
    out=$(LOG="$LOG" sh -c '. "$1"; initd_run sh -c "exit 3"; echo $?' _ "$T/initd.sh")
    [ "$out" = 3 ] && ok "B  a command that finishes keeps its own exit status" \
                   || bad "B  initd_run changed a finished command's status: $out"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "PASS  initd-bounded"
    exit 0
fi
echo "FAIL  $fails case(s) failed"
exit 1
