// Launcher for the per-seat cross-endpoint model router — one command to run the
// wired pipeline (rationale + the whole feature: docs/decisions/per-seat-model-routing.md).
//
// WHAT IT DOES
//   Reads .ai-dev/config.json (the seats' roles.*.model pins) + a routes config
//   (each route either fully specified, or { provider: <id> } resolved against the
//   built-in catalog src/adapter/model-providers.json), then decides:
//     • EXTERNAL — if the routes config sets a non-empty `proxyUrl` (an
//       already-running proxy — a self-hosted/shared modelpipe, or one under a
//       debugger), DON'T spawn a router: point claude straight at that URL. Auth/keys
//       live in the external proxy's OWN env, so the launcher's fail-closed key check
//       does not apply (it cannot see the proxy's credentials). Takes precedence over
//       the endpoint-count decision below.
//     • DIRECT BY DEFAULT — if the seats' models touch fewer than 2 distinct
//       endpoints (e.g. all Anthropic), exec `claude` straight through with NO
//       router. A project that never opts into cross-endpoint routing is unchanged.
//   In EVERY mode it also layers the config's launch-time env onto the child
//   (config.launch.sessionModel → ANTHROPIC_MODEL, guardModel → ANTHROPIC_SMALL_FAST_MODEL,
//   configDir → CLAUDE_CONFIG_DIR, autoCompactWindow → CLAUDE_CODE_AUTO_COMPACT_WINDOW) BEFORE
//   exec'ing `claude` — the RATIFIED source-of-truth:
//   the config is the one home, the launch path consumes it (docs/decisions/multi-model-setup-ux.md
//   `## The fork`; configDir: docs/decisions/launcher-ux.md). Absent/empty values export
//   nothing (a non-routing project stays byte-unchanged).
//     • ROUTER — if ≥2 distinct endpoints are in play, start model-router.mjs on a
//       free localhost port, point the child at it via ANTHROPIC_BASE_URL, ensure
//       CLAUDE_CODE_SUBAGENT_MODEL is UNSET in the child env (it would collapse every
//       seat to one model and defeat the route key — decision doc, fact 2), exec
//       `claude`, and tear the router down on exit.
//
// PERSONAL OVERRIDES: the gitignored .ai-dev/config.local.json carries the per-machine
//   `launch.*` values (a configDir, a personal launch model) — its `launch` section is
//   merged OVER the shared config's, so the shared file stays clean and nothing personal
//   is forced on a teammate. The same split applies to `.ai-dev/model-routes.json` (shared,
//   committed routes table) and its personal sibling `.ai-dev/model-routes.local.json`
//   (gitignored, carries the per-machine `proxyUrl`).
//
// FOREGROUND PROXY (--proxy): start the router, print its URL on stdout, and stay up
//   WITHOUT exec'ing claude — for "don't touch my launch": point your own claude/wrapper
//   at the printed URL. Errors if there is no local router to start (external/direct plan).
//
// PROBE (--probe [url]): best-effort discovery of an ALREADY-RUNNING proxy for the setup
//   dialog. Tries candidate origins (an explicit url arg, the routes-config proxyUrl, the
//   conventional localhost default 8787) at /v1/models, prints ONE JSON line
//   { alive, url, models } to stdout, exits 0 (alive) / 1 (none). Lets the dialog skip
//   asking for a URL when a proxy is up. A launcher-SPAWNED proxy needs no probe (we hold
//   its routes config + use a random free port); this is purely for an external proxy.
//
// FAIL-CLOSED: if a route the launch would use names a key env var that is unset, the
//   launcher errors BEFORE starting anything — never a silent wrong-backend (a seat's
//   traffic reaching the wrong provider, or a request forwarded with no credential).
//
// TESTABILITY: the decisions are PURE exported functions (resolveRoutes,
//   distinctEndpoints, missingKeyEnvs, seatModels, mergeLocalLaunch, launchModelEnv,
//   buildChildEnv, planLaunch, probeCandidates, parseModelsResponse) unit-tested in
//   router-launch.test.mjs. The real `claude` exec and the live probe HTTP are the two
//   untestable rungs — offered as a real-layer check, not run in the suite.
//
// Run:  node src/adapter/router-launch.mjs [--proxy | --probe [url]] [claude args…]
//   --proxy             foreground-only: start the router, print its URL, do NOT exec claude
//   --probe [url]       discovery-only: print { alive, url, models } JSON for a running
//                       proxy, then exit (0 alive / 1 none); never execs claude
//   AI_DEV_CONFIG       override the config path   (default .ai-dev/config.json;
//                       its .local.json sibling is the gitignored personal override)
//   MODEL_ROUTER_ROUTES override the routes path   (default .ai-dev/model-routes.json;
//                       its .local.json sibling is the gitignored personal routes override
//                       and the home of the per-machine `proxyUrl`)
//   The routes config may carry a top-level `proxyUrl` to opt into EXTERNAL mode above.

import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createRouter, pickRoute } from "./model-router.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROVIDERS_PATH = path.join(HERE, "model-providers.json");

// ── pure decision functions (unit-tested) ───────────────────────────────────

// Load + parse the built-in provider catalog. Returns the providers array.
export function loadProviders(providersPath = PROVIDERS_PATH) {
  const parsed = JSON.parse(fs.readFileSync(providersPath, "utf8"));
  if (!Array.isArray(parsed.providers)) throw new Error("model-providers.json: missing providers[]");
  return parsed.providers;
}

// Resolve ONE routes-config entry into one or more concrete router routes
// ({ match, base_url, auth }). A { provider: <id> } entry pulls base_url + auth from
// the catalog (each overridable inline); its match patterns default to the provider's
// `models` globs (so one provider entry can fan out to several routes). A fully
// specified entry (no `provider`) passes through unchanged.
export function resolveRoute(route, providers) {
  if (!route || typeof route !== "object") throw new Error("route: not an object");
  if (!route.provider) return [route];

  const provider = providers.find((p) => p.id === route.provider);
  if (!provider) throw new Error(`route references unknown provider "${route.provider}"`);

  const base_url = route.base_url ?? provider.base_url;
  const auth = route.auth ?? provider.auth;
  if (auth === "passthrough" && !provider.supports_passthrough) {
    throw new Error(`provider "${route.provider}" does not support auth:"passthrough"`);
  }
  const matches = route.match ? [route.match] : (provider.models ?? []);
  if (matches.length === 0) throw new Error(`route for provider "${route.provider}" has no match patterns`);
  return matches.map((match) => ({ match, base_url, auth }));
}

// Resolve a whole routes config (its `routes` array) into concrete router routes.
export function resolveRoutes(routesConfig, providers) {
  const routes = (routesConfig && Array.isArray(routesConfig.routes)) ? routesConfig.routes : [];
  return routes.flatMap((r) => resolveRoute(r, providers));
}

// The distinct backend endpoints (base_url values) among a set of resolved routes.
export function distinctEndpoints(routes) {
  return [...new Set(routes.map((r) => r.base_url))];
}

// The concrete model ids the seats pin: roles.*.model plus a top-level session model,
// dropping the non-concrete wishes ("auto"/"session"/empty) that resolve to the
// default endpoint anyway.
export function seatModels(config) {
  const wishes = [];
  if (config && config.roles && typeof config.roles === "object") {
    for (const seat of Object.values(config.roles)) {
      if (seat && typeof seat.model === "string") wishes.push(seat.model);
    }
  }
  if (config && typeof config.model === "string") wishes.push(config.model);
  return wishes.filter((m) => m && m !== "auto" && m !== "session");
}

// The env vars a set of routes needs but that are unset/empty (fail-closed input).
// Passthrough routes carry no backend key and are skipped.
export function missingKeyEnvs(routes, env) {
  const missing = [];
  for (const r of routes) {
    if (r.auth === "passthrough") continue;
    const keyEnv = r.auth && r.auth.keyEnv;
    if (keyEnv && !env[keyEnv]) missing.push(keyEnv);
  }
  return [...new Set(missing)];
}

// Merge the `launch` section of a LOCAL config override over the shared one. The
// gitignored `.ai-dev/config.local.json` holds the personal/per-machine `launch.*`
// values (a `configDir`, a personal launch model) so the shared `.ai-dev/config.json`
// stays clean and nothing personal is forced on a teammate. Per-field override: a
// non-undefined local field wins over the shared one; an absent local field keeps the
// shared value. Returns a NEW config object carrying the merged `launch` — the rest of
// the shared config is untouched (the local file only ever overrides `launch`). A null
// local config (file absent) ⇒ the shared config unchanged (byte-equivalent path).
export function mergeLocalLaunch(config, localConfig) {
  const base = config && typeof config === "object" ? config : {};
  if (!localConfig || typeof localConfig !== "object") return base;
  const sharedLaunch = base.launch && typeof base.launch === "object" ? base.launch : {};
  const localLaunch = localConfig.launch && typeof localConfig.launch === "object" ? localConfig.launch : {};
  const mergedLaunch = { ...sharedLaunch, ...localLaunch };
  // `aliases` is the one NESTED launch field — deep-merge it PER-TIER so a local file can
  // override one tier (e.g. personal sonnet→glm) without dropping the shared opus/haiku
  // bindings. The shallow spread above would replace the whole object.
  const sharedAliases = sharedLaunch.aliases && typeof sharedLaunch.aliases === "object" ? sharedLaunch.aliases : null;
  const localAliases = localLaunch.aliases && typeof localLaunch.aliases === "object" ? localLaunch.aliases : null;
  if (sharedAliases || localAliases) {
    mergedLaunch.aliases = { ...(sharedAliases || {}), ...(localAliases || {}) };
  }
  return { ...base, launch: mergedLaunch };
}

// Load the shared config at `configPath` merged with its gitignored
// `.ai-dev/config.local.json` sibling's `launch` overrides — the ONE home for "the config
// as BOTH the launcher AND the installer see it". Before this helper only the launcher
// (main() below) merged the local file, so a personal tier binding (e.g. opus → deepseek)
// living ONLY in config.local was INVISIBLE to the Claude installer's bake + routing
// self-verify: the bake produced the concrete native id and the self-verify passed
// against config.json alone, so the seat silently ran NATIVE (the silent-native class —
// docs/decisions/personal-multi-model-setup.md). Routing the installer's config read
// through here fixes that. Returns { config, shared, local }:
//   • config — the launch-merged config (installer bake + self-verify read THIS, so a
//     personal binding takes effect);
//   • shared — the raw config.json (or null when absent), so a caller can tell "no config"
//     from "config with an empty launch";
//   • local  — the raw config.local.json override (or null), so a caller can tell a
//     PERSONAL tier binding (config.local, applied by the launcher at startup, never
//     committed to settings.json) from a SHARED one (config.json, written to settings.json).
export function loadConfigWithLocal(configPath) {
  const shared = readJsonIfPresent(configPath);
  const local = readJsonIfPresent(localSiblingPath(configPath));
  return { config: mergeLocalLaunch(shared, local), shared, local };
}

// Merge a LOCAL routes-config override over the shared one — the routes-config sibling
// of mergeLocalLaunch, same shallow-override-plus-one-special-field shape. The gitignored
// .ai-dev/model-routes.local.json carries the per-machine proxyUrl (and optional personal
// route entries) so the shared, committed .ai-dev/model-routes.json never carries a
// machine-specific proxy pointer (docs/decisions/personal-multi-model-setup.md).
// Every scalar field (proxyUrl, listen, …) is a shallow override: present in local ⇒ local
// wins. `routes` is the one array field: local routes are concatenated BEFORE the shared
// ones (pickRoute is first-match-wins — model-router.mjs — so a personal route must sort
// first to be able to override/narrow a shared glob). No shared routes + no local routes
// ⇒ no `routes` key at all in the merged result (matches today's absent-routes shape, not
// an empty array forced in). No local file ⇒ the shared config returned unchanged
// (byte-equivalent path, mirroring mergeLocalLaunch(config, null)).
export function mergeLocalRoutes(routesConfig, localRoutesConfig) {
  const base = routesConfig && typeof routesConfig === "object" ? routesConfig : null;
  if (!localRoutesConfig || typeof localRoutesConfig !== "object") return base;
  const sharedRoutes = base && Array.isArray(base.routes) ? base.routes : [];
  const localRoutes = Array.isArray(localRoutesConfig.routes) ? localRoutesConfig.routes : [];
  const merged = { ...base, ...localRoutesConfig };
  // `routes` is the one array field: local routes first (first-match-wins), then shared.
  if (localRoutes.length > 0 || sharedRoutes.length > 0) {
    merged.routes = [...localRoutes, ...sharedRoutes];
  } else {
    delete merged.routes;
  }
  return merged;
}

// Load the routes config at `routesPath` merged with its gitignored `.ai-dev/model-
// routes.local.json` sibling — the routes-config sibling of loadConfigWithLocal. Returns
// { routesConfig, shared, local } (routesConfig = the merged one every planLaunch caller
// should use; shared/local = the raw files, for a caller that needs to tell them apart —
// main()'s error/probe messaging does not today, but the shape stays parallel to
// loadConfigWithLocal on purpose).
export function loadRoutesWithLocal(routesPath) {
  const shared = readJsonIfPresent(routesPath);
  const local = readJsonIfPresent(localSiblingPath(routesPath));
  return { routesConfig: mergeLocalRoutes(shared, local), shared, local };
}

// The launch-time env the config `launch` section sources (the RATIFIED option (c)
// source-of-truth: docs/decisions/multi-model-setup-ux.md `## The fork`).
//   config.launch.sessionModel → ANTHROPIC_MODEL (the orchestrator IS the session);
//   config.launch.guardModel   → ANTHROPIC_SMALL_FAST_MODEL (the background/small-fast model);
//   config.launch.configDir    → CLAUDE_CONFIG_DIR (a per-project claude profile dir —
//     the Operator's per-task-keys mechanism, pinned per project with no .bashrc edit;
//     useful even WITHOUT routing, so it is exported here, where every exec path —
//     proxy/direct/external — passes through, docs/decisions/launcher-ux.md).
//   config.launch.aliases.{fable,opus,sonnet,haiku} → ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL
//     (the cross-endpoint tier lever — bind a Claude tier to a foreign model id).
// Returns ONLY the keys with a non-empty value — an absent/empty section exports
// nothing, so a non-routing project's launch env is byte-unchanged. These must be set
// BEFORE `claude` reads them at process launch, which is exactly what the launcher does
// by putting them in the child's env below.
export function launchModelEnv(config) {
  const out = {};
  const launch = config && typeof config.launch === "object" && config.launch ? config.launch : {};
  const session = typeof launch.sessionModel === "string" ? launch.sessionModel.trim() : "";
  const guard = typeof launch.guardModel === "string" ? launch.guardModel.trim() : "";
  const configDir = typeof launch.configDir === "string" ? launch.configDir.trim() : "";
  if (session) out.ANTHROPIC_MODEL = session;
  if (guard) out.ANTHROPIC_SMALL_FAST_MODEL = guard;
  if (configDir) out.CLAUDE_CONFIG_DIR = configDir;
  // autoCompactWindow — OPTIONAL, opt-in: a positive integer (tokens) → CLAUDE_CODE_AUTO_COMPACT_WINDOW.
  //   CC resolves the effective window PER-MODEL, capping each at its REAL window, so this is a
  //   CEILING/target, not a force. Set it ONLY when EVERY seat's model actually supports the chosen
  //   window (e.g. a provider whose every model is 1M-capable, like deepseek) — otherwise a 200K model
  //   assumed at a higher window overflows before compacting. Personal (config.local launch), like the
  //   aliases; absent/invalid ⇒ unset (CC's default 200K-for-foreign assumption stands — the safe
  //   default). Runtime-exported only, never written to committed settings.json. The `[1m]` model-id
  //   suffix is the per-model native lever; this is the global one for foreign providers.
  //   docs/decisions/minimal-in-protocol-proxy.md.
  const acw = launch.autoCompactWindow;
  if (typeof acw === "number" && Number.isFinite(acw) && acw > 0) {
    out.CLAUDE_CODE_AUTO_COMPACT_WINDOW = String(Math.floor(acw));
  }
  // Tier-alias bindings — config.launch.aliases.{fable,opus,sonnet,haiku} → the
  // ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL vars Claude Code resolves a tier through
  // (the FOUR remappable aliases, strongest→weakest). This is the cross-endpoint lever: bind a
  // Claude TIER to a foreign model id (e.g. sonnet → glm-4.6), then a role using that tier
  // routes to it. Same launch-env class as the models above (startup-read, restart-applied);
  // absent/empty per-tier ⇒ not set. Mirror of install-claude.mjs `LAUNCH_ALIAS_ENV_KEYS`
  // (the settings.json env writer) — the two MUST stay in sync tier-for-tier.
  const aliases = launch.aliases && typeof launch.aliases === "object" ? launch.aliases : {};
  const ALIAS_ENV = { fable: "ANTHROPIC_DEFAULT_FABLE_MODEL", opus: "ANTHROPIC_DEFAULT_OPUS_MODEL", sonnet: "ANTHROPIC_DEFAULT_SONNET_MODEL", haiku: "ANTHROPIC_DEFAULT_HAIKU_MODEL" };
  for (const [tier, envVar] of Object.entries(ALIAS_ENV)) {
    const v = typeof aliases[tier] === "string" ? aliases[tier].trim() : "";
    if (v) out[envVar] = v;
  }
  return out;
}

// The tier→model pairs in config.launch.aliases with a non-empty (trimmed) value — the
// SAME four tiers launchModelEnv exports env for. A bound tier means Claude Code will
// send that foreign model id via ANTHROPIC_DEFAULT_<TIER>_MODEL regardless of launch
// mode, so planLaunch's fail-closed check (below) needs to know which tiers are live.
export function boundAliasTiers(config) {
  const aliases = config && config.launch && typeof config.launch.aliases === "object" ? config.launch.aliases : {};
  const tiers = ["fable", "opus", "sonnet", "haiku"];
  const bound = [];
  for (const tier of tiers) {
    const model = typeof aliases[tier] === "string" ? aliases[tier].trim() : "";
    if (model) bound.push({ tier, model });
  }
  return bound;
}

// Detect a `proxyUrl` placed under `config.launch` — a field the launcher does
// NOT read (it reads `proxyUrl` from the routes config, .ai-dev/model-routes.local.json).
// setup.md historically listed it under the personal launch inventory, and a misplaced
// value was silently ignored. Returns a one-line warning (non-blocking) pointing at the
// correct home, or null when the field is absent/empty/whitespace. Pure: takes a config
// object, no side effects.
export function mislaunchedProxyWarning(config) {
  const launch = config && typeof config === "object" && config.launch && typeof config.launch === "object" ? config.launch : null;
  if (!launch) return null;
  const v = typeof launch.proxyUrl === "string" ? launch.proxyUrl.trim() : "";
  if (!v) return null;
  return `[router-launch] WARNING: config.launch.proxyUrl=${JSON.stringify(v)} is not read — \`proxyUrl\` belongs in .ai-dev/model-routes.local.json (top-level). Move it there.`;
}

// The child env for `claude`: layer the config-sourced launch-time models (session +
// guard + configDir, when set) onto the base env, point it at the router when one is
// given, and guarantee CLAUDE_CODE_SUBAGENT_MODEL is UNSET (it overrides per-seat
// model: pins and would collapse every seat to one model — decision doc, fact 2).
// routerUrl may be null/omitted for the direct/external paths that only need the
// launch-model layer.
//
// env precedence (ratified, docs/decisions/launcher-ux.md): the launcher layers ONLY
// ANTHROPIC_BASE_URL (to the proxy, when routing is on) + unsets
// CLAUDE_CODE_SUBAGENT_MODEL + the launch-model env; everything else in baseEnv passes
// through untouched. When routing is ON and baseEnv ALREADY carries a different
// ANTHROPIC_BASE_URL (e.g. a personal wrapper pointed at another proxy), the LAUNCHER
// WINS — but loudly: a visible stderr WARNING names both URLs so a silent hijack of the
// user's own proxy can never happen. `warn` is injectable so the pure function stays
// side-effect-testable; it defaults to a stderr writer.
export function buildChildEnv(baseEnv, routerUrl, config, warn = (m) => process.stderr.write(m)) {
  const env = { ...baseEnv, ...launchModelEnv(config) };
  if (routerUrl) {
    const prior = baseEnv && typeof baseEnv.ANTHROPIC_BASE_URL === "string" ? baseEnv.ANTHROPIC_BASE_URL.trim() : "";
    if (prior && prior !== routerUrl) {
      warn(
        `[router-launch] WARNING: ANTHROPIC_BASE_URL was already set to ${prior}; ` +
          `the launcher is routing through ${routerUrl} and OVERRIDES it for this session.\n`,
      );
    }
    env.ANTHROPIC_BASE_URL = routerUrl;
  }
  delete env.CLAUDE_CODE_SUBAGENT_MODEL;
  return env;
}

// THE decision. Given the loaded config, routes config, catalog, and env, decide
// whether to run direct or through the router, which routes the router would carry,
// and whether any needed key env is missing (fail-closed). Returns:
//   { mode: "direct"|"router"|"external", proxyUrl?, routes, exercisedRoutes, endpoints,
//     missingKeyEnvs, error }
// `mode: "external"` carries `proxyUrl` (an opt-in already-running proxy). `error` is set
// (and mode irrelevant) when the routes config is malformed OR proxyUrl is present but
// not a valid http(s) URL.
export function planLaunch({ config, routesConfig, providers, env }) {
  // Opt-in EXTERNAL proxy: a non-empty `proxyUrl` in the routes config short-circuits
  // the spawn — the launcher just points claude at the already-running proxy. Keys live
  // in that proxy's env, so the fail-closed key check below does not apply. A present-
  // but-malformed proxyUrl is a HARD ERROR (fail-closed) — never a silent fall-through
  // to spawning a local router the operator did not ask for.
  const rawProxy = routesConfig && typeof routesConfig.proxyUrl === "string" ? routesConfig.proxyUrl.trim() : "";
  if (rawProxy) {
    let valid;
    try {
      const u = new URL(rawProxy);
      valid = u.protocol === "http:" || u.protocol === "https:";
    } catch {
      valid = false;
    }
    if (!valid) {
      return { mode: "direct", proxyUrl: null, routes: [], exercisedRoutes: [], endpoints: [], missingKeyEnvs: [], error: `proxyUrl is not a valid http(s) URL: "${rawProxy}"` };
    }
    return { mode: "external", proxyUrl: rawProxy, routes: [], exercisedRoutes: [], endpoints: [], missingKeyEnvs: [], error: null };
  }

  let routes;
  try {
    routes = resolveRoutes(routesConfig, providers);
  } catch (err) {
    return { mode: "direct", routes: [], exercisedRoutes: [], endpoints: [], missingKeyEnvs: [], error: err.message };
  }

  // No routes at all ⇒ direct, UNLESS a tier alias is bound to a foreign model. A bound
  // alias means Claude Code will send that foreign model id regardless of mode; if nothing
  // is configured to route it (no shared/local routes + no proxyUrl), fail-closed with a
  // clear error naming the tier/model and the two file-scoped fixes. The byte-unchanged
  // default (no routes, no aliases) is still direct and error-null.
  if (routes.length === 0) {
    const bound = boundAliasTiers(config);
    if (bound.length > 0) {
      const tiers = bound.map((b) => `${b.tier} → ${b.model}`).join(", ");
      return {
        mode: "direct",
        routes,
        exercisedRoutes: [],
        endpoints: [],
        missingKeyEnvs: [],
        error: `bound alias(es) unreachable: ${tiers} — nothing configured to route them. ` +
          "Add routes to .ai-dev/model-routes.json (shared), or set proxyUrl in " +
          ".ai-dev/model-routes.local.json (personal), or clear the aliases to fall back to native.",
      };
    }
    return { mode: "direct", routes, exercisedRoutes: [], endpoints: [], missingKeyEnvs: [], error: null };
  }

  // Which routes the seats actually exercise. When seat models are pinned, map each to
  // its route; otherwise fall back to every configured route (can't narrow).
  const models = seatModels(config);
  let exercisedRoutes;
  if (models.length) {
    const hit = new Set();
    for (const m of models) {
      const r = pickRoute(m, routes);
      if (r) hit.add(r);
    }
    exercisedRoutes = [...hit];
  } else {
    exercisedRoutes = routes;
  }

  const endpoints = distinctEndpoints(exercisedRoutes);
  const mode = endpoints.length >= 2 ? "router" : "direct";
  // In router mode the launcher carries the FULL routes table (any configured model
  // could be requested), so the key check covers all of it.
  const missing = mode === "router" ? missingKeyEnvs(routes, env) : [];
  return { mode, routes, exercisedRoutes, endpoints, missingKeyEnvs: missing, error: null };
}

// ── proxy probe (setup-dialog model discovery) ───────────────────────────────

// The conventional localhost port a STANDALONE modelpipe listens on (model-router.mjs
// main() default). The launcher-spawned proxy uses a RANDOM free port and never needs
// probing — we hold its routes config — so the probe targets an ALREADY-RUNNING proxy:
// a standalone modelpipe on this port, or any third-party proxy (LiteLLM-class) the
// Operator runs. The discovery the setup dialog uses to skip asking for a URL.
export const DEFAULT_PROXY_PORT = 8787;

// The ordered, de-duplicated probe candidates: an explicit URL arg first (highest
// intent), then the routes-config `proxyUrl` (the Operator's recorded external proxy),
// then the conventional localhost default. Each is validated http(s) and normalised to
// origin (scheme://host); blank/invalid entries are dropped — the probe is best-effort
// and never throws on a bad input.
export function probeCandidates({ argUrl, routesConfig, defaultPort = DEFAULT_PROXY_PORT } = {}) {
  const raw = [
    typeof argUrl === "string" ? argUrl : "",
    routesConfig && typeof routesConfig.proxyUrl === "string" ? routesConfig.proxyUrl : "",
    `http://127.0.0.1:${defaultPort}`,
  ];
  const out = [];
  for (const r of raw) {
    if (typeof r !== "string" || !r.trim()) continue;
    let u;
    try { u = new URL(r.trim()); } catch { continue; }
    if (u.protocol !== "http:" && u.protocol !== "https:") continue;
    const origin = `${u.protocol}//${u.host}`;
    if (!out.includes(origin)) out.push(origin);
  }
  return out;
}

// Parse a /v1/models response body into a concrete model-id list. Accepts the OpenAI /
// modelpipe shape ({ data: [{ id }] }) and a bare array (of strings or { id }). Returns
// { ok, models }: ok=false on unparseable/unexpected JSON (the probe treats it as "not a
// usable list"); models is always an array of non-empty string ids (deduped, order-kept).
export function parseModelsResponse(text) {
  let parsed;
  try { parsed = JSON.parse(text); } catch { return { ok: false, models: [] }; }
  const arr = Array.isArray(parsed)
    ? parsed
    : parsed && Array.isArray(parsed.data)
      ? parsed.data
      : null;
  if (!arr) return { ok: false, models: [] };
  const seen = new Set();
  const models = [];
  for (const item of arr) {
    const id = typeof item === "string" ? item : item && typeof item.id === "string" ? item.id : "";
    if (id && !seen.has(id)) {
      seen.add(id);
      models.push(id);
    }
  }
  return { ok: true, models };
}

// ── impure wiring (the untestable exec rung) ─────────────────────────────────

// Best-effort GET of a JSON endpoint, bounded by a short timeout and a small body cap.
// Resolves { status, body } on any HTTP response, or null on connect/timeout/error — a
// dead candidate is "not alive", never a throw. The live-HTTP rung of the probe (the
// candidate list + parse are the pure, unit-tested half above).
function httpGetJson(urlStr, timeoutMs = 1500) {
  return new Promise((resolve) => {
    let u;
    try {
      u = new URL(urlStr);
    } catch {
      resolve(null);
      return;
    }
    const client = u.protocol === "http:" ? http : https;
    const req = client.request(
      {
        protocol: u.protocol,
        hostname: u.hostname,
        port: u.port || (u.protocol === "http:" ? 80 : 443),
        path: u.pathname,
        method: "GET",
      },
      (res) => {
        const chunks = [];
        let size = 0;
        res.on("data", (c) => {
          size += c.length;
          if (size <= 256 * 1024) chunks.push(c); // bound a pathological body
        });
        res.on("end", () => resolve({ status: res.statusCode || 0, body: Buffer.concat(chunks).toString("utf8") }));
        res.on("error", () => resolve(null));
      },
    );
    req.on("error", () => resolve(null));
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve(null);
    });
    req.end();
  });
}

// Try each candidate origin's /v1/models (then /models), first 200-with-a-usable-list
// wins. Returns { alive, url, models } — the single JSON line --probe prints.
async function runProbe(candidates) {
  for (const base of candidates) {
    for (const p of ["/v1/models", "/models"]) {
      const r = await httpGetJson(`${base}${p}`);
      if (r && r.status === 200) {
        const { ok, models } = parseModelsResponse(r.body);
        if (ok) return { alive: true, url: base, models };
      }
    }
  }
  return { alive: false, url: null, models: [] };
}

function readJsonIfPresent(p) {
  if (!p || !fs.existsSync(p)) return null;
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

function execClaude(env, args) {
  const child = spawn("claude", args, { stdio: "inherit", env });
  child.on("exit", (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    else process.exit(code ?? 0);
  });
  child.on("error", (err) => {
    process.stderr.write(`[router-launch] failed to exec claude: ${err.message}\n`);
    process.exit(127);
  });
  return child;
}

// The local-override sibling path: <dir>/<base>.json → <dir>/<base>.local.json.
// Used for both config.local.json (gitignored personal launch overrides) and
// model-routes.local.json (gitignored personal routes/proxyUrl) — follows the override
// path when customized, so a test/alt file and its local sibling stay paired.
function localSiblingPath(siblingPath) {
  const dir = path.dirname(siblingPath);
  const base = path.basename(siblingPath, ".json");
  return path.join(dir, `${base}.local.json`);
}

function main() {
  const args = process.argv.slice(2);
  // --probe [url]: best-effort discovery of an ALREADY-RUNNING proxy for the setup
  // dialog. Tries the candidate origins (an explicit url arg, the routes-config
  // proxyUrl, the conventional localhost default) at /v1/models, prints ONE JSON line
  // { alive, url, models } to STDOUT (scriptable), and exits 0 (alive) / 1 (nothing
  // answered). Consumed here, never forwarded to claude. Never throws — a refused/
  // timed-out candidate is simply "not alive".
  const probeIdx = args.indexOf("--probe");
  if (probeIdx !== -1) {
    const next = args[probeIdx + 1];
    const argUrl = next && !next.startsWith("-") ? next : "";
    const routesPath = process.env.MODEL_ROUTER_ROUTES || ".ai-dev/model-routes.json";
    let routesConfig = null;
    try {
      ({ routesConfig } = loadRoutesWithLocal(routesPath));
    } catch {
      // a malformed routes file never breaks discovery — routesConfig stays null
    }
    runProbe(probeCandidates({ argUrl, routesConfig })).then((result) => {
      process.stdout.write(`${JSON.stringify(result)}\n`);
      process.exit(result.alive ? 0 : 1);
    });
    return;
  }
  // --proxy: foreground-only proxy mode. Start the router, print its URL, do NOT exec
  // claude — for "don't touch my launch": point your own claude/wrapper at the printed
  // URL (or wire it as the routes config `proxyUrl`). The flag is consumed here, never
  // forwarded to claude (claude is not started in this mode).
  const proxyOnly = args.includes("--proxy");
  const claudeArgs = args.filter((a) => a !== "--proxy");
  const configPath = process.env.AI_DEV_CONFIG || ".ai-dev/config.json";
  const routesPath = process.env.MODEL_ROUTER_ROUTES || ".ai-dev/model-routes.json";

  let config, routesConfig, providers;
  try {
    // Merge the gitignored local override's `launch` over the shared config's `launch`
    // (configDir + any personal launch model / tier binding live there — never forced on
    // a teammate). The ONE home for that merge is loadConfigWithLocal, shared with the
    // installer so the launcher and the bake/self-verify see the identical merged launch.
    ({ config } = loadConfigWithLocal(configPath));
    // Fail-loud on a `proxyUrl` placed under `launch` — the launcher reads it from the routes config, not here.
    const misplacedProxy = mislaunchedProxyWarning(config);
    if (misplacedProxy) process.stderr.write(`${misplacedProxy}\n`);
    // Merge the gitignored local routes override over the shared routes (proxyUrl + any
    // personal route entries live there — never committed, never break a teammate's routing
    // on pull). The ONE home for that merge is loadRoutesWithLocal.
    ({ routesConfig } = loadRoutesWithLocal(routesPath));
    providers = loadProviders();
  } catch (err) {
    process.stderr.write(`[router-launch] config error: ${err.message}\n`);
    process.exit(1);
    return;
  }

  const plan = planLaunch({ config, routesConfig, providers, env: process.env });
  if (plan.error) {
    process.stderr.write(`[router-launch] routes config error: ${plan.error}\n`);
    process.exit(1);
    return;
  }

  // --proxy foreground-only mode: there must be a LOCAL router to start. An external
  // proxyUrl (the proxy already runs elsewhere) or a direct plan (no proxy at all) has
  // nothing for this mode to start — fail-closed with a pointer, never a no-op claude.
  if (proxyOnly && plan.mode !== "router") {
    const why = plan.mode === "external"
      ? `a proxy is already running at ${plan.proxyUrl} (routes config proxyUrl) — point your launch at THAT URL`
      : "fewer than 2 distinct endpoints are in play (nothing to route) — pin a cross-endpoint seat first";
    process.stderr.write(`[router-launch] --proxy: nothing to start: ${why}\n`);
    process.exit(1);
    return;
  }

  if (plan.mode === "external") {
    process.stderr.write(`[router-launch] external proxy at ${plan.proxyUrl} — not spawning a router\n`);
    execClaude(buildChildEnv(process.env, plan.proxyUrl, config), claudeArgs);
    return;
  }

  if (plan.mode === "direct") {
    process.stderr.write("[router-launch] single endpoint — running claude directly (no router)\n");
    // No router URL and CLAUDE_CODE_SUBAGENT_MODEL untouched in direct mode (per-seat
    // baking still applies); only layer the config-sourced launch-time models + configDir.
    execClaude({ ...process.env, ...launchModelEnv(config) }, claudeArgs);
    return;
  }

  // Fail-closed BEFORE starting anything.
  if (plan.missingKeyEnvs.length) {
    process.stderr.write(
      `[router-launch] refusing to launch: unset backend key env var(s): ${plan.missingKeyEnvs.join(", ")}\n` +
        "  set them (or use a passthrough route) — never a silent wrong-backend.\n",
    );
    process.exit(1);
    return;
  }

  const server = createRouter({ listen: { host: "127.0.0.1", port: 0 }, routes: plan.routes });
  server.listen(0, "127.0.0.1", () => {
    const port = server.address().port;
    const routerUrl = `http://127.0.0.1:${port}`;
    process.stderr.write(
      `[router-launch] router on ${routerUrl} — ${plan.endpoints.length} endpoints, ${plan.routes.length} routes\n`,
    );
    if (proxyOnly) {
      // Foreground-only: print the URL on STDOUT (scriptable — capture it to wire your
      // own launch) and stay up until interrupted; never exec claude. The server keeps
      // the event loop alive; Ctrl-C / SIGTERM ends it.
      process.stdout.write(`${routerUrl}\n`);
      process.stderr.write("[router-launch] --proxy: proxy running in the foreground; point your claude at the URL above (Ctrl-C to stop)\n");
      const stop = () => { server.close(); process.exit(0); };
      process.on("SIGINT", stop);
      process.on("SIGTERM", stop);
      return;
    }
    const child = execClaude(buildChildEnv(process.env, routerUrl, config), claudeArgs);
    const teardown = () => server.close();
    child.on("exit", teardown);
    child.on("error", teardown);
  });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) main();
