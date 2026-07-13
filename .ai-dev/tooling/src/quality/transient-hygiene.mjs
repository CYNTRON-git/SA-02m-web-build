// Advisory: surfaces stale loop transients — a plan / review-stamp / audit-run
// file whose feature already shipped, so no open local branch carries its topic.
// Ship-beat transient deletion (PROTOCOL.md beat 5; orchestrator.md `## Your
// seat`) is [persona]: a session that skips it (reset between build and ship, a
// batch shipped several features) leaves these files to accumulate. This check
// is the mechanical SURFACE for that persona discipline — it nudges, it never
// blocks (the same inject-class posture as docs/decisions/audit-cadence-backstop.md).
//
// ADVISORY — it ALWAYS exits 0, even when it finds stale transients: an overdue
// cleanup is not a build failure, and selling it as a gate would be a dishonest
// over-claim. Silent when clean. Under CI (GITHUB_ACTIONS) it also emits a
// GitHub Actions `::warning::` annotation per stale file, so the surface reaches
// the Operator on the PR — the model-independent audience that a relayed nudge
// depends on the model to reach.
//
// Fail-safe: no git / the dir absent / not a repo ⇒ no-op, exit 0, print nothing.
// Never a crash, never a false finding — an unknowable branch set yields no nudge.
//
// Topic derivation: a transient's topic is its filename minus `.md` (a review
// stamp additionally drops the trailing `_review`); a topic is STALE when no
// local branch — `feature/`/`fix/` prefix stripped — matches it.
//
// Usage: node src/quality/transient-hygiene.mjs
import { readdirSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

// The three transient dirs and how each file's topic is read from its name.
const SOURCES = [
  { dir: ".ai-dev/plans", match: (n) => n.endsWith(".md"), topic: (n) => n.slice(0, -".md".length) },
  { dir: ".ai-dev/reviews", match: (n) => n.endsWith("_review.md"), topic: (n) => n.slice(0, -"_review.md".length) },
  { dir: ".ai-dev/audit", match: (n) => n.endsWith(".md"), topic: (n) => n.slice(0, -".md".length) },
];

// The set of live topics from local git branches, or null when git is
// unavailable / this is not a repo — the fail-safe signal (caller no-ops). An
// empty-but-present repo (no commits yet ⇒ no branches) returns an empty Set,
// not null: it IS a repo, so a transient with no matching branch is genuinely
// stale, not unknowable.
function liveTopics(root) {
  let out;
  try {
    out = execFileSync("git", ["branch", "--format=%(refname:short)"], {
      cwd: root,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    });
  } catch {
    return null; // git absent, or not a repo ⇒ fail-safe: no finding
  }
  const topics = new Set();
  for (const raw of out.split("\n")) {
    const name = raw.trim();
    if (!name) continue;
    topics.add(name.replace(/^(?:feature|fix)\//, ""));
  }
  return topics;
}

// The core, exported so the self-test can drive it with an injected root.
// Returns the list of stale transient paths (relative to root); [] when clean
// OR on the fail-safe no-op (no git / no dirs) — both are exit-0 by design.
export function findStaleTransients(root) {
  const topics = liveTopics(root);
  if (topics === null) return [];

  const stale = [];
  for (const src of SOURCES) {
    const dir = path.join(root, src.dir);
    if (!existsSync(dir)) continue;
    let entries;
    try {
      entries = readdirSync(dir);
    } catch {
      continue;
    }
    for (const name of entries) {
      if (!src.match(name)) continue;
      if (!topics.has(src.topic(name))) stale.push(path.join(src.dir, name));
    }
  }
  return stale;
}

// Scan and report. ALWAYS returns 0 — advisory, never red.
export function run(root) {
  const stale = findStaleTransients(root);
  if (stale.length === 0) return 0; // silent when clean (or fail-safe no-op)

  const ci = !!process.env.GITHUB_ACTIONS;
  console.log(`transient-hygiene: ${stale.length} stale transient(s) — no open branch matches the topic (already shipped? prune it):`);
  for (const file of stale) {
    console.log(`  ${file}`);
    if (ci) console.log(`::warning file=${file}::Stale transient '${file}' — no open branch matches its topic. Prune it if the feature shipped.`);
  }
  console.log("Advisory only — an overdue cleanup never blocks a build. False alarm if the work is genuinely unstarted.");
  return 0;
}

// Self-locate the project root from this script's path: <root>/quality/<me> ⇒
// root is two levels up (mirrors run.mjs / neutral-prose.test.mjs), so "the
// project root I run from" is correct on every layout (src/quality/ here,
// .ai-dev/quality/ downstream).
function projectRoot() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  return path.resolve(here, "..", "..");
}

// CLI entry — only when invoked directly, not when imported by the self-test.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(run(projectRoot()));
}
