// OpenCode inject-REALISATION test — pins that the inject class is NOT applied on
// OpenCode (persona-fallback), and that the deny floor stays wired.
//
// History: a `chat.message` hook once pushed a context part (the analog of Claude's
// UserPromptSubmit). On opencode 1.17.8 that push crashed the host —
// `SessionPrompt.createUserMessage` threw `EventV2.InvalidSyncEvent` AFTER the hook
// returned, killing the session on every change-verb message — and injected parts
// rendered unreliably upstream. So the hook was removed: inject is persona-fallback
// on OpenCode (recorded in deny-rules.json `fallback`; INSTALL.md states the reason).
//
// This test drives the DEPLOYED plugin (.opencode/plugins/ai-dev.mjs — the module
// OpenCode actually loads) and asserts the no-op: there is NO `chat.message` hook,
// so no part can be pushed and the 1.17.8 crash trigger is gone. The deny hook
// (`tool.execute.before`) must remain registered — the real [mechanical] floor.
// The engine still DECIDES inject for both platforms (parity.test.mjs covers that
// decision); only the OpenCode adapter no longer APPLIES it.
//
// Run: node src/adapter/opencode-inject.test.mjs

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { AiPmEnforcement } from "../../.opencode/plugins/ai-dev.mjs";

let pass = 0, fail = 0;
function check(name, cond) {
  if (cond) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}`); }
}

// Build the hook set for a given project root (the plugin reads ctx.directory as
// the root). No client → isOrchestrator fails open to false.
async function hooksFor(root) {
  return AiPmEnforcement({ directory: root });
}

// A change-verb message — the exact crash trigger when the push hook existed.
const CHANGE_MSG = "please implement the new export feature";

// ── 1. the deny hook stays registered; the inject hook is GONE ────────────────
const tmpUnconfigured = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-oc-nocfg-"));
const hooks = await hooksFor(tmpUnconfigured);
check("tool.execute.before hook still registered (the deny floor)", typeof hooks["tool.execute.before"] === "function");
check("chat.message hook is NOT registered (inject is persona-only on OpenCode)", hooks["chat.message"] === undefined);

// ── 2. only the deny hook is exposed (no other hook surface to crash the host) ─
const hookNames = Object.keys(hooks);
check("the plugin exposes exactly one hook (tool.execute.before)",
  hookNames.length === 1 && hookNames[0] === "tool.execute.before");

// ── 3. a no-config + change-verb message has no inject path at all ────────────
// With no chat.message hook there is nothing that could push a part — the 1.17.8
// crash trigger (a push into output.parts) cannot occur. We assert the absence of
// the hook rather than driving it (driving an absent hook is a no-op by definition).
const tmpConfigured = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-oc-cfg-"));
fs.mkdirSync(path.join(tmpConfigured, ".ai-dev"), { recursive: true });
fs.writeFileSync(path.join(tmpConfigured, ".ai-dev", "config.json"), "{}");
const hooksCfg = await hooksFor(tmpConfigured);
check("configured root also exposes no chat.message hook", hooksCfg["chat.message"] === undefined);
check("CHANGE_MSG is the documented crash trigger (referenced, not driven)", typeof CHANGE_MSG === "string");

// cleanup
fs.rmSync(tmpUnconfigured, { recursive: true, force: true });
fs.rmSync(tmpConfigured, { recursive: true, force: true });

console.log(`\nOPENCODE-INJECT: ${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
console.log("PASS — inject is persona-only on OpenCode (no chat.message hook); the deny floor stays wired");
