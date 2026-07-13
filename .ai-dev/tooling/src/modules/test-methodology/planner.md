## Test methodology

The **test-methodology** module is on. The floor carries the build-time testing rules
(build-beat tools green, a newly failing test never silenced, the defect-fix ratchet,
the verification ladder) — this module ADDS the plan-time coverage dimension: where the
change touches logic a unit test cannot reach, or a user-visible surface, the plan names
how it is exercised, or names the untested-layer risk consciously. `[persona]`: this
sharpens the plan, denies nothing.

- `[light]` **Unreachable layers** — a layer unit tests cannot reach (fetch+state glue, route handlers, adapters) gets its coverage named in the plan, or the untested-layer risk named in its place; silence on the layer is the failure mode.
- `[rich]` **Full-composition test** — a wiring-level feature carries at least one integration test exercising the real composition, not just each part in isolation.
- `[light]` **App-bug vs test-drift** — classify every failing test before touching anything: a real app bug (fix the code) or test drift (raise it); never patch whichever is cheaper.
