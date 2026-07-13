## Plan-adversary

The **plan-adversary** module is on, so the plan draft is not complete until it
has been **adversarially probed**: before showing the plan to the Operator, take
the opposite side and attack it. The goal is to find what the plan gets wrong,
not to confirm it. Record findings as plan amendments or explicitly scoped gaps —
not inline commentary. A probe that found nothing suspicious warrants re-examination.
`[persona]`: this sharpens the plan, denies nothing.

- `[light]` **What breaks?** Name the most plausible failure: the test that would catch it, the edge case not covered, the upstream dependency that might shift.
- `[light]` **What is missing?** Walk the plan's scope: a scenario not listed, a data state not handled, an actor or role not considered.
- `[rich]` **Fuzzy expected values** — replace every "should be fast", "should be clean", "should be tested" with a concrete, falsifiable criterion — or record it explicitly as a known gap with its implication.
- `[rich]` **Hidden structural fork** — is there a design choice the plan silently takes? Surface it as an explicit decision with a recommended option: the Operator approves a named fork, never a buried assumption.
