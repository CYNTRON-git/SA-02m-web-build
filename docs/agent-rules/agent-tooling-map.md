# Agent tooling map — what to run, for what, and the traps

Project-local quick map for every agent (orchestrator, planner, builder,
reviewer): which tool answers which need, one line each, with the traps that
have already cost time. POINTERS ONLY — each tool's full doc stays in its one
home (cited); this file exists so an agent finds the right tool without a
tree-wide search. Machine-facing: English (PROTOCOL.md invariant 5). Loaded
via `CLAUDE.md` `@`-import.

## Quality gates

| Need | Command | Trap / note |
|---|---|---|
| Builder green check | `node .ai-dev/quality/run.mjs build --touched` | full set at ship (no `--touched`) |
| Reviewer beat | `node .ai-dev/quality/run.mjs review --touched` | — |
| Registry | `.ai-dev/quality/tools.json` | add a tool = add a row, never edit the runner |

## Build / release system

| Need | Command | Trap / note |
|---|---|---|
| There is NO build step | — | frontend is served as-is; "build" = quality gates green |
| Version bump | branch `X.Y.Z.W` + `python3 scripts/sync-app-version.py` | every version home must agree (the list has one home: `sa02m-domain.md ## Version discipline`) — the script is the only writer; never hand-edit `?v=` strings one by one |
| New release | new branch `+1` from the latest version branch; CHANGELOG section | branch name IS the version (`sa02m-domain.md ## Version discipline`) |
| Render/verify UI | headless recipe | `web-diagnostic-tools.md ## Headless UI verification` — the one home |

## Git / forge

| Need | Command | Trap / note |
|---|---|---|
| Push | `git push -u origin <branch>` | version branches only; retry with backoff on network errors |
| PR / issues | GitHub MCP tools (`mcp__github__*`) | `gh` CLI is NOT available in the remote env |
| Ops on the sibling MR-02m clone | ALWAYS `git -C /workspace/mr-02m …` | a leftover `cd` re-anchors every later tool call |
| Commit format | `docs/agent-rules/git-commits.md` | stage named paths only, never `git add -A` |

## Device / bench

Full catalog with the symptom→tool dispatch table:
`docs/agent-rules/web-diagnostic-tools.md` — read it BEFORE writing any ad-hoc
verification or poll script. Highlights:

| Need | Tool |
|---|---|
| Verify a UI change without a device | headless recipe (python http.server + Playwright + CGI stubs) |
| Contrast check for new colors | WCAG script pattern (`## Contrast`) |
| Probe a live device's status | `curl …/cgi-bin/status.cgi?part=…` (auth cookie first) |
| Deploy www to a device | `scripts/update-www-only.sh` per its header / `docs/` runbook |

Traps: one JS syntax error bricks the entire page (no bundler isolation);
renderer-owned DOM containers wipe hand-inserted nodes on the next poll;
`?v=` cache-bust skew makes devices run stale bundles against a new backend.

## Where knowledge lives (before searching the tree)

| Question | One home |
|---|---|
| Which doc answers a system question at all | `docs/architecture.md` — a pointer stub by design (the map), not a full architecture doc |
| Stack, tabs, polling architecture, port lease | `docs/agent-rules/sa02m-domain.md` |
| CGI/JS/CSS floors, contrast, i18n rules | `docs/agent-rules/web-code-rigor.md` |
| Pre/post-edit discipline, «—» trace recipe | `docs/agent-rules/web-workflow.md` |
| MPLC4 runtime API, license reads, the FastCGI-not-HTTP trap | `docs/agent-rules/mplc4-api.md` |
| Regression history ("when did X break") | `CHANGELOG.md` — grep it FIRST |
| Install/HW variants (Operator-facing) | `README.md` |
| MR-02m module internals (Modbus map, .fw) | the sibling repo `CYNTRON-git/MR-02m`, its `docs/agent-rules/` |
| Skills (deep dives) | `.claude/skills/` — sa02m-web-architecture, sa02m-versioning-release, sa02m-ui-style, sa02m-web-testing |
| How a skill is written (triggers, budget) | `docs/agent-rules/skill-authoring.md` |
