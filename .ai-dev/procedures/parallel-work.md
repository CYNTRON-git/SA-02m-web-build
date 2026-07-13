# Procedure: parallel feature work

Loaded on demand when the Operator asks for features in parallel (the trigger
line lives in `orchestrator.md` `## Your seat`). Design rationale:
`docs/decisions/parallel-work.md`.

## Start a parallel feature

1. **Disjointness check first.** Read every active feature's plan
   (`.ai-dev/plans/<topic>.md`) for its named surface. The new feature's
   surface overlaps an active one ⇒ it QUEUES (tell the Operator in one line);
   when in doubt, serial — a conflict later costs the whole branch
   (stale-branch rule).
2. **Branch + worktree** (from current `main`):
   `git worktree add .ai-dev/worktrees/<topic> -b feature/<topic> main`
3. **Plan as usual** — the plan file additionally NAMES the touched surface
   (paths/areas), which is what the disjointness check reads.
4. **State table.** `.ai-dev/state/current.md` lists every active feature:
   topic · branch · beat (plan/build/review/ship) · blocked-on. Point at the
   plan for detail; update the row on every beat change.

## Run the loop per feature

- Spawn the Builder **into the feature's worktree** (its working directory is
  `.ai-dev/worktrees/<topic>/`); background spawns let several features build
  at once. The Reviewer reviews that worktree's diff vs `main`; its stamp
  lands in the MAIN checkout's `.ai-dev/reviews/<topic>_review.md` (where the
  push runs — the merge-gate reads it there).
- **Commits run in the worktree** (`git -C .ai-dev/worktrees/<topic> ...`);
  **pushes run from the main checkout** (a branch pushes from anywhere; the
  stamps live here). Use `git -C` and never leave the shell `cd`'d into a
  worktree between tool calls — see the cwd-re-anchoring hazard in
  `orchestrator.md` `## Your seat` ("Git ops on a nested or sibling repo").
- All loop rules apply per feature unchanged: fresh Reviewer each, Operator
  approves each plan, each merge needs its own explicit word.

## Ship + cleanup

1. **The ship beat is serial** — `package.json` + CHANGELOG are a shared
   surface every ship touches, so the disjointness rule itself forces it: one
   unmerged shipped PR at a time. If `main` advanced since this branch was cut
   (a sibling merged), rebasing the branch onto latest main before ship is
   REQUIRED (stale-branch rule). The re-push after rebase MUST use
   `--force-with-lease` — this safe force-push form no longer asks for
   confirmation (the lease itself protects against overwriting a teammate's
   work). Bump version + CHANGELOG as the LAST commits before push. If the
   branch is truly stale (rebase conflicts), recut instead: fresh branch +
   worktree from current `main` (remove the stale worktree and branch FIRST,
   `git worktree remove` then `git branch -D` — `worktree add -b` fails on an
   existing branch name), cherry-pick the feature's commits (clean by
   disjointness — only the not-yet-written bump could conflict), bump there,
   ship.
2. After merge confirmed: `git worktree remove .ai-dev/worktrees/<topic>`,
   delete the branch, drop the feature's row from the state table.
3. A worktree whose feature is abandoned is removed the same way — never left
   to rot (a stale worktree pins its branch).
