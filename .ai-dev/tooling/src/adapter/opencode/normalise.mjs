// OpenCode adapter — the pure, importable half of the shim. Holds the OpenCode
// input-normaliser and the full decide() path; NO rule logic (every pattern and
// role list lives in ../deny-rules.json + ../engine.mjs). The OpenCode plugin
// entry (plugin.mjs) wraps this with the async actor lookup and the throw-to-deny
// mapping; the parity test imports this file directly.
//
// Separate from plugin.mjs so OpenCode never loads it as a plugin (see
// plugin.mjs); imported, never registered. Deploy layout: adapter README.

import { evaluate } from "../engine.mjs";

// ── normalise: OpenCode tool.execute.before (input, output) → neutral input ──
// `input.tool` is the tool name; `output.args` its arguments. `isOrchestrator`
// is resolved by the plugin entry (an async session lookup) and threaded in here
// so this stays a pure synchronous function the parity test can call.
// Tool→arg map (per the OpenCode plugin docs / .env example):
//   read  → args.filePath          write → args.filePath + args.content
//   edit  → args.filePath          bash  → args.command
//   task  → args.subagent_type|agent   skill → args.name
export function normalise(tool, args, root, isOrchestrator) {
  args = args || {};
  switch (tool) {
    case "read":
      if (typeof args.filePath !== "string" || !args.filePath) return null;
      return { act: "read", root, path: args.filePath };
    case "write": {
      if (typeof args.filePath !== "string" || !args.filePath) return null;
      const content = typeof args.content === "string" ? args.content : "";
      return {
        act: "write", root, path: args.filePath, content,
        contentEmpty: content.replace(/\s/g, "").length === 0,
        isOrchestrator,
      };
    }
    case "edit":
      if (typeof args.filePath !== "string" || !args.filePath) return null;
      return { act: "write", root, path: args.filePath, contentEmpty: false, isOrchestrator };
    case "bash":
      if (typeof args.command !== "string" || !args.command) return null;
      return { act: "bash", root, command: args.command, isOrchestrator };
    case "task": {
      const target = typeof args.subagent_type === "string" ? args.subagent_type : args.agent;
      if (typeof target !== "string" || !target) return null;
      return { act: "spawn", root, spawnTarget: target };
    }
    case "skill":
      if (typeof args.name !== "string" || !args.name) return null;
      return { act: "spawn", root, spawnTarget: args.name };
    default:
      return null;
  }
}

// ── decide: full pure path (for the parity test and the plugin entry) ────────
// `opts.targetsSessionRepo` threads the same session-repo signal the Claude shim
// computes, so the shared engine consumes it identically on both platforms. The
// OpenCode plugin entry does NOT compute it: the runtime hands the session root in
// directly (ctx.directory) and there is no per-command cwd to detect a nested repo, so
// the signal is left undefined ⇒ the engine's fail-CLOSED default (deny applies). The
// param exists so the engine-consumption path is provably identical across shims and
// is ready the day OpenCode surfaces a per-command working dir.
export function decide(tool, args, root, isOrchestrator, config, opts = {}) {
  const input = normalise(tool, args, root, isOrchestrator);
  if (!input) return { verdict: "allow", ruleId: null, reason: "" };
  if (opts.targetsSessionRepo !== undefined) input.targetsSessionRepo = opts.targetsSessionRepo;
  return evaluate(input, config);
}

// ── prompt act: OpenCode chat.message (the analog of Claude UserPromptSubmit) ──
// The chat.message hook fires once per user message before the LLM call; the
// plugin entry joins the message text parts into `userText` and threads in the
// actor signal. The neutral shape MUST match the Claude shim's UserPromptSubmit
// branch — `{ act: "prompt", root, prompt }` — so the single engine decides the
// prompt-class rules (no-config-run-setup, change-route-reminder) identically.
export function normalisePrompt(userText, root, isOrchestrator) {
  if (typeof userText !== "string" || !userText) return null;
  return { act: "prompt", root, prompt: userText, isOrchestrator };
}

export function decidePrompt(userText, root, isOrchestrator, config) {
  const input = normalisePrompt(userText, root, isOrchestrator);
  if (!input) return { verdict: "allow", ruleId: null, reason: "" };
  return evaluate(input, config);
}
