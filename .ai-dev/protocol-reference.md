# Protocol reference

Reference detail for PROTOCOL.md — read on demand. Per-turn essentials are in PROTOCOL.md.

---

## Enforcement

A platform's deny layer can **block a tool call** (and, on some platforms, **ask** the Operator first). It **cannot force a positive act** (cannot make the Orchestrator spawn a reviewer) and **cannot read the Orchestrator's reasoning**. Every protection is therefore either deniable or persona-only, labelled honestly: `[mechanical]` is backed by a deny rule in the adapter data; `[persona]` by this prose alone.

**Mechanically denied** (each adapter realises this as its platform's deny — `## Core and adapter`):

- Read, search (`find`), a **Bash** read (`cat`/`grep`/`head`/`< file` — best-effort, see invariant 2), or write a path that resolves **outside the permitted root set** — the session root, plus any sibling root a valid `.ai-dev/components.json` declares (the multi-repo widening; fail-closed validator + schema: `docs/architecture.md` `## Components`). Absent or invalid manifest ⇒ the single session root, byte-identical to before. With no actor resolution these are `[mechanical]` on **both** platforms (no Claude fail-open caveat) — the Read-tool/`find`/write half is a precise single-path check, the Bash-read half a best-effort command-extractor that fails OPEN on a parse miss (an interpreter/`$VAR`/unlisted-command read slips; the role-scope persona rule below is its backstop). **Only these four read/find/bash-read/write boundary denies consult the component set** — the truncation, self-patch (`.ai-dev/tooling/`), stamp-write, orchestrator-content, and merge-gate denies stay anchored to the session root, unconditional, and a manifest never widens them. The read-family three (Read/`find`/Bash-read) consult a **second** widening alongside the component set — the agent's own harness-assigned scratch (the tool-result overflow store + the per-session temp dir), derived fail-closed from the platform's env and session-id-narrow; the write deny consults the SAME derivation but a **narrower** subset — only the agent's own per-session temp root, never the tool-result overflow store, so a write to the overflow store still denies while a write to the temp root allows. Mechanism, derivation, and the read/write scoping: `docs/architecture.md` `## Sanctioned scratch`.
- A truncating write — empty/whitespace content over an existing non-empty file.
- The **Orchestrator** writing a source or canonical-doc path (sub-agents author content; the Orchestrator routes). **Mechanical only on a platform that resolves the actor (OpenCode); `[persona]` on Claude**, whose hook payload carries no session-role signal — there it fails open and the discipline is prose-held. Allowed exceptions: its own state and a feature plan; the tooling submodule is never writable. Relaxed for source/doc writes when the project's `profile` permits the Orchestrator to build (`## Project config`) — a no-op where the deny already fails open (Claude); the merge-gate, self-patch, stamp-write, and project-boundary denies are never relaxed.
- The **Orchestrator** writing into the review-stamp directory (`.ai-dev/reviews/`) — the stamp is the Reviewer's deliverable and the merge-gate reads its presence, so an Orchestrator-authored stamp is a fabricated gate. Same actor caveat as the row above (mechanical on OpenCode, `[persona]` on Claude) — but **never relaxed by profile**: the Reviewer seat never collapses into the Orchestrator. Deleting the stamp at ship is not a write and stays allowed.
- **Self-patching the enforcer** — the tooling submodule changes only by a version bump, never by an in-place edit.
- An **in-place content edit on a remote system** (`ssh` + an editor or redirect) — a repo-owned file changes through git, never a remote edit (invariant 4).
- **Merging while the review is unstamped** — the ship-time floor under invariant 3, on guarantee profiles (`full`/`lite`/`solo`). Checks the stamp's *presence*; *authorship* is guarded by the stamp-write deny above, with the same per-platform honesty. Off for `yolo` — the merge-gate is explicitly removed for that profile; the Operator's merge word is the only remaining check. The same merge-gate family also denies a **direct commit to `main`** while the review is unstamped (`## Git flow` "Never commit to `main`").
- A **blind bulk git-stage** (`git add -A` / `git add .` / `--all` / `*`) — the working tree holds untracked transients (plans, stamps) by design, so a blind stage would leak them into durable history; stage named paths only (orchestrator's `## Your seat`).
- A **role-duplicator or generic built-in** spawned into a protocol seat (invariant 1).

**Ask-class** (the platform asks the Operator before proceeding, where it supports an "ask" return — otherwise persona):

- A force-push.
- A commit that skips verification.
- A mutating action on a remote system (deploy, maintenance, runtime state). A project may disable this confirm (and other ask/inject guards) via `.ai-dev/config.json` `safeguards` — a recorded conscious risk acceptance; the deny-class floor and the merge-gate are never config-disablable, and the remote-edit DENY is unaffected.
- A merge or push whose branch topic the merge-gate cannot resolve — the stamp is uncheckable, so the gate asks instead of passing.

An ask/inject guard marked `toggleable` may be individually disabled via `.ai-dev/config.json` `safeguards` (a recorded conscious opt-out — orchestrator's `## Safeguards`); the deny-class floor and the merge-gate are never config-disablable.

**hookMode** (`strict | light`, default `strict`, overridable via `.ai-dev/config.local.json`): controls how ask-class rules are realised. In **strict** mode (the default, fail-safe), ask-class rules ask the Operator to confirm where the platform supports an ask-return. In **light** mode, ask-class rules become **deny + an informative message** to the model — the model sees what is denied, why, and the safe alternative, and adapts its plan without interrupting the Operator. No new deny or inject rule is added; this is a mode for existing ask-class only. The personal override lives in the gitignored `.ai-dev/config.local.json` (mirroring the launch personal/shared split), so a developer sets `hookMode: "light"` locally without forcing it on teammates. Absent or unrecognised values resolve to `strict` — the Operator is always asked unless they explicitly opt into light.

**Inject-class** (the platform adds a context note to the turn — it nudges, never blocks; only *reinforces* a `[persona]` act, the act itself stays the Orchestrator's; realised mechanically where a platform has a prompt hook, always-on instruction text where it does not):

- On a repo-change request: a reminder to route the work to the owning role and follow the loop.
- On a work request to an **unconfigured** project: a reminder to run `setup` first.
- On a feature request to a configured project with **no product brief**: a short, declinable offer to run product discovery first.
- On **every** submitted prompt: a reminder to reply in the Operator's conversation language (invariant 5) — the turn's English artifacts must not pull the reply into English. Several injects on one turn aggregate into one note; this always-on reminder never suppresses a conditional one.
- After a **spawned-role handoff** (Task-tool return) — `[mechanical]` on Claude, `[persona]` on OpenCode (inject-class is persona-only there; the chat.message hook was dropped — M18): a short reminder to refresh the active plan's progress note and reconcile the resume pointer. Reinforces the Orchestrator's `[persona]` continuous crash-resume checkpoint rule. The act stays the Orchestrator's; this nudges, never blocks.

**Persona-only** (no deny is possible — these are reasoning acts):

- Pipeline ordering and every positive act — on guarantee profiles, *always* spawn the reviewer, *never* collapse the loop, *a plan precedes code*. The merge-gate is the downstream floor (on guarantee profiles). A `yolo` project explicitly opts out of both; the Operator's merge word remains.
- Never self-substitute a crashed role's deliverable; retry the same spawn up to twice, then **stop and report to the Operator** — never synthesize the verdict, stamp, or merge.
- Never fabricate a review stamp — the gate checks presence; the stamp-write deny adds authorship only where the platform resolves the actor (Claude stays persona).
- Never present a stale on-disk artifact as this turn's fresh gate result.
- **Role scope** — a role never reads another agent's out-of-root working state or transcript, even when a path to it leaks into view; judge the diff and run the tools, never mine a sibling agent's raw output. This is the named backstop for the best-effort Bash-read deny (invariant 2): the mechanical extractor narrows the surface, this scope discipline closes the residual the parser cannot reach.

The single invariant these collapse into — *a deliverable is satisfied only by a fresh spawn this turn; failed / missing / already-existing / skipped all count as "not run"* — is **persona**, enforced where it mechanically can be by the floors above. Over-claiming any of this as mechanical is itself a review-blocking honesty failure (`## Role contracts`).

---

## Project config

`.ai-dev/config.json` binds a project's choices, so the core depends on **no specific agent**:

- **mode** — `autonomous | interactive`; the value-home for invariant 7 (absent or unrecognised ⇒ `interactive`).
- **profile** — `full | lite | solo | yolo` (absent/unrecognised ⇒ `solo`): the speed↔quality dial plus one named escape hatch outside its guarantee. `full`, `lite`, and `solo` are the **guarantee profiles**: they share a floor that holds in every one of them — an independent review by a fresh Reviewer, the merge stamp, the honesty gates, the Operator's explicit merge authorization. Ceremony above the change's risk is a defect the Operator pays for — the dial cuts ceremony, the floor holds rigor. `yolo` is **outside this guarantee**: the merge-gate is off, no Reviewer is required, plans are a running spec. The one floor `yolo` keeps is the Operator's explicit merge word (invariant 7 — unchanged). Threat note: absent/unrecognised still resolve to `solo`, never `yolo` — only an explicit value enters the escape hatch; the audit cadence (offered every N features) is `yolo`'s primary compensating control. Value-home: `.ai-dev/config.json` `profile`.
- **roles** — each seat binds an **agent** and an optional **model**. Defaults to this repo's `src/agents/`; swap the agent for any one that honours the seat's contract (`PROTOCOL.md ## Role contracts`). The **Researcher-Planner, Builder, and Reviewer** are spawned sub-agents, so their `model` is the *wish* (`session` / `auto` / a per-platform pin) the platform bakes into the spawned seat — these are the only baked model pins that take effect (the Planner is `profile`-staffed; its economy is a cheap **Builder** pin against a strong, session-inherited planner). The **Orchestrator is the running session**, so it carries an `agent` only and **no `model`**: its model is the launch model (`launch` below), not a baked pin. The model policy — what `auto` resolves to, and each platform's model authority — lives in the platform adapter (`src/adapter/tool-map.json` `models`).
- **launch** — an optional `{ sessionModel, guardModel, configDir, aliases }` naming the **launch-time** settings: the session/orchestrator model, the harness guard/background model, a per-project claude profile dir, and the cross-endpoint tier→model bindings (`aliases.{opus,sonnet,haiku}`). These are NOT baked into an agent — they are set as environment at session start, so `launch` is the **one home** every launch path reads (the launcher exports them before starting the session; a hand-rolled wrapper reads the same values). Absent / empty ⇒ nothing exported (a non-routing project is byte-unchanged). The concrete env-var names + the launcher's read-and-export are the platform adapter's (`src/adapter/README.md`); the rationale is `docs/decisions/multi-model-setup-ux.md`.
- **platform** — the active adapter (`claude | opencode`).
- **kind** — the artifact consumer: `code` (machine-executed), `docs` (human-read), or `mixed` (both). Seeds the capability-module defaults and the reviewer framing. Absent or unrecognised ⇒ `code` (strict-side default).
- **docLanguage** — a language code string naming the language the project authors its **human-read documentation** in (the axis invariant 5 governs); the value-home for that rule. Default `"en"`; absent / empty / whitespace ⇒ `"en"` (fail-safe to the universally-readable language, matching every other field's safe-default discipline). A non-empty unrecognised code is not an error — it means "author docs in the named language"; only an absent/blank value falls back. The English floor (code, commits, config, machine grammar) is invariant 5's, independent of this value.
- **collaboration** — an opt-in object `{ team, backlog, forge }` for multi-user (team) development (the why: `docs/decisions/multi-user-mode.md`). `team` (`true | false`, default `false`) gates team mode — named `team` to keep it off the `mode` and `profile` axes; `backlog` (`file | forge`, default `file`) selects the file backlog (today's `.ai-dev/backlog.md`) or forge issues; `forge` (`github | gitlab | gitea | auto`, default `auto`) names the forge whose issue CLI the backlog adapter uses, `auto` detecting it from the `origin` remote host. Fail-safe to single-user like every field above: an absent object, an absent key, or an unrecognised value resolves to the safe default (team off, file backlog, forge auto) — a bad value never silently enables team mode or forge backlog. The wiring that reads it is in place, gated by different keys: the forge-issue backlog adapter is **`backlog`-gated** (active on `backlog:forge`, independent of `team`), while the two **`team`-gated** readers — the `team-collaboration` capability module and the ship-time PR-attach of the review verdict — stay inert while `team:false`.
- **hookMode** — `strict | light` (absent/unrecognised ⇒ `strict`): controls how ask-class enforcement rules are realised. `strict` (default, fail-safe) = ask-class rules ask the Operator to confirm where the platform supports an ask-return. `light` = ask-class rules become deny + an informative message to the model (the model sees what is denied, why, and the safe path, and adapts without interrupting the Operator). No new hook — a mode for existing ask-class only. Overridable via the gitignored `.ai-dev/config.local.json` (personal/dev override, mirroring the launch personal/shared split). Value-home: `.ai-dev/config.json` `hookMode`.

The Orchestrator resolves a seat through `roles` before spawning, and reads `mode` for decision authority. A swapped-in agent is bound by the role contract, not by being ours — the ship-gate checks the verdict's *form*, not its author.

## Core and adapter

The protocol is **one neutral core + one thin adapter per platform**.

**The core** — `PROTOCOL.md`, `protocol-reference.md` (this file), the role definitions, the checklists, `docs/architecture.md` — is written in abstract acts only: *read a file*, *write a file*, *spawn a sub-agent*, *ask the Operator a structured question*, *deny a write outside the root*. It names **no** platform, tool, hook, or plugin.

**Each adapter** realises this fixed contract for one platform, and nothing more:

| Contract point (neutral) | What the adapter supplies |
| --- | --- |
| abstract tool → concrete tool | the platform's name for read / write / edit / spawn-sub-agent / ask-structured-question |
| enforce a deny | the platform's deny mechanism, loading the **shared deny-rules data** |
| spawn a sub-agent | how this platform starts a child role |
| continue a sub-agent *(optional)* | how this platform resumes an existing sub-agent, if supported; absent ⇒ fresh spawn (no capability lost, only the re-read token cost) |
| load instructions | how this platform loads this core every turn |
| install into a project | how this platform wires the protocol into a downstream repo |

- The deny **rules** are shared data — one list of patterns and intents, covering every `[mechanical]` row above. Each platform's deny mechanism is a **thin shim that loads that one list**; the rules are never re-authored per platform. The tool-name map is likewise one small per-platform table.
- **Acceptance test for "agnostic":** a *new* platform is supported by writing **only** its adapter (the tool map, the deny shim, the spawn / load / install glue) against this fixed contract, with **zero edits to the core**. A new platform that forces a core edit means the boundary leaked.

---
