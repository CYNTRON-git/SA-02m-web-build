// Command-assembly test — proves the `/dev-setup` explicit-trigger command assembles
// correctly for BOTH platforms from ONE neutral body + per-platform frontmatter,
// and that it stays a THIN WRAPPER (no copy of the `## Setup` dialog). The contract
// this locks (PROTOCOL.md invariant 6, single home):
//   • each platform's installer writes a setup command file;
//   • the file carries the neutral body's pointer at the orchestrator's `## Setup`;
//   • the per-platform frontmatter is right (OpenCode targets `agent: ai-dev`);
//   • the body does NOT restate the dialog steps — it points, never copies.
// A regression in either direction fails loudly here.
//
// Run: node src/adapter/install-commands.test.mjs

import { install as claudeInstall } from "./claude/install-commands.mjs";
import { install as opencodeInstall } from "./opencode/install-commands.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

let pass = 0, fail = 0;
function check(name, cond) {
  if (cond) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}`); }
}

function assembled(install) {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-cmd-"));
  const outPath = install(outDir);
  const text = fs.readFileSync(outPath, "utf8");
  const base = path.basename(outPath);
  fs.rmSync(outDir, { recursive: true, force: true });
  return { text, base };
}

// The single-home guard: the body must POINT at `## Setup`, never restate it. The
// real dialog (src/agents/orchestrator.md `## Setup`) is a NUMBERED 3-step procedure
// with the mode enumeration; a thin wrapper has none of that. We assert the
// pointer is present AND the copied-dialog markers are absent.
const POINTER = "## Setup"; // the wrapper references the single home by section name
const DIALOG_MARKERS = [
  "1. **Discover",          // the numbered step headers only the real procedure has
  "2. **Ask",
  "3. **Write",
  "`autonomous`/`interactive`", // the mode enumeration lives only in the real dialog
];

for (const [platform, install, wantBase, wantFm] of [
  ["claude", claudeInstall, "dev-setup.md", null],
  ["opencode", opencodeInstall, "dev-setup.md", "agent: ai-dev"],
]) {
  const { text, base } = assembled(install);
  check(`${platform}: writes ${wantBase}`, base === wantBase);
  check(`${platform}: has frontmatter block`, /^---\n[\s\S]*?\n---\n/.test(text));
  check(`${platform}: body points at the \`## Setup\` single home`, text.includes(POINTER));
  if (wantFm) check(`${platform}: frontmatter carries ${wantFm}`, new RegExp("^" + wantFm.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "$", "m").test(text));
  // single-home guard: NONE of the copied-dialog markers may appear.
  const leaked = DIALOG_MARKERS.filter((m) => text.includes(m));
  check(`${platform}: no dialog copy (thin wrapper)`, leaked.length === 0);
}

// Both platforms share the ONE neutral body — assert the body text matches across
// the two assembled files (frontmatter aside), the real no-duplication assertion.
const claudeBody = assembled(claudeInstall).text.split("\n---\n").slice(1).join("\n---\n").trim();
const opencodeBody = assembled(opencodeInstall).text.split("\n---\n").slice(1).join("\n---\n").trim();
check("both platforms share the one neutral body (no per-platform dialog copy)", claudeBody === opencodeBody);

console.log(`\nINSTALL-COMMANDS: ${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
console.log("PASS — /dev-setup assembles for both platforms, thin wrapper, one shared body");
