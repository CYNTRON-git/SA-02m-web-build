## Database

The **database** module is on, so where the change touches a schema or persistent
store the review gains the data-layer dimension the floor does not name: judge the
change as the database will live it — through migration, rollout, and failure — not
as the app code wishes it. `[persona]`: this sharpens judgement, denies nothing.

- `[light]` **Migration present** — a schema change without its migration is a finding.
- `[light]` **Integrity where claimed** — a named integrity rule enforced only in app code is a finding; the constraint belongs in the database.
- `[light]` **Loss and rollout named** — a destructive change without its named data-loss risk, or a schema change that breaks code still running during rollout, is a finding.
- `[rich]` **Atomicity and access patterns** — a multi-step write left non-atomic, or a named access pattern with no index serving it, is a cited finding.
