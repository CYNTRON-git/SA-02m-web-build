// Merge-gate: topic resolution, stamp forms, and unresolvable-topic edge cases.
//
// Covers the blocks whose logic lives entirely in how a topic is resolved from
// the branch name and how the stamp heading is read:
//
//   1–2. resolveMergeTopic once matched only `feature/<topic>` — an UNSTAMPED
//        fix/ push resolved null → failed open → the floor escaped.
//   3.   stamp-form edges: split-line verdicts (a reviewer writing "## Code review:\n
//        APPROVED" instead of inline), heading-level variants.
//   4.   unresolvable-topic edges: detached HEAD ⇒ ask, never a silent pass.
//   7.   the accepted stamp labels are exactly the documented two (the pre-4.0
//        `## Validation:` label was dropped — an intentional removal, pinned).
//
// Run: node src/adapter/merge-gate-stamp-resolution.test.mjs

import { evaluate, loadConfig, _internals } from "./engine.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { rootOnBranch, stamp, rootDetached } from "./merge-gate-helpers.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const config = loadConfig(HERE);
const { resolveMergeTopic } = _internals;

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; }
  else { fail++; console.log(`  ✗ ${name}: got ${got}, want ${want}`); }
}

// ── 1. TOPIC RESOLUTION — any prefix stripped ────────────────────────────────
console.log("TOPIC RESOLUTION (any branch prefix stripped):");

// 1a. via the command (no .git/HEAD in this root → command fallback).
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-mergegate-cmd-"));
  for (const [branch, topic] of [
    ["feature/foo", "foo"],   // UNCHANGED — must resolve exactly as before
    ["fix/bar", "bar"],
    ["hotfix/x", "x"],
  ]) {
    check(`cmd:${branch}`, resolveMergeTopic(`git push origin ${branch}`, root), topic);
  }
  // A bare topic in a push command has no slash → not a branch ref → no command
  // match (a remote-only token is correctly ignored); HEAD is the bare-topic path.
  fs.rmSync(root, { recursive: true, force: true });
}

// 1b. via HEAD (the reliable signal) — including the bare, slash-less branch.
for (const [branch, topic] of [
  ["feature/foo", "foo"],
  ["fix/bar", "bar"],
  ["hotfix/x", "x"],
  ["topic", "topic"],       // bare branch, no slash → the whole name is the topic
]) {
  const root = rootOnBranch(branch);
  check(`head:${branch}`, resolveMergeTopic("git push", root), topic);
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 2. THE SECURITY CASE — an unstamped fix/ push is now DENIED ───────────────
console.log("SECURITY CASE (the fix/ hole is closed):");

// 2a. UNSTAMPED fix/ push ⇒ DENY (was ALLOW — the bug).
{
  const root = rootOnBranch("fix/leak");
  const v = evaluate({ act: "bash", root, command: "git push origin fix/leak" }, config);
  check("unstamped-fix-push:denies", v.verdict, "deny");
  check("unstamped-fix-push:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 2b. STAMPED fix/ push ⇒ ALLOW (the gate is satisfied, not blanket-blocking).
{
  const root = rootOnBranch("fix/leak");
  stamp(root, "leak");
  const v = evaluate({ act: "bash", root, command: "git push origin fix/leak" }, config);
  check("stamped-fix-push:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 2c. REGRESSION — an unstamped feature/ push STILL denies (the original floor).
{
  const root = rootOnBranch("feature/foo");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/foo" }, config);
  check("unstamped-feature-push:denies", v.verdict, "deny");
  check("unstamped-feature-push:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 3. SPLIT-LINE STAMP — verdict on the next line is also accepted ───────────
// Guards the stampOK() fallback: a reviewer that writes "## Code review:\nAPPROVED"
// instead of the canonical inline form must still satisfy the gate.
console.log("SPLIT-LINE STAMP (next-line verdict accepted):");

// 3a. Verdict on the next line ⇒ ALLOW (the new fallback).
{
  const root = rootOnBranch("feature/split");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "split_review.md"), "## Code review:\nAPPROVED\n\nFull review body follows.\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/split" }, config);
  check("split-line-stamp:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 3b. Empty heading + no next-line content ⇒ DENY (truly empty stamp still blocks).
{
  const root = rootOnBranch("feature/empty");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "empty_review.md"), "## Code review:\n\n\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/empty" }, config);
  check("empty-stamp:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 3c. "NOT YET RUN" on the next line ⇒ DENY (the NOT-YET-RUN guard applies to fallback content).
{
  const root = rootOnBranch("feature/nyr");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "nyr_review.md"), "## Code review:\nNOT YET RUN\n\nMore text.\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/nyr" }, config);
  check("next-line-nyr:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 3d. Verdict after a blank separator line ⇒ DENY (the fallback matches only the immediately-next line).
{
  const root = rootOnBranch("feature/blanksep");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "blanksep_review.md"), "## Code review:\n\nAPPROVED\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/blanksep" }, config);
  check("blank-separator:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 3e. A heading as the next line ⇒ DENY (the [^\r\n#] guard excludes it).
{
  const root = rootOnBranch("feature/nextheading");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "nextheading_review.md"), "## Code review:\n## Another section\nAPPROVED\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/nextheading" }, config);
  check("next-heading:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 3f. HEADING LEVEL is incidental — any level (#…######) is accepted (8D
// reviewer-stamp-heading-level): a reviewer opening a fresh file with an H1
// title, or any other level, satisfies the gate. The load-bearing part is the
// label + the verdict on the heading line, not the `#` count.
{
  for (const [lvl, hashes] of [["h1", "#"], ["h3", "###"], ["h6", "######"]]) {
    const root = rootOnBranch(`feature/level-${lvl}`);
    const dir = path.join(root, ".ai-dev", "reviews");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, `level-${lvl}_review.md`), `${hashes} Code review: APPROVED\n`);
    const v = evaluate({ act: "bash", root, command: `git push origin feature/level-${lvl}` }, config);
    check(`heading-${lvl}:allows`, v.verdict, "allow");
    fs.rmSync(root, { recursive: true, force: true });
  }
}

// ── 4. UNRESOLVABLE TOPIC — ask, never a silent pass ──────────────────────────
// Guards the no-silent-pass companion (merge-topic-unresolvable, ask-class): when
// neither HEAD nor the command yields a topic, the stamp is UNCHECKABLE — the old
// behavior allowed the push (fail open); now the Operator is asked.
console.log("UNRESOLVABLE TOPIC (ask, never pass):");

// 4a. Detached HEAD + bare push (no branch ref in the command) ⇒ ASK.
{
  const root = rootDetached();
  const v = evaluate({ act: "bash", root, command: "git push" }, config);
  check("detached-bare-push:asks", v.verdict, "ask");
  check("detached-bare-push:ruleId", v.ruleId, "merge-topic-unresolvable");
  fs.rmSync(root, { recursive: true, force: true });
}

// 4b. Detached HEAD + a refspec to TRUNK (HEAD:main) ⇒ DENY (F1: an explicit trunk
// push is denied, never asked — it was the silent-pass hole on OpenCode). The earlier
// behaviour here was ASK; the F1 trunk-push deny now (correctly) outranks it.
{
  const root = rootDetached();
  const v = evaluate({ act: "bash", root, command: "git push uni HEAD:main" }, config);
  check("detached-refspec-trunk:denies", v.verdict, "deny");
  check("detached-refspec-trunk:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 4b'. Detached HEAD + a refspec to a NON-trunk unresolvable ref (HEAD:release) ⇒ still
// ASK — the genuinely-unresolvable detached case the ask rule is for (no slash, no
// trunk, no HEAD branch ref).
{
  const root = rootDetached();
  const v = evaluate({ act: "bash", root, command: "git push uni HEAD:release" }, config);
  check("detached-refspec-nontrunk:asks", v.verdict, "ask");
  check("detached-refspec-nontrunk:ruleId", v.ruleId, "merge-topic-unresolvable");
  fs.rmSync(root, { recursive: true, force: true });
}

// 4c. Detached HEAD + a slashed branch ref in the command ⇒ RESOLVES via the
// command fallback ⇒ the ordinary deny path, not ask (unstamped here).
{
  const root = rootDetached();
  const v = evaluate({ act: "bash", root, command: "git push origin feature/foo" }, config);
  check("detached-cmd-ref:denies", v.verdict, "deny");
  check("detached-cmd-ref:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 4d. A resolvable, STAMPED push stays ALLOW — the ask rule does not over-fire.
{
  const root = rootOnBranch("feature/ok");
  stamp(root, "ok");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/ok" }, config);
  check("resolved-stamped:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 4e. A non-merge/push git command with no resolvable topic ⇒ ALLOW (scope check).
{
  const root = rootDetached();
  const v = evaluate({ act: "bash", root, command: "git status" }, config);
  check("non-push-detached:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 7. STAMP LABELS — the documented set, exactly ─────────────────────────────
// `## Validation:` (the pre-4.0 documentation-kind label; no current role writes
// it) was DROPPED from the accepted set — reviewer.md documents `## Code review:`
// and `## Doc review:`, and the engine accepts exactly those. An intentional
// behaviour removal, pinned here.
console.log("STAMP LABELS (Code review / Doc review accepted; Validation dropped):");

// 7a. A `## Validation:` stamp no longer satisfies the gate ⇒ DENY.
{
  const root = rootOnBranch("feature/val");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "val_review.md"), "## Validation: APPROVED\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/val" }, config);
  check("validation-label:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 7b. The `## Doc review:` label (docs-kind projects) still satisfies ⇒ ALLOW.
{
  const root = rootOnBranch("feature/docs");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "docs_review.md"), "## Doc review: APPROVED\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/docs" }, config);
  check("doc-review-label:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
