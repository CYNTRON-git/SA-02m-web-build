// Invariants-parity test — the one-home guard for the <!-- ai-dev:invariants -->
// composition. Every assembled role agent must contain the PROTOCOL.md §Invariants
// block verbatim. If someone edits PROTOCOL.md §Invariants without re-baking the
// assembled agents, this test fails (the drift the guard is named for).
//
// The test reads the COMMITTED assembled agents (.claude/agents/dev-*.md) and the
// live PROTOCOL.md, then checks each agent contains the extracted block byte-for-byte.
// Both sides use the same extractInvariantsSection() so the extraction is one home.
//
// Run: node src/adapter/invariants-parity.test.mjs

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { extractInvariantsSection } from "./modules.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");

let pass = 0, fail = 0;
function check(name, cond) {
  if (cond) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}`); }
}

// The expected block — extracted from PROTOCOL.md (the single source of truth).
const expected = extractInvariantsSection(ROOT);

// Each assembled Claude role agent must contain the block verbatim.
const AGENTS = [
  { rel: path.join(".claude", "agents", "dev-builder.md"), role: "builder" },
  { rel: path.join(".claude", "agents", "dev-planner.md"), role: "planner" },
  { rel: path.join(".claude", "agents", "dev-reviewer.md"), role: "reviewer" },
  { rel: path.join(".claude", "agents", "dev-reviewer-fixup.md"), role: "reviewer-fixup" },
];

console.log("INVARIANTS-PARITY (each assembled agent contains PROTOCOL.md §Invariants verbatim):");
for (const { rel, role } of AGENTS) {
  const p = path.join(ROOT, rel);
  const text = fs.readFileSync(p, "utf8");
  check(`${rel} (${role}) contains §Invariants verbatim`, text.includes(expected));
}

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
