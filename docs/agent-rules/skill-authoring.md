# Skill authoring — description, triggers, token budget

Project-local rule for anyone adding or editing a skill under
`.claude/skills/`. Adapted from the obra/superpowers writing-skills
methodology to this repo's skill set. Loaded into the orchestrator session
via `CLAUDE.md` `@`-import. Machine-facing: English (`PROTOCOL.md`
invariant 5).

This is the **one home** for how a skill is written here; the house example
of a well-triggered description is `sa02m-web-testing` (symptom-keyword
triggers, "Use when" opener). The 2026-08-26 skill audit brought the set of
4 under this rule: descriptions reordered to the "Use when" opener, the
web-architecture tab table merged into its domain-doc one home.

---

## Description — the trigger, not the summary

- Starts with **"Use when ..."** / **"Use BEFORE ..."** and names TRIGGERING
  CONDITIONS only — never a workflow summary (an agent acts on the
  description before opening the body; a summarized workflow gets
  half-executed).
- Carries concrete, searchable keywords: the symptom wording the Operator
  actually uses («виджет застыл на "—"», "stale cache"), file names the
  skill guards (`app.js`, `status.cgi`), tokens and identifiers
  (`session_token`, `data-hide-for`), error strings.
- English only. Honesty note: this repo has NO `verify-english` quality row —
  the rule is persona-held, not mechanically scanned.

## Body — token budget and one home

- A skill likely to load often stays under ~200 words of body; any skill
  under ~500. Heavy content lives in its one home (`docs/agent-rules/`,
  `docs/contracts/`, `docs/decisions/`) — the skill POINTS at it, never
  restates it (`PROTOCOL.md` invariant 6). A skill that restates a rule doc
  will drift from it.
- One concept per skill. A skill that needs an index of sub-topics is two
  skills, or a rule doc.

## When NOT to make a skill

- A one-off fix or investigation → `docs/bugs/`.
- A standing project convention that binds every session →
  `docs/agent-rules/` (session-loaded via `CLAUDE.md`), not a skill.
- Content already homed in a doc → cite the doc; a skill adds value only as
  a trigger + dispatch layer.

New skills follow this rule; the pre-existing set was audited into compliance
on 2026-08-26 (descriptions reordered, the duplicated table merged — see that
PR and git history — a docs-only PR carries no CHANGELOG entry).
