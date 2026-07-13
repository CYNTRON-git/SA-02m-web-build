## Database

The **database** module is on. The floor's plan checklist names no data-layer
dimension — this module ADDS it: where the change touches a schema or persistent
store, the plan covers the rows below; schema churn, an irreversible migration, and
integrity living only in app code are the failures this prevents. `[persona]`: this
sharpens the plan, denies nothing.

- `[light]` **Migration always** — every schema change ships as a migration; no hand-applied schema drift.
- `[light]` **Reversible or named** — a migration is reversible, or its irreversibility is a conscious, named choice in the plan.
- `[light]` **Rollout compatible** — the schema change is compatible with the code still running during rollout (expand, migrate, contract — not flag-day).
- `[light]` **Data-loss named** — a destructive change names its data-loss risk before it runs, never after.
- `[light]` **Integrity at the DB** — foreign keys, NOT NULL, unique constraints live in the database, not in app code alone; app-only integrity is one bug away from corrupt data.
- `[rich]` **Schema serves access patterns** — the schema is designed against the named access patterns, and indexes follow those patterns, not habit.
- `[rich]` **Atomic multi-step writes** — a multi-step write is a transaction; partial completion is a named impossibility, not an accepted risk.
