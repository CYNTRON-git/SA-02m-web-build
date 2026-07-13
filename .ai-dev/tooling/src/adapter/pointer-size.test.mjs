// Mechanical size-guard for the session resume pointer (.ai-dev/state/current.md).
//
// The pointer is a THIN checkpoint: queue + cadence markers + non-canonical conventions.
// Durable project knowledge belongs in .ai-dev/notes/ (committed), not the pointer
// (gitignored, volatile). Without a cap the persona rule "keep it thin" demonstrably
// fails: the pointer bloats with knowledge that outlasts the session, and only the
// audit's durable-text-hygiene dimension catches it — too late, at cadence.
//
// This cap makes "pointer is thin" a MECHANICAL build-beat finding so the bloat
// cannot silently recur between audits. The remedy when it fires: move durable content
// to .ai-dev/notes/<topic>.md and supersede the pointer.
//
// CAP SELECTION: measured against a healthy compacted pointer (~24 lines / ~3.4KB).
//   LINE_CAP = 120  — 5× the healthy baseline; a pointer approaching this is already
//                     accumulating knowledge that belongs in .ai-dev/notes/.
//   CHAR_CAP = 8192 — ~2.4× the healthy baseline in bytes; same rationale.
// Both caps exceed any realistic compacted pointer while still catching a journal-style
// accumulation (hundreds of lines / tens of KB). Fail if EITHER is exceeded.
//
// Fail-safe: an ABSENT pointer (fresh clone, no current.md yet) is NOT a failure —
// only an over-cap present pointer is red. Mirrors agent-size.mjs's absent-is-skip rule.
//
// Run: node src/adapter/pointer-size.test.mjs   (build beat)

import { readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");

// The pointer path (always this path — never file-search).
export const POINTER_PATH = ".ai-dev/state/current.md";

// Hard caps — see header rationale.
export const LINE_CAP = 120;
export const CHAR_CAP = 8192;

// Pure check: given { lines, chars }, returns an array of exceeded-cap descriptions
// (empty ⇒ green). Exported so the test can exercise the threshold logic without the
// filesystem.
export function overCap(size, lineCap = LINE_CAP, charCap = CHAR_CAP) {
  const hits = [];
  if (size.lines > lineCap) hits.push(`${size.lines} lines > ${lineCap}-line cap`);
  if (size.chars > charCap) hits.push(`${size.chars} chars > ${charCap}-char cap`);
  return hits;
}

// Measure the pointer if present. Returns null when absent (fresh clone → skip, not fail).
export function measurePointer(pointerPath = POINTER_PATH, root = ROOT) {
  const abs = path.resolve(root, pointerPath);
  if (!existsSync(abs)) return null;
  const text = readFileSync(abs, "utf8");
  return { path: pointerPath, lines: text.split("\n").length, chars: text.length };
}

// ── logic tests (no filesystem) ────────────────────────────────────────────────
let pass = 0;
const fails = [];
function check(name, cond) {
  if (cond) { pass++; } else { fails.push(`  ✗ ${name}`); }
}

// exactly at cap → green
check("at the line cap ⇒ green", overCap({ lines: LINE_CAP, chars: 0 }).length === 0);
check("at the char cap ⇒ green", overCap({ lines: 0, chars: CHAR_CAP }).length === 0);
// one over cap → red
check("one line over ⇒ red", overCap({ lines: LINE_CAP + 1, chars: 0 }).length === 1);
check("one char over ⇒ red", overCap({ lines: 0, chars: CHAR_CAP + 1 }).length === 1);
// both over → two violations
check("both over ⇒ two violations", overCap({ lines: LINE_CAP + 1, chars: CHAR_CAP + 1 }).length === 2);
// well under → green
check("well under cap ⇒ green", overCap({ lines: 24, chars: 3400 }).length === 0);
// custom limits honoured
check("custom line cap honoured", overCap({ lines: 5, chars: 0 }, 4, 99999).length === 1);
check("custom char cap honoured", overCap({ lines: 0, chars: 5 }, 99999, 4).length === 1);

// ── live pointer check ─────────────────────────────────────────────────────────
// The committed .ai-dev/state/current.md (when present) must be under the cap.
// Absent pointer = fresh clone / gitignored = skip (not a failure).
const live = measurePointer();
if (live === null) {
  check("pointer absent (fresh clone) — skip, not a failure (ok)", true);
} else {
  const violations = overCap(live);
  check(
    `live pointer is present and under the cap (${live.lines} lines, ${live.chars} chars ≤ ${LINE_CAP}L/${CHAR_CAP}C)`,
    violations.length === 0,
  );
  if (violations.length > 0) {
    for (const v of violations) {
      console.error(`  pointer-size VIOLATION: ${v}`);
    }
    console.error("  Remedy: move durable content to .ai-dev/notes/<topic>.md and supersede the pointer.");
  }
}

// ── report ─────────────────────────────────────────────────────────────────────
if (fails.length) {
  console.error(`POINTER-SIZE TEST: ${pass} passed, ${fails.length} failed`);
  for (const f of fails) console.error(f);
  process.exit(1);
}
const liveNote = live ? `live pointer ${live.lines}L/${live.chars}C (cap ${LINE_CAP}L/${CHAR_CAP}C)` : "pointer absent (skip)";
console.log(`pointer-size.test: PASS (${pass} checks; ${liveNote})`);
