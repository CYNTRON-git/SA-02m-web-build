// Inverse orphan-guard for the quality registry. The forward direction (every
// registry row points at a real, green check) is covered by run.mjs running them.
// This is the REVERSE: every committed `*.test.mjs` under src/ must be referenced
// by some row's `run` command in tools.json — so a test file cannot be added and
// then silently never run (the gap the post-re-unification audit found:
// install-commands.test.mjs and opencode-inject.test.mjs were tracked + passing
// but in no row, so absent from build/CI/audit; opencode-inject is even named in
// reviewer.md as the canonical enforcement-realisation pattern).
//
// Reads the REAL tools.json (a membership check, not a run — no recursion). This
// file is itself a *.test.mjs, so it must carry its own row; that row is what
// keeps THIS guard in the suite.
//
// Run: node src/quality/registry-coverage.test.mjs

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));   // …/src/quality
const repoRoot = path.resolve(here, "..", "..");
const srcDir = path.join(repoRoot, "src");

// Every *.test.mjs under src/ (posix-relative to repoRoot).
function testFiles(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const abs = path.join(dir, entry.name);
    if (entry.isDirectory()) { out.push(...testFiles(abs)); continue; }
    if (entry.isFile() && entry.name.endsWith(".test.mjs")) {
      out.push(path.relative(repoRoot, abs).split(path.sep).join("/"));
    }
  }
  return out;
}

const registry = JSON.parse(fs.readFileSync(path.join(here, "tools.json"), "utf8"));
const runCommands = (registry.tools ?? []).map((t) => t.run ?? "");
const referenced = (rel) => runCommands.some((cmd) => cmd.includes(rel));

const orphans = testFiles(srcDir).filter((rel) => !referenced(rel));

if (orphans.length) {
  console.log("REGISTRY-COVERAGE:");
  orphans.forEach((f) => console.log(`  ✗ ${f} — no tools.json row runs it`));
  console.log(`\nFAIL — ${orphans.length} test file(s) orphaned from the registry; add a row in src/quality/tools.json`);
  process.exit(1);
}
console.log(`PASS — every src/**/*.test.mjs is wired into the registry`);
