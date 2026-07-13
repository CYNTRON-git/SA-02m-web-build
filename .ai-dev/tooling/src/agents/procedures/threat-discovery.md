# Procedure: threat discovery

Loaded on demand when threat discovery fires (the trigger line lives in
`orchestrator.md` `## Side-tools`).

`threat discovery` records **who attacks this product and what they can take** into `docs/threat-model.md` — the standing threat model a security-relevant feature plan cites, the way every plan cites the brief. The short sketch from inception or doc bootstrap is the seed; this is the depth. Not a side-tool: it runs through the normal loop as a feature. `[persona]`.

**When it fires:**

- **Offered** — when an inception or doc-bootstrap threat sketch finds real users or data and the Operator wants depth beyond the sketch.
- **Lazy** — on a security-relevant feature request while `docs/threat-model.md` is absent or still the sketch. Short, declinable offer — never a block.
- **Explicit** — the Operator asks.
- **Never without real users or data** — nothing worth modelling means no offer (the sketch step's own gate), not a thinner ritual.

**One pass:**

1. The Builder (its threat-model fold) drafts from the brief + the tree into the template's shape (`src/templates/threat-model.md`): on a brownfield the actors, assets, and boundaries are visible in the tree; on a greenfield they come from the inception decisions.
2. The dialog walks one axis per round — actors, assets, trust boundaries, abuse cases — a different kind of inquiry each round (discovery's rule); never invent a threat: an axis nobody assessed stays `[?]`.
3. The Operator corrects in plain language — they know the adversaries and what is worth taking better than the tree shows.
4. Conclude — at the end, on top of everything gathered: the strongest unmitigated threat, named honestly. "This is currently exposed" is a legal verdict; a threat model that cannot reach it is theater (discovery's conclude-honesty pattern).
5. The Reviewer checks the draft against the brief + the tree — an invented actor or asset, or a secret value copied in, blocks (honesty item). Ship like any feature.
