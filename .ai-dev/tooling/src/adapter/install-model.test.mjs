// install-model bake test — the per-platform model-line bake contract, covering BOTH
// platforms because the two install-agents.mjs export a same-named resolveModelPin that
// differs precisely because the platforms differ:
//
// OPENCODE — NEVER bakes a reviewer `model:` line. The `task` runtime parses a subagent's
// `model:` frontmatter but does NOT apply it at execution (open upstream bugs #21632 /
// #17870 / #18615, no fix through our 1.17.7 — research:
// docs/decisions/opencode-task-capabilities.md Q1), so baking a concrete pin would claim a
// cross-model reviewer the runtime silently swallows. EVERY input — a concrete
// `provider/model` string, the per-platform `{opencode: …}` form, `auto`, `session`, or
// absent — resolves to NO `model:` line; the subagent inherits the session model, no false
// claim of cross-model independence.
//
// CLAUDE — DOES bake, because its `task` runtime honours a baked subagent `model:` (probe-
// verified; without the line a Claude subagent inherits the session model and the reviewer
// reviews under itself — the bug this path fixes). Resolved against the Claude model policy
// (tool-map.json `models.claude`, allow-list opus/sonnet/haiku): a concrete allow-listed pin
// (`opus`/`sonnet`/`haiku` or a `claude-<alias>-*` id) bakes that model; `session`/absent and
// an off-allowlist id bake NO line (honest inherit; never invent a model). `auto` bakes the
// model OPPOSITE the session — opus↔sonnet, never haiku — but ONLY in the VANILLA state (no
// concrete seat pin and no launch model anywhere); in a CUSTOMIZED config a reviewer `auto`
// (or absent ⇒ auto) degrades to `session` ⇒ NO line, because the opus↔sonnet guess is a
// fiction once the Operator makes any explicit model decision.
//
// (The WHY a cross-model reviewer is unavailable on OpenCode is documented for the Operator
// in orchestrator.md `## Your seat`, tool-map.json `models.opencode._note`, and the research
// doc above — not in the install log.) A regression that bakes on OpenCode, or stops baking
// on Claude, fails loudly here.
//
// Run: node src/adapter/install-model.test.mjs

import { install, resolveModelPin } from "./opencode/install-agents.mjs";
import {
  install as installClaude,
  resolveModelPin as resolveClaudePin,
  isVanilla,
  autoHonored,
  runtimeModelEnvSet,
  loadClaudeModelPolicy,
} from "./claude/install-agents.mjs";
import { checkRouting, verifyRoutingConsistency, checkCrossModelIndependence, mergeLaunchEnv } from "./install-claude.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

let pass = 0, fail = 0;
function check(name, cond) {
  if (cond) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}`); }
}

// The ENV dimension of `auto`-honor (install-agents.mjs runtimeModelEnvSet): a proxy
// (ANTHROPIC_BASE_URL) or a foreign tier alias (ANTHROPIC_DEFAULT_*_MODEL) in process.env
// makes auto's opus↔sonnet baked concrete id routeless on a foreign-only proxy (#356) ⇒
// auto degrades to session. The HOST env (a proxied dev session) may carry these vars, so
// every auto-honor case runs under an explicit env snapshot — `sets` names the keys to SET;
// every other model-routing key is DELETED for the call (a controlled baseline), then restored.
const AUTO_ENV_KEYS = ["ANTHROPIC_BASE_URL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"];
function withEnv(sets, fn) {
  const saved = {};
  for (const k of AUTO_ENV_KEYS) saved[k] = process.env[k];
  try {
    for (const k of AUTO_ENV_KEYS) {
      if (sets && Object.prototype.hasOwnProperty.call(sets, k) && sets[k] != null) process.env[k] = sets[k];
      else delete process.env[k];
    }
    return fn();
  } finally {
    for (const k of AUTO_ENV_KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  }
}

// 1. resolveModelPin — on OpenCode ALWAYS null (the runtime ignores subagent
//    model: pins), across every input shape.
check("no-pin: a concrete bare provider/model string is dropped", resolveModelPin("deepseek/deepseek-chat") === null);
check("no-pin: a per-platform {opencode} pin is dropped", resolveModelPin({ opencode: "deepseek/deepseek-chat" }) === null);
check("no-pin: 'auto' resolves to null", resolveModelPin("auto") === null);
check("no-pin: 'session' resolves to null", resolveModelPin("session") === null);
check("no-pin: absent (undefined) resolves to null", resolveModelPin(undefined) === null);
check("no-pin: a {claude}-only pin gives no opencode id", resolveModelPin({ claude: "opus" }) === null);

// A config skeleton with the role agent ids install() requires (orchestrator + planner + builder; reviewer added per-case).
const baseRoles = {
  orchestrator: { agent: "ai-dev" },
  planner: { agent: "dev-planner" },
  builder: { agent: "dev-builder" },
};

function reviewerFrontmatter(reviewerModel) {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-bake-"));
  const config = { roles: { ...baseRoles, reviewer: { agent: "dev-reviewer", ...(reviewerModel !== undefined ? { model: reviewerModel } : {}) } } };
  const written = install(outDir, config);
  const text = fs.readFileSync(written["dev-reviewer"], "utf8");
  const fm = text.split("\n---")[0]; // the frontmatter block, before the body
  fs.rmSync(outDir, { recursive: true, force: true });
  return fm;
}

// 2. end-to-end: a concrete pin emits NO model line — the runtime ignores it, so
//    the install must not claim it (the honesty case this change locks).
check("no-bake: a concrete {opencode} pin emits no model line", !/^model:/m.test(reviewerFrontmatter({ opencode: "deepseek/deepseek-chat" })));
check("no-bake: a concrete bare-string pin emits no model line", !/^model:/m.test(reviewerFrontmatter("deepseek/deepseek-reasoner")));

// 3. end-to-end: auto / session / absent emit NO model line (already honest).
check("no-bake: 'auto' emits no model line", !/^model:/m.test(reviewerFrontmatter("auto")));
check("no-bake: 'session' emits no model line", !/^model:/m.test(reviewerFrontmatter("session")));
check("no-bake: absent model emits no model line", !/^model:/m.test(reviewerFrontmatter(undefined)));

// ───────────────────────── Claude — DOES bake (additive) ─────────────────────────
// 4. resolveModelPin (Claude) — the real resolution against the live tool-map policy.
//    `auto` with a not-knowable session (orchestrator absent) → the opus-class default sonnet.
check("claude-pin: 'auto' (session not knowable) → claude-sonnet-4-6", resolveClaudePin("auto", undefined) === "claude-sonnet-4-6");
check("claude-pin: 'auto' opposite an opus session → claude-sonnet-4-6", resolveClaudePin("auto", "opus") === "claude-sonnet-4-6");
check("claude-pin: 'auto' opposite a sonnet session → claude-opus-4-8", resolveClaudePin("auto", "sonnet") === "claude-opus-4-8");
check("claude-pin: 'auto' opposite a claude-opus-* session id → claude-sonnet-4-6", resolveClaudePin("auto", "claude-opus-4-8") === "claude-sonnet-4-6");
// A1 — the full-id form of the sonnet session (the mirror of the line above); the
// 5.34.0 Reviewer named this edge as missing.
check("claude-pin: 'auto' opposite a claude-sonnet-* session id → claude-opus-4-8", resolveClaudePin("auto", "claude-sonnet-4-6") === "claude-opus-4-8");
check("claude-pin: alias 'sonnet' → canonical id claude-sonnet-4-6", resolveClaudePin("sonnet", undefined) === "claude-sonnet-4-6");
check("claude-pin: alias 'opus' → canonical id claude-opus-4-8", resolveClaudePin("opus", undefined) === "claude-opus-4-8");
check("claude-pin: a concrete claude-* id bakes verbatim", resolveClaudePin("claude-opus-4-8", undefined) === "claude-opus-4-8");
// haiku is now allow-listed (tool-map.json), so it BAKES like any other alias:
check("claude-pin: alias 'haiku' → canonical id claude-haiku-4-5", resolveClaudePin("haiku", undefined) === "claude-haiku-4-5");
check("claude-pin: a concrete claude-haiku-* id bakes verbatim", resolveClaudePin("claude-haiku-4-5", undefined) === "claude-haiku-4-5");
check("claude-pin: 'session' → null (honest inherit)", resolveClaudePin("session", undefined) === null);
check("claude-pin: absent (undefined) → null", resolveClaudePin(undefined, undefined) === null);
check("claude-pin: 'auto' never resolves to haiku (opposite stays opus↔sonnet)", resolveClaudePin("auto", "opus") !== "claude-haiku-4-5");
check("claude-pin: an off-allowlist id → null (never invent)", resolveClaudePin("claude-fictional-9-9", undefined) === null);
check("claude-pin: a non-claude pin → null", resolveClaudePin("deepseek/deepseek-chat", undefined) === null);

// 4b. BARE ALIAS vs CONCRETE ID — the cross-endpoint bake mechanic (papercut 13). A bare
//     tier alias whose tier is BOUND FOREIGN (config.launch.aliases[tier] set) bakes the
//     BARE ALIAS (so Claude resolves it through ANTHROPIC_DEFAULT_<TIER>_MODEL → foreign);
//     an UNBOUND tier bakes the CONCRETE id (native passthrough). The 4th boundTiers arg
//     defaults to {} — every 2-/3-arg call above is byte-identical to before it existed.
const policy = loadClaudeModelPolicy();
check("claude-pin: bare 'opus' with opus BOUND foreign → bare alias 'opus'", resolveClaudePin("opus", undefined, policy, { opus: "glm-4.6" }) === "opus");
check("claude-pin: bare 'sonnet' with sonnet BOUND foreign → bare alias 'sonnet'", resolveClaudePin("sonnet", undefined, policy, { sonnet: "glm-5.2" }) === "sonnet");
check("claude-pin: bare 'haiku' with haiku BOUND foreign → bare alias 'haiku'", resolveClaudePin("haiku", undefined, policy, { haiku: "deepseek-v4-pro" }) === "haiku");
check("claude-pin: bare 'sonnet' with only a DIFFERENT tier bound → concrete claude-sonnet-4-6", resolveClaudePin("sonnet", undefined, policy, { haiku: "deepseek-v4-pro" }) === "claude-sonnet-4-6");
check("claude-pin: bare 'opus' with an EMPTY opus binding → concrete claude-opus-4-8 (empty = not bound)", resolveClaudePin("opus", undefined, policy, { opus: "  " }) === "claude-opus-4-8");
check("claude-pin: a CONCRETE claude-opus-* id stays verbatim even with opus BOUND (explicit native pick)", resolveClaudePin("claude-opus-4-8", undefined, policy, { opus: "glm-4.6" }) === "claude-opus-4-8");
check("claude-pin: bare 'opus' with NO boundTiers arg → concrete (backward-compatible default)", resolveClaudePin("opus", undefined, policy) === "claude-opus-4-8");

// 4c. FABLE — the 4th remappable tier (papercut 14, strongest→weakest fable·opus·sonnet·haiku).
//     Same bare-alias-vs-concrete mechanic; resolves for free because aliasOf loops the whole
//     allow-list once fable is in tool-map.json `models.claude` (allow + ids).
check("claude-policy: allow-list includes fable (4 tiers)", policy.allow.includes("fable") && policy.allow.length === 4);
check("claude-policy: ids.fable → claude-fable-5", policy.ids.fable === "claude-fable-5");
check("claude-pin: alias 'fable' → canonical id claude-fable-5", resolveClaudePin("fable", undefined) === "claude-fable-5");
check("claude-pin: a concrete claude-fable-* id bakes verbatim", resolveClaudePin("claude-fable-5", undefined) === "claude-fable-5");
check("claude-pin: bare 'fable' with fable BOUND foreign → bare alias 'fable'", resolveClaudePin("fable", undefined, policy, { fable: "glm-4.6" }) === "fable");
check("claude-pin: bare 'fable' with only a DIFFERENT tier bound → concrete claude-fable-5 (native)", resolveClaudePin("fable", undefined, policy, { opus: "glm-4.6" }) === "claude-fable-5");
check("claude-pin: 'auto' never resolves to fable (opposite stays opus↔sonnet)", resolveClaudePin("auto", "opus") !== "claude-fable-5" && resolveClaudePin("auto", "sonnet") !== "claude-fable-5");

// 5. end-to-end: assert the REAL assembled Claude reviewer frontmatter carries (or omits)
//    the right `model:` line. Claude's install() also writes the orchestrator load surface
//    to outDir's PARENT, so a tmp dir keeps it isolated. `sessionModel` sets the
//    orchestrator's wish so the `auto` opposite is exercised. `opts` injects what moves the
//    config OUT of the vanilla state: `builderModel` (a concrete builder pin) and/or
//    `launch` (a launch-time model block) — both default unset, so the helper builds a
//    VANILLA config unless an opt is passed (the existing vanilla cases stay unchanged).
function claudeReviewerFrontmatter(reviewerModel, sessionModel, opts = {}) {
  // outDir nested under a tmp parent: Claude's install writes the orchestrator surface to
  // dirname(outDir), so the parent (not /tmp) is what gets cleaned up.
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-claude-bake-"));
  const outDir = path.join(tmp, "agents");
  const config = {
    roles: {
      orchestrator: { agent: "ai-dev", ...(sessionModel !== undefined ? { model: sessionModel } : {}) },
      planner: { agent: "dev-planner" },
      builder: { agent: "dev-builder", ...(opts.builderModel !== undefined ? { model: opts.builderModel } : {}) },
      reviewer: { agent: "dev-reviewer", ...(reviewerModel !== undefined ? { model: reviewerModel } : {}) },
    },
    ...(opts.launch !== undefined ? { launch: opts.launch } : {}),
  };
  const written = installClaude(outDir, config);
  const text = fs.readFileSync(written["dev-reviewer"], "utf8");
  const fm = text.split("\n---")[0]; // the frontmatter block, before the body
  fs.rmSync(tmp, { recursive: true, force: true });
  return fm;
}

// `auto`'s opus↔sonnet OPPOSITE logic is unit-tested directly above (resolveClaudePin) and
// stays unchanged. END-TO-END through install(), an orchestrator pin (sessionModel) is now a
// CONCRETE model decision ⇒ the config is CUSTOMIZED ⇒ a reviewer `auto` degrades to session
// ⇒ NO line (the new vanilla-only-auto spec). So these formerly-baked e2e cases now bake NO
// line — the opposite resolution they once exercised is covered by the pure-resolver tests.
check("claude-bake: 'auto' with an opus orchestrator pin (customized) bakes NO line", !/^model:/m.test(claudeReviewerFrontmatter("auto", "opus")));
check("claude-bake: 'auto' with a sonnet orchestrator pin (customized) bakes NO line", !/^model:/m.test(claudeReviewerFrontmatter("auto", "sonnet")));
// VANILLA (no orchestrator pin, no other decision): `auto` still bakes the sonnet default.
check("claude-bake: 'auto' in a vanilla config (clean env) bakes the sonnet default", withEnv({}, () => /^model: claude-sonnet-4-6$/m.test(claudeReviewerFrontmatter("auto", undefined))));
check("claude-bake: a concrete allow-listed pin (sonnet) bakes model: claude-sonnet-4-6", /^model: claude-sonnet-4-6$/m.test(claudeReviewerFrontmatter("sonnet", undefined)));
check("claude-bake: a concrete claude-* id bakes that line verbatim", /^model: claude-opus-4-8$/m.test(claudeReviewerFrontmatter("claude-opus-4-8", undefined)));
check("claude-bake: 'session' bakes NO model line", !/^model:/m.test(claudeReviewerFrontmatter("session", undefined)));
// A2 — an ABSENT reviewer model defaults to `auto` (cross-model-review.md: absent ⇒
// auto), so a no-pin reviewer still cross-models. This REPLACES the pre-A2 expectation
// (absent ⇒ no line): the reviewer-defaults-to-auto contract clause the 5.34.0 Reviewer
// flagged. The pure resolver still returns null for absent (asserted above) — the
// default is applied at the reviewer seat in install(), not in resolveModelPin.
check("claude-bake: absent reviewer (defaults to auto) in a vanilla config (clean env) bakes the sonnet default", withEnv({}, () => /^model: claude-sonnet-4-6$/m.test(claudeReviewerFrontmatter(undefined, undefined))));
// An orchestrator pin makes the config customized, so the absent⇒auto reviewer degrades to session ⇒ NO line:
check("claude-bake: absent reviewer WITH a sonnet orchestrator pin (customized) bakes NO line", !/^model:/m.test(claudeReviewerFrontmatter(undefined, "sonnet")));
// haiku is now allow-listed → a reviewer pinned haiku BAKES (flips the old off-allowlist case):
check("claude-bake: a reviewer pinned 'haiku' bakes model: claude-haiku-4-5", /^model: claude-haiku-4-5$/m.test(claudeReviewerFrontmatter("haiku", undefined)));
check("claude-bake: a reviewer pinned a claude-haiku-* id bakes that line verbatim", /^model: claude-haiku-4-5$/m.test(claudeReviewerFrontmatter("claude-haiku-4-5", undefined)));
check("claude-bake: a still-off-allowlist id bakes NO model line", !/^model:/m.test(claudeReviewerFrontmatter("claude-fictional-9-9", undefined)));

// ───────── PLANNER seat (docs/decisions/planning-model-split.md) — no special default ─────────
// The planner takes NO auto default (unlike the reviewer): absent ⇒ session inherit (no
// line), so a no-pin planner runs on the strong session model by construction. A concrete
// strong pin bakes verbatim (a project that wants the planner off-session). OpenCode bakes
// nothing (its task runtime ignores subagent model:). This is the seat's new-path coverage.
function claudePlannerFrontmatter(plannerModel, sessionModel) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-planner-bake-"));
  const outDir = path.join(tmp, "agents");
  const config = {
    roles: {
      orchestrator: { agent: "ai-dev", ...(sessionModel !== undefined ? { model: sessionModel } : {}) },
      planner: { agent: "dev-planner", ...(plannerModel !== undefined ? { model: plannerModel } : {}) },
      builder: { agent: "dev-builder" },
      reviewer: { agent: "dev-reviewer" },
    },
  };
  const written = installClaude(outDir, config);
  const fm = fs.readFileSync(written["dev-planner"], "utf8").split("\n---")[0];
  fs.rmSync(tmp, { recursive: true, force: true });
  return fm;
}
check("claude-bake: absent planner bakes NO line (session-strong inherit, never auto)", !/^model:/m.test(claudePlannerFrontmatter(undefined, undefined)));
check("claude-bake: absent planner even in a CUSTOMIZED config bakes NO line (no auto default)", !/^model:/m.test(claudePlannerFrontmatter(undefined, "sonnet")));
check("claude-bake: a strong planner pin (opus) bakes model: claude-opus-4-8", /^model: claude-opus-4-8$/m.test(claudePlannerFrontmatter("opus", undefined)));
// OpenCode: even a concrete strong planner pin bakes no line (its task runtime ignores it).
{
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-oc-planner-"));
  const written = install(outDir, { roles: { ...baseRoles, planner: { agent: "dev-planner", model: "opus" }, reviewer: { agent: "dev-reviewer" } } });
  const fm = fs.readFileSync(written["dev-planner"], "utf8").split("\n---")[0];
  fs.rmSync(outDir, { recursive: true, force: true });
  check("opencode-bake: a concrete planner pin emits no model line (runtime ignores it)", !/^model:/m.test(fm));
}

// ───────── 6. isVanilla + the customized-state `auto` degrade (the new spec) ─────────
// isVanilla — `session`/`auto`/absent are NOT explicit decisions (stay vanilla); a concrete
// seat pin OR a non-empty launch model is (customized).
check("isVanilla: bare config (no models) → vanilla", isVanilla({ roles: { orchestrator: { agent: "ai-dev" }, builder: { agent: "b" }, reviewer: { agent: "r" } } }) === true);
check("isVanilla: reviewer 'auto' alone → still vanilla", isVanilla({ roles: { reviewer: { agent: "r", model: "auto" } } }) === true);
check("isVanilla: a 'session' seat → still vanilla", isVanilla({ roles: { builder: { agent: "b", model: "session" } } }) === true);
check("isVanilla: empty/whitespace launch models → still vanilla", isVanilla({ launch: { sessionModel: "", guardModel: "  " } }) === true);
check("isVanilla: a concrete builder pin → customized", isVanilla({ roles: { builder: { agent: "b", model: "sonnet" } } }) === false);
check("isVanilla: a concrete orchestrator pin → customized", isVanilla({ roles: { orchestrator: { agent: "ai-dev", model: "opus" } } }) === false);
check("isVanilla: a non-empty launch.sessionModel → customized", isVanilla({ launch: { sessionModel: "deepseek-chat" } }) === false);
check("isVanilla: a non-empty launch.guardModel → customized", isVanilla({ launch: { guardModel: "claude-haiku-4-5" } }) === false);
// A tier-alias binding is an explicit cross-endpoint decision ⇒ customized (auto no longer honest).
check("isVanilla: a launch.aliases tier binding → customized", isVanilla({ launch: { aliases: { sonnet: "glm-4.6" } } }) === false);
check("isVanilla: empty/whitespace alias tiers → still vanilla", isVanilla({ launch: { aliases: { sonnet: "", haiku: "  " } } }) === true);
check("isVanilla: non-object aliases → still vanilla (fail-safe, no crash)", isVanilla({ launch: { aliases: "nope" } }) === true);

// end-to-end: in a CUSTOMIZED config, a reviewer `auto` / absent ⇒ degrades to session ⇒ NO line.
check("claude-bake: reviewer 'auto' WITH a concrete builder pin (customized) bakes NO line", !/^model:/m.test(claudeReviewerFrontmatter("auto", undefined, { builderModel: "sonnet" })));
check("claude-bake: ABSENT reviewer WITH a concrete builder pin (customized) bakes NO line", !/^model:/m.test(claudeReviewerFrontmatter(undefined, undefined, { builderModel: "sonnet" })));
check("claude-bake: reviewer 'auto' WITH a launch model (customized) bakes NO line", !/^model:/m.test(claudeReviewerFrontmatter("auto", undefined, { launch: { sessionModel: "deepseek-chat" } })));
check("claude-bake: ABSENT reviewer WITH a launch model (customized) bakes NO line", !/^model:/m.test(claudeReviewerFrontmatter(undefined, undefined, { launch: { guardModel: "claude-haiku-4-5" } })));
// but a CONCRETE reviewer pin in a customized config still bakes (the explicit choice is honored):
check("claude-bake: a concrete reviewer pin in a customized config still bakes", /^model: claude-opus-4-8$/m.test(claudeReviewerFrontmatter("opus", undefined, { builderModel: "sonnet" })));

// ───────── 6b. the ENV dimension of `auto`-honor (#356) ─────────
// A proxied-via-env project (config-vanilla, but ANTHROPIC_BASE_URL or an
// ANTHROPIC_DEFAULT_*_MODEL set in the runtime env) is CUSTOMIZED at the env layer: the
// opus↔sonnet opposite bakes a CONCRETE id that bypasses the alias env, so on a foreign-only
// proxy it is routeless ⇒ spawn 400. `auto` must DEGRADE to session (no line) there — the
// documented "never an error" fallback. isVanilla is the config half; runtimeModelEnvSet the
// env half; autoHonored the join. Each case runs under a controlled env (the host may be proxied).
// A proxy URL alone ⇒ degrade (the concrete id's route through it is unverified):
check("claude-bake: 'auto' in a vanilla config WITH ANTHROPIC_BASE_URL set bakes NO line (#356)", withEnv({ ANTHROPIC_BASE_URL: "http://localhost:1234" }, () => !/^model:/m.test(claudeReviewerFrontmatter("auto", undefined))));
// A foreign tier alias (the env form of launch.aliases) ⇒ degrade (the tier semantics are customized):
check("claude-bake: 'auto' in a vanilla config WITH ANTHROPIC_DEFAULT_SONNET_MODEL set bakes NO line (#356)", withEnv({ ANTHROPIC_DEFAULT_SONNET_MODEL: "glm-4.7" }, () => !/^model:/m.test(claudeReviewerFrontmatter("auto", undefined))));
// An ABSENT reviewer (⇒ auto) under a proxied env ⇒ degrade too (absent⇒auto⇒session):
check("claude-bake: ABSENT reviewer (⇒ auto) WITH ANTHROPIC_BASE_URL set bakes NO line (#356)", withEnv({ ANTHROPIC_BASE_URL: "http://localhost:1234" }, () => !/^model:/m.test(claudeReviewerFrontmatter(undefined, undefined))));
// A CONCRETE reviewer pin under a proxied env STILL bakes (the explicit choice is honored, env-independent):
check("claude-bake: a concrete reviewer pin WITH ANTHROPIC_BASE_URL set still bakes", withEnv({ ANTHROPIC_BASE_URL: "http://localhost:1234" }, () => /^model: claude-sonnet-4-6$/m.test(claudeReviewerFrontmatter("sonnet", undefined))));
// The opus↔sonnet opposite is STILL baked when env is clean (regression guard for the fix's other arm):
check("claude-bake: 'auto' in vanilla WITH env clean bakes the sonnet default (regression)", withEnv({}, () => /^model: claude-sonnet-4-6$/m.test(claudeReviewerFrontmatter("auto", undefined))));

// runtimeModelEnvSet / autoHonored unit tests (the pure predicates; explicit env, no host leak):
check("runtimeModelEnvSet: clean env → false", runtimeModelEnvSet({}) === false);
check("runtimeModelEnvSet: ANTHROPIC_BASE_URL set → true", runtimeModelEnvSet({ ANTHROPIC_BASE_URL: "http://x" }) === true);
check("runtimeModelEnvSet: ANTHROPIC_BASE_URL blank → false", runtimeModelEnvSet({ ANTHROPIC_BASE_URL: "   " }) === false);
check("runtimeModelEnvSet: ANTHROPIC_DEFAULT_OPUS_MODEL set → true", runtimeModelEnvSet({ ANTHROPIC_DEFAULT_OPUS_MODEL: "glm-5.2" }) === true);
check("runtimeModelEnvSet: ANTHROPIC_DEFAULT_HAIKU_MODEL set → true", runtimeModelEnvSet({ ANTHROPIC_DEFAULT_HAIKU_MODEL: "glm-4.5-air" }) === true);
check("runtimeModelEnvSet: a non-routing env var ignored → false", runtimeModelEnvSet({ ANTHROPIC_MODEL: "claude-opus-4-8" }) === false);
const vanillaCfg = { roles: { orchestrator: { agent: "ai-dev" }, builder: { agent: "b" }, reviewer: { agent: "r" } } };
check("autoHonored: vanilla config + clean env → true", autoHonored(vanillaCfg, {}) === true);
check("autoHonored: vanilla config + BASE_URL set → false (#356)", autoHonored(vanillaCfg, { ANTHROPIC_BASE_URL: "http://x" }) === false);
check("autoHonored: vanilla config + DEFAULT_SONNET set → false", autoHonored(vanillaCfg, { ANTHROPIC_DEFAULT_SONNET_MODEL: "glm-4.7" }) === false);
check("autoHonored: customized config + clean env → false (config half)", autoHonored({ roles: { builder: { agent: "b", model: "sonnet" } } }, {}) === false);
check("autoHonored: customized config + BASE_URL set → false (both halves)", autoHonored({ roles: { builder: { agent: "b", model: "sonnet" } } }, { ANTHROPIC_BASE_URL: "http://x" }) === false);

// ───────── 7. cross-endpoint bake: bare-alias-for-bound-tier, end-to-end (papercut 13) ─────────
// The worked example (docs/decisions/multi-model-setup-ux.md papercut 13; the plan): a seat
// on a FOREIGN-bound tier bakes the BARE alias (routes through ANTHROPIC_DEFAULT_*); a seat
// on an UNBOUND (native) tier bakes the CONCRETE id. Assert both the builder and reviewer
// assembled frontmatter in ONE config so the seat-agnostic loop is covered on both seats.
function claudeSeatFrontmatter(agentKey, roleModels, launch) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-claude-xseat-"));
  const outDir = path.join(tmp, "agents");
  const config = {
    roles: {
      orchestrator: { agent: "ai-dev" },
      planner: { agent: "dev-planner" },
      builder: { agent: "dev-builder", ...(roleModels.builder !== undefined ? { model: roleModels.builder } : {}) },
      reviewer: { agent: "dev-reviewer", ...(roleModels.reviewer !== undefined ? { model: roleModels.reviewer } : {}) },
    },
    ...(launch !== undefined ? { launch } : {}),
  };
  const written = installClaude(outDir, config);
  const text = fs.readFileSync(written[agentKey], "utf8");
  const fm = text.split("\n---")[0];
  fs.rmSync(tmp, { recursive: true, force: true });
  return fm;
}
// The worked example: builder = foreign GLM on the opus tier, reviewer = native sonnet.
const xLaunch = { sessionModel: "claude-opus-4-8", guardModel: "", aliases: { opus: "glm-4.6" } };
check("claude-bake: builder on a FOREIGN-bound opus tier bakes the BARE alias 'model: opus'", /^model: opus$/m.test(claudeSeatFrontmatter("dev-builder", { builder: "opus", reviewer: "sonnet" }, xLaunch)));
check("claude-bake: reviewer on the UNBOUND sonnet tier bakes the CONCRETE 'model: claude-sonnet-4-6'", /^model: claude-sonnet-4-6$/m.test(claudeSeatFrontmatter("dev-reviewer", { builder: "opus", reviewer: "sonnet" }, xLaunch)));
// Symmetry: a foreign-bound reviewer seat also bakes the bare alias.
check("claude-bake: reviewer on a FOREIGN-bound sonnet tier bakes the BARE alias 'model: sonnet'", /^model: sonnet$/m.test(claudeSeatFrontmatter("dev-reviewer", { builder: "haiku", reviewer: "sonnet" }, { aliases: { sonnet: "glm-5.2", haiku: "deepseek-v4-pro" } })));

// FABLE end-to-end (papercut 14): a foreign-bound fable seat bakes the bare alias; a native
// fable seat bakes the concrete claude-fable-5. The 4th tier flows through the same bake path.
check("claude-bake: builder on a FOREIGN-bound fable tier bakes the BARE alias 'model: fable'", /^model: fable$/m.test(claudeSeatFrontmatter("dev-builder", { builder: "fable", reviewer: "opus" }, { aliases: { fable: "glm-4.6" } })));
check("claude-bake: reviewer on the UNBOUND fable tier bakes CONCRETE 'model: claude-fable-5' (native)", /^model: claude-fable-5$/m.test(claudeSeatFrontmatter("dev-reviewer", { builder: "opus", reviewer: "fable" }, { aliases: { opus: "glm-4.6" } })));

// PLANNER is a first-class BAKED seat (papercut 14 C — it is in SPAWNABLE, so roles.planner.model
// bakes like builder/reviewer). A planner on a foreign-bound tier bakes the bare alias (routes
// foreign); an inherit-session planner (absent model) bakes NO line (strong-planner default).
{
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-planner-fable-"));
  const outDir = path.join(tmp, "agents");
  const config = {
    roles: {
      orchestrator: { agent: "ai-dev" },
      planner: { agent: "dev-planner", model: "fable" },
      builder: { agent: "dev-builder" },
      reviewer: { agent: "dev-reviewer" },
    },
    launch: { aliases: { fable: "glm-4.6" } },
  };
  const fm = fs.readFileSync(installClaude(outDir, config)["dev-planner"], "utf8").split("\n---")[0];
  fs.rmSync(tmp, { recursive: true, force: true });
  check("claude-bake: planner on a FOREIGN-bound fable tier bakes the BARE alias 'model: fable' (planner routes foreign like the baked seats)", /^model: fable$/m.test(fm));
}

// ───────── 8. routing self-check (papercut 13 candidate (b): fail loud, no live proxy) ─────────
// checkRouting is pure: config INTENT ↔ baked ACTUAL ↔ settings.json env. A correctly-routed
// config passes with per-seat confirmation lines; every silent-native / disagreeing-env class
// fails LOUD, naming the seat.
const rpolicy = loadClaudeModelPolicy();
const goodRouted = {
  config: { roles: { builder: { agent: "dev-builder", model: "opus" }, reviewer: { agent: "dev-reviewer", model: "sonnet" } }, launch: { aliases: { opus: "glm-4.6" } } },
  seatModelLines: { builder: "opus", reviewer: "claude-sonnet-4-6" },
  env: { ANTHROPIC_DEFAULT_OPUS_MODEL: "glm-4.6" },
  policy: rpolicy,
};
const goodRes = checkRouting(goodRouted);
check("routing: a correctly-routed config passes (ok)", goodRes.ok === true && goodRes.failures.length === 0);
check("routing: pass reports the FOREIGN seat as 'via <id>'", goodRes.reports.some((r) => /builder → opus tier via glm-4.6 \(foreign\)/.test(r)));
check("routing: pass reports the NATIVE seat as concrete '(native)'", goodRes.reports.some((r) => /reviewer → claude-sonnet-4-6 \(native\)/.test(r)));

// broken (i): a FOREIGN-intended seat that baked the CONCRETE id — the silent-native class.
const brokeConcrete = checkRouting({ ...goodRouted, seatModelLines: { builder: "claude-opus-4-8", reviewer: "claude-sonnet-4-6" } });
check("routing: foreign seat baked CONCRETE → fails loud (not ok)", brokeConcrete.ok === false);
check("routing: the concrete-bake failure NAMES the seat and 'NATIVE'", brokeConcrete.failures.some((f) => f.includes("seat builder") && /NATIVE/.test(f)));

// broken (ii): a bound tier whose env var did not land (settings.json env empty).
const brokeEnv = checkRouting({ ...goodRouted, env: {} });
check("routing: bound tier missing its env var → fails loud", brokeEnv.ok === false);
check("routing: the missing-env failure names ANTHROPIC_DEFAULT_OPUS_MODEL", brokeEnv.failures.some((f) => f.includes("ANTHROPIC_DEFAULT_OPUS_MODEL")));

// broken (iii): a NATIVE-intended seat (unbound tier) that baked a BARE alias — would route
// foreign the moment that tier were ever bound; a native seat must bake the concrete id.
const brokeNativeBare = checkRouting({
  config: { roles: { builder: { agent: "dev-builder", model: "sonnet" }, reviewer: { agent: "dev-reviewer", model: "sonnet" } }, launch: { aliases: {} } },
  seatModelLines: { builder: "sonnet", reviewer: "claude-sonnet-4-6" },
  env: {},
  policy: rpolicy,
});
check("routing: native seat baked a BARE alias → fails loud, names the seat", brokeNativeBare.ok === false && brokeNativeBare.failures.some((f) => f.includes("seat builder") && /NATIVE/.test(f)));

// an inherit/auto seat (session/absent) is reported, never failed.
const inheritRes = checkRouting({
  config: { roles: { builder: { agent: "dev-builder" }, reviewer: { agent: "dev-reviewer", model: "session" } }, launch: {} },
  seatModelLines: { builder: null, reviewer: null }, env: {}, policy: rpolicy,
});
check("routing: inherit/session seats pass (reported, not failed)", inheritRes.ok === true && inheritRes.reports.length === 2);

// FABLE in the routing self-check (papercut 14): the bound-synonym loop covers the 4th tier —
// a fable-bound seat passes when it bakes the bare alias AND carries ANTHROPIC_DEFAULT_FABLE_MODEL,
// and fails LOUD (naming the fable env var) when the binding did not land.
const fableInput = {
  config: { roles: { builder: { agent: "dev-builder", model: "fable" }, reviewer: { agent: "dev-reviewer", model: "opus" } }, launch: { aliases: { fable: "glm-4.6" } } },
  seatModelLines: { builder: "fable", reviewer: "claude-opus-4-8" },
  env: { ANTHROPIC_DEFAULT_FABLE_MODEL: "glm-4.6" },
  policy: rpolicy,
};
const fableRouted = checkRouting(fableInput);
check("routing: a fable-bound seat passes, reported 'via glm-4.6'", fableRouted.ok === true && fableRouted.reports.some((r) => /builder → fable tier via glm-4.6 \(foreign\)/.test(r)));
const fableNoEnv = checkRouting({ ...fableInput, env: {} });
check("routing: fable bound but ANTHROPIC_DEFAULT_FABLE_MODEL missing → fails loud, names the var", fableNoEnv.ok === false && fableNoEnv.failures.some((f) => f.includes("ANTHROPIC_DEFAULT_FABLE_MODEL")));

// PLANNER in the routing self-check (papercut 14 C-follow): the planner is now a covered
// ROUTED_SEAT. A foreign-pinned planner that (hypothetically) baked the CONCRETE native id is
// the silent-native class — it must FAIL loud, naming the planner seat; an UNPINNED planner
// (inherit session) has no tier intent and passes (reported, never failed — no false red).
const plannerBrokeConcrete = checkRouting({
  config: { roles: { planner: { agent: "dev-planner", model: "opus" }, builder: { agent: "dev-builder" }, reviewer: { agent: "dev-reviewer" } }, launch: { aliases: { opus: "glm-4.6" } } },
  seatModelLines: { planner: "claude-opus-4-8", builder: null, reviewer: null },
  env: { ANTHROPIC_DEFAULT_OPUS_MODEL: "glm-4.6" },
  policy: rpolicy,
});
check("routing: a FOREIGN planner that baked CONCRETE → fails loud, names 'seat planner' + NATIVE", plannerBrokeConcrete.ok === false && plannerBrokeConcrete.failures.some((f) => f.includes("seat planner") && /NATIVE/.test(f)));
const plannerForeignOk = checkRouting({
  config: { roles: { planner: { agent: "dev-planner", model: "opus" }, builder: { agent: "dev-builder" }, reviewer: { agent: "dev-reviewer" } }, launch: { aliases: { opus: "glm-4.6" } } },
  seatModelLines: { planner: "opus", builder: null, reviewer: null },
  env: { ANTHROPIC_DEFAULT_OPUS_MODEL: "glm-4.6" },
  policy: rpolicy,
});
check("routing: a FOREIGN planner that baked the BARE alias passes, reported 'via glm-4.6'", plannerForeignOk.ok === true && plannerForeignOk.reports.some((r) => /planner → opus tier via glm-4.6 \(foreign\)/.test(r)));
const plannerInherit = checkRouting({
  config: { roles: { planner: { agent: "dev-planner" }, builder: { agent: "dev-builder" }, reviewer: { agent: "dev-reviewer" } }, launch: {} },
  seatModelLines: { planner: null, builder: null, reviewer: null }, env: {}, policy: rpolicy,
});
check("routing: an UNPINNED planner (inherit session) passes, reported not failed (no false red)", plannerInherit.ok === true && plannerInherit.reports.some((r) => /planner → inherits the session model/.test(r)));

// ───────── guard independence over a foreign-bound haiku (papercut 14 B / G1) ─────────
// The guard (background) is COUPLED to the haiku slot in the modern path. When haiku is bound
// foreign for the baked seats, the DEFAULT guard (empty guardModel) follows that foreign slot;
// an explicit OVERRIDE (guardModel = a concrete native id) keeps the background NATIVE via the
// deprecated ANTHROPIC_SMALL_FAST_MODEL, independent of the haiku slot. Also asserts fable's
// env write (the 4-tier LAUNCH_ALIAS_ENV_KEYS map).
{
  const envFable = mergeLaunchEnv(undefined, { aliases: { fable: "glm-4.6" } });
  check("launch-env: aliases.fable → ANTHROPIC_DEFAULT_FABLE_MODEL (4-tier map)", envFable.ANTHROPIC_DEFAULT_FABLE_MODEL === "glm-4.6");
  const envDefault = mergeLaunchEnv(undefined, { aliases: { haiku: "glm-4.6" } });
  check("guard: DEFAULT (empty guardModel) ⇒ no ANTHROPIC_SMALL_FAST_MODEL; background follows the foreign haiku slot",
    !("ANTHROPIC_SMALL_FAST_MODEL" in envDefault) && envDefault.ANTHROPIC_DEFAULT_HAIKU_MODEL === "glm-4.6");
  const envOverride = mergeLaunchEnv(undefined, { guardModel: "claude-haiku-4-5", aliases: { haiku: "glm-4.6" } });
  check("guard: OVERRIDE (guardModel concrete) ⇒ ANTHROPIC_SMALL_FAST_MODEL native, keeps background NATIVE over a foreign-bound haiku",
    envOverride.ANTHROPIC_SMALL_FAST_MODEL === "claude-haiku-4-5" && envOverride.ANTHROPIC_DEFAULT_HAIKU_MODEL === "glm-4.6");
}

// ── config.local (PERSONAL) tier binding: the launcher applies the env at startup, so an
// absent settings.json env var is EXPECTED, not the silent-native failure. The merged config
// carries the binding (so the bake produced the bare alias); localAliases marks it personal.
// docs/decisions/personal-multi-model-setup.md — the Part A core.
const localRouted = {
  config: { roles: { builder: { agent: "dev-builder", model: "opus" }, reviewer: { agent: "dev-reviewer", model: "sonnet" } }, launch: { aliases: { opus: "deepseek-v4-pro" } } },
  seatModelLines: { builder: "opus", reviewer: "claude-sonnet-4-6" },
  env: {}, // committed settings.json does NOT carry the personal alias
  policy: rpolicy,
  localAliases: { opus: "deepseek-v4-pro" },
};
const localRes = checkRouting(localRouted);
check("routing: a config.local-bound foreign tier passes with NO committed env (launcher-applied)", localRes.ok === true && localRes.failures.length === 0);
check("routing: the launcher-applied pass reports 'from config.local'", localRes.reports.some((r) => /builder → opus tier via deepseek-v4-pro \(foreign, applied by the launcher from config\.local\)/.test(r)));

// Control: the SAME merged config WITHOUT the personal marker (i.e. the tier were bound in the
// SHARED config.json) still FAILS on the missing committed env — a shared binding must land in
// settings.json. Proves the pass above is earned by the config.local origin, not a weakened check.
const sharedNoEnv = checkRouting({ ...localRouted, localAliases: {} });
check("routing: the SAME tier bound SHARED (not config.local) still fails on missing env", sharedNoEnv.ok === false && sharedNoEnv.failures.some((f) => f.includes("ANTHROPIC_DEFAULT_OPUS_MODEL")));

// ───────── 9. cross-model independence check (#334) ─────────
// Detects when the reviewer's effective model resolves to the same model as the session,
// meaning the protocol's `auto` cross-model promise is hollow. Non-blocking advisory.
const cmiPolicy = rpolicy; // reuse the loaded policy

// 9a. WARN path: foreign-bound tier resolves to the same model as the session.
const cmiWarnForeign = checkCrossModelIndependence({
  config: { launch: { sessionModel: "deepseek-v4-pro", aliases: { sonnet: "deepseek-v4-pro" } } },
  seatModelLines: { reviewer: "sonnet" }, // baked bare alias (foreign-bound)
  policy: cmiPolicy,
});
check("cross-model: foreign-bound sonnet == session model → warning", cmiWarnForeign.warnings.length === 1);
check("cross-model: warning names the colliding model", cmiWarnForeign.warnings[0].includes("deepseek-v4-pro"));

// 9b. WARN path: native tier (unbound) resolves to the same concrete id as session.
const cmiWarnNative = checkCrossModelIndependence({
  config: { launch: { sessionModel: "claude-sonnet-4-6" } },
  seatModelLines: { reviewer: "claude-sonnet-4-6" }, // concrete native id, same as session
  policy: cmiPolicy,
});
check("cross-model: native concrete id == session model → warning", cmiWarnNative.warnings.length === 1);
check("cross-model: native warning names the colliding model", cmiWarnNative.warnings[0].includes("claude-sonnet-4-6"));

// 9c. WARN path: native tier alias (unbound, baked concrete id via aliasOf) == session model.
const cmiWarnAlias = checkCrossModelIndependence({
  config: { launch: { sessionModel: "claude-opus-4-8" } },
  seatModelLines: { reviewer: "opus" }, // bare alias → native id = claude-opus-4-8 = session
  policy: cmiPolicy,
});
check("cross-model: unbound opus alias native id == session model → warning", cmiWarnAlias.warnings.length === 1);
check("cross-model: alias warning names the colliding model", cmiWarnAlias.warnings[0].includes("claude-opus-4-8"));

// 9d. NO-WARN path: foreign-bound tier resolves to a DIFFERENT model than session.
const cmiNoWarnDiff = checkCrossModelIndependence({
  config: { launch: { sessionModel: "deepseek-v4-pro", aliases: { sonnet: "glm-5.2" } } },
  seatModelLines: { reviewer: "sonnet" },
  policy: cmiPolicy,
});
check("cross-model: foreign sonnet != session model → no warning", cmiNoWarnDiff.warnings.length === 0);

// 9e. NO-WARN path: no explicit session model (session model unknown → can't detect).
const cmiNoWarnNoSession = checkCrossModelIndependence({
  config: { launch: {} },
  seatModelLines: { reviewer: "sonnet" },
  policy: cmiPolicy,
});
check("cross-model: no explicit session model → no warning", cmiNoWarnNoSession.warnings.length === 0);

// 9f. NO-WARN path: reviewer inherits session (null baked model line) — honest, no false claim.
const cmiNoWarnInherit = checkCrossModelIndependence({
  config: { launch: { sessionModel: "deepseek-v4-pro" } },
  seatModelLines: { reviewer: null }, // no model line → session inherit (honest)
  policy: cmiPolicy,
});
check("cross-model: reviewer inherits session (null) → no warning (honest)", cmiNoWarnInherit.warnings.length === 0);

// 9g. NO-WARN path: empty/whitespace session model → treated as absent → no warning.
const cmiNoWarnBlankSession = checkCrossModelIndependence({
  config: { launch: { sessionModel: "   " } },
  seatModelLines: { reviewer: "sonnet" },
  policy: cmiPolicy,
});
check("cross-model: blank session model → no warning", cmiNoWarnBlankSession.warnings.length === 0);

// 9h. NO-WARN path: reviewer absent (no seat at all) → no warning.
const cmiNoWarnNoSeat = checkCrossModelIndependence({
  config: { launch: { sessionModel: "deepseek-v4-pro" } },
  seatModelLines: {},
  policy: cmiPolicy,
});
check("cross-model: no reviewer seat → no warning", cmiNoWarnNoSeat.warnings.length === 0);

// 9i. WARN path: fable tier bound foreign, same as session (the 4th tier).
const cmiWarnFable = checkCrossModelIndependence({
  config: { launch: { sessionModel: "glm-4.6", aliases: { fable: "glm-4.6" } } },
  seatModelLines: { reviewer: "fable" },
  policy: cmiPolicy,
});
check("cross-model: foreign-bound fable == session model → warning", cmiWarnFable.warnings.length === 1);

// 9j. NO-WARN path: different models on opus and session.
const cmiNoWarnOpus = checkCrossModelIndependence({
  config: { launch: { sessionModel: "deepseek-v4-pro", aliases: { opus: "glm-4.6" } } },
  seatModelLines: { reviewer: "opus" },
  policy: cmiPolicy,
});
check("cross-model: foreign opus != session model → no warning", cmiNoWarnOpus.warnings.length === 0);

// e2e through verifyRoutingConsistency (the file-reading + THROW half): a real routed apply
// passes; a tampered baked agent throws loud, naming the seat.
function applyRoutedTarget() {
  const target = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-route-e2e-"));
  const config = {
    roles: {
      orchestrator: { agent: "ai-dev" },
      planner: { agent: "dev-planner" },
      builder: { agent: "dev-builder", model: "opus" },
      reviewer: { agent: "dev-reviewer", model: "sonnet" },
    },
    launch: { sessionModel: "claude-opus-4-8", guardModel: "", aliases: { opus: "glm-4.6" } },
  };
  fs.mkdirSync(path.join(target, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(target, ".ai-dev", "config.json"), JSON.stringify(config, null, 2));
  installClaude(path.join(target, ".claude", "agents"), config);
  const settingsPath = path.join(target, ".claude", "settings.json");
  fs.writeFileSync(settingsPath, JSON.stringify({ env: { ANTHROPIC_MODEL: "claude-opus-4-8", ANTHROPIC_DEFAULT_OPUS_MODEL: "glm-4.6" } }, null, 2));
  return { target, settingsPath };
}
{
  const { target, settingsPath } = applyRoutedTarget();
  let threw = false;
  try { verifyRoutingConsistency(target, settingsPath); } catch { threw = true; }
  check("routing e2e: a real routed apply passes verifyRoutingConsistency (no throw)", threw === false);
  // Tamper: rewrite the baked builder agent's bare `model: opus` to the concrete id (the
  // silent-native regression) — verify must now THROW and name the seat.
  const builderAgent = path.join(target, ".claude", "agents", "dev-builder.md");
  fs.writeFileSync(builderAgent, fs.readFileSync(builderAgent, "utf8").replace(/^model: opus$/m, "model: claude-opus-4-8"));
  let msg = "";
  try { verifyRoutingConsistency(target, settingsPath); } catch (e) { msg = e.message; }
  check("routing e2e: a tampered (concrete) builder bake THROWS, naming the seat", /Routing self-verify FAILED/.test(msg) && msg.includes("seat builder"));
  fs.rmSync(target, { recursive: true, force: true });
}

// e2e: a foreign tier bound ONLY in the gitignored config.local.json ⇒ the installer's bake
// (merging config.local) produces the bare alias AND verifyRoutingConsistency (reading the same
// merge) passes even though the committed settings.json env deliberately omits the personal
// binding. This is Part A's headline scenario — the install succeeds, no silent-native, no false
// red (docs/decisions/personal-multi-model-setup.md).
{
  const target = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-route-local-"));
  // SHARED config.json: builder rides the opus TIER LANE, reviewer native sonnet — NO alias here.
  const sharedConfig = {
    roles: {
      orchestrator: { agent: "ai-dev" },
      planner: { agent: "dev-planner" },
      builder: { agent: "dev-builder", model: "opus" },
      reviewer: { agent: "dev-reviewer", model: "sonnet" },
    },
    launch: { sessionModel: "claude-opus-4-8" },
  };
  // PERSONAL config.local.json: the opus tier binding lives here (gitignored, per-machine).
  const localConfig = { launch: { aliases: { opus: "deepseek-v4-pro" } } };
  fs.mkdirSync(path.join(target, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(target, ".ai-dev", "config.json"), JSON.stringify(sharedConfig, null, 2));
  fs.writeFileSync(path.join(target, ".ai-dev", "config.local.json"), JSON.stringify(localConfig, null, 2));
  // The real installer bakes with the MERGED launch (its subprocess calls loadConfigWithLocal);
  // simulate that by baking under the merged config so builder gets the bare `model: opus`.
  const mergedConfig = { ...sharedConfig, launch: { ...sharedConfig.launch, aliases: { opus: "deepseek-v4-pro" } } };
  installClaude(path.join(target, ".claude", "agents"), mergedConfig);
  const builderAgent = fs.readFileSync(path.join(target, ".claude", "agents", "dev-builder.md"), "utf8");
  check("routing local e2e: builder on a config.local-bound opus tier bakes the BARE alias 'model: opus'", /^model: opus$/m.test(builderAgent.split("\n---")[0]));
  // settings.json carries ONLY the shared session model — NOT the personal opus alias
  // (mergeLaunchEnv writes only config.json values; the launcher exports the alias at startup).
  const settingsPath = path.join(target, ".claude", "settings.json");
  fs.writeFileSync(settingsPath, JSON.stringify({ env: { ANTHROPIC_MODEL: "claude-opus-4-8" } }, null, 2));
  let threw = false, emsg = "";
  try { verifyRoutingConsistency(target, settingsPath); } catch (e) { threw = true; emsg = e.message; }
  if (threw) console.log(`    (unexpected throw: ${emsg})`);
  check("routing local e2e: verifyRoutingConsistency PASSES with a config.local-only foreign tier (no throw)", threw === false);
  fs.rmSync(target, { recursive: true, force: true });
}

// The REAL installer bake entry (install-agents.mjs run as a subprocess, the way wireClaude
// invokes it) merges config.local's launch before baking — the production path the e2e above
// simulates. Spawn it against a fixture carrying a config.local opus binding and confirm the
// builder bakes the BARE alias (not the concrete native id). Guards the main-block glue itself.
{
  const AGENTS_SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), "claude", "install-agents.mjs");
  const target = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-subproc-local-"));
  fs.mkdirSync(path.join(target, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(target, ".ai-dev", "config.json"), JSON.stringify({
    roles: {
      orchestrator: { agent: "ai-dev" },
      planner: { agent: "dev-planner" },
      builder: { agent: "dev-builder", model: "opus" },
      reviewer: { agent: "dev-reviewer", model: "sonnet" },
    },
    launch: { sessionModel: "claude-opus-4-8" },
  }));
  fs.writeFileSync(path.join(target, ".ai-dev", "config.local.json"), JSON.stringify({ launch: { aliases: { opus: "deepseek-v4-pro" } } }));
  execFileSync("node", [AGENTS_SCRIPT, path.join(target, "agents")], {
    env: { ...process.env, AI_DEV_CONFIG: path.join(target, ".ai-dev", "config.json") },
    stdio: "pipe",
  });
  const bakedFm = fs.readFileSync(path.join(target, "agents", "dev-builder.md"), "utf8").split("\n---")[0];
  check("subprocess bake: the real installer entry merges config.local ⇒ builder bakes BARE 'model: opus'", /^model: opus$/m.test(bakedFm));
  fs.rmSync(target, { recursive: true, force: true });
}

console.log(`\nINSTALL-MODEL bake: ${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
console.log("PASS — OpenCode bakes no line (runtime ignores subagent model:); Claude bakes the resolved per-seat model");
