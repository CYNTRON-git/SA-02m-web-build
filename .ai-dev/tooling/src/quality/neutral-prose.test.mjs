// Neutral-prose check — the core MUST stay platform-neutral (PROTOCOL.md
// `## Core and adapter`: the core "names NO platform, tool, hook, or plugin").
// This is the `review`-beat guard that makes that promise testable instead of
// merely asserted: it greps the core surface for bare platform primitives and
// fails if any leaked in. A leak is a Reviewer-blocking find, not a thing to
// quietly rewrite — the build hands it back as a finding.
//
// What counts as a leak — the signal, not a blunt word match:
//   • a PRIMITIVE rendered as a `code-span` — the core writes every real tool /
//     config / hook name in backticks (`find`, `ai-dev.config.json`), so a leaked
//     `Bash` / `settings.json` shows up backticked. The bare English verbs
//     "Read this file" / "read and write" are NOT primitives and must pass.
//   • a few primitives unambiguous even unbackticked — a hook event name
//     (`PreToolUse`), a config filename, a dot-dir path (`.claude/`).
// NOT a leak: the proper nouns "Claude" / "OpenCode" — the core names the two
// adapters and points at them on purpose (protocol-reference.md `## Core and adapter`).
// They are allowed.
//
// Run: node src/quality/neutral-prose.test.mjs

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CORE = path.resolve(HERE, "..", ".."); // the core root (repo root)

// The core surface this check guards: the constitution, the role agents, the
// architecture model, and the capability-module FRAGMENTS (src/modules/<id>/<role>.md —
// core prose composed INTO the role agents, so subject to the same neutrality). The
// adapter (which legitimately names platform tools) is out of scope by construction.
function moduleFragments() {
  const dir = path.join(CORE, "src", "modules");
  if (!fs.existsSync(dir)) return [];
  const out = [];
  for (const id of fs.readdirSync(dir)) {
    const sub = path.join(dir, id);
    if (!fs.statSync(sub).isDirectory()) continue;
    for (const f of fs.readdirSync(sub)) {
      if (f.endsWith(".md")) out.push(path.join("src", "modules", id, f));
    }
  }
  return out;
}
const surface = [
  "PROTOCOL.md",
  "docs/architecture.md",
  ...fs
    .readdirSync(path.join(CORE, "src", "agents"))
    .filter((f) => f.endsWith(".md"))
    .map((f) => path.join("src", "agents", f)),
  ...moduleFragments(),
];

// Tool nouns that are also ordinary English verbs/nouns — a leak ONLY when used
// as a primitive, i.e. inside a `code-span`. Bare prose use is fine.
const codeSpanNouns = ["Skill", "Task", "Bash", "Read", "Write"];
// Primitives unambiguous wherever they appear, backticked or not.
const anywhere = [
  "settings.json",
  "PreToolUse",
  "UserPromptSubmit",
  "AskUserQuestion",
  "CLAUDE.md",
  ".claude/",
  ".opencode/",
  "tool.execute.before",
];

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// A `code-span` carrying exactly one of the dual-use tool nouns.
const codeSpanRe = new RegExp("`\\s*(" + codeSpanNouns.join("|") + ")\\s*`", "g");
const anywhereRes = anywhere.map((p) => ({ p, re: new RegExp(esc(p), "g") }));

function leaksIn(text) {
  const hits = [];
  for (const line of text.split("\n")) {
    let m;
    codeSpanRe.lastIndex = 0;
    while ((m = codeSpanRe.exec(line))) hits.push(`\`${m[1]}\` (tool primitive in a code-span)`);
    for (const { p, re } of anywhereRes) {
      re.lastIndex = 0;
      if (re.test(line)) hits.push(p);
    }
  }
  return hits;
}

let fail = 0;
const report = [];
for (const rel of surface) {
  const hits = leaksIn(fs.readFileSync(path.join(CORE, rel), "utf8"));
  if (hits.length) {
    fail += hits.length;
    report.push(`  ✗ ${rel}: ${[...new Set(hits)].join(", ")}`);
  }
}

// Self-test: the deny-list must actually trip. If injecting a primitive into a
// code-span did NOT flag, the check is vacuous and that is itself a failure.
const probe = "the orchestrator reads `Bash` and writes `settings.json` via .claude/";
const probeHits = leaksIn(probe);
const vacuous = probeHits.length < 3; // expect `Bash`, settings.json, .claude/

console.log("NEUTRAL-PROSE (the core names no bare platform primitive):");
console.log(`  surface: ${surface.join(", ")}`);
if (report.length) report.forEach((r) => console.log(r));
if (vacuous) console.log("  ✗ self-test: the deny-list did not trip on an injected primitive");

if (fail || vacuous) {
  console.log(`\nFAIL — ${fail} primitive leak(s) in the core${vacuous ? " + vacuous check" : ""}`);
  process.exit(1);
}
console.log("\nPASS — core is platform-neutral, deny-list is live");
