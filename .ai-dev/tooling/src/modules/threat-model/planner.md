## Threat model

The **threat-model** module is on, so the plan's **Security surface** question is
deepened from one threat-and-mitigation line to a plan-time **enumeration**: where the
change touches auth, secrets, untrusted input, or a network boundary, walk the surfaces
below and record each live threat WITH its mitigation and the `file:line` that closes
it. Silence on a surface means "considered, not exposed" — not skipped. Where the
project has a standing threat model (`docs/threat-model.md`), cite which named actor,
boundary, or asset the change touches instead of enumerating from scratch. `[persona]`:
this sharpens the plan, denies nothing.

- `[light]` **Attack surface** — every new input / endpoint / parser / format / interface this change exposes; validate each where untrusted data first enters.
- `[light]` **Secrets & credentials** — any secret the change reads / writes / logs; source it from a git-ignored file, never hard-code or commit a key.
- `[light]` **Trust boundaries** — each point untrusted input crosses into trusted code; put the validation AT the boundary.
- `[light]` **Injection & unsafe ops** — guard any shell / SQL / path / template construction, `eval`, or deserialization fed a tainted value.
- `[light]` **Fail-open vs fail-closed** — design every error path to tighten, never relax, a guard; default to the strict side.
- `[rich]` **Data & privacy exposure** — scope reads to what's needed; keep PII out of logs; don't widen a data flow.
- `[rich]` **AuthZ / AuthN** — give each new surface its access check; match the strictest peer, don't leave a privileged path open.
- `[rich]` **Supply chain** — name and justify any new dependency; confirm its source is trusted before adding it.
- `[rich]` **Isolation / identity invariant** *(when the change touches a per-user, visibility, or identity surface)* — record the isolation invariant as an explicit contract string in the plan BEFORE build: who may see what (the owner-filter every read carries), and who may issue the identity-binding action (the auth every mutating endpoint demands). A per-user surface built without its invariant stated is built blind to the cross-user leak it must prevent.
