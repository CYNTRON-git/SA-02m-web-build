# Procedure: backlog

Loaded on demand when a backlog item must be recorded or read (the trigger + the one-home
note live in `orchestrator.md` `## Backlog`). The **one home** for *where a backlog item
lives and how I record/read it* — every backlog reference elsewhere (`## Your seat` "you
author only", `## Audit` finding-dispatch, `.ai-dev/procedures/8d.md` D7,
`.ai-dev/procedures/downstream-feedback.md` dedup/intake) routes here, never restates the
logic. The neutral act is *record / read a backlog item*; the realisation is the
`collaboration.backlog` adapter point (`protocol-reference.md ## Project config`; the why:
`docs/decisions/multi-user-mode.md` §2). `[persona]`.

- **Resolve once.** `collaboration.backlog` (absent/unrecognised ⇒ `file`):
  - `file` (default) ⇒ edit `.ai-dev/backlog.md` — today's behaviour, byte-for-byte. Done.
  - `forge` ⇒ items live as forge **issues**. Resolve `collaboration.forge` (`github|gitlab|gitea|auto`) and read the matching verb from `src/adapter/forge-map.json` (the one home for each forge's issue CLI — persona-read, no engine loads it). `auto` ⇒ detect from the `origin` host (github.com/gitlab.com); a self-hosted host is not auto-resolvable ⇒ confirm the forge with the Operator.
- **Fail safe to `file`.** Anything unresolved — no forge detected, the forge CLI absent, no network — ⇒ stay on `.ai-dev/backlog.md` and say so; **never silently lose a backlog item** to a forge call that cannot land.
- **`file → forge` migration** (Operator decision, one-time on a project flipping to `forge`): export the **open** `.ai-dev/backlog.md` items to forge issues (each through the create verb), then **empty the file to a short marker** pointing at the forge ("tickets now live in the forge — see issues"). No two-way sync; the forge becomes the single home (invariant 6).
- **Creating a forge issue is outward-facing.** It crosses local → a possibly-public tracker, so it carries the **same discipline as `.ai-dev/procedures/downstream-feedback.md`**: announce + leak-sweep the title/body, show the Operator the exact text, file only on their OK, always with the explicit `--repo`/host target — point at that section's steps, do not restate them. **Pass the body via `--body-file -` (stdin), never an inline `--body` carrying interpolated content** — shell-metachar safety, and the form `forge-map.json` already names as the non-interactive path. Editing `.ai-dev/backlog.md` (the `file` case) is local and needs none of this.
- **Status hygiene (invariant 6 applied to the backlog — supersede, don't accumulate).** A backlog entry carries a clear status (OPEN / RESOLVED); a RESOLVED entry is **pruned at the next backlog touch**, not left as archaeology. In `forge` mode the same close-the-loop rule reaches the tracker: an issue for **shipped** work is **closed on ship**, not left open. The audit's durable-text-hygiene dimension (`## Audit`) is the periodic catch for cruft that slipped through.
