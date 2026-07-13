// Ratchet for the assembled-orchestrator size-guard (agent-size.mjs, audit H1 / NIT-1).
// Pins the threshold logic so a future edit cannot invert it (e.g. flip the comparison so
// an over-limit file passes) without going RED. No filesystem — the pure `overLimit` is
// exercised directly over crafted size lists, plus the real LIVE surfaces are asserted to
// sit under the limit (the end-to-end half: the committed assembled artifacts fit).
//
// Run: node src/quality/agent-size.test.mjs

import { overLimit, measureSurfaces, LIMIT } from "./agent-size.mjs";

let pass = 0;
const fails = [];
function check(name, cond) {
  if (cond) { pass++; } else { fails.push(`  ✗ ${name}`); }
}

// ── pure threshold logic ───────────────────────────────────────────────────────
// A file exactly AT the limit is green (the limit is the max allowed); one char over is red.
check("at the limit ⇒ no offender (green)", overLimit([{ path: "a", chars: LIMIT }]).length === 0);
check("one char over ⇒ one offender (red)", overLimit([{ path: "a", chars: LIMIT + 1 }]).length === 1);
check("well under ⇒ green", overLimit([{ path: "a", chars: 26000 }]).length === 0);
check("empty list ⇒ green", overLimit([]).length === 0);

// only the over-limit entries are returned, by path
const mixed = overLimit([
  { path: "small", chars: 100 },
  { path: "big", chars: LIMIT + 5000 },
  { path: "edge", chars: LIMIT },
]);
check("mixed list returns ONLY the over-limit offender", mixed.length === 1 && mixed[0].path === "big");

// the custom-limit arg overrides the default (so the threshold is data, not hard-wired)
check("custom limit arg is honoured", overLimit([{ path: "a", chars: 500 }], 400).length === 1);

// ── live surfaces fit (end-to-end) ─────────────────────────────────────────────
// The committed assembled artifacts must be present in this source repo AND under the
// limit — the decompose's whole point. RED if a surface drifts back over the cap.
const live = measureSurfaces();
check("at least one live orchestrator surface is present in the repo", live.length >= 1);
check("every live surface is under the limit", overLimit(live).length === 0);

// ── report ──────────────────────────────────────────────────────────────────────
if (fails.length) {
  console.error(`AGENT-SIZE TEST: ${pass} passed, ${fails.length} failed`);
  for (const f of fails) console.error(f);
  process.exit(1);
}
console.log(`agent-size.test: PASS (${pass} checks; live surfaces ${live.map((s) => `${s.path}=${s.chars}`).join(", ")}, limit ${LIMIT})`);
