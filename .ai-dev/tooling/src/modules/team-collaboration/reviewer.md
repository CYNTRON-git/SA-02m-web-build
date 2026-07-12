## Team collaboration

The **team-collaboration** module is on. In a `collaboration.team: true` project the
change lands on a `main` shared with concurrent authors, so the review gains a
conflict-safety dimension the single-author floor never needs; on a single-user project
this section is inert. `[persona]`: this sharpens judgement, denies nothing.

- `[light]` **Scoped for concurrent work** — a sprawling cross-cutting edit that maximises the conflict surface (when an atomic, disjoint-surface change would do) is a cited finding; the diff should be small and one-purpose, the atomic-step floor read for the team case.
- `[light]` **No blind conflict-resolution evidence** — a conflict marker left in the diff, or evidence of a blind resolve-merge that may have dropped a colleague's change, blocks; the floor is re-cut from `main`, never auto-resolve (`PROTOCOL.md` `## Git flow`). A trivial mechanical re-apply (a version / CHANGELOG line) is acceptable only when the plan's progress note recorded it.
- `[rich]` **Stale-base check** — a branch built on a `main` it never synced from is a conflict waiting to ship; where the diff shows a stale base against current `main`, raise it.
