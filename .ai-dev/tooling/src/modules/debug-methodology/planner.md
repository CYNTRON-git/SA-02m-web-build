## Debug methodology

The **debug-methodology** module is on. The floor names no debugging method — this
module ADDS it: where the change fixes a bug, work cause-first under the rules below;
guess-patching (the symptom "fixed", the cause alive) is the failure this prevents.
`[persona]`: this sharpens the work, denies nothing.

- `[light]` **Reproduce before fix** — no reproduction means nothing to verify a fix against; the repro comes first.
- `[rich]` **Cheapest discriminating experiment** — form a hypothesis, run the cheapest experiment that discriminates between hypotheses, and change one variable at a time.
- `[rich]` **Read the real state** — read the actual logs, data, and state; never simulate the system mentally and call it evidence.
- `[light]` **Cause, not symptom** — fix what made the bug possible; a conscious containment is fine when named as containment, never sold as the fix.
- `[rich]` **Debuggable by design** — the fix leaves the system more debuggable, not less: no silent catch, errors carry context, boundaries log what crossed them.
