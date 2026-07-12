// F4 floor — the trunk-commit + bulk-stage denies (backlog "Persona floor collapses…"
// finding F4). Both are DENY-class on BOTH platforms (the command is visible to both
// shims, no actor resolution needed — unlike orchestrator-content). Operator decision:
//   • git add -A / . / --all / *  ⇒ DENY (a blind bulk-stage leaks untracked transients
//     into history; the day-zero bootstrap stages NAMED paths instead).
//   • git commit on a main/master checkout of a CONFIGURED project WITH commit history
//     and no satisfied trunk stamp ⇒ DENY ("never commit to main"). Carve-outs: a
//     reviewed main-named change (main_review.md), the day-zero bootstrap (unconfigured
//     OR fresh-init repo with no commits), yolo (gate off).
//
// Each predicate ships its ALLOW + DENY matrix here (new-path coverage). The cross-
// platform parity is asserted in parity.test.mjs (both shims reach the same verdict).
//
// Run: node src/adapter/f4-floor.test.mjs

import { evaluate, loadConfig } from "./engine.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const config = loadConfig(HERE);
const g = "git ";

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; }
  else { fail++; console.log(`  ✗ ${name}: got ${got}, want ${want}`); }
}

// Build a root with the requested .git/HEAD branch, optional config, optional commit
// history (a loose ref under refs/heads/), optional satisfied trunk stamp.
function mkRoot({ branch, configured = false, commits = false, stampTrunk = null, profile = null } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-f4-"));
  fs.mkdirSync(path.join(root, ".git", "refs", "heads"), { recursive: true });
  if (branch) fs.writeFileSync(path.join(root, ".git", "HEAD"), `ref: refs/heads/${branch}\n`);
  if (commits && branch) {
    const ref = path.join(root, ".git", "refs", "heads", branch);
    fs.mkdirSync(path.dirname(ref), { recursive: true });
    fs.writeFileSync(ref, "abc1234\n");
  }
  if (configured || profile) {
    fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
    fs.writeFileSync(path.join(root, ".ai-dev", "config.json"), profile ? `{ "profile": "${profile}" }` : "{}");
  }
  if (stampTrunk) {
    const d = path.join(root, ".ai-dev", "reviews");
    fs.mkdirSync(d, { recursive: true });
    fs.writeFileSync(path.join(d, `${stampTrunk}_review.md`), "## Code review: APPROVED\n");
  }
  return root;
}
function verdict(root, command) {
  return evaluate({ act: "bash", root, command }, config);
}

// ── 1. git add -A / . / --all / * ⇒ DENY; named/.gitignore/-p ⇒ ALLOW ──────────
console.log("BULK STAGE (git add -A/./--all/* denies; named paths allow):");
for (const [name, cmd, want, rule] of [
  ["add-A", g + "add -A", "deny", "git-add-all"],
  ["add-dot", g + "add .", "deny", "git-add-all"],
  ["add-all", g + "add --all", "deny", "git-add-all"],
  ["add-star", g + "add *", "deny", "git-add-all"],
  ["add-Av-bundle", g + "add -Av", "deny", "git-add-all"],
  ["add-named", g + "add src/adapter/engine.mjs", "allow", null],
  ["add-patch", g + "add -p", "allow", null],
  ["add-gitignore", g + "add .gitignore", "allow", null], // a real file, not the `.` bulk token
  ["add-two-named", g + "add a.txt b.txt", "allow", null],
]) {
  const root = mkRoot({ branch: "feature/x", configured: true, commits: true });
  const v = verdict(root, cmd);
  check(`bulk:${name}`, v.verdict, want);
  if (rule) check(`bulk:${name}:ruleId`, v.ruleId, rule);
  fs.rmSync(root, { recursive: true, force: true });
}
// Quoted prose mentioning `git add -A` in a message is masked — never trips.
{
  const root = mkRoot({ branch: "feature/x", configured: true, commits: true });
  const v = verdict(root, g + 'commit -m "ran git add -A earlier"');
  check("bulk:quoted-prose-not-add", v.ruleId === "git-add-all", false);
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 2. git commit on main ⇒ DENY (configured + history); carve-outs ⇒ ALLOW ─────
console.log("COMMIT-ON-MAIN (denies for a configured project with history; carve-outs allow):");
const commit = g + "commit -m x";

// DENY: configured project, has history, on main/master, no stamp.
for (const branch of ["main", "master"]) {
  const root = mkRoot({ branch, configured: true, commits: true });
  const v = verdict(root, commit);
  check(`commit-main:${branch}:denies`, v.verdict, "deny");
  check(`commit-main:${branch}:ruleId`, v.ruleId, "commit-on-unstamped-main");
  fs.rmSync(root, { recursive: true, force: true });
}

// ALLOW: a feature-branch commit is the normal path.
{
  const root = mkRoot({ branch: "feature/x", configured: true, commits: true });
  check("commit-feature:allows", verdict(root, commit).verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
// ALLOW: a reviewed main-named change (main_review.md) still commits (the stamp carve-out).
{
  const root = mkRoot({ branch: "main", configured: true, commits: true, stampTrunk: "main" });
  check("commit-main-stamped:allows", verdict(root, commit).verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
// ALLOW: the day-zero bootstrap — an UNCONFIGURED fresh repo on main (no config, no commits).
{
  const root = mkRoot({ branch: "main", configured: false, commits: false });
  check("commit-main-bootstrap-unconfigured:allows", verdict(root, commit).verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
// ALLOW: a configured project but a FRESH-init repo with no commits yet (the carve-out's
// other half — `git init` happened, config written, but the initial commit hasn't landed).
{
  const root = mkRoot({ branch: "main", configured: true, commits: false });
  check("commit-main-bootstrap-fresh:allows", verdict(root, commit).verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
// ALLOW: yolo profile turns the gate off (consistency with the merge-gate).
{
  const root = mkRoot({ branch: "main", commits: true, profile: "yolo" });
  check("commit-main-yolo:allows", verdict(root, commit).verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
// ALLOW: a non-commit git command on main is untouched (scope check).
{
  const root = mkRoot({ branch: "main", configured: true, commits: true });
  check("status-on-main:allows", verdict(root, g + "status").verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
