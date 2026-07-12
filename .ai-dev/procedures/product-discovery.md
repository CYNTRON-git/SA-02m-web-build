# Procedure: product discovery

Loaded on demand when product discovery fires (the trigger lives in `orchestrator.md`
`## Product discovery`). `product discovery` records **what product, and for whom** into
`docs/product.md` before features are built. Your procedure — talk to the Operator, write
the brief you own. `[persona]` — blocks nothing mechanically.

**Two phases — never mix them:**

1. **Gather** — gap detector, not a judge. Record what the Operator gives; mark unknowns `[?]`. Never grade whether an answer is "good"; never plant risk/trap flags mid-stream.
2. **Conclude** — a **named turn after gather completes**: announce it explicitly ("now the conclude round"), then apply 2–3 adversarial techniques from the elicitation catalog (`.ai-dev/modules/elicitation/catalog.md`) — Pre-mortem, Persona panel, and Red vs blue are the natural fit. Fill §7 of the brief: strongest reason this won't succeed · who it is wrong for · stop signals. **Be willing to report the build is wrong.** A discovery that cannot reach that verdict is a confirmation ritual. All-`[?]` in §7 is the same failure.

**The dialog** — single home: `src/templates/product.md` (do not restate the questions here):

- Run through the structured-question tool, a different kind of inquiry each round.
- Never invent an answer; a number not fixed is `[?]`.
- Anchor on the idea first (brief §0 — one line; legacy = read from the tree, new = ask). Every later question stays plausible *given* the product.
- Walk the user's zero-to-working story: who it is for · the problem in their words · how a new user finds out it exists · first steps from nothing to working · access across sessions/devices and what happens on lost access · who runs and funds it.
- Customer is usually a spectrum — ask it openly; never force a pick-one fork on a range axis.
- Research the competition first if unknown — use the `research` side-tool; draft what you found; let the Operator correct it.
- After a section drafts, the elicitation offer may fire (`.ai-dev/procedures/elicitation.md`; depth choice first, light default, declinable) — it deepens the section without extending the question list.
- When the Operator **declares the product unfamiliar** (adopting someone else's codebase), flip the whole brief to draft-first — the competition bullet's research-then-draft-then-correct pattern, extended from one question to every section: read the tree, draft each section as evidence-based hypotheses with confidence marks and the explicit provenance "reconstructed from the tree", then walk the Operator through it section by section to correct.
- What the tree cannot show — the real users, their pain, who runs and funds it — stays `[?]` unless the Operator fills it; the conclude phase runs unchanged, still able to say "wrong product".

**When it fires:**

- **At onboarding** — right after `setup`, as the natural continuation.
- **Lazy** — on the first feature request to a configured project with no `docs/product.md`. Short, declinable offer — not a block.
- **Explicit** — the Operator asks to define or revisit.
