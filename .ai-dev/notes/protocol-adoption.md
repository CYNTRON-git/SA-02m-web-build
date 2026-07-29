# ai-dev protocol adoption (2026-07-12)

- Framework version **5.67.1**, ported from the sibling project
  `CYNTRON-git/MR-02m` (same protocol, same role set: ai-dev orchestrator +
  dev-planner / dev-builder / dev-reviewer / dev-reviewer-fixup).
- Install was performed manually per `.ai-dev/tooling/src/adapter/INSTALL.md`
  (the one-command installer could not be executed in the porting session);
  a re-run of `node .ai-dev/tooling/src/adapter/install.mjs . --platform claude`
  is idempotent and is the normal upgrade path.
- Project-specific rulesets live in `docs/agent-rules/` (web-code-rigor,
  web-workflow, sa02m-domain, git-commits, web-diagnostic-tools,
  agent-tooling-map) — adapted from MR-02m's firmware rulesets; loaded via
  `CLAUDE.md` @-imports.
- Skills (`.claude/skills/`): sa02m-web-architecture, sa02m-versioning-release,
  sa02m-ui-style, sa02m-web-testing — written for this project (MR-02m's
  STM32/firmware skills were NOT ported; module internals stay in that repo).
- Quality registry (`.ai-dev/quality/tools.json`): js-syntax, bash-cgi-syntax,
  version-consistency (build beat); install-sh-syntax (review beat).
- Not yet done: `/dev-setup` dialog with the Operator (config.json was seeded
  with MR-02m-matching defaults: interactive / lite / claude / ru); GitHub
  branch protection on the quality gate (setup step 5).
- **Merge-gate review-stamp resolution is by BRANCH NAME, not plan topic**
  (discovered 2026-07-28, feature `1.0.5.47`): the merge-gate denies
  `git push` when it can't find a satisfied review stamp, and it resolves
  the stamp file by the current branch name — `.ai-dev/reviews/<branch>_review.md`
  — not by the plan's topic slug (`.ai-dev/plans/<topic>.md` /
  `.ai-dev/reviews/<topic>_review.md`, the natural name a Reviewer spawned
  from the plan file produces). A topic-named stamp from the review beat
  does NOT satisfy the gate on its own; it took a second fresh-Reviewer
  spawn just to re-confirm and write under the branch-matched filename.
  **Going forward:** when spawning the review-beat Reviewer, tell it to
  write its verdict to `.ai-dev/reviews/<branch-name>_review.md` directly
  (the branch is already known/checked-out by that point in the loop) —
  saves the extra spawn at push time.
