#!/usr/bin/env node
// Claude adapter shim. The ONLY Claude-specific code: it normalises a Claude
// hook payload to the neutral input shape, calls the shared engine, and maps the
// engine verdict to Claude's hook stdout JSON. It holds NO rule logic — every
// pattern, role list, and predicate lives in ../deny-rules.json + ../engine.mjs.
//
// Run as the hook entry (`node shim.mjs`): reads the hook payload from stdin,
// prints the deny/ask/inject JSON (nothing for allow) to stdout, exits 0 — a
// drop-in for the inline shell+jq guards. Install wiring: adapter README.

import { evaluate, loadConfig } from "../engine.mjs";
import { resolveSessionRoot, targetsSessionRepo, extractGitEffectiveCwd } from "../session-root.mjs";
import { deriveSanctionedScratch, deriveSanctionedScratchWritable } from "./sanctioned-scratch.mjs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// SessionStart inject — the crash-resume FIRST-action reminder. The pointer's STATUS content
// (version, branch, queue) lives in `.ai-dev/state/current.md` alone (invariant 6); this text
// names only the ACTION + the path + the fresh-clone fall-back, never the status values. One
// home for the wording — the orchestrator's resume line + CLAUDE.md's top line POINT at the
// same action; this is the mechanical backstop that fires on the recovery events a prose line
// can miss (startup / resume / clear / compact).
const SESSION_START_INJECT = `**FIRST action — crash-resume discipline.** You operate with continuous checkpoints (the active plan's progress note) and a resume pointer at \`.ai-dev/state/current.md\`. On startup, resume, clear, and compact — and ANY time context feels degraded, contradictory, or unfamiliar — read \`.ai-dev/state/current.md\` FIRST (exact path; never file-search/glob — dot-dirs hide on some harnesses), reconcile its CURRENT STATE against reality (version vs \`git tag\`, active branch, the queue), THEN act. Absent (fresh clone / first session): fall back to \`git log --oneline -5\` + \`gh pr list\`. Do not build, review, or ship before the pointer is read.`;

// PostToolUse / Task checkpoint nudge — fires after a spawned-role handoff (the canonical
// "significant step" the continuous crash-resume checkpoint rule names). Pure inject:
// no engine evaluation (PostToolUse carries no permission act, only the task result).
// Claude-only mechanical; OpenCode is persona-only (inject-class is persona on OpenCode —
// the chat.message hook was dropped, M18). One home for the wording — the orchestrator's
// checkpoint rule POINTS at this as the mechanical backstop; the wording lives here.
const CHECKPOINT_NUDGE = `A spawned role just handed back (a significant step). If a feature is in flight, refresh its active plan progress note (goal · progress · next · open findings) — supersede, don't append — and reconcile the resume pointer if it drifted. Keep the pointer thin.`;

// ── normalise: Claude hook payload → neutral input ───────────────────────────
// Returns the neutral `{ act, root, ... }` shape the engine consumes, or null
// when this payload carries nothing the engine watches (the shim then allows).
// `isOrchestrator` is intentionally left undefined: a Claude hook payload carries
// no session-role signal, so the actor-dependent rules (orchestrator-authors-
// content, stamp-write) fall back to persona by the engine's fail-open-on-actor
// contract. This is the documented Claude capability gap, not an omission.
export function normalise(payload, root) {
  if (!payload || typeof payload !== "object") return null;

  // UserPromptSubmit — the only prompt-act event.
  if (payload.hook_event_name === "UserPromptSubmit") {
    if (typeof payload.prompt !== "string" || !payload.prompt) return null;
    return { act: "prompt", root, prompt: payload.prompt };
  }

  const tool = payload.tool_name;
  const ti = payload.tool_input || {};
  switch (tool) {
    case "Read":
      if (typeof ti.file_path !== "string" || !ti.file_path) return null;
      return { act: "read", root, path: ti.file_path };
    case "Write": {
      if (typeof ti.file_path !== "string" || !ti.file_path) return null;
      const content = typeof ti.content === "string" ? ti.content : "";
      return {
        act: "write", root, path: ti.file_path, content,
        contentEmpty: content.replace(/\s/g, "").length === 0,
      };
    }
    case "Edit":
      // Edit never truncates to empty (the Write tool does) — contentEmpty:false
      // so only the path-boundary / orchestrator-content rules apply.
      if (typeof ti.file_path !== "string" || !ti.file_path) return null;
      return { act: "write", root, path: ti.file_path, contentEmpty: false };
    case "Bash":
      if (typeof ti.command !== "string" || !ti.command) return null;
      return { act: "bash", root, command: ti.command };
    case "Task":
      if (typeof ti.subagent_type !== "string" || !ti.subagent_type) return null;
      return { act: "spawn", root, spawnTarget: ti.subagent_type };
    case "Skill":
      if (typeof ti.skill !== "string" || !ti.skill) return null;
      return { act: "spawn", root, spawnTarget: ti.skill };
    default:
      return null;
  }
}

// ── mapVerdict: engine verdict → Claude hook stdout object ───────────────────
// Returns the object to print as JSON, or null for allow (print nothing). The
// engine's class is realised directly: Claude supports deny, ask, and inject.
export function mapVerdict(result, eventName) {
  if (!result || result.verdict === "allow") return null;
  if (result.verdict === "inject") {
    return {
      hookSpecificOutput: {
        hookEventName: eventName || "UserPromptSubmit",
        additionalContext: result.reason,
      },
    };
  }
  // deny | ask — both are PreToolUse permission decisions.
  return {
    hookSpecificOutput: {
      hookEventName: eventName || "PreToolUse",
      permissionDecision: result.verdict,
      permissionDecisionReason: result.reason,
    },
  };
}

// ── decide: full pure path (for the parity test) ─────────────────────────────
// `opts.targetsSessionRepo` (when supplied) is threaded onto the neutral input so the
// engine's git-targeting denies (f4-floor, merge-gate) can scope to the session repo.
// Omitted (undefined) ⇒ the engine's fail-CLOSED default applies (the deny fires) —
// so a caller that passes no signal gets the unchanged, strict behaviour.
export function decide(payload, root, config, opts = {}) {
  const input = normalise(payload, root);
  if (!input) return { verdict: "allow", ruleId: null, reason: "" };
  if (opts.targetsSessionRepo !== undefined) input.targetsSessionRepo = opts.targetsSessionRepo;
  // The agent's OWN out-of-root scratch (tool-result overflow + per-session temp), derived
  // fail-closed from the Claude env — the read-family predicates carve these out so a
  // fetched/overflow artifact the harness placed is not false-blocked. `sanctionedScratchWritable`
  // is the narrower WRITE allow-set — only the per-session temp root (the harness's own
  // system prompt directs the agent to write temp files there); the tool-result overflow
  // store never appears in it, so it stays write-denied. `home` lets a bash `~/…` reference
  // reach either carve-out.
  const env = opts.env || process.env;
  input.sanctionedScratch = deriveSanctionedScratch(env, root);
  input.sanctionedScratchWritable = deriveSanctionedScratchWritable(env, root);
  input.home = env.HOME;
  return evaluate(input, config);
}

// ── main: stdin → stdout ─────────────────────────────────────────────────────
function resolveCwd(payload) {
  return (payload && typeof payload.cwd === "string" && payload.cwd) || process.cwd();
}

function main() {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (c) => (raw += c));
  process.stdin.on("end", () => {
    let payload;
    try { payload = JSON.parse(raw); } catch { process.exit(0); }
    // SessionStart — a pure inject (the crash-resume FIRST-action reminder above), not an
    // engine event: it carries no act to evaluate, so it bypasses decide() and emits the
    // fixed inject directly. Fires on every source (startup/resume/clear/compact) — exactly
    // the recovery cases where loaded context may be stale or the model disoriented. Fail-open
    // like the rest (a parse miss already exited above; this branch cannot throw).
    if (payload && payload.hook_event_name === "SessionStart") {
      process.stdout.write(JSON.stringify({
        hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: SESSION_START_INJECT },
      }));
      process.exit(0);
    }
    // PostToolUse — a pure inject (the checkpoint nudge above) when the tool is Task (a
    // spawned-role handoff = the canonical significant step). Bypasses decide(): PostToolUse
    // carries no permission act and must never hit the engine deny (a Task spawn-deny would
    // incorrectly fire after the fact). The matcher already scopes this hook to Task, but
    // we guard defensively on the tool name. Non-Task tools: no output (a silent no-op exit).
    // OpenCode asymmetry: inject-class is persona-only on OpenCode (no hook entry there).
    if (payload && payload.hook_event_name === "PostToolUse") {
      if (payload.tool_name === "Task") {
        process.stdout.write(JSON.stringify({
          hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: CHECKPOINT_NUDGE },
        }));
      }
      process.exit(0);
    }
    // Fail-OPEN past this point: a malformed deny-rules.json (loadConfig throws) or any
    // other decide-path error logs to stderr and exits 0 (allow), never crashes the hook.
    // Rationale: the tooling dir is immutable (self-patch deny) and ships a valid registry,
    // so a broken registry means a broken install, not an attack — and a fail-CLOSED crash
    // that blocks EVERY tool call makes the harness unusable, which the Operator routes
    // around by disabling the hook entirely (strictly worse than fail-open). The immutable
    // tooling dir is the compensating control. Matches the JSON.parse(raw) fail-open above.
    try {
      const cwd = resolveCwd(payload);
      const root = resolveSessionRoot(cwd);
      const config = loadConfig(path.dirname(path.dirname(fileURLToPath(import.meta.url))));
      // Scope the git-targeting denies to the session repo (fail-CLOSED — see
      // session-root.mjs): a command in a separate nested repo is exempt, the session
      // repo's floor is untouched, any doubt denies.
      // Extract the effective cwd from a `git -C <path>` flag (#383 Feature A): a command
      // targeting a provably-different repo via `-C` is no longer falsely blocked by the
      // session repo's HEAD state. Fail-closed: any doubt → cwd → unchanged strict behaviour.
      const bashCmd = (payload && typeof payload.tool_input === "object")
        ? (payload.tool_input.command ?? null) : null;
      const effectiveCwd = (typeof bashCmd === "string")
        ? extractGitEffectiveCwd(bashCmd, cwd) : cwd;
      const result = decide(payload, root, config, { targetsSessionRepo: targetsSessionRepo(effectiveCwd, root) });
      const out = mapVerdict(result, payload.hook_event_name);
      if (out) process.stdout.write(JSON.stringify(out));
    } catch (e) {
      console.error("[ai-dev] config load / decide failed, allowing this call: " + (e && e.message ? e.message : String(e)));
    }
    process.exit(0);
  });
}

// Run main only as the hook entry, not when imported by the parity test.
// pathToFileURL, not string concatenation: on Windows argv[1] is `C:\...` while
// import.meta.url is `file:///C:/...` — the old `file://${argv[1]}` compare was
// always false, main() never ran, and the hook exited 0 (= allow): the ENTIRE
// deny layer was silently inert on win32 (CE-02m-3 Phase-10 acceptance probes,
// 2026-07-11; Unix unaffected, which is why it shipped).
if (import.meta.url === pathToFileURL(process.argv[1]).href) main();
