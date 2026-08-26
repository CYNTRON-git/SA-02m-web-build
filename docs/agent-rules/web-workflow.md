# Web workflow — planner and reviewer discipline

Project-local workflow rules for the SA-02m web interface. Adapted from the
MR-02m `firmware-workflow.md` (same discipline, web failure classes). This is
the **one home** for the pre-edit and post-edit discipline — never restated in
plans or reviews; cited. Loaded via `CLAUDE.md` `@`-import.

---

## Pre-edit — before writing anything (planner)

1. **Map the change against real code.** Before drafting the plan, `grep` the
   affected symbols across `www/` AND `etc/` (element ids, CGI field names,
   function names, CSS classes, config keys). List every consumer: the HTML id,
   its JS reader/writer, its CSS hooks, the CGI that feeds it, the device
   script behind the CGI. An under-scoped plan is a review-time finding.
2. **State the surface scope up front.** Frontend-only / CGI / device scripts /
   installer — every plan names which of the four layers the change touches
   and which it deliberately leaves alone. A CGI field rename implies "every
   JS consumer + the device script producing it". Silence on scope is a finding.
3. **Data-chain scope (dashboard/status work).** For any change touching a
   status value, trace and name the WHOLE chain in the plan:
   `device source (/proc, script, daemon) → CGI part (status.cgi?part=X) →
   apply*Status() → DOM id → CSS state`. Verify every stage: units, formats,
   thresholds, i18n. A plan that only touches "render" without confirming
   "produce" and "poll cadence" is under-scoped.
4. **Preserve the output contract by default.** CGI JSON fields, cookie names,
   config-file keys (`/etc/sa02m_*.conf`), and MQTT topics are consumed by
   deployed devices and external clients (SCADA, scripts). A change keeps the
   format unless the plan explicitly proposes a compatible migration with a
   client-impact note. Removing/renaming a JSON field an older frontend may
   still request (cached JS!) is called out explicitly.
5. **Cache-bust awareness.** Any change to a served static asset ships with
   the `?v=` bump (branch version); a plan changing JS/CSS without the version
   flow (`scripts/sync-app-version.py`) is under-scoped — devices will run the
   cached old bundle against a new backend.

## Plan-time audit (planner)

Every non-trivial plan carries these three columns of judgement:

- **Poll-path cost.** Anything on the status-poll hot path (a `status.cgi`
  part, an `apply*` function) is reviewed for added forks, added DOM churn,
  or a new fetch outside the scheduler. State the added cost and why it is
  acceptable on the shared ARM target.
- **Race / re-entry risks.** Async CGI actions (service start/stop, flasher
  jobs, reboots) — state what happens on a double click, a poll landing
  mid-action, an F5 mid-job. Buttons disable during pending ops; reconnect
  paths (flasher sessionStorage pattern) are named.
- **Failure-mode UX.** For every new fetch: what does the widget show on
  timeout / 500 / auth loss? The failure state is designed, not accidental
  (`toast`, badge `badge-unk`, poll-alert bar are the existing vocabulary).

## During-edit (planner + builder)

- Match the touched file's existing idiom (function style, `var` vs `const`,
  comment density, Russian UI strings + English code comments).
- When copying a widget/endpoint as a template: rename every id, i18n key,
  and CGI field that carried the source's meaning; leftover donor ids are a
  classic finding.
- Renaming or moving a file: grep the OLD name across `www/`, `etc/`,
  `install.sh`, nginx config, and docs; update every reference before deleting.

## Refactor discipline (planner + builder)

- **Chesterton's Fence** — before simplifying or removing code, understand why
  it exists (git blame, a `CHANGELOG.md` grep, the comment at the site); a
  guard that "looks redundant" usually pins a past regression.
- **Rule of 500** — a refactor touching more than ~500 lines wants a script or
  codemod (re-runnable, reviewable), not manual edits.

(Both adapted from addyosmani/agent-skills.)

## Post-edit — the verification checklist (reviewer)

Every review confirms:

1. **Quality gates green** — `node .ai-dev/quality/run.mjs build` (js-syntax,
   bash-cgi-syntax, version-consistency) and the review beat.
2. **id/i18n contract** — new ids exist in both HTML and JS; new strings have
   DICT entries (`web-code-rigor.md` floors).
3. **Both themes, both variants** — dark AND light theme render correctly;
   `sa02m-1eth` AND `sa02m-2eth` visibility (`data-hide-for`) is respected.
4. **Headless render check** — for any visible change, the screenshot recipe
   (`web-diagnostic-tools.md ## Headless UI verification`) was run: default
   state + the relevant data-driven states (link/no-link, warn thresholds,
   modem/storage, narrow viewport).
5. **Deployed-cache compatibility** — the change survives an old cached bundle
   against the new backend for one poll cycle (or the `?v=` bump is in place).
6. **Re-render survival** — nothing added inside renderer-owned containers.

## «Значение — / не обновляется» — the standing recipe

When the Operator reports a widget stuck at `—`, zero, or not updating, DO NOT
lead with "please attach logs". For a regression with a known-good version,
`git bisect` across the version branches beats reading diffs; and a NEW
failure appearing mid-task stops the line — halt feature work until it is
understood (adapted from addyosmani/agent-skills). Trace the chain first,
by code:

1. **Device source** — does the script/`/proc` path exist on this HW variant
   and kernel? (RT vs SMP, 1eth vs 2eth differ.)
2. **CGI part** — is the field emitted by `status.cgi?part=X`? Is the part
   enabled in `sa02m_status_blocks.conf`? Is the part paused after failures
   (`statusPauseUntil`)?
3. **Fetch layer** — is the part in `BACKGROUND_STATUS_PARTS` / scheduled?
   Auth still valid?
4. **apply function** — does the field name match exactly? `undefined` guards
   skipping the update?
5. **DOM** — correct id, element not hidden by `data-hide-for`, not
   overwritten by a later renderer pass.

A log/screenshot request comes AFTER this trace, only when the code path
cannot answer decisively.

## Response format after fixes (agent-facing)

- **Do not write** a long "what was wrong / what we did" essay after a fix.
  State minimally: what was fixed, in which files, what was verified (which
  screenshots/states). Then stop. Full reasoning belongs in the commit message
  and the plan's progress note.
- **Visible plan progress (orchestrator).** For any multi-step work: announce
  the plan ONCE up front as a short numbered list (plain language), tag every
  in-progress narration line with its step marker (`[2/5] …`), announce step
  completions and any plan CHANGE before proceeding. The Operator must always
  see what was planned, what is happening, and which step — without scrolling.
- **Consolidated attention block (orchestrator).** Every end-of-work relay
  ENDS with ONE clearly headed block gathering EVERYTHING awaiting the
  Operator: merge words, branch decisions, declined offers, pending on-device
  checks, audit-cadence offers. An empty block is stated explicitly («решений
  от вас не требуется»), never omitted.

## Response scope — "explore all"

When the Operator says "study all X" (all tabs, all CGI, all themes), the
response covers the ACTUAL exhaustive set — not a convenient subset. A partial
answer to a comprehensive request is a review-time finding.

## Repeat-request avoidance (agent-facing)

Recurring reasons the Operator has had to repeat a request:

1. **Missing scope statement** — name frontend/CGI/device/installer scope up
   front, every time.
2. **One-off answers** — a useful script or checklist lands in-tree
   (`scripts/`, `docs/`), not only in chat.
3. **Log-first anti-pattern** — trace code first; request logs/screenshots
   only when the code path is ambiguous.
4. **Single-theme blindness** — a fix verified only in dark theme leaves light
   broken; verify both.
5. **Variant blindness** — SA-02m-2 hides/renames widgets; verify both
   variants when touching the dashboard/network tabs.
6. **Cached-bundle blindness** — a backend change tested only against fresh
   JS breaks devices running the cached bundle; keep contracts compatible or
   bump the version.
