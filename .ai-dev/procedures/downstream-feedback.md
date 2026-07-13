# Procedure: downstream feedback

Loaded on demand when downstream feedback fires (the trigger line lives in
`orchestrator.md` `## Side-tools`).

`downstream feedback` is the protocol's two-way problem channel: a downstream session **emits** a self-report when the protocol itself fails it; the upstream session **intakes** and triages. Side-tool. `[persona]`. Design rationale: `docs/decisions/feedback-loop.md`.

**Emitting — you are the downstream session and the protocol failed you** (fired from `orchestrator.md` `## When something is off`: a deny blocking legitimate work, a gap, a gate you cannot satisfy honestly, instructions that contradict each other):

1. Write `.ai-dev/feedback/<slug>.md` **immediately, while the failure context is still in view** — through your own eyes: what was asked (one line) · what the protocol/instruction told you to do (cite file/rule) · the exact step where it broke · **your context at that moment** — what you had read, what was missing, which two things contradicted, what you guessed · what you did instead (stopped / asked / worked around) · the cost (blocked work / wrong result / confusion). Honesty: this is a SYMPTOM report by the model that just failed — it may itself be confused; never present it as diagnosis.
2. Tell the Operator it exists. Before any send: **leak-sweep the draft** — secrets and tokens, credentialed URLs, internal paths, project/product names, personal data, business content; strip or redact each, the issue carries the **protocol-level symptom only**. Then **show the Operator the exact title and body that will be published** (verbatim, with a translation relay where the chat language differs) and wait for their explicit OK on that shown text — never a paraphrase, never silent. Only then file it — **always with the explicit repo flag**: `gh issue create --repo wirenboard/ai-pm-protocol` (or whatever upstream the tooling came from): a bare `gh issue create` inside a downstream checkout defaults to the DOWNSTREAM's own tracker and publishes the report to the wrong, possibly public, place. An issue is public and effectively permanent. Anything swept out stays in the local file only. No `gh`, no network, or a declined OK ⇒ the Operator carries the file by hand.
3. The local file is transient — delete once carried or filed.

**Intake — you are the upstream session** (the Operator pastes a report, or points at a filed issue):

1. Read it; do NOT echo it back verbatim. Map it to the protocol's structure: owning file, the invariant or rule it touches, the failure class (honesty over-claim, mechanical gap, unclear procedure, missing coverage).
2. Dedup against the backlog (`orchestrator.md` `## Backlog` — file or forge) — if the substance is already there, add the downstream signal as a note on the existing item (one home — invariant 6).
3. If new: draft a compact backlog entry — the **protocol-level finding**, not raw downstream content — and record it through `orchestrator.md` `## Backlog` (a `file`-backlog edit, or a forge issue under that section's outward-facing discipline). Confirm with the Operator before it lands — never silent.
4. **Privacy:** what lands in the backlog or issue is the protocol-mapped finding; raw downstream content is not committed unless the Operator explicitly OKs it. The boundary holds both ways: this session never reads into a downstream repo (project-boundary deny).
