// OpenCode plugin entry — install drops THIS file (only) into .opencode/plugins/
// (PLURAL — opencode 1.17 loads `.opencode/plugins/`, not the singular form). Rewrite
// the adapter path below if the adapter is vendored somewhere other than the tooling
// submodule.
//
// It must DEFINE the plugin function inline, NOT import-and-re-expose it. Dogfooded on
// opencode 1.17.0: an own export of an imported function (`import { X as impl }; export
// const X = impl`) — like a bare re-export — LOADS without error but its hooks NEVER
// fire. opencode only registers hooks off a function DEFINED in the loaded module. So
// the thin wrappers live here, inline; only the rule logic (loadConfig + decide) is
// imported, so the rules stay single-sourced. Verified live: the inline form blocks a
// write into `.ai-dev/tooling/` (the engine's self-patch deny).
//
// ONE hook, the one class OpenCode realises mechanically: `tool.execute.before`
// (deny — throw). The `inject` class is NOT realised on OpenCode: the former
// `chat.message` hook that pushed a context part crashed the host on opencode
// 1.17.8 — pushing into `output.parts` made `SessionPrompt.createUserMessage`
// throw `EventV2.InvalidSyncEvent: Expected string aggregate field sessionID`
// AFTER the hook returned (uncatchable in-hook), crashing the session on every
// change-verb message; injected parts were also unreliably rendered upstream. So
// inject is persona-fallback on OpenCode (recorded per-rule in deny-rules.json
// `fallback`; rationale in INSTALL.md). Do NOT re-add a chat.message push without
// a confirmed-stable opencode hook. `ask` likewise has no plugin-hook realisation
// and falls back to persona.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadConfig } from "../../../.ai-dev/tooling/src/adapter/engine.mjs";
import { decide } from "../../../.ai-dev/tooling/src/adapter/opencode/normalise.mjs";

const ADAPTER = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", ".ai-dev", "tooling", "src", "adapter");

// Resolve whether the calling session is the orchestrator (no parentID, or agent id
// `ai-dev`). FAIL-OPEN to false on any lookup failure — a miss never produces a false
// denial; persona is the fail-safe (matches the engine's fail-open-on-actor contract).
async function isOrchestrator(client, sessionID) {
  try {
    if (!client || !client.session || !client.session.get || !sessionID) return false;
    const res = await client.session.get({ path: { id: sessionID } });
    const data = (res && res.data) || {};
    if (data.agent === "ai-dev") return true;
    return data.parentID == null;
  } catch { return false; }
}

export const AiPmEnforcement = async (ctx) => {
  const root = (ctx && (ctx.directory || ctx.worktree)) || process.cwd();
  // Fail-OPEN on a config-load failure: a malformed deny-rules.json yields a NO-OP hook
  // set (no enforcement) rather than crashing the session. Same rationale as the Claude
  // shim's main() try/catch — the tooling dir is immutable and ships a valid registry, so
  // a broken registry means a broken install, not an attack; a crash that kills the
  // session is worse than a fail-open the Operator would otherwise route around by
  // disabling the plugin. The immutable tooling dir is the compensating control.
  let config;
  try { config = loadConfig(ADAPTER); }
  catch (e) {
    console.error("[ai-dev] config load failed, enforcement off this session: " + (e && e.message ? e.message : String(e)));
    return {};
  }
  return {
    "tool.execute.before": async (input, output) => {
      const isOrch = await isOrchestrator(ctx && ctx.client, input && input.sessionID);
      const r = decide(input && input.tool, (output && output.args) || {}, root, isOrch, config);
      if (r.verdict === "deny") throw new Error("[ai-dev] " + r.reason);
    },
    // No `chat.message` hook: the inject class is persona-fallback on OpenCode (see
    // the file header — the push crashed opencode 1.17.8 and rendered unreliably).
  };
};
