# The adapter layer

The protocol is one neutral core (`../../PROTOCOL.md`, `../agents/`) plus one thin adapter per platform. This directory is where a platform plugs in. The contract is fixed and small (`protocol-reference.md ## Core and adapter`); an adapter realises it and nothing more.

## How it's shaped

```text
deny-rules.json   the registry — every guard, as data (intent + class + predicate + params)
tool-map.json     neutral noun → per-platform concrete tool; which return-classes a platform supports
engine.mjs        the shared check engine — holds the PREDICATES; one copy, every platform
claude/shim.mjs   Claude shim: stdin hook payload → engine → verdict JSON on stdout
opencode/         OpenCode shim: normalise.mjs (pure normalise + decide) + plugin.mjs (the
                  single-export entry — async actor lookup, throw-to-deny)
```

The split that makes this work: **rules are data, the check is code.** A deny rule's *intent, class, and parameters* (the role-deny list, the change-verb pattern, the orchestrator-writable prefixes) live in `deny-rules.json`. The *predicate* that decides — "does this path resolve outside the root", "is this an empty write over a non-empty file" — is a function in `engine.mjs`, shared by every platform. So there is exactly one copy of each rule and one copy of each check.

## What a platform adapter does

A platform shim is the only platform-specific code. It:

1. **normalises input** — turns the harness's hook/plugin payload into the neutral shape `{act, path, command, content, spawnTarget, actor, prompt}`;
2. **calls the engine** — `evaluate(input, denyRules)` returns `allow | deny | ask | inject` with a reason;
3. **maps the verdict** to the harness's own mechanism — a Claude `PreToolUse` hook emits the deny/ask JSON; an OpenCode `tool.execute.before` plugin throws on deny.

A class the platform can't realise (OpenCode has no `ask`) **falls back to persona** for those rules — named in each rule's `fallback`, and the honest enforcement map in `PROTOCOL.md` already labels them. Nothing is silently dropped.

## Adding a new platform

Write `<platform>/` only: the input-normaliser, the three-class verdict mapper, and the install glue. Map its tools in `tool-map.json` and its `class_support`. **Zero edits to `engine.mjs`, `deny-rules.json`, or the core.** If a new platform forces an edit to any of those, the boundary leaked — that is the design's one failure condition.

## No regex drift — by construction

The danger: a guard's pattern (the change-verb regex, the ssh idioms, the role-deny list) drifts between the Claude realisation and the OpenCode one, so the *same* command is blocked on one platform and waved through on the other.

Here there is **no second copy to drift**: the patterns live once in `deny-rules.json`, and both shims `import` the same `engine.mjs`, which applies them with the same regex engine at runtime. The residual risk — a shim re-implementing a predicate instead of calling the engine — is held by two mechanical guards in `parity.test.mjs`:

- **Parity** — a shared fixture of `{input → expected verdict}` cases runs through each shim's full path (normalise → engine → verdict); both must return the identical class for every case, bar the one documented actor divergence (orchestrator-content: mechanical on OpenCode, persona on Claude).
- **Single-engine** — a shim must contain no rule logic of its own (only input-normalising + verdict-mapping); the test greps each shim for inline patterns/role-lists and fails if a rule leaked out of the registry or the engine.

Install glue is in `INSTALL.md` (where each file lands, the Claude hook fragment, the OpenCode plugin entry). The OpenCode entry defines its hook functions inline and imports only the rule logic — a hook imported and re-exported is not registered by the loader. Per-class support is in `tool-map.json`.

## Model router — per-seat cross-endpoint routing

A second, independent adapter-layer tool (`model-router.mjs`), unrelated to the deny-engine above. It lets the loop's seats run on models behind **different endpoints** — Opus/Sonnet on Anthropic, the Builder on DeepSeek's Anthropic-compatible endpoint — under **one** Claude Code instance, which itself takes a single `ANTHROPIC_BASE_URL`. The *why*, the alternatives, and the empirical routing probe live in `docs/decisions/per-seat-model-routing.md` — read it; this section is the HOW-TO only.

It is a reverse-proxy: it reads each request body's `model`, matches it against a config route table, swaps in that backend's auth header (key from an env var, never inline, never logged), and streams the response back — with one reactive exception, the vision fallback below. An unroutable request fails closed (a 4xx/5xx error, never a silent default backend) — sending a seat's traffic to the wrong provider is the worst outcome it must prevent.

**Run it:**

```sh
cp src/adapter/model-router.example.json router.json   # then edit the routes
export ANTHROPIC_API_KEY=sk-ant-…                       # the keys the routes name
export DEEPSEEK_API_KEY=sk-…
node src/adapter/model-router.mjs router.json           # listens on 127.0.0.1:8787
```

Then point Claude Code at it: `export ANTHROPIC_BASE_URL=http://127.0.0.1:8787`. Set `MODEL_ROUTER_LOG=1` for an opt-in `model -> host` line per request on stderr (never a key, body, or header).

**The route config** (`model-router.example.json`) is a list of `{ match, base_url, auth: { header, keyEnv } }`: `match` is a glob over the model id (`claude-*`, `deepseek-*`); `base_url` is the backend origin (with an optional base path the request path is appended to); `auth.keyEnv` names the env var holding that backend's key — **never a key value in the file** (it is committed). An optional `auth.scheme` (e.g. `"Bearer"`) prepends a prefix to the key for backends that want `Authorization: Bearer <key>` rather than a raw `x-api-key`. Verified per the official DeepSeek docs (2026-06-30): its Anthropic-compatible endpoint is `https://api.deepseek.com/anthropic` and authenticates with `x-api-key`, the same scheme as Anthropic.

**Vision fallback (`forImages: true`).** One route may carry `forImages: true` to mark it the **vision fallback target**. When a non-vision backend rejects an image-bearing request with a `400 … does not support image blocks`, the router buffers that `400`, re-forwards the **same** request to the `forImages` route, and caches the model as non-vision so the next image call pre-routes there directly — the client sees the vision route's response. Capability is **discovered from the wire, not declared**: a multimodal model just returns `200` and is never rerouted; an ambiguous `400` (a real bad request) relays as-is; and with no `forImages` route configured the router fails loud with a clear error rather than the cryptic upstream `400`. It is **reroute only — the payload (image bytes included) is never transformed**. At most one route may set `forImages`. The why and the full case table: `docs/decisions/vision-routing.md`.

**Load-bearing constraint — `CLAUDE_CODE_SUBAGENT_MODEL` must stay UNSET.** It has the highest precedence in Claude Code's per-subagent model resolution and **collapses every seat to one model** when set — which defeats the router (every request would arrive carrying the same model id, so the route table can no longer tell the Builder seat from the Reviewer seat). The per-seat `model:` pins are the routing key; this env var overrides them. See `docs/decisions/per-seat-model-routing.md` (fact 2) for the empirical proof.

### The provider catalog — `model-providers.json`

A built-in, data-only map of the known Anthropic-format backends, so a routes config can name a provider by `id` instead of restating its endpoint. Each entry carries `id`, `base_url` (the origin **plus the path prefix before `/v1/messages`** — the router appends the client's request path), the `models` globs that select it, and `auth { header, scheme?, keyEnv }` (key by env-var **name** only). The four verified providers (2026-06-30): **anthropic** (`x-api-key`, also `supports_passthrough`), **deepseek** (`x-api-key`), **glm** (z.ai GLM Coding Plan — `Authorization: Bearer`, models `GLM-*`), **openrouter** (`Authorization: Bearer`). **Governing constraint: Anthropic-format only, no translator ever** — OpenAI-shaped providers are out of scope permanently (`docs/decisions/per-seat-model-routing.md`).

A route in a routes config references one: `{ "provider": "deepseek" }` (base_url + auth + match patterns pulled from the catalog), with any field overridable inline — e.g. `{ "provider": "anthropic", "auth": "passthrough" }` to forward a subscription/OAuth session's own auth (no API key), or a `base_url` override for a regional GLM host.

### The launcher — `.ai-dev/launch` (and the engine `router-launch.mjs`)

**`.ai-dev/launch` is the convenient entry — OPTIONAL, generated always.** The installer
writes a short, root-relative drop-in for `claude` at `.ai-dev/launch` (a drop-in for
`claude`, run from the project root) that wraps the engine below. You only NEED it for
multi-model + proxy, or a per-project claude profile (`launch.configDir`) — a plain
single-model Claude project can keep launching `claude` (or its own wrapper) directly; the
launcher then just execs claude straight through. It is generated even on a vanilla project
so enabling routing or a `configDir` later needs no re-install. On **OpenCode** it is an
honest stub: routing is Claude-Code only (the provider-catalog `opencode._note`), so it
prints that and execs `opencode` (no proxy). The launcher framing + the modes × launch
matrix live in the top `README.md` `## Multi-model routing`; the why is
`docs/decisions/launcher-ux.md`.

```sh
./.ai-dev/launch [claude args…]   # normal launch (direct, or through the proxy)
./.ai-dev/launch --proxy          # foreground proxy only — prints the URL, does NOT exec claude
```

The engine it wraps, also runnable directly, is **direct by default**:

```sh
node src/adapter/router-launch.mjs [--proxy] [claude args…]
#   --proxy              foreground-only: start the router, print its URL, do NOT exec claude
#   AI_DEV_CONFIG        config path   (default .ai-dev/config.json; its .local.json
#                        sibling is the gitignored personal override)
#   MODEL_ROUTER_ROUTES  routes path   (default .ai-dev/model-routes.json; its .local.json
#                        sibling is the gitignored personal override and the home of proxyUrl)
```

**Personal overrides — `.ai-dev/config.local.json` (gitignored).** The launcher merges the
`launch` section of `.ai-dev/config.local.json` OVER the shared `.ai-dev/config.json`'s
`launch`, so per-machine values (a `configDir`, a personal launch model) never land in the
shared, committed file or get forced on a teammate. The installer ensures it is gitignored.

**Personal routes overrides — `.ai-dev/model-routes.local.json` (gitignored).** The launcher
merges the gitignored `.ai-dev/model-routes.local.json` **over** the shared
`.ai-dev/model-routes.json`, so per-machine values (a `proxyUrl`, personal route entries)
never land in the shared, committed file or break a teammate's routing on pull. The shared file
carries only the team-shareable routes table; the personal sibling carries the machine-specific
`proxyUrl` (a loopback or personal external proxy) and any personal route overrides. The installer
ensures it is gitignored. The merge follows the same pattern as `config.local.json` — scalar
fields (like `proxyUrl`) are shallow overrides (local wins, shared is kept); `routes` is the
one array field, and local routes are concatenated **before** shared ones (because the router's
matching is first-match-wins, so a personal route must sort first to override a shared glob).

**Per-project claude profile — `launch.configDir` → `CLAUDE_CONFIG_DIR`.** When set, the
launcher exports `CLAUDE_CONFIG_DIR` to it before exec'ing claude — a per-project claude
profile/keys dir with **no `.bashrc` edit**, the per-task-keys mechanism a global wrapper
used to own. It is exported in **every** mode (direct / external / router), so it is useful
**without routing too** (just pin a project's claude profile). Home it in
`config.local.json` (it is a per-machine path).

**Foreground proxy — `--proxy`.** Starts the router, prints its URL on stdout, and stays up
WITHOUT exec'ing claude — for "don't touch my launch": point your own claude/wrapper at the
printed URL (or wire it as the routes config `proxyUrl`). Errors when there is no LOCAL
router to start (an external `proxyUrl` already runs, or fewer than 2 endpoints are in play).

**Probe — `--probe [url]`.** Best-effort discovery of an **already-running** proxy for the
setup dialog. Tries candidate origins — an explicit `url` arg, the **merged** routes-config
`proxyUrl` (shared + personal), then the conventional localhost default `http://127.0.0.1:8787`
— at `GET /v1/models` (then `/models`), and prints ONE JSON line `{ alive, url, models }` to
stdout, exiting `0` (alive) / `1` (none). Lets the dialog **skip asking for a URL** when a
proxy is up. Never throws — a refused/timed-out candidate is simply "not alive". Note: a
launcher-**spawned** proxy needs no probe (the launcher holds the routes config and uses a
random free port) — the probe is for an **external** proxy the Operator already runs (a
standalone modelpipe, or a LiteLLM-class proxy that exposes `/v1/models`). The built-in
router (`model-router.mjs`) is a pure passthrough proxy; it answers `/v1/models` only once
the upstream gains that endpoint, so against an older upstream the probe finds nothing and
the dialog falls through to the spawn fork — honest, no false promise. The candidate-list +
response parse are
pure (`probeCandidates` / `parseModelsResponse`, unit-tested); the live HTTP is the one
untestable rung.

It reads the seats' `roles.*.model` pins and the routes config (resolved against the catalog), then decides: if the seats touch **fewer than 2 distinct endpoints** (e.g. all-Anthropic), it execs `claude` **directly, no router** — a project that never opts in is unchanged. If **≥2 endpoints** are in play, it starts `model-router.mjs` on a free localhost port, points the child at it (`ANTHROPIC_BASE_URL`), **guarantees `CLAUDE_CODE_SUBAGENT_MODEL` is unset** in the child env, execs `claude`, and tears the router down on exit. **Fail-closed:** if a route in play names a backend key env var that is unset, the launcher errors **before launching anything** — never a silent wrong-backend.

**External proxy (opt-in `proxyUrl`).** Set a top-level `proxyUrl` in the routes config to point at an **already-running** proxy (a self-hosted or shared modelpipe, or one under a debugger) instead of spawning one. The launcher then skips the spawn entirely and just points `claude` at that URL (still unsetting `CLAUDE_CODE_SUBAGENT_MODEL`). Auth/keys live in **that proxy's own env**, so the launcher's fail-closed key check does not apply — it cannot see the external proxy's credentials. A present-but-malformed `proxyUrl` is a hard error (never a silent fall-through to a local spawn); absent/blank ⇒ the launcher decides direct-vs-spawn itself. **Home `proxyUrl` in the gitignored personal `.ai-dev/model-routes.local.json`, never in the shared `.ai-dev/model-routes.json`** — it is a per-machine pointer that breaks routing for teammates who pull it.

**Env precedence.** The launcher layers ONLY `ANTHROPIC_BASE_URL` (to the proxy, **when routing is on**) + unsets `CLAUDE_CODE_SUBAGENT_MODEL` + the launch-time env (`ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` / `CLAUDE_CONFIG_DIR`, each only when its config field is non-empty); **everything else in the environment passes through untouched**. When routing is on and the environment already carries a *different* `ANTHROPIC_BASE_URL` (e.g. a personal wrapper pointed at another proxy), the **launcher wins** — but loudly: a visible stderr WARNING names both URLs, so a silent hijack of the user's own proxy can never happen. Routing off ⇒ a preset `ANTHROPIC_BASE_URL` passes through unchanged, no warning.

**Launch-time models (session + guard) — config is the source, every launch path consumes it.** Two seats are NOT baked into an assembled agent and so cannot be a `roles.*.model` pin: the **session** model (the orchestrator IS the running session — its model is whatever `claude` launches under) and the **guard** model (the background/small-fast model). Claude Code reads both from env *at process launch*, so they must be set **before `claude` starts**. The one home for their values is the config `launch` section (the RATIFIED option (c) source-of-truth — `docs/decisions/multi-model-setup-ux.md` `## The fork`):

```json
{ "launch": {
    "sessionModel": "claude-opus-4-8",
    "guardModel": "claude-haiku-4-6",
    "aliases": { "fable": "claude-fable-5", "opus": "claude-opus-4-8", "sonnet": "glm-5.2", "haiku": "deepseek-v4-pro" }
} }
```

**Tier-alias bindings — `launch.aliases.{fable,opus,sonnet,haiku}`.** The cross-endpoint lever. Claude Code resolves a tier (`fable`/`opus`/`sonnet`/`haiku`, strongest→weakest) through `ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL`; `launch.aliases` is the config home for those, so you **bind a Claude tier to a foreign model id** (e.g. `sonnet → glm-5.2`) and any role using that tier routes to it. This is the recommended cross-endpoint path — a role carries a *tier*, not a foreign id (`## Setup` Phase 1/2). Same launch-env class as the two models above (startup-read, restart-applied); per-tier empty/absent ⇒ that var is not set. Because the bindings are proxy-specific, they may live in the gitignored `.ai-dev/config.local.json` `launch.aliases` — `mergeLocalLaunch` deep-merges the `aliases` object **per tier**, so a local override of one tier keeps the shared others.

There are **two consumers of this source**, both wrapper-less by default:

- **The installer writes the startup env.** On Claude the install path (`install-claude.mjs`) writes `sessionModel` → `ANTHROPIC_MODEL`, `guardModel` → `ANTHROPIC_SMALL_FAST_MODEL`, and `aliases.{fable,opus,sonnet,haiku}` → `ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL` into `.claude/settings.json` `env`, which Claude Code reads **at process startup** — so a routed project gets the launch-time models applied with **no personal export wrapper** (the proxy *process* is still started separately). It merges **only those keys**, never clobbering a foreign `env` key; an empty/absent value writes nothing and prunes any key it previously set, so a non-routing project's `settings.json` is byte-unchanged. **`settings.json` `env` is read only at startup**, so changing a launch-time value (re-running the installer with a new `launch`) takes effect **on the next session — restart the running session for it to apply** (the setup dialog and the model-switch handler announce this; `src/agents/orchestrator.md` `## Setup`).
- **`router-launch.mjs` exports the same source.** When launched through the protocol launcher, it reads the `launch` section and exports `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` / `ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL` into the child env in **every** mode (direct, external, router) before exec'ing `claude` — absent/empty values export nothing, byte-unchanged for a non-routing project.

**Guard knob — `ANTHROPIC_SMALL_FAST_MODEL` (G1, deprecated-but-kept).** The guard is the harness's background/small-fast model. The modern path folds the background model into the haiku slot (`ANTHROPIC_DEFAULT_HAIKU_MODEL`), which in a typical routed setup is *also* the builder seat — so the only way to set the background model **independently** of the haiku slot is the deprecated `ANTHROPIC_SMALL_FAST_MODEL` (still honoured by Claude Code today). G1 keeps the separate guard seat mapped to it for that independence; watch for its removal in a future Claude Code release (tracked in the backlog) — on removal, the independent guard knob is gone and background folds into the haiku slot.

**A personal launch wrapper** (used when `ANTHROPIC_BASE_URL` must also be set pre-launch and the user prefers their own launcher) reads the **same** config values — the installer's `settings.json` `env` and the launcher are not the only consumers. Mirror them with the ready export block setup prints:

```sh
# wrapper recipe — read the launch-time models from the config, then exec claude
export ANTHROPIC_MODEL=$(node -e 'process.stdout.write((JSON.parse(require("fs").readFileSync(".ai-dev/config.json","utf8")).launch||{}).sessionModel||"")')
export ANTHROPIC_SMALL_FAST_MODEL=$(node -e 'process.stdout.write((JSON.parse(require("fs").readFileSync(".ai-dev/config.json","utf8")).launch||{}).guardModel||"")')
export ANTHROPIC_BASE_URL=http://127.0.0.1:8800   # → your running proxy
claude "$@"
```

**Configured from the session, applied on restart (the honest framing).** The *configuration* is fully writable from inside a running session — the installer/dialog writes `settings.json` `env`, `config.json`, and `model-routes.json` like any other file. What a running session **cannot** do is rebind **its own** process's env: Claude Code reads `ANTHROPIC_BASE_URL` and the launch-time models at process startup, and a `SessionStart` hook fires after the API client has already bound, so there is **no per-session pre-launch hook** that re-points the *current* process. The accurate statement is therefore *"set it from the session → restart to apply it"*, never *"impossible from the session"*. The two declarative startup homes carry the values across the restart: **`settings.json` `env`** (installer-written, wrapper-less, for the launch-time models) and, where the user needs `ANTHROPIC_BASE_URL` set too, **their own launch wrapper** (or `./.ai-dev/launch`) reading the same config source. Either way a launch-time change takes effect on the **next session** (`docs/decisions/multi-model-setup-ux.md` `## Requirement 7`).

**Setup-dialog model discovery — probe-first.** The dialog discovers model ids two ways, in order. **First** it **probes for an already-running proxy** (the common "my proxy is up" case):

```sh
./.ai-dev/launch --probe              # → { "alive": true, "url": "...", "models": [...] }  (exit 0/1)
```

A live proxy's `/v1/models` ids are offered per seat and **the URL question is skipped**. **Second**, where no proxy answers but a routes **config** is available, it MAY list the config's configured ids instead of asking blind:

```sh
modelpipe <routes-config> --list          # or: node <path-to>/modelpipe.mjs <routes-config> --list
```

Both fall back to plainly asking when neither surface is available (no hard dependency — `docs/decisions/multi-model-setup-ux.md` papercut 8). Each exposes model ids + capabilities + host only — **never key values or auth secrets**.

**What a model string does end to end — write a concrete id, or an alias?** The string a seat carries travels this chain, so a route's `match` glob must target what actually arrives:

```text
config pin (roles.{builder,reviewer}.model)  or  launch env (ANTHROPIC_MODEL / ANTHROPIC_SMALL_FAST_MODEL)
        │
        ▼  Claude Code alias resolution
   an alias ("sonnet"/"haiku"/"opus")  →  ANTHROPIC_DEFAULT_{SONNET,HAIKU,OPUS}_MODEL  (if set)
   a concrete id ("deepseek-v4-pro")   →  passthrough, used verbatim
        │
        ▼  the string lands in the request body's `model` field
        │
        ▼  the built-in router (model-router.mjs pickRoute)
   matched against each route's `match` glob, LITERAL FIRST-MATCH — the router does NOT
   alias and does NOT translate; it routes by body.model ALONE and forwards.
```

So there are two ways to point a seat at a foreign model, and the chain shows why **tier binding is the recommended one**:

- **Tier binding (recommended — and, for a baked seat on Claude, the ONLY path that routes).** A role carries a **tier** (`sonnet`), and `launch.aliases.sonnet` sets `ANTHROPIC_DEFAULT_SONNET_MODEL=glm-5.2`. Claude Code resolves the tier → `glm-5.2` → the router routes `glm-*`. This is the one lever that also moves **subagents and the background model** (they pick tiers), and it is what `## Setup` Phase 1/2 drives. One binding, every tier-user follows.
- **Concrete id — launch-env ONLY, never a baked seat on Claude.** A concrete foreign id works for the **launch-env** seats — the session (`ANTHROPIC_MODEL`) and guard (`ANTHROPIC_SMALL_FAST_MODEL`): those are not baked subagent lines, so the id passes through verbatim (route matches `deepseek-*`). But a concrete foreign id written straight into a **baked** seat (`roles.{builder,reviewer}.model`) does **NOT** route on Claude: it is off the Claude allow-list, so the installer bakes **no** `model:` line and the seat silently inherits the session model (`resolveModelPin` in `claude/install-agents.mjs`). So for a cross-endpoint builder/reviewer, use tier binding — never a raw foreign id in the seat.

Either way the router matches the exact string in `body.model` — alias resolution is Claude Code's job, upstream, never the proxy's.
