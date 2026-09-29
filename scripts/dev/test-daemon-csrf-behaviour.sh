#!/bin/bash
# Behavioural harness for the two HTTP daemons' session/CSRF gates — the sibling
# of test-cgi-csrf-behaviour.sh (a bash CGI sandbox; plan fork F5 kept the two
# harnesses apart rather than teach a CGI sandbox to drive Python daemons).
# Quality row `daemon-csrf-behaviour`.
#
# comment-mutation-proof-exempt: behavioural harness - it RUNS the shipped daemon code (the flasher's _dispatch lifted by ast into a recording handler, the real DevicesAPIHandler on an ephemeral port and an AF_UNIX socket) against real Cookie / X-SA02M-CSRF headers and asserts the answers and whether the route handler ran; it pins no source line a comment token could satisfy. Its RED is recorded below against the pre-fix daemons.
#
# What it runs (stdlib unittest, so it runs where py-unit-devices' pytest runner
# would skip):
#   opt/sa02m-flasher/tests/test_csrf_dispatch.py  — every flasher POST refused
#       before its handler without the token (200 + E_CSRF + reason), GET on the
#       session alone, /health before everything, the local INTERNAL_TOKEN seam
#       exempt, session-authenticated POSTs never; ordering pins on _dispatch.
#   opt/sa02m-devices/tests/test_api_csrf.py       — devices-api validates the
#       session itself (401 on every route but health), then the token on POST
#       (widgets file byte-unchanged on a refusal).
#   opt/sa02m-devices/tests/test_api_socket.py     — the loopback half: AF_UNIX
#       0660 listener, the STAND_API_TCP_COMPAT decision over fake site files,
#       bind failures that must never exit the process. Its live-socket class
#       SKIPS off POSIX and is reported here as a skip, never a pass
#       (.ai-dev/notes/quality-gate-environment.md; the Orchestrator runs it
#       under WSL).
#
# Non-vacuity: each suite must run at least its floor of tests («Ran N tests»
# is parsed, not assumed); a collection error, an import error or zero tests
# FAILS. Skips are counted and printed.
#
# RED, observed 2026-09-29 with these suites run against the d66d7b6 (1.0.6.56)
# daemons (git archive into a scratch dir, the new tests + websession.py copied
# in): test_csrf_dispatch 9 of 12 FAIL — cases 1, 2, 4, 7 (every POST route
# reached its handler without / with a stale token), 8 (no local-seam helper)
# and the four ordering pins; test_api_csrf 6 of 9 FAIL — 2, 4, 5 (200 without a
# session, the widgets file rewritten with no cookie), 6, 7, 9 (no E_CSRF);
# test_api_socket all 12 non-live tests RED (no tcp_compat_decision /
# build_listeners / bind_unix_listener / socket literals; the live class
# skipped on Windows). GREEN after the fix, on Windows and under WSL (the live
# AF_UNIX class included — 17/17, 0 skipped).
#
# Run: bash scripts/dev/test-daemon-csrf-behaviour.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

PY=""
for p in python3 python py; do
    if "$p" -c "import sys" >/dev/null 2>&1; then PY="$p"; break; fi
done
[ -n "$PY" ] || { echo "daemon-csrf-behaviour: FAIL — no working python interpreter"; exit 1; }

fails=0
ok()  { printf 'daemon-csrf-behaviour: ok    %s\n' "$1"; }
bad() { printf 'daemon-csrf-behaviour: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

run_suite() {  # $1 = tests dir  $2 = top dir  $3 = pattern  $4 = floor
    local out rc ran skipped
    out=$("$PY" -m unittest discover -s "$1" -t "$2" -p "$3" 2>&1); rc=$?
    ran=$(printf '%s\n' "$out" | sed -nE 's/^Ran ([0-9]+) tests?.*/\1/p' | tail -n1)
    skipped=$(printf '%s\n' "$out" | sed -nE 's/.*skipped=([0-9]+).*/\1/p' | tail -n1)
    [ -n "$ran" ] || ran=0
    [ -n "$skipped" ] || skipped=0
    if [ "$rc" -ne 0 ]; then
        bad "$3: exit $rc (ran $ran, skipped $skipped)"
        printf '%s\n' "$out" | tail -n 40 | sed 's/^/daemon-csrf-behaviour:        /'
    elif [ "$ran" -lt "$4" ]; then
        bad "$3: only $ran test(s) ran (floor $4) — the suite was not collected"
    else
        ok "$3: $ran tests, $skipped skipped$( [ "$skipped" -gt 0 ] && printf ' (SKIPPED, not passed — POSIX-only cases; run under WSL/CI)' )"
    fi
}

run_suite opt/sa02m-flasher/tests opt/sa02m-flasher 'test_csrf_dispatch.py' 10
run_suite opt/sa02m-devices/tests opt/sa02m-devices 'test_api_csrf.py' 8
run_suite opt/sa02m-devices/tests opt/sa02m-devices 'test_api_socket.py' 10

echo
if [ "$fails" -eq 0 ]; then
    echo "daemon-csrf-behaviour: ALL OK — both daemons refuse token-less POSTs before the handler; devices-api demands the session and binds the socket"
    exit 0
fi
echo "daemon-csrf-behaviour: $fails FAILED"
exit 1
