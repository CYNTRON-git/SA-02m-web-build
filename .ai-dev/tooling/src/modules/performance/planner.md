## Performance

The **performance** module is on. The floor's plan checklist names no quantity
dimension — this module ADDS it: where the change touches a loop, query, or payload
over user-scale data, plan for the scale users actually bring; a pretty loop that
dies at 10k rows is the failure this prevents. `[persona]`: this sharpens the plan,
denies nothing.

- `[light]` **Name the expected scale** — how many rows / items / bytes this path sees in real use; a plan that never states the number cannot be checked against it.
- `[light]` **No unbounded path** — every query and load over user-scale data is bounded: limits and pagination at the boundaries, never "fetch all and hope".
- `[rich]` **No N+1 / O(n²) at user scale** — a per-row query or a quadratic pass on a user-scale path is a defect at plan time, not a later optimization.
- `[rich]` **Measure before optimizing** — optimize only on a measurement; an optimization without a number is vibes, and out of scope.
