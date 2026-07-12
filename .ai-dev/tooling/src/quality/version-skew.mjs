// Checks that the installed VERSION stamp matches the vendored tooling VERSION.
// A skew means the installer ran and updated the stamp, but the tooling dir was
// not re-vendored (or vice versa) — the two must always be bumped together.
// Skips silently when .ai-dev/VERSION or .ai-dev/tooling/VERSION is absent:
// on a source checkout neither file is present; on a downstream they must match.
import { readFileSync, existsSync } from "node:fs";

const stamp = ".ai-dev/VERSION";
const tooling = ".ai-dev/tooling/VERSION";

if (!existsSync(stamp) || !existsSync(tooling)) process.exit(0);

const installed = readFileSync(stamp, "utf8").trim();
const vendored = readFileSync(tooling, "utf8").trim();

if (installed !== vendored) {
  console.error(`version-skew: installed=${installed}, vendored tooling=${vendored}`);
  console.error("Re-run the installer to re-vendor the tooling at the installed version.");
  process.exit(1);
}
console.log(`version-skew: ok (${installed})`);
