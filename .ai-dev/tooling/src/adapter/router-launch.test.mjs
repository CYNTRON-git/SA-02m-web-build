// Unit test for the model-router launcher's PURE decision functions
// (router-launch.mjs). NO network, NO `claude` exec — the real exec is the one
// untestable rung (a live process), offered as a real-layer check, not run here.
//
// Covers: catalog resolution (provider id → base_url/auth, overrides, passthrough,
// unknown provider, fan-out by model patterns), distinct-endpoint counting, the
// fail-closed key-env check, seat-model extraction, child-env construction, and the
// whole planLaunch decision (direct-by-default vs router, fail-closed). Also loads
// the REAL src/adapter/model-providers.json and pins the four verified providers.
//
// Run: node src/adapter/router-launch.test.mjs

import {
  loadProviders,
  resolveRoute,
  resolveRoutes,
  distinctEndpoints,
  seatModels,
  missingKeyEnvs,
  mergeLocalLaunch,
  loadConfigWithLocal,
  launchModelEnv,
  buildChildEnv,
  boundAliasTiers,
  mislaunchedProxyWarning,
  mergeLocalRoutes,
  loadRoutesWithLocal,
  planLaunch,
  probeCandidates,
  parseModelsResponse,
  DEFAULT_PROXY_PORT,
} from "./router-launch.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

let pass = 0;
const fails = [];
function check(name, got, want) {
  if (got === want) { pass++; return; }
  fails.push(`  ✗ ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

// A small fixture catalog (independent of the shipped one, so a catalog edit can't
// silently pass these MECHANISM tests).
const PROVIDERS = [
  { id: "anthropic", base_url: "https://api.anthropic.com", models: ["claude-*"], auth: { header: "x-api-key", keyEnv: "ANTHROPIC_API_KEY" }, supports_passthrough: true },
  { id: "deepseek", base_url: "https://api.deepseek.com/anthropic", models: ["deepseek-*"], auth: { header: "x-api-key", keyEnv: "DEEPSEEK_API_KEY" } },
  { id: "glm", base_url: "https://api.z.ai/api/anthropic", models: ["GLM-*"], auth: { header: "Authorization", scheme: "Bearer", keyEnv: "ZAI_API_KEY" } },
];

function main() {
  // ── resolveRoute / resolveRoutes ──────────────────────────────────────────
  const r1 = resolveRoute({ provider: "deepseek" }, PROVIDERS);
  check("provider ref pulls base_url from catalog", r1[0].base_url, "https://api.deepseek.com/anthropic");
  check("provider ref pulls auth from catalog", r1[0].auth.keyEnv, "DEEPSEEK_API_KEY");
  check("provider ref fans out to catalog model patterns", r1[0].match, "deepseek-*");

  const r2 = resolveRoute({ provider: "anthropic", match: "claude-opus-4-8" }, PROVIDERS);
  check("inline match overrides the catalog patterns", r2[0].match, "claude-opus-4-8");

  const r3 = resolveRoute({ provider: "anthropic", auth: "passthrough" }, PROVIDERS);
  check("passthrough override carried through (anthropic supports it)", r3[0].auth, "passthrough");
  check("passthrough route keeps the provider base_url", r3[0].base_url, "https://api.anthropic.com");

  const r4 = resolveRoute({ provider: "anthropic", base_url: "https://example.test" }, PROVIDERS);
  check("inline base_url override wins", r4[0].base_url, "https://example.test");

  const r5 = resolveRoute({ match: "x-*", base_url: "https://x.test", auth: { header: "x-api-key", keyEnv: "X" } }, PROVIDERS);
  check("fully-specified route passes through unchanged", r5[0].base_url, "https://x.test");

  let unknownThrew = false;
  try { resolveRoute({ provider: "nope" }, PROVIDERS); } catch { unknownThrew = true; }
  check("unknown provider throws", unknownThrew, true);

  let badPassthrough = false;
  try { resolveRoute({ provider: "deepseek", auth: "passthrough" }, PROVIDERS); } catch { badPassthrough = true; }
  check("passthrough rejected on a provider that can't (deepseek)", badPassthrough, true);

  const resolved = resolveRoutes({ routes: [{ provider: "anthropic" }, { provider: "deepseek" }] }, PROVIDERS);
  check("resolveRoutes flattens to all routes", resolved.length, 2);

  // ── distinctEndpoints ─────────────────────────────────────────────────────
  check("distinctEndpoints counts unique base_urls", distinctEndpoints(resolved).length, 2);
  check("same endpoint twice ⇒ 1",
    distinctEndpoints([{ base_url: "a" }, { base_url: "a" }]).length, 1);

  // ── seatModels ────────────────────────────────────────────────────────────
  const cfg = { model: "claude-opus-4-8", roles: { builder: { model: "deepseek-chat" }, reviewer: { model: "claude-sonnet-4-6" }, guard: { model: "auto" }, orchestrator: { model: "session" } } };
  const sm = seatModels(cfg);
  check("seatModels keeps concrete pins", sm.includes("deepseek-chat") && sm.includes("claude-sonnet-4-6"), true);
  check("seatModels drops auto/session wishes", sm.includes("auto") || sm.includes("session"), false);

  // ── missingKeyEnvs (fail-closed input) ────────────────────────────────────
  const mk = missingKeyEnvs(resolved, { ANTHROPIC_API_KEY: "x" }); // DEEPSEEK_API_KEY unset
  check("missingKeyEnvs flags the unset backend key", mk.includes("DEEPSEEK_API_KEY"), true);
  check("missingKeyEnvs does NOT flag the set key", mk.includes("ANTHROPIC_API_KEY"), false);
  check("missingKeyEnvs skips passthrough routes",
    missingKeyEnvs([{ auth: "passthrough" }], {}).length, 0);

  // ── launchModelEnv (config-sourced launch-time models) ────────────────────
  const both = launchModelEnv({ launch: { sessionModel: "claude-opus-4-8", guardModel: "claude-haiku-4-6" } });
  check("launchModelEnv maps sessionModel → ANTHROPIC_MODEL", both.ANTHROPIC_MODEL, "claude-opus-4-8");
  check("launchModelEnv maps guardModel → ANTHROPIC_SMALL_FAST_MODEL", both.ANTHROPIC_SMALL_FAST_MODEL, "claude-haiku-4-6");
  // Absent/empty section ⇒ exports nothing (a non-routing project stays byte-unchanged).
  check("launchModelEnv: absent launch ⇒ empty", Object.keys(launchModelEnv({})).length, 0);
  check("launchModelEnv: empty/whitespace values ⇒ dropped",
    Object.keys(launchModelEnv({ launch: { sessionModel: "", guardModel: "   " } })).length, 0);
  // Only one set ⇒ only that one key.
  const onlyGuard = launchModelEnv({ launch: { guardModel: "claude-haiku-4-6" } });
  check("launchModelEnv: only guard set ⇒ only the guard key", "ANTHROPIC_MODEL" in onlyGuard, false);
  check("launchModelEnv: only guard set ⇒ guard key present", onlyGuard.ANTHROPIC_SMALL_FAST_MODEL, "claude-haiku-4-6");
  // Non-string / non-object launch ⇒ safe empty (fail-safe like the rest).
  check("launchModelEnv: non-object launch ⇒ empty", Object.keys(launchModelEnv({ launch: "nope" })).length, 0);

  // configDir → CLAUDE_CONFIG_DIR (a per-project claude profile dir). Exported by the
  // SAME launchModelEnv that every exec path (proxy/direct/external) layers, so the
  // export covers them all.
  const withDir = launchModelEnv({ launch: { configDir: "/home/me/.claude-work" } });
  check("launchModelEnv: configDir → CLAUDE_CONFIG_DIR", withDir.CLAUDE_CONFIG_DIR, "/home/me/.claude-work");
  // configDir is useful WITHOUT routing — set alone, only that key is exported.
  check("launchModelEnv: configDir alone ⇒ no model keys", "ANTHROPIC_MODEL" in withDir || "ANTHROPIC_SMALL_FAST_MODEL" in withDir, false);
  // absent/empty/whitespace configDir ⇒ CLAUDE_CONFIG_DIR is NOT set (unset, byte-unchanged).
  check("launchModelEnv: absent configDir ⇒ no CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_DIR" in launchModelEnv({ launch: { sessionModel: "x" } }), false);
  check("launchModelEnv: blank configDir ⇒ no CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_DIR" in launchModelEnv({ launch: { configDir: "   " } }), false);

  // ── mislaunchedProxyWarning (config.launch.proxyUrl is a silent no-op today) ──
  // setup.md historically listed `proxyUrl` under the personal config.local launch
  // inventory; the launcher reads it from .ai-dev/model-routes.local.json instead. The
  // warning is the fail-loud backstop so a misplaced value is no longer silent.
  check("mislaunchedProxyWarning: absent launch ⇒ null", mislaunchedProxyWarning({}), null);
  check("mislaunchedProxyWarning: launch without proxyUrl ⇒ null",
    mislaunchedProxyWarning({ launch: { sessionModel: "x" } }), null);
  check("mislaunchedProxyWarning: empty/whitespace proxyUrl ⇒ null",
    mislaunchedProxyWarning({ launch: { proxyUrl: "   " } }), null);
  check("mislaunchedProxyWarning: non-string proxyUrl ⇒ null",
    mislaunchedProxyWarning({ launch: { proxyUrl: 1234 } }), null);
  check("mislaunchedProxyWarning: non-object config ⇒ null", mislaunchedProxyWarning(null), null);
  const mpw = mislaunchedProxyWarning({ launch: { proxyUrl: "http://127.0.0.1:8787" } });
  check("mislaunchedProxyWarning: present proxyUrl ⇒ warning string", typeof mpw, "string");
  check("mislaunchedProxyWarning: warning names the correct home (model-routes.local.json)",
    mpw && mpw.includes("model-routes.local.json"), true);
  check("mislaunchedProxyWarning: warning names the field's source (config.launch.proxyUrl)",
    mpw && mpw.includes("config.launch.proxyUrl"), true);
  check("mislaunchedProxyWarning: warning echoes the misplaced value",
    mpw && mpw.includes("http://127.0.0.1:8787"), true);
  check("mislaunchedProxyWarning: warning is non-blocking (returns a string, not throws)",
    typeof mpw, "string");

  // ── launchModelEnv: autoCompactWindow → CLAUDE_CODE_AUTO_COMPACT_WINDOW (opt-in, personal) ──
  check("launchModelEnv: autoCompactWindow (number) → CLAUDE_CODE_AUTO_COMPACT_WINDOW string",
    launchModelEnv({ launch: { autoCompactWindow: 1000000 } }).CLAUDE_CODE_AUTO_COMPACT_WINDOW, "1000000");
  check("launchModelEnv: absent autoCompactWindow ⇒ unset",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in launchModelEnv({ launch: { sessionModel: "x" } }), false);
  check("launchModelEnv: non-number autoCompactWindow ⇒ unset",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in launchModelEnv({ launch: { autoCompactWindow: "1000000" } }), false);
  check("launchModelEnv: zero/negative autoCompactWindow ⇒ unset",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in launchModelEnv({ launch: { autoCompactWindow: -5 } }), false);
  check("launchModelEnv: fractional autoCompactWindow ⇒ floored",
    launchModelEnv({ launch: { autoCompactWindow: 1000000.9 } }).CLAUDE_CODE_AUTO_COMPACT_WINDOW, "1000000");

  // ── launchModelEnv: tier-alias bindings → ANTHROPIC_DEFAULT_*_MODEL ─────────
  const al = launchModelEnv({ launch: { aliases: { opus: "claude-opus-4-8", sonnet: "glm-4.6", haiku: "deepseek-chat" } } });
  check("aliases: opus → ANTHROPIC_DEFAULT_OPUS_MODEL", al.ANTHROPIC_DEFAULT_OPUS_MODEL, "claude-opus-4-8");
  check("aliases: sonnet → ANTHROPIC_DEFAULT_SONNET_MODEL", al.ANTHROPIC_DEFAULT_SONNET_MODEL, "glm-4.6");
  check("aliases: haiku → ANTHROPIC_DEFAULT_HAIKU_MODEL", al.ANTHROPIC_DEFAULT_HAIKU_MODEL, "deepseek-chat");
  // Only the set tiers are exported; blanks/absent are dropped (byte-unchanged otherwise).
  const alPartial = launchModelEnv({ launch: { aliases: { sonnet: "glm-4.6", haiku: "  " } } });
  check("aliases: only the set tier is exported", alPartial.ANTHROPIC_DEFAULT_SONNET_MODEL, "glm-4.6");
  check("aliases: blank tier dropped", "ANTHROPIC_DEFAULT_HAIKU_MODEL" in alPartial, false);
  check("aliases: absent tier dropped", "ANTHROPIC_DEFAULT_OPUS_MODEL" in alPartial, false);
  check("aliases: absent section ⇒ no alias keys", "ANTHROPIC_DEFAULT_SONNET_MODEL" in launchModelEnv({ launch: { sessionModel: "x" } }), false);
  check("aliases: non-object aliases ⇒ ignored", Object.keys(launchModelEnv({ launch: { aliases: "nope" } })).length, 0);

  // ── mergeLocalLaunch (gitignored config.local.json overrides the shared launch) ──
  const merged = mergeLocalLaunch(
    { profile: "solo", launch: { sessionModel: "shared-sess", guardModel: "shared-guard" } },
    { launch: { sessionModel: "local-sess", configDir: "/home/me/.cc" } },
  );
  check("mergeLocalLaunch: local sessionModel wins over shared", merged.launch.sessionModel, "local-sess");
  check("mergeLocalLaunch: shared field with no local override is kept", merged.launch.guardModel, "shared-guard");
  check("mergeLocalLaunch: local-only field (configDir) is added", merged.launch.configDir, "/home/me/.cc");
  check("mergeLocalLaunch: the rest of the shared config is untouched", merged.profile, "solo");
  // No local config (file absent) ⇒ the shared config unchanged (byte-equivalent path).
  const noLocal = mergeLocalLaunch({ launch: { sessionModel: "s" } }, null);
  check("mergeLocalLaunch: null local ⇒ shared unchanged", noLocal.launch.sessionModel, "s");
  // A local file with no launch section ⇒ shared launch preserved (empty local launch).
  const localNoLaunch = mergeLocalLaunch({ launch: { sessionModel: "s" } }, { other: 1 });
  check("mergeLocalLaunch: local with no launch ⇒ shared launch kept", localNoLaunch.launch.sessionModel, "s");
  // configDir homed ONLY in the local file still reaches the env via the merge.
  const localDirEnv = launchModelEnv(mergeLocalLaunch({ launch: {} }, { launch: { configDir: "/x/y" } }));
  check("mergeLocalLaunch + launchModelEnv: local-only configDir reaches CLAUDE_CONFIG_DIR", localDirEnv.CLAUDE_CONFIG_DIR, "/x/y");

  // aliases is the one NESTED launch field — DEEP-merged per tier so a local override of one
  // tier keeps the shared others (a shallow spread would drop them).
  const mergedAliases = mergeLocalLaunch(
    { launch: { aliases: { opus: "claude-opus-4-8", sonnet: "shared-sonnet", haiku: "shared-haiku" } } },
    { launch: { aliases: { sonnet: "glm-4.6" } } },
  );
  check("mergeLocalLaunch: local tier overrides shared", mergedAliases.launch.aliases.sonnet, "glm-4.6");
  check("mergeLocalLaunch: shared tiers kept (opus)", mergedAliases.launch.aliases.opus, "claude-opus-4-8");
  check("mergeLocalLaunch: shared tiers kept (haiku)", mergedAliases.launch.aliases.haiku, "shared-haiku");
  // local-only aliases (no shared) still reach the env via the merge.
  const localOnlyAliasEnv = launchModelEnv(mergeLocalLaunch({ launch: {} }, { launch: { aliases: { sonnet: "glm-4.6" } } }));
  check("mergeLocalLaunch + launchModelEnv: local-only alias reaches ANTHROPIC_DEFAULT_SONNET_MODEL", localOnlyAliasEnv.ANTHROPIC_DEFAULT_SONNET_MODEL, "glm-4.6");
  // shared-only aliases (no local) are preserved untouched.
  const sharedOnlyAlias = mergeLocalLaunch({ launch: { aliases: { opus: "claude-opus-4-8" } } }, { launch: { sessionModel: "s" } });
  check("mergeLocalLaunch: shared-only aliases preserved", sharedOnlyAlias.launch.aliases.opus, "claude-opus-4-8");

  // ── loadConfigWithLocal (the shared read-and-merge the installer + launcher both use) ──
  // Writes config.json + its gitignored config.local.json sibling to disk and confirms the
  // helper returns { config: merged, shared, local } — the one home the Claude bake + routing
  // self-verify read so a PERSONAL tier binding takes effect (docs/decisions/personal-multi-model-setup.md).
  {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-loadcfg-"));
    const configPath = path.join(dir, "config.json");
    fs.writeFileSync(configPath, JSON.stringify({ profile: "solo", roles: { builder: { agent: "b", model: "opus" } }, launch: { sessionModel: "claude-opus-4-8" } }));
    fs.writeFileSync(path.join(dir, "config.local.json"), JSON.stringify({ launch: { aliases: { opus: "deepseek-v4-pro" } } }));
    const { config, shared, local } = loadConfigWithLocal(configPath);
    check("loadConfigWithLocal: personal alias reaches the MERGED config", config.launch.aliases.opus, "deepseek-v4-pro");
    check("loadConfigWithLocal: shared launch field is kept in the merge", config.launch.sessionModel, "claude-opus-4-8");
    check("loadConfigWithLocal: roles come through from the shared config", config.roles.builder.model, "opus");
    check("loadConfigWithLocal: `shared` is the raw config.json (no alias)", shared.launch.aliases, undefined);
    check("loadConfigWithLocal: `local` is the raw override (personal alias)", local.launch.aliases.opus, "deepseek-v4-pro");
    fs.rmSync(dir, { recursive: true, force: true });
  }
  {
    // No config.local sibling ⇒ merged === shared launch, local null (byte-unchanged path).
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-loadcfg-nolocal-"));
    const configPath = path.join(dir, "config.json");
    fs.writeFileSync(configPath, JSON.stringify({ launch: { sessionModel: "s" } }));
    const { config, local } = loadConfigWithLocal(configPath);
    check("loadConfigWithLocal: no config.local ⇒ shared launch unchanged", config.launch.sessionModel, "s");
    check("loadConfigWithLocal: no config.local ⇒ local is null", local, null);
    fs.rmSync(dir, { recursive: true, force: true });
  }

  // ── boundAliasTiers (bound tier→model pairs from config.launch.aliases) ────
  check("boundAliasTiers({})", boundAliasTiers({}).length, 0);
  check("boundAliasTiers({ launch: {} })", boundAliasTiers({ launch: {} }).length, 0);
  check("boundAliasTiers({ launch: { aliases: {} } })", boundAliasTiers({ launch: { aliases: {} } }).length, 0);
  const bound1 = boundAliasTiers({ launch: { aliases: { sonnet: "glm-5.1", haiku: "   ", opus: "" } } });
  check("boundAliasTiers: returns only the non-empty (trimmed) bound aliases", bound1.length, 1);
  check("boundAliasTiers: exact entry for the bound tier", bound1[0].tier, "sonnet");
  check("boundAliasTiers: exact model id", bound1[0].model, "glm-5.1");
  const bound2 = boundAliasTiers({ launch: { aliases: { opus: "o", sonnet: "s", haiku: "h" } } });
  check("boundAliasTiers: multiple bound tiers", bound2.length, 3);

  // ── mergeLocalRoutes (gitignored model-routes.local.json overrides shared) ──
  const mrg1 = mergeLocalRoutes(
    { routes: [{ match: "shared-*", base_url: "shared.test" }] },
    { proxyUrl: "http://127.0.0.1:8787" },
  );
  check("mergeLocalRoutes: local scalar wins", mrg1.proxyUrl, "http://127.0.0.1:8787");
  check("mergeLocalRoutes: shared routes kept when local has none", mrg1.routes[0].match, "shared-*");
  const noLocalMrg = mergeLocalRoutes({ routes: [{ match: "x" }] }, null);
  check("mergeLocalRoutes: null local ⇒ shared routes unchanged", noLocalMrg.routes[0].match, "x");
  check("mergeLocalRoutes: null + null ⇒ null", mergeLocalRoutes(null, null), null);
  const mrg2 = mergeLocalRoutes(
    { routes: [{ match: "shared-*", base_url: "shared" }] },
    { routes: [{ match: "local-*", base_url: "local" }] },
  );
  check("mergeLocalRoutes: routes concatenated (local first)", mrg2.routes.length, 2);
  check("mergeLocalRoutes: local routes come first (first-match-wins)", mrg2.routes[0].match, "local-*");
  check("mergeLocalRoutes: shared routes come second", mrg2.routes[1].match, "shared-*");
  const mrg3 = mergeLocalRoutes(
    { proxyUrl: "https://shared.example", routes: [{ match: "s" }] },
    { proxyUrl: "http://127.0.0.1:8787" },
  );
  check("mergeLocalRoutes: local proxyUrl overrides shared (shallow-override)", mrg3.proxyUrl, "http://127.0.0.1:8787");
  check("mergeLocalRoutes: routes preserved when local has none", mrg3.routes.length, 1);
  const mrg4 = mergeLocalRoutes(null, { proxyUrl: "http://127.0.0.1:8787" });
  check("mergeLocalRoutes: null shared + local proxyUrl ⇒ local only", mrg4.proxyUrl, "http://127.0.0.1:8787");
  check("mergeLocalRoutes: no routes key when both absent", "routes" in mrg4, false);
  const mrg5 = mergeLocalRoutes({}, { routes: [] });
  check("mergeLocalRoutes: empty local routes ⇒ no routes key", "routes" in mrg5, false);

  // ── loadRoutesWithLocal (the shared read-and-merge for routes, like loadConfigWithLocal) ──
  // Writes model-routes.json + its gitignored model-routes.local.json sibling to disk and
  // confirms the helper returns { routesConfig: merged, shared, local }.
  {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-loadroutes-"));
    const routesPath = path.join(dir, "model-routes.json");
    fs.writeFileSync(routesPath, JSON.stringify({ routes: [{ provider: "anthropic" }] }));
    fs.writeFileSync(path.join(dir, "model-routes.local.json"), JSON.stringify({ proxyUrl: "http://127.0.0.1:8787" }));
    const { routesConfig, shared, local } = loadRoutesWithLocal(routesPath);
    check("loadRoutesWithLocal: personal proxyUrl reaches the MERGED config", routesConfig.proxyUrl, "http://127.0.0.1:8787");
    check("loadRoutesWithLocal: shared routes reach the merged config", routesConfig.routes[0].provider, "anthropic");
    check("loadRoutesWithLocal: `shared` is the raw model-routes.json (no proxyUrl)", "proxyUrl" in shared, false);
    check("loadRoutesWithLocal: `local` is the raw override (personal proxyUrl)", local.proxyUrl, "http://127.0.0.1:8787");
    fs.rmSync(dir, { recursive: true, force: true });
  }
  {
    // No model-routes.local sibling ⇒ merged === shared, local null (byte-unchanged path).
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-loadroutes-nolocal-"));
    const routesPath = path.join(dir, "model-routes.json");
    fs.writeFileSync(routesPath, JSON.stringify({ routes: [{ provider: "anthropic" }] }));
    const { routesConfig, local } = loadRoutesWithLocal(routesPath);
    check("loadRoutesWithLocal: no model-routes.local ⇒ shared routes kept", routesConfig.routes[0].provider, "anthropic");
    check("loadRoutesWithLocal: no model-routes.local ⇒ local is null", local, null);
    fs.rmSync(dir, { recursive: true, force: true });
  }

  // ── buildChildEnv ─────────────────────────────────────────────────────────
  const childEnv = buildChildEnv({ FOO: "1", CLAUDE_CODE_SUBAGENT_MODEL: "haiku" }, "http://127.0.0.1:9999");
  check("buildChildEnv sets ANTHROPIC_BASE_URL", childEnv.ANTHROPIC_BASE_URL, "http://127.0.0.1:9999");
  check("buildChildEnv UNSETS CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL" in childEnv, false);
  check("buildChildEnv preserves the rest of the env", childEnv.FOO, "1");
  // buildChildEnv also layers the config-sourced launch-time models.
  const childEnvWithModels = buildChildEnv({ FOO: "1" }, "http://127.0.0.1:9999",
    { launch: { sessionModel: "claude-opus-4-8", guardModel: "claude-haiku-4-6" } });
  check("buildChildEnv layers session model", childEnvWithModels.ANTHROPIC_MODEL, "claude-opus-4-8");
  check("buildChildEnv layers guard model", childEnvWithModels.ANTHROPIC_SMALL_FAST_MODEL, "claude-haiku-4-6");
  // routerUrl null ⇒ no ANTHROPIC_BASE_URL (the direct/external-without-url shape).
  const childEnvNoUrl = buildChildEnv({ FOO: "1" }, null, { launch: { sessionModel: "claude-opus-4-8" } });
  check("buildChildEnv: null routerUrl ⇒ no ANTHROPIC_BASE_URL", "ANTHROPIC_BASE_URL" in childEnvNoUrl, false);
  check("buildChildEnv: null routerUrl still layers the launch model", childEnvNoUrl.ANTHROPIC_MODEL, "claude-opus-4-8");
  // buildChildEnv also layers configDir → CLAUDE_CONFIG_DIR (covers the direct path too).
  const childEnvDir = buildChildEnv({ FOO: "1" }, null, { launch: { configDir: "/home/me/.cc" } });
  check("buildChildEnv: layers CLAUDE_CONFIG_DIR from configDir", childEnvDir.CLAUDE_CONFIG_DIR, "/home/me/.cc");

  // ── env precedence: ANTHROPIC_BASE_URL conflict warning ────────────────────
  // Routing ON + baseEnv already carries a DIFFERENT ANTHROPIC_BASE_URL ⇒ launcher wins
  // (its router URL), but a visible WARNING is emitted naming both URLs (never a silent
  // hijack of the user's own proxy). `warn` is injected to capture the message purely.
  let warned = "";
  const conflictEnv = buildChildEnv(
    { ANTHROPIC_BASE_URL: "http://user-proxy:9000" },
    "http://127.0.0.1:8800",
    {},
    (m) => { warned += m; },
  );
  check("buildChildEnv: launcher wins the ANTHROPIC_BASE_URL conflict", conflictEnv.ANTHROPIC_BASE_URL, "http://127.0.0.1:8800");
  check("buildChildEnv: conflict emits a WARNING", warned.includes("WARNING") && warned.includes("http://user-proxy:9000") && warned.includes("http://127.0.0.1:8800"), true);
  // No conflict (same URL, or none preset) ⇒ NO warning.
  let warnedSame = "";
  buildChildEnv({ ANTHROPIC_BASE_URL: "http://127.0.0.1:8800" }, "http://127.0.0.1:8800", {}, (m) => { warnedSame += m; });
  check("buildChildEnv: identical preset URL ⇒ no warning", warnedSame, "");
  let warnedNone = "";
  buildChildEnv({ FOO: "1" }, "http://127.0.0.1:8800", {}, (m) => { warnedNone += m; });
  check("buildChildEnv: no preset URL ⇒ no warning", warnedNone, "");
  // routing OFF (null routerUrl) ⇒ a preset URL passes through untouched, NO warning.
  let warnedOff = "";
  const offEnv = buildChildEnv({ ANTHROPIC_BASE_URL: "http://user-proxy:9000" }, null, {}, (m) => { warnedOff += m; });
  check("buildChildEnv: routing off ⇒ preset URL passes through", offEnv.ANTHROPIC_BASE_URL, "http://user-proxy:9000");
  check("buildChildEnv: routing off ⇒ no warning", warnedOff, "");

  // ── planLaunch ────────────────────────────────────────────────────────────
  // all-Anthropic seats ⇒ 1 endpoint ⇒ direct (proxy off by default).
  const planDirect = planLaunch({
    config: { roles: { builder: { model: "claude-opus-4-8" }, reviewer: { model: "claude-sonnet-4-6" } } },
    routesConfig: { routes: [{ provider: "anthropic" }, { provider: "deepseek" }] },
    providers: PROVIDERS, env: { ANTHROPIC_API_KEY: "x" },
  });
  check("all-Anthropic seats ⇒ direct (no router)", planDirect.mode, "direct");

  // cross-endpoint seats ⇒ ≥2 endpoints ⇒ router; with the key set, nothing missing.
  const planRouter = planLaunch({
    config: { roles: { builder: { model: "deepseek-chat" }, reviewer: { model: "claude-sonnet-4-6" } } },
    routesConfig: { routes: [{ provider: "anthropic" }, { provider: "deepseek" }] },
    providers: PROVIDERS, env: { ANTHROPIC_API_KEY: "x", DEEPSEEK_API_KEY: "y" },
  });
  check("cross-endpoint seats ⇒ router", planRouter.mode, "router");
  check("router plan: 2 endpoints in play", planRouter.endpoints.length, 2);
  check("router plan: keys present ⇒ none missing", planRouter.missingKeyEnvs.length, 0);

  // same seats but the deepseek key is unset ⇒ fail-closed (surfaced before launch).
  const planFailClosed = planLaunch({
    config: { roles: { builder: { model: "deepseek-chat" }, reviewer: { model: "claude-sonnet-4-6" } } },
    routesConfig: { routes: [{ provider: "anthropic" }, { provider: "deepseek" }] },
    providers: PROVIDERS, env: { ANTHROPIC_API_KEY: "x" },
  });
  check("router plan with unset key ⇒ flagged (fail-closed)", planFailClosed.missingKeyEnvs.includes("DEEPSEEK_API_KEY"), true);

  // anthropic passthrough (keyless) + deepseek ⇒ router, no anthropic key needed.
  const planPassthrough = planLaunch({
    config: { roles: { builder: { model: "deepseek-chat" }, reviewer: { model: "claude-sonnet-4-6" } } },
    routesConfig: { routes: [{ provider: "anthropic", auth: "passthrough" }, { provider: "deepseek" }] },
    providers: PROVIDERS, env: { DEEPSEEK_API_KEY: "y" }, // NO ANTHROPIC_API_KEY
  });
  check("passthrough anthropic + deepseek ⇒ router", planPassthrough.mode, "router");
  check("passthrough means no Anthropic key required", planPassthrough.missingKeyEnvs.length, 0);

  // no routes config ⇒ direct (byte-unchanged default).
  const planNoRoutes = planLaunch({ config: {}, routesConfig: null, providers: PROVIDERS, env: {} });
  check("no routes config ⇒ direct", planNoRoutes.mode, "direct");

  // ── fail-closed: bound alias but no routes (the bug repro) ────────────────
  // A bound tier alias with nothing configured to route it ⇒ error before launch.
  const planBoundNoRoutes = planLaunch({
    config: { launch: { aliases: { sonnet: "glm-5.1" } } },
    routesConfig: null,
    providers: PROVIDERS, env: {},
  });
  check("bound alias + no routes ⇒ error (fail-closed)", typeof planBoundNoRoutes.error === "string" && planBoundNoRoutes.error.includes("glm-5.1"), true);
  check("bound alias + no routes ⇒ error names the tier", planBoundNoRoutes.error.includes("sonnet"), true);
  check("bound alias + no routes ⇒ error names both file-scoped fixes", planBoundNoRoutes.error.includes("model-routes.json") && planBoundNoRoutes.error.includes("model-routes.local.json"), true);
  // Same with present-but-empty routes array.
  const planBoundEmptyRoutes = planLaunch({
    config: { launch: { aliases: { sonnet: "glm-5.1" } } },
    routesConfig: { routes: [] },
    providers: PROVIDERS, env: {},
  });
  check("bound alias + empty routes ⇒ error (fail-closed)", typeof planBoundEmptyRoutes.error === "string", true);
  // Two bound tiers with no routes ⇒ error names both.
  const planTwoBound = planLaunch({
    config: { launch: { aliases: { sonnet: "glm-5.1", haiku: "deepseek-chat" } } },
    routesConfig: null,
    providers: PROVIDERS, env: {},
  });
  check("two bound aliases + no routes ⇒ error names both pairs", planTwoBound.error.includes("sonnet → glm-5.1") && planTwoBound.error.includes("haiku → deepseek-chat"), true);
  // Aliases present but ALL blank/whitespace ⇒ still direct, still error-null (the regression guard).
  const planBlankAliases = planLaunch({
    config: { launch: { aliases: { sonnet: "   ", haiku: "" } } },
    routesConfig: null,
    providers: PROVIDERS, env: {},
  });
  check("no routes + all-blank aliases ⇒ still direct + error-null", planBlankAliases.mode, "direct");
  check("no routes + all-blank aliases ⇒ error is null (no bound tiers)", planBlankAliases.error, null);

  // malformed routes (unknown provider) ⇒ error, not a crash.
  const planErr = planLaunch({ config: {}, routesConfig: { routes: [{ provider: "ghost" }] }, providers: PROVIDERS, env: {} });
  check("unknown provider in routes ⇒ error surfaced", typeof planErr.error === "string" && planErr.error.includes("ghost"), true);

  // ── external proxy (opt-in proxyUrl) ───────────────────────────────────────
  // A valid proxyUrl ⇒ external mode, carried through; takes precedence over the
  // endpoint-count decision (cross-endpoint seats would otherwise be "router").
  const planExternal = planLaunch({
    config: { roles: { builder: { model: "deepseek-chat" }, reviewer: { model: "claude-sonnet-4-6" } } },
    routesConfig: { proxyUrl: "http://127.0.0.1:8800", routes: [{ provider: "anthropic" }, { provider: "deepseek" }] },
    providers: PROVIDERS, env: {}, // NO keys — external proxy owns auth
  });
  check("proxyUrl ⇒ external mode", planExternal.mode, "external");
  check("external mode carries the proxyUrl", planExternal.proxyUrl, "http://127.0.0.1:8800");
  check("external mode does not fail-closed on missing keys", planExternal.missingKeyEnvs.length, 0);
  check("external mode takes precedence over router", planExternal.error, null);

  // A present-but-malformed proxyUrl ⇒ hard error (fail-closed), never a silent spawn.
  const planBadProxy = planLaunch({ config: {}, routesConfig: { proxyUrl: "not a url" }, providers: PROVIDERS, env: {} });
  check("malformed proxyUrl ⇒ error surfaced", typeof planBadProxy.error === "string" && planBadProxy.error.includes("proxyUrl"), true);
  check("malformed proxyUrl ⇒ not external mode", planBadProxy.mode !== "external", true);

  // A non-http scheme is rejected too (only http/https proxies).
  const planFileProxy = planLaunch({ config: {}, routesConfig: { proxyUrl: "file:///etc/passwd" }, providers: PROVIDERS, env: {} });
  check("non-http proxyUrl ⇒ error", typeof planFileProxy.error === "string", true);

  // Empty/whitespace proxyUrl ⇒ ignored, normal decision proceeds (here: no routes ⇒ direct).
  const planBlankProxy = planLaunch({ config: {}, routesConfig: { proxyUrl: "   ", routes: [] }, providers: PROVIDERS, env: {} });
  check("blank proxyUrl ⇒ ignored (falls through to normal)", planBlankProxy.mode, "direct");
  check("blank proxyUrl ⇒ no error", planBlankProxy.error, null);

  // ── probeCandidates (setup-dialog discovery: ordered, deduped, validated) ──
  // Explicit url arg first, then routes-config proxyUrl, then the conventional default.
  const cand1 = probeCandidates({
    argUrl: "http://localhost:9000",
    routesConfig: { proxyUrl: "http://127.0.0.1:8800" },
  });
  check("probeCandidates: arg url first", cand1[0], "http://localhost:9000");
  check("probeCandidates: proxyUrl second", cand1[1], "http://127.0.0.1:8800");
  check("probeCandidates: conventional default last", cand1[cand1.length - 1], `http://127.0.0.1:${DEFAULT_PROXY_PORT}`);
  // No arg, no proxyUrl ⇒ just the conventional default.
  const cand2 = probeCandidates({});
  check("probeCandidates: empty ⇒ only the default", cand2.length, 1);
  check("probeCandidates: default origin", cand2[0], `http://127.0.0.1:${DEFAULT_PROXY_PORT}`);
  // Origin-normalised (path/trailing slash dropped) and de-duplicated against the default.
  const cand3 = probeCandidates({ argUrl: `http://127.0.0.1:${DEFAULT_PROXY_PORT}/v1/` });
  check("probeCandidates: normalises to origin + dedupes the default", cand3.length, 1);
  check("probeCandidates: normalised origin", cand3[0], `http://127.0.0.1:${DEFAULT_PROXY_PORT}`);
  // Invalid / non-http entries are dropped, never thrown.
  const cand4 = probeCandidates({ argUrl: "not a url", routesConfig: { proxyUrl: "file:///etc/passwd" } });
  check("probeCandidates: drops invalid arg + non-http proxyUrl", cand4.length, 1);
  check("probeCandidates: falls back to the default", cand4[0], `http://127.0.0.1:${DEFAULT_PROXY_PORT}`);

  // ── parseModelsResponse (OpenAI/modelpipe shape + bare array, fail-safe) ───
  const pm1 = parseModelsResponse(JSON.stringify({ object: "list", data: [{ id: "deepseek-chat" }, { id: "claude-*" }] }));
  check("parseModelsResponse: OpenAI shape ok", pm1.ok, true);
  check("parseModelsResponse: extracts data[].id", pm1.models.join(","), "deepseek-chat,claude-*");
  const pm2 = parseModelsResponse(JSON.stringify(["a", "b", "a"]));
  check("parseModelsResponse: bare string array", pm2.models.join(","), "a,b");
  check("parseModelsResponse: dedupes ids", pm2.models.length, 2);
  const pm3 = parseModelsResponse("not json");
  check("parseModelsResponse: unparseable ⇒ ok:false", pm3.ok, false);
  check("parseModelsResponse: unparseable ⇒ empty models", pm3.models.length, 0);
  const pm4 = parseModelsResponse(JSON.stringify({ foo: 1 }));
  check("parseModelsResponse: unexpected shape ⇒ ok:false", pm4.ok, false);
  const pm5 = parseModelsResponse(JSON.stringify({ data: [{ id: "x" }, { noid: 1 }, "y", ""] }));
  check("parseModelsResponse: skips entries with no string id (and blanks)", pm5.models.join(","), "x,y");

  // ── the REAL shipped catalog (pins the verified facts) ─────────────────────
  const real = loadProviders();
  const byId = Object.fromEntries(real.map((p) => [p.id, p]));
  check("catalog has the four verified providers",
    ["anthropic", "deepseek", "glm", "openrouter"].every((id) => byId[id]), true);
  check("catalog anthropic supports passthrough", byId.anthropic.supports_passthrough, true);
  check("catalog anthropic base_url", byId.anthropic.base_url, "https://api.anthropic.com");
  check("catalog deepseek is x-api-key", byId.deepseek.auth.header, "x-api-key");
  check("catalog deepseek base_url", byId.deepseek.base_url, "https://api.deepseek.com/anthropic");
  check("catalog glm is Authorization Bearer", `${byId.glm.auth.header} ${byId.glm.auth.scheme}`, "Authorization Bearer");
  check("catalog glm base_url (z.ai Coding Plan)", byId.glm.base_url, "https://api.z.ai/api/anthropic");
  check("catalog glm matches GLM-*", byId.glm.models.includes("GLM-*"), true);
  check("catalog openrouter is Authorization Bearer", `${byId.openrouter.auth.header} ${byId.openrouter.auth.scheme}`, "Authorization Bearer");
  check("catalog openrouter base_url (path-append → /api/v1/messages)", byId.openrouter.base_url, "https://openrouter.ai/api");
  // every real provider resolves cleanly through resolveRoute (no dead/invalid entry).
  check("every catalog provider resolves",
    real.every((p) => resolveRoute({ provider: p.id }, real)[0].base_url === p.base_url), true);

  if (fails.length) {
    console.log("ROUTER-LAUNCH SELF-TEST:");
    fails.forEach((f) => console.log(f));
    console.log(`\nFAIL — ${fails.length} case(s) failed`);
    process.exit(1);
  }
  console.log(`PASS — ${pass} passed`);
}

main();
