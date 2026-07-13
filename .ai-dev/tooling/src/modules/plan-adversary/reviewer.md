## Plan-adversary

The **plan-adversary** module is on, so the plan check is deepened from "does the
plan match the work" to **probe evidence**: confirm the plan shows signs of
adversarial self-examination before build. A plan with no named failure mode, no
replaced fuzzy criterion, and no surfaced fork under a `rich` depth is likely
unexamined — flag it. `[persona]`: this sharpens judgement, denies nothing.

- `[light]` **Probe present** — does the plan name at least one failure mode or missing scenario? Absence of any adversarial signal under `rich` depth is a finding.
- `[rich]` **Forks resolved** — every structural fork the probe surfaced has a named decision and a recommendation; none passed silently into the build.
- `[rich]` **Fuzzy criteria replaced** — any criterion left fuzzy is explicitly recorded as a gap with its implication; none is tacitly carried forward as "good enough".
