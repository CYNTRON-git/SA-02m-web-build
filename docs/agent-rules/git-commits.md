# Git commit messages (SA-02m web)

Project-local commit format for the orchestrator when it commits after review.
Conventional Commits; GitHub flow (issue refs `#123` in the body / PR
description). Loaded via `CLAUDE.md` `@`-import. Adapted from the MR-02m
ruleset; the format matches this repo's existing history
(`<type>(<version-or-scope>): summary`).

Machine-facing: subject and body prose in **English** (`PROTOCOL.md`
invariant 5). Existing history mixes Russian bodies — new commits use English;
do not rewrite history.

---

## Problem to avoid

**Never put the full change explanation in the commit title (subject).** Long
subjects truncate in `git log --oneline`, GitHub UI, and blame.

Target **≤50 chars** for the subject; hard limit **72 chars**.

---

## Format

```
<type>(<scope>): <short imperative summary>

**Summary of change:**

<why; audience — device operator, integrator, web user; purpose>

**What changed:**

<visible behaviour: UI, CGI JSON fields, device scripts, installer; if none —
explicitly "no change for web users / deployed devices">

**Verification method:**

<quality gates run, headless screenshots (which states/themes), curl checks,
on-device check if any>

<optional footer: Refs #123, BREAKING CHANGE:>
```

- Blank line between subject and body when the body is present.
- **Body sections (required for non-trivial commits):** the three blocks with
  headers exactly as above. For dev-only work, state "no change for web
  users" under **What changed:**; under **Verification method:** list what
  was actually run — never an empty section.
- Subject: imperative mood, no trailing period, lowercase after the colon
  (except proper nouns: Ethernet, MQTT, RS-485, MPLC, CODESYS, USB).
- Scope: the release version (`1.0.4.1`) for release-branch work — matching
  this repo's convention — or the narrowest subsystem otherwise.

## Types and scopes

Conventional Commits types (`feat`, `fix`, `refactor`, `docs`, `test`,
`chore`, `build`, `ci`, `perf`). Scopes seen in this repo's history:
the version (`1.0.4.1`), `web`, `flasher`, `mqtt`, `gateway`, `kernel`,
`install`, `net`, `services`, `ui`, `i18n`, `tools`.

## Checklist before `git commit`

1. Subject ≤72 chars? (≤50 preferred)
2. Subject = one line, no trailing period?
3. Non-trivial commit body contains all three sections?
4. Details, file lists, JSON field names → body, not subject?
5. Type and scope match the change (version scope on release branches)?
6. `Refs #N` when applicable?

Commit only when the Operator explicitly asks (or the ship beat requires it
after review). Stage **named paths only** — never `git add -A` (the tree holds
untracked transients: plans, state, scratch).
