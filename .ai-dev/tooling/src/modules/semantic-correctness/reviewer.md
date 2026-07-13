## Semantic correctness

The **semantic-correctness** module is on, so the floor's **Hygiene** ("no stub
where real logic belongs"), **Correctness**, and **Honesty** items are deepened from
"is the code clean and true" to a behaviour check: a subsystem can pass every floor
item — clean names, no obvious stub, plausible structure — and still not DO what it
claims. This module asks whether the named behaviour actually happens, tying each
finding to the `file:line` that proves or fails it — same cite-or-it-didn't-happen
rule as the floor. `[persona]`: this sharpens judgement, denies nothing.

- `[light]` **Facade / inert subsystem** — does each non-trivial subsystem do what its name and docstring claim? State described as persisted (a "remembers / learning / adaptive" component) must be written to a durable store AND read back — a component that never persists cannot remember. A declared level / tier / branch that is a hardcoded constant or reached-but-inert is a finding, not a feature.
- `[light]` **Swallowed failure** — every caught exception logs with context or re-raises; a bare/blind `except`, or a defensive `try` that turns a broken call into a silent no-op (the feature "exists" but is dead), is a blocking finding.
- `[light]` **Parallel-path parity** — a path parallel to an existing one (streaming↔blocking, batch↔single, async↔sync) inherits ALL the original's guards: timeouts, circuit-breakers, cost / usage accounting, limits, cleanup of spawned tasks. Enumerate the original's guards and confirm each is present in the new path or consciously N/A.
- `[rich]` **Claim vs test** — a behavioural claim the plan made (learns / adapts / evolves / is N-level) is backed by a test that exercises it or an explicit placeholder mark; an unmarked claim over inert code is the honesty over-claim the floor blocks, made class-specific here.
- `[rich]` **Guard drift over time** — when the original path later gained a guard the parallel one predates, the parallel path is re-checked against the CURRENT original, not the one it was forked from; a guard added to one branch and not its twin is a cited finding.
