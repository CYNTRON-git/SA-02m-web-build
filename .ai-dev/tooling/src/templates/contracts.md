# Product contracts

> One file, every product promise this project makes — one short block per user-facing feature. The Operator reads it to see what the product guarantees; the Reviewer checks every change against it. A backend-only change (refactor, infra) adds no block; it touches one only if it changes visible behaviour.
>
> Plain product language only — what the user can do, not how it is wired. A machine grammar (id formats, enums, value ranges, topic conventions) lives once in `architecture.md`; reference it here, never restate it. A promised number the Operator hasn't fixed yet is `[?]` — never invent one.
>
> *Delete this guidance block when filling.*

<!-- One block per feature. Copy the shape below; delete this comment and the example. -->

## `<feature name>`

**Value** — `<one plain sentence: what the user gets and why it exists>`.
**Who** — `<the specific user or role, not "the user">`.

**Must work** — what the feature must do

- `<a concrete thing the user can do or expect>`

**Must not break** — invariants that stay true across future changes

- `<something that must remain true>`
- ``<a user-facing limit the product promises (max items, perceived speed) — `[?]` if unfixed>``

**Verified by** — `<test name or command the Reviewer runs on every change touching this feature>`.
**Last reviewed** — `<YYYY-MM-DD>`, against `<commit / "current main">`.

---
