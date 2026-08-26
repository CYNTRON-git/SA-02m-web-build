# .ai-dev/notes/ — durable project knowledge

**Committed, team-shared.** Each change rides a PR (invariant 4). Read by the
orchestrator on the understand beat alongside `docs/architecture.md`.

## What belongs here

- Known project quirks the next session should know ("why we do X here")
- Architectural context not yet formalized into `docs/architecture.md`
- Open decisions pending a `docs/decisions/` record
- Agent conventions specific to this project that have not earned a canonical home yet

## What does NOT belong here

| Content | Correct home |
|---|---|
| Formal decisions | `docs/decisions/<topic>.md` |
| Contracts (API surface, schema, routes) | `docs/contracts/` |
| Product brief | `docs/product.md` |
| Architecture | `docs/architecture.md` |
| Session state (active branch, PR, cadence) | `.ai-dev/state/current.md` (gitignored) |
| Temp / ephemeral context | External scratch (outside the project) |
| Harness `#`/host memory | Never for project knowledge — unshared |

## Taxonomy

```
checkpoint    (.ai-dev/state/current.md)  volatile, gitignored, superseded on each update
durable notes (.ai-dev/notes/)            committed, team-shared, changes via PR
formal decisions (docs/decisions/)        committed, titled, sourced
external scratch                          outside project root, ephemeral
```

The harness `#`/host memory (external, per-profile, unshared with the team) is
**never** a project knowledge home. Write `.ai-dev/notes/<topic>.md` instead.
