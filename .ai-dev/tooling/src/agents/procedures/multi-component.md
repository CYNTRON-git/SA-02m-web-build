# Procedure: multi-component coordination

Loaded on demand when a multi-component session is recognised (the recognise-announce
trigger lives in `orchestrator.md` `## Multi-component coordination`). A cross-component
feature spans a **declared component set** — N sibling repos a valid `.ai-dev/components.json`
at the session root names (`docs/architecture.md` `## Components` is the schema's one home).
The PR1 boundary deny already permits an agent to work across that set; this procedure is
the orchestrator's body for it. Everything here is **`[persona]`** — it shapes your
reasoning and blocks nothing mechanically; the only `[mechanical]` thing in the whole
capability, the boundary deny, shipped in PR1.

**Recognise, don't offer.** On the understand beat (`PROTOCOL.md` beat 1), a manifest present at the session root with ≥1 declared sibling means the session is multi-component. This is **not a declinable offer** like product discovery — the opt-in already happened when the manifest was committed (it is repo-owned, changes via git — invariant 4). You simply **announce** the working set in one plain line ("this session spans N declared repos: …") so the Operator and every later beat know it. The hub is implicit: the manifest lives in one member repo's `.ai-dev/`, and the multi-component session runs from that root (the deny reads the set from the session's own root). If a sibling declares this repo but the manifest is not at *this* root, say so — start the session from the hub repo for the wide boundary to apply.

**One loop, not N.** The boundary widening exists precisely so **one** Builder and **one** Reviewer span the set — a cross-component feature is a SINGLE loop:

- **One plan** names every touched root and the per-repo surface within it (mirroring `parallel-work.md`'s surface-naming). Its verification scenario names which repo's integration layer the scenario runs through.
- **One Builder** edits across the declared set in one session.
- **One fresh Reviewer** reviews the **unified cross-repo diff** — the cumulative change across all touched roots, reviewed once (the beat-4 cumulative-diff rule, now spanning repos instead of just several commits on one branch). The Reviewer reads every touched root because the session boundary includes them.
- **Ship fans out to per-repo git** (`PROTOCOL.md` `## Git flow`): one plan / one review, then N commits / N PRs / N merge words at ship. This is the load-bearing asymmetry — coordination is unified UP TO ship, then fans out to the per-repo git model.

**The seam between two components is a contract — one home, no copy.** A seam contract (a frontend↔backend API, a backend↔firmware wire protocol) lives in **one home**: its OWNING component's `docs/contracts/` (invariant 6). Within this coordinated session the widened boundary already lets you read a declared sibling's tree, so a consuming component reads the seam contract **directly from its owner's `docs/contracts/`** — never a copy. A manifest entry MAY declare which seams it owns via an optional advisory `contracts` field (`docs/architecture.md` `## Components` is its one home; the security validator ignores it). Outside a coordinated session reference by pointer, never copy — a drift-guarded snapshot only if a build needs the file present. Full design: `docs/decisions/seam-contract-transport.md`; trust posture: `docs/contracts/project-boundary.md`. `[persona]` — the only `[mechanical]` part is the boundary deny that already ships.

**The stamp stays session-root-anchored.** One unified review ⇒ **one stamp**, at the session/hub root's `.ai-dev/reviews/<topic>_review.md` (reusing `parallel-work.md`'s rule — the stamp lands in the hub checkout). The **hub repo's** push is gated by it: the merge-gate reads `reviewStampSatisfied` on the hub root, unchanged from PR1. A **sibling repo's** push resolves to its *own* root (the shim's `resolveRoot`), where the hub stamp does not exist — so it is **not** gated by the hub stamp locally; the unified review covers the whole set and the sibling's own CI / branch protection is its repo-local re-check. No per-repo stamp, no per-root merge-gate — that would imply N reviews, re-introducing the N-loop shape this design collapses. This residual (a sibling's merge-gate satisfied centrally by the unified review, not repo-locally) is recorded in the durable doc (`docs/contracts/project-boundary.md`).

**Relation to `parallel-work.md`.** Worktrees split one repo's branches; the component set spans separate repos — orthogonal axes. The multi-component path is simpler (no worktree dance — each repo is its own checkout already); point at `parallel-work.md`, do not duplicate it.
