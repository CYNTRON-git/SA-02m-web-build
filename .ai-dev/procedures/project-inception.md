# Procedure: project inception

Loaded on demand when project inception fires (the trigger line lives in
`orchestrator.md` `## Side-tools`).

`project inception` records a greenfield's **day-zero decisions** — stack, environment, ops, license — into the decision-base and a seeded `docs/architecture.md`. Doc bootstrap's greenfield mirror: bootstrap reads an existing tree; inception records the decisions a new project has no tree to show yet. Not a side-tool: it runs through the normal loop as the project's first feature. `[persona]`.

**When it fires:**

- **At onboarding** — right after product discovery on a greenfield (no meaningful tree), the next link in the chain.
- **Lazy** — on a work request while the tree is essentially empty. Short, declinable offer — never a block.
- **Explicit** — the Operator asks.
- **Never on a brownfield** — an existing tree is doc bootstrap's case (`.ai-dev/procedures/doc-bootstrap.md`).

**One pass:**

1. Stack as a researched decision — the `research` side-tool drafts alternatives, trade-offs, and a recommendation; the Operator decides; lands at `docs/decisions/stack.md`.
2. Environment constraints recorded — where it runs, the budget ceiling, the expected scale, offline needs.
3. Day-zero ops answered — the deploy path, the secrets home, the backup owner (and whether restore was ever tested), how a production failure becomes visible. For a project that deploys, these answers **seed `docs/deployment.md`** from the template (`src/templates/deployment.md`) — its durable home, so the deploy-per-doc discipline (`.ai-dev/procedures/deployment.md`) has a runbook to follow from day one; the decision-base/architecture record only the *why*, never the runbook (invariant 6). The repository itself is day-zero ops: init + remote + first commit recorded (created at setup's repo check, `orchestrator.md` `## Setup` step 0 — by inception's time it must exist, the loop runs on it).
4. License chosen day one — the Operator's call, recorded. For an OSS project, a README is a day-zero artifact alongside the architecture doc — cover at minimum: what it is, how to install, and where to get help.
5. `docs/architecture.md` seeded FROM the decisions (the greenfield twist on bootstrap's fill-from-tree), same size ceiling (normal prose, never a wall-of-text line) and `[?]` discipline.
6. First-feature recommendation: a walking skeleton — the thinnest end-to-end slice proving the deploy path before features pile up. CLI skeleton = one invocation with flags → result. GUI skeleton = a window where the user can complete the full cycle including configuration; for a GUI that depends on an external service, a configuration verification action (e.g. a "Test" button) is part of the minimal skeleton, not a deferred feature.
7. For a product with real users or data, a short threat sketch — actors, assets, trust boundaries — lands at `docs/threat-model.md`, deepened later.
