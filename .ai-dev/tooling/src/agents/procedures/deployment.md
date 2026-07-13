# Procedure: deployment

Loaded on demand when a deploy / release / ops-mutating action is requested (the
trigger line lives in `orchestrator.md` `## Side-tools`).

`deployment` is the **strict deploy-per-doc discipline**: a deploy, release, or
ops-mutating action follows `docs/deployment.md` **step by step** — never improvised.
Deploy/ops knowledge has **one** durable home, the doc; it is never journaled into the
transient resume pointer (`.ai-dev/state/current.md` — invariant 6: the pointer points,
it does not hold a runbook). `[persona]` — there is **no mechanical deploy-gate**: a
deploy crosses many surfaces (an ssh session, a forge release, a CI pipeline, a package
push) and "this command is a deploy" is not reliably detectable, so the discipline is
held by this prose plus the audit dimension (`orchestrator.md` `## Audit`), not a deny.
Why this exists (a prod incident): `docs/decisions/deployment-discipline.md`.

**Cross-links** (point, don't restate): the **remote-mutation ask-class**
(`protocol-reference.md ## Enforcement`) already confirms a remote mutation before it runs — the
nearest mechanical neighbour, but it gates the *act*, not deploy-per-doc. **Invariant 4**
keeps repo-owned files under git and carves out runtime/deploys/remote state as the remote
surface this discipline governs.

**When it fires:**

- **On a deploy/release/ops request** — the Operator asks to deploy, cut a release, run a migration, or mutate production/runtime state.
- **Lazy** — the same request while `docs/deployment.md` is absent or still the unfilled template ⇒ STOP and create/confirm it first (step 1), never improvise.
- **Never on a no-deploy project** — a pure library / docs project that never deploys needs no deployment doc and no discipline (mirrors threat discovery's real-users-or-data gate).

**One pass:**

1. **Doc-first gate.** Read `docs/deployment.md`. Absent, still the template, or visibly stale (it does not match the real deploy path — the CI config, the deploy scripts) ⇒ **STOP**: do not improvise. Create or confirm the doc WITH the Operator first, seeding it from the template (`src/templates/deployment.md`) — the deploy procedure, rollback, environments, secrets home (locations), failure visibility, post-deploy verification. This is a build through the normal loop, not a silent edit.
2. **Deploy strictly per the doc** — follow its steps in order, honour each precondition; a remote-mutating step still passes through the remote-mutation ask-class confirm (`protocol-reference.md ## Enforcement`).
3. **A step is wrong or missing** ⇒ stop, fix the DOC with the Operator, then proceed — never patch around it ad-hoc (an undocumented deploy step is the root cause this discipline prevents). A deploy that fails repeatedly hits the fix-loop ceiling (`orchestrator.md` `## When something is off` — 2–3 attempts, then stop + a declinable 8D offer).
4. **Post-deploy** — run the doc's verification and confirm production is healthy by its failure-visibility signal.
5. **Land knowledge durably** — anything learned (a new step, a changed precondition, a rollback gotcha) lands in `docs/deployment.md` through the loop, **never** in the resume pointer or a transient note. The pointer may name "deployed vX" as state; the runbook content lives only in the doc (invariant 6).
