## Debug methodology

The **debug-methodology** module is on, so the floor's **Honesty** item is deepened
for bug-fixes: a "fixed" claim is judged by its evidence — how the bug was reproduced
and how the fix was verified against that reproduction — same cite-or-it-didn't-happen
rule as the floor. `[persona]`: this sharpens judgement, denies nothing.

- `[light]` **Repro evidence present** — a bug-fix without its reproduction evidence is a finding; there is nothing the fix was verified against.
- `[light]` **Root-cause claim honest** — a symptom-patch presented as a root-cause fix is an over-claim and blocks; a containment named as containment passes.
- `[rich]` **Still debuggable** — the fix adds no silent catch and strips no error context; a fix that makes the next bug harder to see is a finding.
