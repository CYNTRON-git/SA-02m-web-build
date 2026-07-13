## Semantic correctness

The **semantic-correctness** module is on. The floor's plan checklist names the
behaviour the change adds — this module ADDS the discipline of NOT claiming behaviour
the code does not actually perform: a "learns / adapts / evolves / remembers / N-level"
component must be real or marked, and a path built parallel to an existing one must
carry the original's guards. `[persona]`: this sharpens the plan, denies nothing.

- `[light]` **Real or marked** — a plan claim that a component learns, adapts, evolves, is N-level, or remembers across runs carries EITHER a test that exercises the claimed behaviour OR an explicit placeholder mark (stubbed / deferred-by-design) — never an unmarked claim over inert code. State described as persisted names its durable store and the read-back path.
- `[light]` **Parallel-path guards** — when the change adds a path parallel to an existing one (streaming↔blocking, batch↔single, async↔sync), list the original's guards — timeouts, circuit-breakers, cost / usage accounting, limits, cleanup of spawned tasks — and carry each into the new path, or record why it is N/A.
- `[rich]` **No silent swallow by design** — the plan does not rely on an error path that catches-and-continues without logging or re-raising; if a failure must be absorbed, name where it is recorded so a dead feature surfaces instead of hiding.
