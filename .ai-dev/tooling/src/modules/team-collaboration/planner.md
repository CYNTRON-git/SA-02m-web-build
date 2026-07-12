## Team collaboration

The **team-collaboration** module is on. In a `collaboration.team: true` project the
change shares `main` with concurrent authors, so the plan gains a conflict-avoidance
dimension the single-author floor never needs; on a single-user project this section is
inert. `[persona]`: this sharpens the plan, denies nothing.

- `[light]` **Conflict-avoiding shape** — plan the change as a small atomic one-purpose PR over a **disjoint file surface** where the work allows; a sprawling cross-cutting edit maximises the conflict surface a teammate's branch collides with. This is the atomic-step floor (`builder.md` `## Build`) read for the concurrent case.
- `[light]` **Sync before push** — the plan names a **sync from `main` before push** so the branch builds on current `main`, not a stale base; a branch that diverged silently is the dominant conflict source.
- `[light]` **Never blind auto-resolve** — name in the plan how a conflict is handled: a **real content conflict ⇒ re-cut from `main` / escalate**, never a blind textual merge that can silently drop a colleague's change. This is the existing stale-branch rule (`PROTOCOL.md` `## Git flow`: "if conflicts appear, the branch is stale; cut a fresh one") made explicit for concurrent authors — point at it, do not restate it. Only a trivial mechanical conflict (a version / CHANGELOG line) may be re-applied, and the plan's progress note records that it was.
- `[rich]` **Disjoint-surface check named** — where two in-flight features must touch the same file, name the seam in the plan and the order they land, so the second rebases onto the first rather than racing it.
