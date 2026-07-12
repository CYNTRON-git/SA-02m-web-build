# The quality layer

This is where a project says **what "green" means** for it — its linters, formatters, type-checkers, test runners, security scanners. The core (`../PROTOCOL.md`) stays stack-agnostic and never names a tool; the concrete tools live here as data, so a new stack is a registry edit, not a core edit.

## Shape

```text
tools.json        the registry — one row per tool (id, checks, config, run, beat, init)
<tool configs>    each tool's NATIVE config, dropped in beside tools.json
```

A row binds a tool to a **beat** of the loop:

- `build` — the Builder runs it and hands back only when it passes.
- `review` — the Reviewer runs/reads it as part of the review checklist.
- `ship` — runs at the ship beat (e.g. a release-gate check).

A red tool is *not green*: the beat isn't done.

## Template vs project

In a **fresh downstream** project `tools.json` ships **only the shape** — one `example-lint` row showing the format. (This repo is its own first project, so its rows are already the real suite — the parity and neutral-prose checks — not examples.) A real project:

1. deletes the `example-lint` row,
2. adds a row per tool it actually runs,
3. drops each tool's native config beside `tools.json`.

That keeps the template application-agnostic; the project owns its own stack's quality bar.

## Adding a tool

Drop its config here, add one row to `tools.json` (what it checks · the run command · which beat · a one-line init). Nothing in the core or the roles changes — they already read this registry by beat.
