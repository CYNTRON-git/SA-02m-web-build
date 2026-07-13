# Procedure: ship & git edge cases

Loaded on demand when one of the rarely-fired ship/git situations below arises (each
trigger lives in `orchestrator.md` `## Your seat`). These are the seat's edge-case bodies —
release rollback, a stacked PR queue, a cross-component N-repo ship, and the team-mode
ship steps — kept out of the always-loaded core because they fire on a minority of
features (and the team-mode pair is inert on the default single-user project). The
per-turn ship essentials — commit-after-review, the CI-green-before-merge gate,
async-merge-verify, the cost + completion lines, the transient delete — stay in-core in
`## Your seat`; this file is only the rarely-fired remainder. `[persona]`.

## Release rollback

Trigger: **a shipped version is wrong** and must be pulled back.

Revert the squash-merge commit on main (`git revert <sha>`), push, re-tag the prior
version.

## Stacked / cross-repo merge order

Trigger: **a stacked PR queue, or a cross-repo merge order** in a cross-component feature.

- **Merging a stacked queue:** retarget the next PR to `main` BEFORE merging the current
  one — deleting a merged base branch auto-closes its dependent PRs.
- The same ordering discipline applies **across repos** in a cross-component feature: when
  repo B's PR depends on repo A's merged change, the Operator names the order — merge A,
  verify it landed (the async-merge-verify rule in `## Your seat`), then merge B.

## Cross-component ship

Trigger: **a declared component set** (`.ai-dev/components.json`) — the feature spans N
sibling repos.

The git model is **per-repo** — commit/push/PR run once per touched root (`PROTOCOL.md`
`## Git flow` is the one home for the N-repo git rule; the one-loop-then-fan-out shape is
`orchestrator.md` `## Multi-component coordination` → `.ai-dev/procedures/multi-component.md`).
The seat's per-repo specifics:

- Commit/push/PR run **per repo**: you hold N separate merge words and execute each only on
  its own explicit Operator authorisation — never infer "merge all" from one word.
- The state pointer row names **every touched repo and its per-repo PR/merge state**, so a
  session reset resumes an N-repo ship losslessly (which PRs are open, which merged).
- Merge ordering across the repos follows the cross-repo rule under
  *Stacked / cross-repo merge order* above.

## Team mode (`collaboration.team: true` only)

Trigger: **a `collaboration.team: true` project** — both steps are inert on the default
single-user project (`team: false`).

- **Sync before push** — before pushing a feature branch, **sync it from current `main`**
  so it builds on the shared base, not a stale one; a **real content conflict ⇒ re-cut
  from `main` / escalate to the Operator**, never a blind auto-resolve that can silently
  drop a concurrent author's change (the stale-branch rule, `PROTOCOL.md` `## Git flow` —
  point, don't restate). Only a trivial mechanical conflict (a version / CHANGELOG line)
  may be re-applied, named in the plan's progress note. `[persona]`
- **Surface the AI-review verdict on the PR** — at ship, **before** deleting the review
  stamp (the strictly-LAST-delete rule in `## Your seat`), copy the Reviewer's verdict onto
  the PR — its body's review section and/or a comment carrying the stamp file
  (`gh pr comment <n> --body-file .ai-dev/reviews/<topic>_review.md`) — so colleagues and
  the human reviewer read the AI's findings and can pull the review for diagnosis.
  **Honesty: this is visibility, NOT a mechanical gate** — a PR body/comment is
  author-written, so it cannot count as a blocking check; the mechanical remote floor stays
  the `quality` required-status-check + the forge's own approval rules
  (`docs/decisions/multi-user-mode.md` §4). **Human approval is the forge's** — the protocol
  manages no approval count; a team-mode merge the forge blocks for a missing approval is
  **reported to the Operator, never worked around**. `[persona]`
