## Modularity

The **modularity** module is on, so the floor's scope check is deepened from
"does the diff match the plan" to a **boundary verification**: confirm that
cross-module dependencies introduced by this change were named in the plan and
intentional, and that any boundary update landed in `docs/architecture.md`. A
new undocumented cross-boundary dependency, or a boundary changed without an
`architecture.md` update, **blocks**. `[persona]`: this sharpens judgement,
denies nothing.

- `[light]` **Boundary documented** — if the change introduced or modified a module boundary, confirm `docs/architecture.md` was updated; an undocumented new boundary is a finding.
- `[light]` **Dependency direction** — any new inter-module dependency follows the direction rules; a dependency against the grain requires a recorded Operator decision, not a silent bypass.
- `[rich]` **Linter result** — if a dependency/boundary linter is registered in `src/quality/tools.json`, confirm its output is green; a red linter is not green (same rule as every quality tool).
- `[rich]` **Cohesion** — does the diff introduce responsibility scatter the plan didn't name? A cross-boundary concern that appeared mid-build without a plan amendment is a scope deviation.
