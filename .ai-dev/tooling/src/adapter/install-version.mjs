// The version / upgrade channel (docs/decisions/upgrade-migration.md): resolve the
// installing protocol's own version, read the target's prior version, stamp the new
// one + write the UPGRADING handoff marker on a change, and the stale-npx-cache
// heuristic. The mechanical half of the upgrade side-tool's trigger.

import fs from "node:fs";
import path from "node:path";
import { SOURCE } from "./install-fs.mjs";

// ── version stamp + upgrade detection (docs/decisions/upgrade-migration.md) ──

// The installing protocol's own version: this repo's package.json when running from
// the repo / the npx package; the vendored stamp (.ai-dev/tooling/VERSION, written by
// vendorTooling) when the vendored installer re-runs from a downstream's tooling dir.
export function resolveSourceVersion() {
  const pkg = path.join(SOURCE, "package.json");
  if (fs.existsSync(pkg)) return JSON.parse(fs.readFileSync(pkg, "utf8")).version;
  const vendored = path.join(SOURCE, "VERSION");
  if (fs.existsSync(vendored)) return fs.readFileSync(vendored, "utf8").trim();
  throw new Error("cannot resolve the protocol version — neither package.json nor VERSION sits beside the installer's source root");
}

// The target's previously installed version, read BEFORE any write: the stamp where
// one exists; "pre-5.10" for an existing .ai-dev/ tree that predates the stamp
// (every version before the stamp shipped); null for a fresh install (no upgrade).
export function readPriorVersion(target) {
  const stamp = path.join(target, ".ai-dev", "VERSION");
  if (fs.existsSync(stamp)) return fs.readFileSync(stamp, "utf8").trim();
  if (fs.existsSync(path.join(target, ".ai-dev"))) return "pre-5.10";
  return null;
}

// Stamp .ai-dev/VERSION on every run (outside the agent-read-denied tooling dir, so
// the session can read it). On a detected version CHANGE additionally write the
// transient handoff marker .ai-dev/UPGRADING.md and print the restart notice — an
// installer cannot restart a session; the print is the whole ask. The marker is the
// session's to consume and delete (`.ai-dev/procedures/upgrade.md`); a same-version re-run
// never touches it, so idempotence holds.
export function stampVersion(target, version, prior) {
  fs.mkdirSync(path.join(target, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(target, ".ai-dev", "VERSION"), version + "\n");
  // An unconsumed marker from an earlier bump keeps its ORIGIN: the migration range
  // must start at the last version whose notes actually RAN, not at the last
  // stamped (installed-but-unmigrated) one — chained re-runs must not eat notes.
  const markerPath = path.join(target, ".ai-dev", "UPGRADING.md");
  if (prior && fs.existsSync(markerPath)) {
    const origin = fs.readFileSync(markerPath, "utf8").match(/Upgraded (\S+) → /);
    if (origin) prior = origin[1];
  }
  if (!prior || prior === version) return false;
  const date = new Date().toISOString().slice(0, 10);
  fs.writeFileSync(
    markerPath,
    `# Protocol upgrade pending\n\nUpgraded ${prior} → ${version} on ${date}.\n` +
      `Next session: read the \`(${prior}, ${version}]\` sections of \`.ai-dev/upgrades.md\` and run the upgrade check ` +
      "(`.ai-dev/procedures/upgrade.md`); the check deletes this marker last.\n",
  );
  console.log(`\n⚠ Protocol upgraded ${prior} → ${version} — RESTART YOUR SESSION.`);
  console.log("  The next session will offer the migration check (.ai-dev/UPGRADING.md → .ai-dev/upgrades.md).");
  return true;
}

// The stale-npx-cache heuristic (item 2), as a pure predicate so it is unit-testable
// without a heavyweight _npx tree on disk. A re-run that did NOT bump the version
// (prior === version, both non-null) is the stale-no-op signature WHEN the source was
// fetched via npx — detected by an `_npx` path SEGMENT in the installer's own source
// root (npx extracts the GitHub checkout under <cache>/_npx/...). HEURISTIC, not
// detection: the installer runs FROM the (possibly stale) source, so it cannot KNOW it
// is stale; this surfaces the signal so the Operator can judge. Absent `_npx` ⇒ false
// (no false alarm: a git-clone / repo run never trips it).
export function isStaleNpxReRun(sourcePath, prior, version) {
  if (!prior || !version || prior !== version) return false;
  return String(sourcePath).split(path.sep).includes("_npx");
}
