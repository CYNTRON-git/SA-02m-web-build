// Adapter error-handling coverage — the two malformed-input surfaces hardened in the
// mechanical-floor batch (backlog "Adapter error handling — loadConfig + RegExp without
// try/catch"). Before this, a malformed deny-rules.json crashed the hook (non-zero exit
// blocking every tool call) and a bad regex in a config pattern threw SyntaxError from an
// inject predicate. Both now fail in their named-safe direction:
//
//   • loadConfig (shim call site): fail-OPEN — the Claude shim subprocess exits 0 (allow),
//     logs to stderr, never crashes. Rationale: the tooling dir is immutable + ships a
//     valid registry, so a broken registry is a broken install, not an attack; a
//     fail-closed crash makes the harness unusable (Operator disables the hook → worse).
//   • new RegExp in an INJECT predicate: returns false (no nudge) on a compile error.
//     Safe for inject only — a missing nudge is a lost reminder, never a missing deny.
//     A deny-class consumer must NOT reuse safeTest (guarded by a comment in engine.mjs).
//
// Run: node src/adapter/error-handling.test.mjs

import { evaluate, loadConfig } from "./engine.mjs";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHIM = path.join(HERE, "claude", "shim.mjs");
const config = loadConfig(HERE);

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; }
  else { fail++; console.log(`  ✗ ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
}

// ── 1. MALFORMED deny-rules.json ⇒ the Claude shim fails OPEN (exit 0, no crash) ──
// Drive the REAL shim subprocess (the same entry a Claude PreToolUse hook runs) with a
// tooling dir whose deny-rules.json is broken JSON. The shim's loadConfig throws; the
// main() try/catch must swallow it, log to stderr, exit 0, and print nothing (allow).
console.log("MALFORMED REGISTRY (shim fails open — exit 0, allow, no crash):");
{
  // A fake tooling root the shim's resolveRoot will land in: it runs git rev-parse from
  // payload.cwd; in a non-git tmp dir that falls back to cwd. But loadConfig reads from
  // the SHIM's own dir (path.dirname(path.dirname(import.meta.url))) = src/adapter — we
  // cannot break the real registry. So instead we COPY the adapter into a temp dir with a
  // broken deny-rules.json and run that copy's shim, mirroring how the parity NODE-SPIKE
  // drives a real subprocess.
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-errh-"));
  const adapter = path.join(tmp, "adapter");
  fs.mkdirSync(path.join(adapter, "claude"), { recursive: true });
  // Vendor the engine + its sibling modules + shim verbatim; replace deny-rules.json
  // with broken JSON. The engine entry imports the engine-*.mjs helper modules, so
  // every one must land in the temp adapter or the import chain breaks before the
  // fail-open path under test is reached.
  for (const f of ["engine.mjs", "engine-paths.mjs", "engine-bash.mjs", "engine-git.mjs", "engine-config.mjs", "engine-components.mjs", "session-root.mjs"]) {
    fs.copyFileSync(path.join(HERE, f), path.join(adapter, f));
  }
  fs.copyFileSync(SHIM, path.join(adapter, "claude", "shim.mjs"));
  // The shim imports the sanctioned-scratch derivation (the agent's own out-of-root scratch
  // allow-set) — vendor it too or the import chain breaks before the fail-open path is reached.
  fs.copyFileSync(path.join(HERE, "claude", "sanctioned-scratch.mjs"), path.join(adapter, "claude", "sanctioned-scratch.mjs"));
  fs.writeFileSync(path.join(adapter, "deny-rules.json"), "{ this is : not valid json ][");
  const brokenShim = path.join(adapter, "claude", "shim.mjs");

  let exitCode, stdout = "", threw = false;
  try {
    stdout = execFileSync("node", [brokenShim], {
      input: JSON.stringify({ tool_name: "Read", tool_input: { file_path: "/etc/passwd" }, cwd: tmp }),
      encoding: "utf8",
    });
    exitCode = 0;
  } catch (e) {
    threw = true;
    exitCode = e.status;
  }
  check("malformed-registry:exit-0", exitCode, 0);
  check("malformed-registry:did-not-crash", threw, false);
  // Fail-open ⇒ allow ⇒ the shim prints NOTHING (mapVerdict returns null for allow).
  check("malformed-registry:allows-empty-stdout", stdout.trim(), "");
  fs.rmSync(tmp, { recursive: true, force: true });
}

// ── 2. BAD REGEX in a config pattern ⇒ inject predicates return allow, no throw ────
// A change_verbs.pattern that is an invalid regex (an unterminated group) must not throw
// from any of the three inject predicates; safeTest returns false ⇒ no inject ⇒ allow.
console.log("BAD REGEX PATTERN (inject predicates fail toward no-inject, never throw):");
{
  const bad = JSON.parse(JSON.stringify(config));
  bad.change_verbs.pattern = "("; // unterminated group ⇒ SyntaxError on compile

  // Stage 1 — unconfigured root would normally fire no-config-run-setup on a change verb.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-errh-cfg-"));
  let v, threw = false;
  try { v = evaluate({ act: "prompt", root, prompt: "please implement the export feature" }, bad); }
  catch { threw = true; }
  check("bad-regex:no-throw", threw, false);
  // The CONDITIONAL injects compile the bad pattern via safeTest ⇒ silenced (no setup/route
  // nudge). The always-on language-mirror inject compiles NO pattern, so it still fires:
  // verdict is inject with ruleId language-mirror, proving the bad regex was swallowed (no
  // throw) AND the conditional nudge it would have driven is gone.
  check("bad-regex:conditional-silenced", v && v.ruleId, "language-mirror");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 3. REGRESSION — a VALID config + valid pattern still injects ───────────────────
// The happy path is unbroken: a change-verb prompt on an unconfigured root still fires
// the setup nudge (inject), so the safeTest wrap did not silence a legitimate match.
console.log("REGRESSION (valid pattern still injects):");
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-errh-ok-"));
  const v = evaluate({ act: "prompt", root, prompt: "please implement the export feature" }, config);
  check("valid-pattern:injects", v.verdict, "inject");
  check("valid-pattern:ruleId", v.ruleId, "no-config-run-setup");
  // A non-change prompt no longer allows: the always-on language-mirror inject fires on
  // EVERY prompt (the conditional setup nudge stays silent — "good morning" is no change verb).
  const n = evaluate({ act: "prompt", root, prompt: "good morning" }, config);
  check("valid-pattern:non-change-injects-mirror", n.verdict, "inject");
  check("valid-pattern:non-change-ruleId", n.ruleId, "language-mirror");
  fs.rmSync(root, { recursive: true, force: true });
}

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
