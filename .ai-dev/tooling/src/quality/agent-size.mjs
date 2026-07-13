// Mechanical size-guard for the assembled orchestrator load surfaces (audit H1 / NIT-1).
//
// The orchestrator load surface is a MEMORY file the harness injects into EVERY turn's
// context. Claude Code caps such a file at ~40,000 chars — past the cap, the tail of the
// file silently fails to load, so a section near the end (the audit found `## Fixup`
// onward at risk) is dropped without any error. This guard makes the cap a MECHANICAL
// build-beat finding instead of a thing only a human notices, so the H1 regression
// (the surface drifting back over the limit) cannot recur silently.
//
// THRESHOLD: the hard harness limit is 40,000; this guard trips at LIMIT (39,000) — a
// 1,000-char margin below the real cap, so the build goes red BEFORE a section is at
// risk, not after. The decompose target keeps the surface well under this (~26k), so the
// headroom (39k − 26k ≈ 13k) lets the core grow naturally before the guard fires; when it
// does fire, the remedy is `decompose` (move a heavy section body to .ai-dev/procedures/,
// leave a trigger→pointer stub — the pattern this guard's own feature established).
//
// Run: node src/quality/agent-size.mjs   (build beat)

import { readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// The trip point. ABOVE the decompose target (~38k) so a little growth is allowed, BELOW
// the 40,000 harness limit so the red fires with a 1k safety margin.
export const LIMIT = 39000;

// The assembled load surfaces this guard covers — one per platform adapter. Relative to
// the repo root (the cwd the runner invokes from). An absent artifact is SKIPPED, never a
// failure: a downstream project carries only its own active platform's surface, and this
// row is a protocol-repo concern; the guard must not red a tree that simply lacks a file.
export const SURFACES = [".claude/ai-dev.md", ".opencode/agents/ai-dev.md"];

// Pure check over a {path, chars} list: returns the over-LIMIT offenders (empty ⇒ green).
// Exported so the ratchet test exercises the threshold logic directly (over/under), with
// no filesystem.
export function overLimit(sizes, limit = LIMIT) {
  return sizes.filter((s) => s.chars > limit);
}

// Read the present surfaces' sizes. An absent surface is omitted (skip, not fail).
export function measureSurfaces(surfaces = SURFACES) {
  return surfaces
    .filter((p) => existsSync(p))
    .map((p) => ({ path: p, chars: readFileSync(p, "utf8").length }));
}

// CLI: measure the present surfaces, fail the build on any offender, else report green.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const sizes = measureSurfaces();
  if (sizes.length === 0) {
    console.log("agent-size: no assembled orchestrator surface present — nothing to check");
    process.exit(0);
  }
  const offenders = overLimit(sizes);
  if (offenders.length) {
    for (const o of offenders) {
      console.error(`agent-size: ${o.path} is ${o.chars} chars — over the ${LIMIT} limit (Claude memory-file cap ~40000; the tail would silently not load)`);
    }
    console.error("Decompose: move a heavy section body to .ai-dev/procedures/<x>.md and leave a trigger→pointer stub (.ai-dev/procedures/decompose.md).");
    process.exit(1);
  }
  console.log(`agent-size: ok — ${sizes.map((s) => `${s.path} ${s.chars}`).join(", ")} (limit ${LIMIT})`);
}
