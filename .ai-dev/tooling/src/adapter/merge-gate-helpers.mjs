// Shared test fixtures for the merge-gate test suite.
// Imported by the split sibling files (merge-gate-*.test.mjs) — kept here so no
// fixture logic is duplicated.  Not a test file itself; carries no check() calls.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// A temp root whose .git/HEAD points at `branch` (the reliable signal the gate reads).
export function rootOnBranch(branch) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-mergegate-"));
  fs.mkdirSync(path.join(root, ".git"));
  fs.writeFileSync(path.join(root, ".git", "HEAD"), `ref: refs/heads/${branch}\n`);
  return root;
}

// Drop a SATISFIED review stamp for `topic` into a root (so the gate sees it stamped).
// Carries a `Contracts:` line too — the gate requires it (8D
// contracts-ignored-autonomous, fix B); omitting it here would make every
// caller of this fixture fail closed for a reason unrelated to what it tests.
export function stamp(root, topic) {
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, `${topic}_review.md`),
    "## Code review: APPROVED\n## Contracts: none\n"
  );
}

// A root whose HEAD is DETACHED (raw sha, no ref) — the HEAD signal yields nothing.
export function rootDetached() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-mergegate-det-"));
  fs.mkdirSync(path.join(root, ".git"));
  fs.writeFileSync(
    path.join(root, ".git", "HEAD"),
    "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678\n"
  );
  return root;
}

// A checkout whose `root/.git` is a FILE pointing at a real (private) git dir holding HEAD.
export function rootOnWorktree(branch) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-wt-root-"));
  const gitDir = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-wt-git-"));
  fs.writeFileSync(path.join(gitDir, "HEAD"), `ref: refs/heads/${branch}\n`);
  fs.writeFileSync(path.join(root, ".git"), `gitdir: ${gitDir}\n`); // absolute pointer (what git writes)
  return root;
}
