#!/usr/bin/env bash
# Quality row `headless-smoke` (see .ai-dev/quality/tools.json). Runs the committed
# characterization harness (scripts/dev/characterize-ui.mjs --compare baseline):
# loads the dashboard headless over every tab/theme/variant and diffs globals +
# new console errors + DOM skeleton against scripts/dev/baseline/. Skips (exit 0)
# when its preconditions are absent (playwright not installed, or SA02M_WEB_PASS
# unset) so it never breaks a box that has not set up the harness — it is the
# on-demand real-layer UI check, not a headless-less CI gate. Lives in a script
# (not inline in the JSON `run`) to keep cmd.exe metacharacters out of `run`.
# COMMENT-BLINDNESS AUDIT (1.0.6.24): N/A — it drives a real browser through
# the committed characterization harness and diffs against a baseline; it
# greps no source. Its real weakness is a different one, honestly labelled in
# its registry row: it SKIPS wherever playwright or SA02M_WEB_PASS is absent,
# CI included (audit finding C4).
set -u

if [ ! -d scripts/dev/node_modules/playwright ] || [ -z "${SA02M_WEB_PASS:-}" ]; then
  echo "headless-smoke: skipped (playwright deps or SA02M_WEB_PASS absent - on-demand check, see scripts/dev/README.md)"
  exit 0
fi

exec node scripts/dev/characterize-ui.mjs --compare baseline
