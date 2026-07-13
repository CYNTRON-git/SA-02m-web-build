# Protocol upgrade notes — per-version migrations

The one home of "what to do after the protocol version bumps". The installer lays this file down at `.ai-dev/upgrades.md` on **every** run (always the new version's copy); the session's upgrade check (`src/agents/procedures/upgrade.md`) reads the sections in the `(old, new]` range and executes the applicable steps through the loop. A version with no section here needs nothing beyond the installer re-run itself — the re-run's idempotence and never-clobber guarantees are the whole upgrade (`INSTALL.md` `## Upgrade`).

**Downgrades are unsupported.** A version-pinned re-run (`npx github:wirenboard/ai-pm-protocol#v<tag> <target>`) converges the vendored files back, but no downgrade notes exist — a MAJOR downgrade is the Operator's own risk.

## MINOR 5.60.0 — protocol-reference.md companion doc

Nothing manual beyond the installer re-run. This version moves three reference-only
sections (`## Enforcement`, `## Project config`, `## Core and adapter`) from
`PROTOCOL.md` into a new companion doc `protocol-reference.md`, deployed to
`.ai-dev/protocol-reference.md` (sibling of `.ai-dev/PROTOCOL.md`). The installer
re-run creates the companion; PROTOCOL.md's resident summaries point to it.

A downstream project installed before this version has no `.ai-dev/protocol-reference.md`
— the pointer in `.ai-dev/PROTOCOL.md` will not resolve. Re-run the installer once
to receive the companion:

```sh
node .ai-dev/tooling/src/adapter/install.mjs .
```

No manual config or rename steps required.

## MINOR 5.51.0 — model-routes.json split (shared routes / personal proxyUrl)

After the re-run, if your `.ai-dev/model-routes.json` carries a `proxyUrl` (a personal,
per-machine value), move it by hand into the gitignored `.ai-dev/model-routes.local.json`.

**Common case — the shared file carries ONLY `proxyUrl` (no `routes`):** a plain rename:

```sh
mv .ai-dev/model-routes.json .ai-dev/model-routes.local.json
```

**Mixed case — the shared file carries BOTH `proxyUrl` AND a `routes` array:** split them
by hand — create the local file with just the `proxyUrl`, then remove that key from the
shared file so it keeps only `routes`:

```sh
cat <<'EOF' > .ai-dev/model-routes.local.json
{
  "proxyUrl": "http://127.0.0.1:8787"
}
EOF
# then edit .ai-dev/model-routes.json to remove the "proxyUrl" key, keeping "routes"
```

**Why:** the shared `.ai-dev/model-routes.json` is team-committed and must never carry
machine-specific values that break routing for teammates on pull. The new personal sibling
`.ai-dev/model-routes.local.json` (gitignored, like `config.local.json`) is the correct home
for `proxyUrl`. The launcher now merges both files, so a personal `proxyUrl` in the local file
is discovered and used just as it was in the shared file before — but now teammates pulling
the repo get only the safe, team-shareable routes table (docs/decisions/personal-multi-model-setup.md).

## MINOR 5.48.0 — the `roles.planner` seat (Researcher-Planner)

Nothing manual. This version adds a fourth role, the profile-staffed **Researcher-Planner**, as a new spawnable seat (`roles.planner`). A config that predates it lacks the seat; the installer **auto-backfills** it with the default `{ "agent": "dev-planner" }` binding on the next run — additively, never overwriting any existing seat's `agent`/`model` or any other value. The re-run then bakes the new `dev-planner` agent on both platforms. No action required. (The planner is a strong-model *plan* seat; its model benefit is Claude-only and off by default on the `solo` profile — `docs/decisions/planning-model-split.md`.)

## MINOR 5.10.0 — version stamp + upgrade channel introduced

Nothing manual. The re-run lays down `.ai-dev/VERSION` (the installed protocol version), this notes file, and a minimal breadcrumb load-surface for the inactive platform. An upgrade from any pre-stamp version is detected as `pre-5.10`; walk every section below that applies to your actual starting version.

## MINOR 5.8.0 — config moved into .ai-dev/

From 5.8.0 onward the installer writes the project config at `.ai-dev/config.json` (inside the `.ai-dev/` directory). The old location `ai-dev.config.json` at the project root is no longer read. After the re-run, move the file by hand: `mv ai-dev.config.json .ai-dev/config.json`. A re-run of the installer will create a minimal default at the new location if the file is absent, but the old file at the project root is NOT read automatically and must be moved (or removed) manually to avoid confusion.

## MAJOR 5.0.0 — ai-pm → ai-dev rename

After the re-run, rename these in your project by hand (the installer cannot rename files it did not create):

1. **Config:** rename `ai-pm.config.json` → `ai-dev.config.json`. Update its `"platform"` and `"roles"` agent IDs: `"ai-pm"` → `"ai-dev"`, `"pm-builder"` → `"dev-builder"`, `"pm-reviewer"` → `"dev-reviewer"`.
2. **State directory:** rename `.ai-pm/` → `.ai-dev/` (all subdirs: `state/`, `plans/`, `reviews/`, `audit/`, `8d/`, `backlog.md`; the `tooling/` submodule is re-vendored by the re-run, so delete the old `.ai-pm/tooling/` after renaming).
3. **Commands:** rename `.claude/commands/pm-setup.md` → `dev-setup.md`; `.opencode/commands/pm-setup.md` → `dev-setup.md`.
4. **Agents:** rename `.claude/agents/pm-builder.md` → `dev-builder.md`, `pm-reviewer.md` → `dev-reviewer.md`; `.opencode/agents/ai-pm.md` → `ai-dev.md`, `pm-builder.md` → `dev-builder.md`, `pm-reviewer.md` → `dev-reviewer.md`.
5. **Plugin:** rename `.opencode/plugins/ai-pm.mjs` → `ai-dev.mjs`.
6. **Hook paths** in `.claude/settings.json`: replace any `/.ai-pm/tooling/` path references with `/.ai-dev/tooling/` (the hook re-runs the shim — if you wired it manually, update the command path; a 5.10+ installer re-run replaces the stale group automatically).
7. **CLAUDE.md / AGENTS.md**: no changes needed (they import by protocol file names, not agent IDs).
8. **Re-run** `node .ai-dev/tooling/src/adapter/install.mjs . --platform <your-platform>` one more time to regenerate assembled agents with the new IDs.
9. **Search your codebase** for any remaining `/pm-setup`, `.ai-pm`, `ai-pm.config`, `pm-builder`, `pm-reviewer` references and update them.

## Old-protocol migration (pre-5.0)

A downstream running a **prior protocol version** (pre-5.0, when the docs were `WORKFLOW.md`, agent roster `pm-*`, state dir `.ai-pm/`) migrates mechanically then runs doc bootstrap in old-protocol source mode (`src/agents/procedures/doc-bootstrap.md`).

**Mechanical steps:**

1. **Bump and re-run** — bump the tooling to the new version and re-run `node .ai-dev/tooling/src/adapter/install.mjs . --platform <platform>`. The installer vendors the new adapter and lays down the new template docs (only where absent — it never clobbers existing content).
2. **Rename the old surface** — follow MAJOR 5.0.0 steps above for any `ai-pm.config.json`, `.ai-pm/` dirs, `pm-*` agent files, and command files not already renamed.
3. **Run doc bootstrap (old-protocol source mode)** — the Builder reads the old docs (`WORKFLOW.md`, prior role files, the legacy agent roster) as primary source and compresses their truth into the new templates (`docs/architecture.md`, `docs/contracts.md`). Any old-doc claim contradicting the code surfaces as a finding for the Operator. After drafting, a comment de-water pass removes wall comments that duplicate the new docs.
4. **Delete old docs** — once their content moved, delete `WORKFLOW.md` and any pm-* role files (supersede, one home — invariant 6).
5. **Accept the closing audit** — the orchestrator offers a whole-project sweep after close; take it to catch any drift the per-diff bootstrap missed.
