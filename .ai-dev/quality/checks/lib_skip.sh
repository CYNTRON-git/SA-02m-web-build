#!/usr/bin/env bash
# lib_skip.sh — the one shell helper a check harness uses to report a check it
# could NOT run in this environment while the rest of its run went ahead. NOT a
# check row itself: sourced by harnesses.
#
# WHY. A row whose harness skipped a section but passed everything else used to
# exit 0 and read `PASS` in the runner — «all checks» on a host where part of
# them never ran (1.0.6.62 review A3). The runner hands every row a fresh file in
# QUALITY_SKIP_LOG; a line appended there turns the row's verdict into PARTIAL
# (the protocol and the verdicts: .ai-dev/quality/run.mjs header). Exit codes do
# not change — a gate's exit status is also read directly by
# comment-mutation-proof, where any non-zero means RED.
#
# Contract: `quality_skip "<case id and reason>"` prints `SKIP  <text>` (the
# line a human reads) and, when QUALITY_SKIP_LOG is set, appends <text> to it.
# Unset (a hand run, or a gate run by comment-mutation-proof) ⇒ print only.
# Always returns 0, so a caller under `set -e` is not stopped by it.
# Self-test: .ai-dev/quality/run.test.mjs section G (row quality-runner-self-test).

quality_skip() {
    printf 'SKIP  %s\n' "$1"
    if [ -n "${QUALITY_SKIP_LOG:-}" ]; then
        printf '%s\n' "$1" >> "$QUALITY_SKIP_LOG"
    fi
    return 0
}
