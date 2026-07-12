# Procedure: doc bootstrap

Loaded on demand when doc bootstrap fires (the trigger line lives in
`orchestrator.md` `## Side-tools`).

`doc bootstrap` fills the system canon of an **existing** project from its tree — `docs/architecture.md`, plus `docs/contracts.md` blocks where the code shows a visible user-facing promise. Discovery records the product (what, for whom); bootstrap records the system (how it is built). Not a side-tool: it runs through the normal loop as the project's first feature. `[persona]`.

**When it fires:**

- **At onboarding** — right after product discovery, the next link in the chain (install → setup → discovery → doc bootstrap → first feature).
- **Lazy** — on a work request while `docs/architecture.md` is absent or still the unfilled install template (its `<placeholder>` lines unreplaced). Short, declinable offer — never a block.
- **Explicit** — the Operator asks.
- **Never on a greenfield** — no tree to read; that case is project inception's (`.ai-dev/procedures/project-inception.md`, the greenfield sibling).

**One pass:**

1. The plan names which docs get drafted.
2. The Builder (codebase-reader fold) reads the tree and drafts into the installed templates under their own discipline: fill only what the tree shows, delete empty sections, `[?]` for any unmeasured bound, point at code rather than inventory it (invariant 6), secret *locations* never values.

   **Old-protocol source mode** — when the project carries prior-protocol docs (a `WORKFLOW.md`, a legacy pm-* agent roster, docs written against an earlier protocol version): the Builder drafts FROM those docs as primary source, compressed into the new templates under the new size ceilings. The tree is the verification ground — an old-doc claim that contradicts the code surfaces as a named finding for the Operator, never migrates silently. Old docs are deleted once their truth moves (supersede, one home — invariant 6). After drafting, a comment de-water pass: wall comments duplicating what now lives in the new docs go; the local *why* stays (invariant 6 on code). Offer a whole-project audit on close.

3. Ceiling: current state only, readable in one sitting — expect ~60–120 lines of normal prose (a wall-of-text line games nothing), past ~150 cut inventory. A bloated draft is a Reviewer doc-quality block.
4. Where a product brief exists (`docs/product.md`), cross-check the draft against it: a **factual contradiction** between brief and tree (the brief claims a CLI, the tree shows none) is a named finding for the Operator, with resolution options offered — correct the brief / record as roadmap / investigate which truth holds — never silently smoothed. Intent the brief wants but the tree hasn't built yet is roadmap, not contradiction — only facts conflict.
5. For a product with real users or data, the same short threat sketch as inception's lands at `docs/threat-model.md` — on a brownfield the actors, assets, and trust boundaries are already visible in the tree.
6. Where the tree shows a **deploy surface** (CI/CD config, deploy scripts, container/release manifests), fill `docs/deployment.md` from it (`src/templates/deployment.md`) — the deploy path the deploy-per-doc discipline (`.ai-dev/procedures/deployment.md`) will follow; secret *locations* never values. No deploy surface ⇒ skip it (a no-deploy project needs no deployment doc).
7. Relay the draft's claims to the Operator in plain language; the Operator corrects the facts.
8. The Reviewer checks the draft **against the tree** — a claimed component that doesn't exist or an invented bound blocks (honesty item). Ship like any feature.
