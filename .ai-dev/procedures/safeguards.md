# Procedure: safeguards

Loaded on demand when the Operator queries or toggles a safeguard (the trigger lives in
`orchestrator.md` `## Safeguards`). `safeguards` lets the Operator see and individually
toggle the **ask/nudge** guards — config is yours to author (like setup). The guard
registry is one home: `src/adapter/deny-rules.json` (each rule's `label`, `class`,
`toggleable`); the engine exposes it via `safeguardRegistry`/`disabledSafeguards` in
`_internals`. `[persona]` — the engine enforces the floor mechanically; this is the surface.

- **Query** ("what safeguards are on", "show the guards") — read the registry + `.ai-dev/config.json` `safeguards`; report each guard's label, class (mechanical-deny / ask-confirm / nudge) and current state. A non-toggleable rule shows as "floor — permanent".
- **Toggle** ("turn off force-push", "enable the remote-mutation confirm") — if the named guard is `toggleable`, flip `safeguards[id]` in `.ai-dev/config.json`, announce it (a recorded, git-tracked conscious risk acceptance — invariant 4). If it is a **floor** guard (any deny-class rule or the merge-gate), **REFUSE** and explain it is a permanent mechanical floor that cannot be switched off by words — else the model could disable its own enforcer.
- Plainly: ONLY ask/nudge guards are conversationally toggleable; the deny-class floor and the merge-gate are permanent.
