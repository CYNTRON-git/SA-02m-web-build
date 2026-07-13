## UI & UX

The **ui-ux** module is on, so the plan's **Product questions** item is deepened on
its surface half — from "the success / empty / error state" to a usability
enumeration: where the change touches a user-facing surface, close each dimension
below in the plan or consciously descope it with a reason. A feature can pass every
gate and still be unusable in minutes — these dimensions are where that failure
hides. Honour `docs/hmi-conventions.md` where the project has one. `[persona]`: this
sharpens the plan, denies nothing.

- `[rich]` **Adaptivity** — the surface works across screen sizes and devices; no hardcoded dimensions.
- `[rich]` **Accessibility** — keyboard navigation, contrast, roles/alt text, assistive-tech compatibility; WCAG as orientation, not a checkbox.
- `[light]` **Responsiveness** — loading states, feedback for every action, no dead air while the system works.
- `[light]` **Clarity** — each control affords its use; error text says what to DO next, in the user's language, never a raw internal code.
- `[light]` **Adverse states** — offline, device loss, reconnect, partial failure, restart; the plan covers them, not just the happy path.
- `[light]` **User-flow check** — the GUI deepening of the plan's **Verification scenario** item (one scenario at the floor): for a new or modified user-facing flow, enumerate the critical path as (step → UI element → action) for at minimum 3 steps; this surfaces DOM lifecycle dependencies (does the element exist when the initializer runs?) and missing feedback paths (how does the user verify their configuration works?) before code is written.
