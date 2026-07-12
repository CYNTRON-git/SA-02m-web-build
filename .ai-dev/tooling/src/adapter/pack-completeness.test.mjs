// Pack-completeness guard (#369 regression, shipped-5.60.0 downstream ENOENT).
//
// The installer copyFiles some ROOT-level files from SOURCE (PROTOCOL.md,
// protocol-reference.md) into a downstream project. npm/git only publishes what
// package.json `files` whitelists, so a root file the installer copies but the
// whitelist omits is ABSENT from the packed package — the install then ENOENTs
// (5.60.0: `copyfile … protocol-reference.md` → no such file). The existing
// install-drift tests run in DOGFOOD mode (SOURCE === target, every root file
// present), so they never exercise the packed tarball and cannot catch this.
//
// This guard closes that gap: extract every single-segment root file the
// installer copies from SOURCE, then assert each is in `npm pack --dry-run`'s
// published file list. A new installer root-copy that isn't whitelisted fails here.
//
// Run: node src/adapter/pack-completeness.test.mjs

import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
let pass = 0, fail = 0;
const check = (name, ok) => { if (ok) { pass++; console.log(`  ok   ${name}`); } else { fail++; console.log(`  FAIL ${name}`); } };

// 1. Extract root-level SOURCE files the installer copies: path.join(SOURCE, "X.ext")
//    where X is a single segment (a root file) — multi-arg src/... copies are
//    covered by the "src/" entry in `files` and are excluded by the no-slash class.
const installSrc = fs.readFileSync(path.join(ROOT, "src", "adapter", "install-core.mjs"), "utf8");
const rootCopyRe = /path\.join\(\s*SOURCE\s*,\s*"([^"/]+\.[A-Za-z0-9]+)"\s*\)/g;
const rootFiles = new Set();
for (const m of installSrc.matchAll(rootCopyRe)) rootFiles.add(m[1]);

check("[pack] extracted at least the known root docs (PROTOCOL.md, protocol-reference.md)",
  rootFiles.has("PROTOCOL.md") && rootFiles.has("protocol-reference.md"));

// 2. Ask npm what it would publish.
const r = spawnSync("npm", ["pack", "--dry-run", "--json"], { cwd: ROOT, encoding: "utf8" });
let packed = null;
try { packed = new Set(JSON.parse(r.stdout)[0].files.map((f) => f.path)); } catch { /* leave null */ }
check("[pack] `npm pack --dry-run --json` produced a parseable file list", !!packed);

// 3. Every installer root-copied file must be in the packed set.
if (packed) {
  for (const f of rootFiles) {
    check(`[pack] installer root file "${f}" is in the published package`, packed.has(f));
  }
}

console.log(fail === 0 ? `\nPACK-COMPLETENESS: PASS — ${pass} passed` : `\nPACK-COMPLETENESS: FAIL — ${fail} failed, ${pass} passed`);
process.exit(fail === 0 ? 0 : 1);
