// Shape guard for the forge-issue verb map (src/adapter/forge-map.json).
// forge-map.json is PERSONA-READ reference data — no engine loads it, so without
// a test it could rot silently (the "data no code reads" smell). This is the
// minimal structural net: it parses, carries the three supported forges, and each
// forge declares all five backlog operations with a runnable command string.
// It does NOT assert flag accuracy (that is the persona-read reader's concern and
// some flags are honestly marked unverified) — only that the abstraction is whole.
//
// Run: node src/adapter/forge-map.test.mjs

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));   // …/src/adapter
const mapPath = path.join(here, "forge-map.json");

const FORGES = ["github", "gitlab", "gitea"];
const OPERATIONS = ["create", "list", "view", "close", "comment"];

const fail = (msg) => { console.log(`FORGE-MAP:\n  ✗ ${msg}`); process.exit(1); };

let map;
try {
  map = JSON.parse(fs.readFileSync(mapPath, "utf8"));
} catch (e) {
  fail(`forge-map.json does not parse as JSON: ${e.message}`);
}

const forges = map.forges;
if (!forges || typeof forges !== "object") fail("no `forges` object");

for (const forge of FORGES) {
  const f = forges[forge];
  if (!f || typeof f !== "object") fail(`missing forge: ${forge}`);
  if (typeof f.cli !== "string" || !f.cli) fail(`${forge}: missing cli string`);
  const ops = f.operations;
  if (!ops || typeof ops !== "object") fail(`${forge}: missing operations object`);
  for (const op of OPERATIONS) {
    const o = ops[op];
    if (!o || typeof o !== "object") fail(`${forge}: missing operation '${op}'`);
    if (typeof o.command !== "string" || !o.command) {
      fail(`${forge}.${op}: missing command string`);
    }
  }
}

console.log(`PASS — forge-map.json carries ${FORGES.length} forges, each with the ${OPERATIONS.length} operations [${OPERATIONS.join(", ")}]`);
