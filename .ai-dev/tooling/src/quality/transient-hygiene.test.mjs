// Self-test for the advisory transient-hygiene check (transient-hygiene.mjs).
// Drives the core against a REAL git fixture (git-init'd temp repos with real
// branches + real .ai-dev/{plans,reviews,audit} files) rather than a mocked
// branch list — the topic-matching logic is exactly the producer↔consumer seam
// (a real `git branch` format vs a real filename) the backlog's producer/consumer
// lesson says to feed real inputs. Asserts the flagged SET is right AND that the
// script stays advisory (exit 0) in every case, including the fail-safe no-ops.
//
// Run: node src/quality/transient-hygiene.test.mjs

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { findStaleTransients, run } from "./transient-hygiene.mjs";

let pass = 0;
const fails = [];
function check(name, got, want) {
  if (got === want) { pass++; return; }
  fails.push(`  ✗ ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}
// Compare a stale-list against an expected set, order-independent.
function checkSet(name, got, want) {
  check(name, JSON.stringify([...got].sort()), JSON.stringify([...want].sort()));
}
// Silence run()'s advisory chatter so the suite output stays clean (mirrors
// run.test.mjs); the exit code it returns is what we assert.
const realLog = console.log;
const quiet = (fn) => { console.log = () => {}; try { return fn(); } finally { console.log = realLog; } };

const made = [];
function tmp(prefix) {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), prefix)));
  made.push(dir);
  return dir;
}
function git(dir, args) {
  execFileSync("git", args, { cwd: dir, stdio: ["ignore", "ignore", "ignore"] });
}
// A git repo with one commit on `main` (a branch needs a commit to exist).
function initRepo(prefix) {
  const dir = tmp(prefix);
  git(dir, ["init", "-q", "-b", "main"]);
  fs.writeFileSync(path.join(dir, "seed.txt"), "seed\n");
  git(dir, ["add", "seed.txt"]);
  git(dir, ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"]);
  return dir;
}
function write(dir, rel, body) {
  const p = path.join(dir, rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, body ?? "x\n");
}

try {
  // 1–3. A repo with live branches feature/alive + fix/bugfix (plus main).
  // alive.md matches feature/alive ⇒ kept; bugfix_review.md matches fix/bugfix
  // (suffix _review stripped) ⇒ kept; the orphans (plan, stamp, audit run) flag.
  {
    const r = initRepo("ai-dev-th-live-");
    git(r, ["branch", "feature/alive"]);
    git(r, ["branch", "fix/bugfix"]);
    write(r, ".ai-dev/plans/alive.md");          // matches feature/alive ⇒ kept
    write(r, ".ai-dev/plans/orphan.md");          // no branch ⇒ flagged
    write(r, ".ai-dev/reviews/bugfix_review.md");  // matches fix/bugfix ⇒ kept
    write(r, ".ai-dev/reviews/gone_review.md");    // no branch ⇒ flagged
    write(r, ".ai-dev/audit/oldsweep.md");         // no branch ⇒ flagged
    checkSet("live-branch + orphans ⇒ only orphans flagged", findStaleTransients(r), [
      ".ai-dev/plans/orphan.md",
      ".ai-dev/reviews/gone_review.md",
      ".ai-dev/audit/oldsweep.md",
    ]);
    check("findings present ⇒ still exit 0 (advisory)", quiet(() => run(r)), 0);
  }

  // 4. A checkout with only `main` (no feature branch) ⇒ every transient flags.
  {
    const r = initRepo("ai-dev-th-mainonly-");
    write(r, ".ai-dev/plans/foo.md");
    write(r, ".ai-dev/reviews/bar_review.md");
    checkSet("only main ⇒ all flagged", findStaleTransients(r), [
      ".ai-dev/plans/foo.md",
      ".ai-dev/reviews/bar_review.md",
    ]);
    check("only main ⇒ exit 0", quiet(() => run(r)), 0);
  }

  // 5. A git repo with NO .ai-dev dir ⇒ no-op, nothing flagged, exit 0.
  {
    const r = initRepo("ai-dev-th-nodir-");
    checkSet("absent .ai-dev ⇒ no finding", findStaleTransients(r), []);
    check("absent .ai-dev ⇒ exit 0", quiet(() => run(r)), 0);
  }

  // 6. A NON-git dir carrying transients ⇒ fail-safe no-op (branch set
  //    unknowable ⇒ no false finding), exit 0.
  {
    const r = tmp("ai-dev-th-nongit-");
    write(r, ".ai-dev/plans/whatever.md");
    checkSet("non-git ⇒ no finding (fail-safe)", findStaleTransients(r), []);
    check("non-git ⇒ exit 0", quiet(() => run(r)), 0);
  }
} finally {
  for (const d of made) fs.rmSync(d, { recursive: true, force: true });
}

if (fails.length) {
  console.log("TRANSIENT-HYGIENE SELF-TEST:");
  fails.forEach((f) => console.log(f));
  console.log(`\nFAIL — ${fails.length} case(s) failed`);
  process.exit(1);
}
console.log(`PASS — ${pass} passed`);
