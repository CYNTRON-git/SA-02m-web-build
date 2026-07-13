// Shared test helpers for the installer test files (install-core.test.mjs +
// install-claude-wiring.test.mjs). Extracted from the former monolithic install.test.mjs
// when it was decomposed (audit LOW-2) — ONE home for the checker, the snapshot/temp-dir
// helpers, ROOT/PKG_VERSION, and the straggler-cleanup sweep, so the two cohesive test
// files share them without duplication. NOT a `*.test.mjs` (it asserts nothing itself), so
// the registry-coverage guard does not require a row for it — it is a helper module the
// test rows' files import.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(HERE, "..", "..");
export const PKG_VERSION = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8")).version;

// A self-contained checker per test file: its OWN pass/fail counters (so each split file
// reports and exits on its own results), plus a `report(label, tail)` that prints the
// summary and exits non-zero on any failure — the same contract the monolith's trailing
// summary had, now reusable.
export function makeChecker() {
  let pass = 0, fail = 0;
  function check(name, cond) {
    if (cond) { pass++; console.log(`  ok   ${name}`); }
    else { fail++; console.log(`  FAIL ${name}`); }
  }
  function report(label, tail) {
    console.log(`\n${label}: ${pass} passed, ${fail} failed`);
    if (fail) process.exit(1);
    if (tail) console.log(tail);
  }
  return { check, report };
}

// Snapshot a directory tree as a sorted relPath→bytes map, for the idempotence diff.
export function snapshot(dir) {
  const out = {};
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else out[path.relative(dir, p)] = fs.readFileSync(p, "utf8");
    }
  };
  walk(dir);
  return out;
}

// A fresh temp target INSIDE the repo root (allowed by the boundary). Random suffix
// so parallel runs never collide; cleaned up at the end.
export function freshTarget(tag) {
  return fs.mkdtempSync(path.join(ROOT, `.tmp-install-${tag}-`));
}

// Catch-all hardening: even with every arm's `finally` cleanup, an arm that THROWS
// before its `finally`, or a transient EPERM/EBUSY from a just-exited install child,
// can leave a `.tmp-install-*` dir behind — which then flakes the eslint row (scandir
// into a half-deleted subtree) and pollutes `git status`. The .gitignore + eslint
// `ignores` entries neutralise both harms; this sweep keeps the tree physically clean
// by removing any straggler at process exit, with retries for the transient-lock case.
// Each split test file calls it once at load.
export function registerStragglerSweep() {
  process.on("exit", () => {
    for (const entry of fs.readdirSync(ROOT, { withFileTypes: true })) {
      if (entry.isDirectory() && entry.name.startsWith(".tmp-install-")) {
        try {
          fs.rmSync(path.join(ROOT, entry.name), { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
        } catch {
          // best-effort: gitignore + eslint-ignore already neutralise a survivor.
        }
      }
    }
  });
}
