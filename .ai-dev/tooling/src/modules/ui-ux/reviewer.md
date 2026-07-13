## UI & UX

The **ui-ux** module is on, so the floor's **Correctness** item is deepened on the
user's actual path: for a user-facing surface, confirm each usability dimension the
module names at this depth (adaptivity, accessibility, responsiveness, clarity,
adverse states) is closed in the diff or consciously descoped with a reason. The
failure this catches: a change that passes every gate and is unusable in minutes.
`[persona]`: this sharpens judgement, denies nothing.

- `[light]` **Each dimension closed or descoped** — every dimension is closed in the diff or carries a conscious "descoped: why"; a blank skip is a finding.
- `[light]` **Adverse-state silence is a finding** — a user-facing change that never names offline / partial-failure / restart behaviour is unreviewed on the paths users actually hit.
- `[rich]` **Rich dimensions concretely checked** — a hardcoded dimension, a control unreachable by keyboard, or a missing role/alt is a cited finding, not a vibe.
- `[rich]` **Graphical walkthrough where the platform offers a driver** — the graphical deepening of the floor's integration-layer walk (`reviewer.md` `## Verdict`): when the Operator has confirmed the real-layer exercise for this change (the offered act — `orchestrator.md` `## Orchestrator`, real-layer-verification offer) AND the surface is graphical AND the environment carries a UI driver (a Playwright-class browser automation for web, a WebDriver/tauri-driver for native), drive it — load the surface, capture a screenshot and the accessibility snapshot, read the console for errors, and click the primary user path end to end — each finding cited with the captured evidence. This adds the visual/a11y capture on top of the floor's "exercised through the real layer"; it is not a parallel web-only requirement. Honest residual: where no driver is available, or the exercise was not confirmed, say so and review from the diff alone — never imply the surface was exercised when it was not.
- `[light]` **Init order / lifecycle** — for a UI change that initializes components imperatively, confirm initialization happens AFTER the DOM element is ready (e.g. `onMount`/`$effect` in Svelte, `useEffect` in React, not in event handlers before the element mounts); a silent fail (initializer exits when `containerEl === null`) is a cited finding.
- `[light]` **External-service config verification** — if a core feature depends on an external API/service, confirm a configuration verification path exists in the UI — a test action the user can invoke without triggering the main flow; absent = a finding (severity: the user cannot validate their setup).
