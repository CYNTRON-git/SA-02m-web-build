## Concurrency

The **concurrency** module is on, so where the change touches shared state, async
flows, or parallel execution the review gains the interleaving dimension the floor
does not name: judge the code as concurrent runs will interleave it, not as one
sequential read-through suggests. `[persona]`: this sharpens judgement, denies nothing.

- `[light]` **Check-then-act without atomicity is a finding** — a window on shared state left to timing races by design.
- `[light]` **Non-idempotent retry is a finding** — a retried or replayed operation that double-applies fails under the load users actually bring.
- `[rich]` **Simulation is not evidence** — a concurrency claim backed only by a mental interleaving run (no stress/property test on a load-bearing path), an unnamed ordering assumption, or an inconsistent lock order is a cited finding.
