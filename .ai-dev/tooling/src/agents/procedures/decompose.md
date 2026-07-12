# Procedure: decompose

Loaded on demand when decompose fires (the trigger line lives in `orchestrator.md`
`## Side-tools`).

`decompose` brings an oversized or incohesive file into order **without changing its behaviour** — it splits one file into cohesive single-home modules, never rewrites what it does. A side-tool, not a beat; like doc-bootstrap it frames work that itself runs through the normal loop (Builder, fresh Reviewer, Operator merge). `[persona]`.

**When it fires:**

- **Explicit** — the Operator asks to decompose a named file.
- **Audit follow-up** — the audit's maintainability dimension (`orchestrator.md` `## Audit` step 2) flags oversized modules and hands back a decomposition worklist; each entry is offered as a decompose.
- **Offered** — when the Reviewer's per-file size finding or the Builder's architect-fold over-threshold offer surfaces a file past the stack's size threshold.

**One pass:**

1. **Net first** — a decompose is behaviour-preserving, so a behaviour net precedes any split. Assess the target's coverage: thin-covered **code** ⇒ the Builder writes **characterization tests capturing current behaviour FIRST**, before a line moves (a decompose with no behaviour net is forbidden). A target a unit test cannot reach (prose, a declarative artifact) ⇒ name in the plan the preservation evidence the Reviewer will check instead (a drift guard, a neutral-prose check, a semantic read-through) — the `deferred: <reason>` escape, never silence.
2. **Seams** — identify the file's distinct responsibilities; the split follows them, never an arbitrary line count.
3. **Split** — each responsibility moves to one cohesive module; the plan asserts **no behaviour change**, and the behaviour net stays green across the move.
4. **Review** — a fresh Reviewer confirms behaviour preserved (the net ran green), the split is cohesive (responsibility-aligned, not line-cut), and no new duplication entered (each fact still one home).
5. **Big file ⇒ multi-PR** — a large target decomposes as a worklist, each split its own behaviour-preserving PR through the loop, never one unreviewable mega-diff.

**Run-note** at `.ai-dev/decompose/<slug>.md` — transient, deleted at close; the durable record is the split commits + CHANGELOG.
