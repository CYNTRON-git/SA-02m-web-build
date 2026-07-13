## Concurrency

The **concurrency** module is on. The floor names no interleaving dimension — this
module ADDS it: where the change touches shared state, async flows, or parallel
execution, the plan covers the rows below; the race a single sequential read-through
never shows is the failure this prevents. `[persona]`: this sharpens the plan, denies
nothing.

- `[light]` **Name the shared state** — what is shared and who mutates it; a plan silent on the sharing cannot be judged for races.
- `[light]` **Atomic windows** — a check-then-act or read-modify-write window on shared state is made atomic (a transaction, a lock, compare-and-swap), never left to timing.
- `[light]` **Idempotent retries** — a retried or replayed operation is idempotent; a non-idempotent retry double-applies under real load.
- `[rich]` **Ordering named** — an ordering assumption on events or messages is named in the plan, never implied.
- `[rich]` **Lock ordering consistent** — locks are taken in one consistent order; mixed orders are a deadlock waiting for load.
- `[rich]` **Cancellation and timeouts** — a cancelled or timed-out path completes or rolls back cleanly, never half-applies.
- `[rich]` **Stress over simulation** — a load-bearing concurrent path gets a stress or property test; a mental interleaving run proves nothing.
