// Fixup-agent install test — covers both platforms end-to-end (Claude + OpenCode
// install into tmp dirs), verifying that dev-reviewer-fixup.md is correctly assembled
// as a floor-only variant: §Invariants present verbatim, full floor prose present,
// NO module fragments, strictly smaller than the full reviewer by >10,000 chars,
// naming convention, and (Claude only) the model pin is preserved.
//
// Run: node src/adapter/install-fixup-agents.test.mjs

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { extractInvariantsSection } from "./modules.mjs";
import { install as installClaude } from "./claude/install-agents.mjs";
import { install as installOpencode } from "./opencode/install-agents.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const config = JSON.parse(fs.readFileSync(path.join(ROOT, ".ai-dev", "config.json"), "utf8"));

const invariants = extractInvariantsSection(ROOT);
// A floor-level string that lives in ## Check (the floor body, not a module fragment).
const FLOOR_PROBE = "a security-relevant change names its threats";
// Any module fragment contains "module is on" — present in full body, absent in fixup.
const MODULE_MARKER = "module is on";

let pass = 0, fail = 0;
function check(name, cond, hint) {
  if (cond) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}${hint ? `\n       ${hint}` : ""}`); }
}

function runPlatform(label, installFn, agentsDir) {
  console.log(`\n--- ${label} ---`);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), `ai-dev-fixup-${label.toLowerCase()}-`));
  const outDir = path.join(tmp, agentsDir);
  const written = installFn(outDir, config);

  const reviewerId = config.roles?.reviewer?.agent;
  const fixupId = `${reviewerId}-fixup`;

  // 1. Both the full and fixup variants are produced.
  check(`${label}: dev-reviewer.md produced`, !!written[reviewerId]);
  check(`${label}: ${fixupId}.md produced`, !!written[fixupId]);

  if (!written[fixupId]) return; // nothing more to check without the file

  const fullText = fs.readFileSync(written[reviewerId], "utf8");
  const fixupText = fs.readFileSync(written[fixupId], "utf8");

  // 2. §Invariants present verbatim in the fixup body.
  check(
    `${label}: §Invariants present verbatim in fixup body`,
    fixupText.includes(invariants),
    "composeFloorOnly must compose the invariants marker",
  );

  // 3. Floor prose present (the floor-level security item, not a module fragment).
  check(
    `${label}: floor prose present in fixup body`,
    fixupText.includes(FLOOR_PROBE),
    `expected to find "${FLOOR_PROBE}" in the fixup body`,
  );

  // 4. No module fragment markers in fixup body.
  check(
    `${label}: no module fragment markers in fixup body`,
    !fixupText.includes(MODULE_MARKER),
    `"${MODULE_MARKER}" found in fixup body — composeFloorOnly did not suppress modules`,
  );

  // 5. The raw modules marker is consumed (not left in the body).
  check(
    `${label}: <!-- ai-dev:modules --> marker consumed in fixup body`,
    !fixupText.includes("<!-- ai-dev:modules -->"),
    "the marker must be replaced, not left in the body",
  );

  // 6. The raw invariants marker is consumed.
  check(
    `${label}: <!-- ai-dev:invariants --> marker consumed in fixup body`,
    !fixupText.includes("<!-- ai-dev:invariants -->"),
    "the invariants marker must be replaced, not left in the body",
  );

  // 7. Strictly smaller — must save at least 10,000 chars vs the full body.
  const fullChars = fullText.length;
  const fixupChars = fixupText.length;
  const saved = fullChars - fixupChars;
  check(
    `${label}: fixup body is strictly smaller by >10,000 chars (saved ${saved})`,
    fixupChars < fullChars && saved > 10_000,
    `full=${fullChars} fixup=${fixupChars} saved=${saved} — expected saved > 10000`,
  );

  // 8. Naming convention: fixupId === reviewerId + "-fixup".
  check(
    `${label}: fixup agent id is {reviewerId}-fixup`,
    fixupId === `${reviewerId}-fixup`,
    `expected ${reviewerId}-fixup, got ${fixupId}`,
  );

  // Platform-specific checks.
  if (label === "Claude") {
    // 9. Model pin: the fixup body should carry the same model: line as the full body.
    const fullModelLine = fullText.split("\n").find((l) => l.startsWith("model:")) ?? "";
    const fixupModelLine = fixupText.split("\n").find((l) => l.startsWith("model:")) ?? "";
    check(
      `Claude: model pin in fixup frontmatter matches full reviewer (${fullModelLine || "none"})`,
      fullModelLine === fixupModelLine,
      `full has "${fullModelLine}", fixup has "${fixupModelLine}"`,
    );
  }

  console.log(`  info: ${label} fixup-agents: ${fixupId} chars=${fixupChars} vs ${reviewerId} chars=${fullChars} (saved ${saved})`);
}

runPlatform("Claude", installClaude, path.join(".claude", "agents"));
runPlatform("OpenCode", installOpencode, path.join(".opencode", "agents"));

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
