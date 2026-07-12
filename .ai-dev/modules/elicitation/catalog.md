# Elicitation technique catalog

The one home for the protocol's elicitation techniques — the angle-changing prompts
the interactive elicitation offer (`src/agents/procedures/elicitation.md`) draws from (and the Builder's plan-beat
fragment on a `full` profile). Each technique is a one-round act: applied to the
draft at hand, producing amendments or a conscious "considered, keep as is".
Offer 3–5 that fit the content — never the whole list (the menu, not the catalog,
is what the Operator sees).

| Technique | One-round act |
| --- | --- |
| **Pre-mortem** | "It is six months later and this failed — what killed it?" Work back from the named failure to the requirement it implies. |
| **Hindsight 20/20** | The mirror: it succeeded — what almost stopped it, and which "if only we had…" was true at the start? |
| **Inversion** | State the goal, then design the opposite: "how would we guarantee users abandon this?" Each anti-feature flags a real requirement. |
| **User role-play** | Speak AS the target user (from the brief's audience) walking zero-to-working: where do they stall, what word don't they know, what do they expect that isn't there? |
| **Persona panel** | Three short takes on the draft — a skeptic investor, the target user, a competitor's PM — each in two sentences; the Operator reacts to the spread. |
| **Red vs blue** | One voice attacks the draft's weakest claim, another defends it; the exchange names the assumption both sides depend on. |
| **Socratic chain** | Ask the Operator three "why" questions deep on one claim — stop at the first answer that is a guess, mark it `[?]`. |
| **First principles** | Strip the draft to the constraints that are physically/legally true; everything else is a choice — name which choices are load-bearing. |
| **Constraint removal** | "If budget/time/stack were unlimited, what would this be?" The delta names what the constraint is actually costing. |
| **Stakeholder sweep** | Who else is affected and never mentioned — admin, support, the person who pays, the person who migrates away? One line each. |
| **Analogy transfer** | "What is the closest product that solved this shape of problem, and what did they learn the hard way?" (pairs with the `research` side-tool when unknown). |
| **Scale shock** | Re-read the draft at 10× and at 1/10th the expected usage — which sentence breaks first? |
| **Assistive-tech walk** | Walk the primary path as a keyboard-only or screen-reader user — where does the idea silently assume a mouse, a screen, perfect vision? (The capture-time half of accessibility; the review-time half is the ui-ux module's.) |

Retention: this catalog is data, not procedure — the *when* and the depth-choice
entry live in `src/agents/procedures/elicitation.md` (one home each). Extend by adding
a row; a technique nobody picks in practice is a row to delete, not a ritual to keep.
