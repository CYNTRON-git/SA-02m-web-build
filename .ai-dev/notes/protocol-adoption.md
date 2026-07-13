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
