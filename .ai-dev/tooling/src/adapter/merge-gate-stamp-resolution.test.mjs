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
const { resolveMergeTopic, STAMP_SEPARATORS, STAMP_LABELS } = _internals;

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
  fs.writeFileSync(path.join(dir, "split_review.md"), "## Code review:\nAPPROVED\n\n## Contracts: none\n\nFull review body follows.\n");
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
    fs.writeFileSync(path.join(dir, `level-${lvl}_review.md`), `${hashes} Code review: APPROVED\n${hashes} Contracts: none\n`);
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
  fs.writeFileSync(path.join(dir, "docs_review.md"), "## Doc review: APPROVED\n## Contracts: none\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/docs" }, config);
  check("doc-review-label:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 8. CONTRACTS ANCHOR — the verdict alone no longer satisfies the gate ──────
// 8D contracts-ignored-autonomous, fix B: a stamp missing the `Contracts:`
// line fails closed even with an APPROVED verdict — the anchor forces a
// recorded claim (any non-empty value, including "none"), never silent
// omission.
console.log("CONTRACTS ANCHOR (verdict alone is no longer enough):");

// 8a. APPROVED verdict, no Contracts line at all ⇒ DENY.
{
  const root = rootOnBranch("feature/nocontracts");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "nocontracts_review.md"), "## Code review: APPROVED\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/nocontracts" }, config);
  check("no-contracts-line:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 8b. APPROVED verdict, an empty `Contracts:` heading (no inline or next-line
// value) ⇒ DENY — same empty-stamp discipline as the verdict line.
{
  const root = rootOnBranch("feature/emptycontracts");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "emptycontracts_review.md"),
    "## Code review: APPROVED\n## Contracts:\n\n\n"
  );
  const v = evaluate({ act: "bash", root, command: "git push origin feature/emptycontracts" }, config);
  check("empty-contracts-line:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 8c. APPROVED verdict + `Contracts: none` ⇒ ALLOW — "none" is a valid,
// recorded claim, not a silent gap.
{
  const root = rootOnBranch("feature/nonecontracts");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "nonecontracts_review.md"),
    "## Code review: APPROVED\n## Contracts: none\n"
  );
  const v = evaluate({ act: "bash", root, command: "git push origin feature/nonecontracts" }, config);
  check("none-contracts-line:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 8d. APPROVED verdict + a filled Contracts value on the next line (split
// form) ⇒ ALLOW — mirrors the verdict's own split-line fallback.
{
  const root = rootOnBranch("feature/splitcontracts");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "splitcontracts_review.md"),
    "## Code review: APPROVED\n## Contracts:\nmessengers.md — satisfied, unchanged\n"
  );
  const v = evaluate({ act: "bash", root, command: "git push origin feature/splitcontracts" }, config);
  check("split-contracts-line:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 8e. APPROVED verdict + `Contracts: NOT YET RUN` ⇒ DENY — the anchor rejects
// the same not-yet-engaged placeholder the verdict line rejects (mirrors 3c);
// a reviewer that stamped APPROVED but left Contracts unaddressed must not
// satisfy the gate just because the line exists.
{
  const root = rootOnBranch("feature/nyrcontracts");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "nyrcontracts_review.md"),
    "## Code review: APPROVED\n## Contracts: NOT YET RUN\n"
  );
  const v = evaluate({ act: "bash", root, command: "git push origin feature/nyrcontracts" }, config);
  check("not-yet-run-contracts-line:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 8b. DIAGNOSTIC OUTPUT (Fix A) ──────────────────────────────────────────
// The deny reason includes a structured diagnostic with expected path,
// failing anchor reason, and remediation guidance.
console.log("DIAGNOSTIC OUTPUT (Fix A transparency):");

// 8b-i. Missing stamp file — diagnostic lists siblings.
{
  const root = rootOnBranch("feature/diagmissing");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  // Create a sibling stamp to verify it's listed
  fs.writeFileSync(path.join(dir, "other_review.md"), "## Code review: APPROVED\n## Contracts: none\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/diagmissing" }, config);
  check("diag-missing-verdict", v.verdict, "deny");
  // Verify diagnostic mentions the expected path and lists siblings
  const hasPath = v.reason.includes("diagmissing_review.md");
  const hasSiblings = v.reason.includes("other_review.md");
  const hasRemediation = v.reason.includes("Re-spawn the Reviewer");
  if (!hasPath || !hasSiblings || !hasRemediation) {
    fail++; console.log(`  ✗ diag-missing-content: path=${hasPath}, siblings=${hasSiblings}, remediation=${hasRemediation}`);
  } else { pass++; }
  fs.rmSync(root, { recursive: true, force: true });
}

// 8b-ii. Present stamp, verdict absent — diagnostic explains the failure.
{
  const root = rootOnBranch("feature/diagbadverdict");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "diagbadverdict_review.md"), "## Code review:\n\n## Contracts: none\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/diagbadverdict" }, config);
  check("diag-bad-verdict", v.verdict, "deny");
  const hasReason = v.reason.includes("verdict value empty") || v.reason.includes("absent");
  const hasRemediation = v.reason.includes("Re-spawn the Reviewer");
  if (!hasReason || !hasRemediation) {
    fail++; console.log(`  ✗ diag-bad-verdict-content: reason=${hasReason}, remediation=${hasRemediation}`);
  } else { pass++; }
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 9. SEPARATOR TOLERANCE (Fix C) ──────────────────────────────────────────
// Accept em-dash, en-dash, hyphen as alternatives to colon. No separator
// accepted only when value's first token is ≥2 uppercase chars or literal "none".
console.log("SEPARATOR TOLERANCE (colon/dash alternatives, no-separator guards):");

// 9a. Em-dash separator ⇒ ALLOW.
{
  const root = rootOnBranch("feature/emdash");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "emdash_review.md"), "## Code review— APPROVED\n## Contracts— none\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/emdash" }, config);
  check("em-dash-separator:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 9b. En-dash separator ⇒ ALLOW.
{
  const root = rootOnBranch("feature/endash");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "endash_review.md"), "## Code review– APPROVED\n## Contracts– none\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/endash" }, config);
  check("en-dash-separator:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 9c. Hyphen separator ⇒ ALLOW.
{
  const root = rootOnBranch("feature/hyphen");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "hyphen_review.md"), "## Code review- APPROVED\n## Contracts- none\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/hyphen" }, config);
  check("hyphen-separator:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 9d. No separator, value starts with ≥2 uppercase (APPROVED) ⇒ ALLOW.
{
  const root = rootOnBranch("feature/nosep-upper");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "nosep-upper_review.md"), "## Code review APPROVED\n## Contracts none\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/nosep-upper" }, config);
  check("no-separator-uppercase:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 9e. No separator, value is literal "none" (lowercase) ⇒ ALLOW.
{
  const root = rootOnBranch("feature/none-lower");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "none-lower_review.md"), "## Code review: APPROVED\n## Contracts none\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/none-lower" }, config);
  check("none-literal:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 9f. No separator, value starts with mixed case (checklist) ⇒ DENY.
{
  const root = rootOnBranch("feature/nosep-mixed");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "nosep-mixed_review.md"), "## Code review checklist\n## Contracts and guarantees\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/nosep-mixed" }, config);
  check("no-separator-mixed-case:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 10. ANCHOR BYPASS REGRESSION (Fix C normalization order) ───────────────────
// `## Code review — NOT YET RUN` must DENY after separator normalization.
// The separator `— ` is stripped before the NOT YET RUN test.
console.log("ANCHOR BYPASS REGRESSION (separator + NOT YET RUN normalization):");

// 10a. Separator before NOT YET RUN value ⇒ DENY.
{
  const root = rootOnBranch("feature/sep-nyr");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "sep-nyr_review.md"), "## Code review — NOT YET RUN\n## Contracts: none\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/sep-nyr" }, config);
  check("separator-not-yet-run:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 11. FIX D — NEXT-LINE FALLBACK HOLE CLOSURE ──────────────────────────────
// Empty verdict heading followed only by labelled stamp lines must DENY.
console.log("FIX D — NEXT-LINE FALLBACK HOLE (labelled lines not accepted as values):");

// 11a. `## Code review:` (empty) + `Runtime verification: static — …` ⇒ DENY.
// Today: the next-line fallback accepts `Runtime verification: static — …` as the verdict value.
// After Fix D: the next-line check skips labelled stamp lines, so verdict is empty ⇒ DENY.
{
  const root = rootOnBranch("feature/fix-d-hole");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "fix-d-hole_review.md"),
    "## Code review:\nRuntime verification: static — read the diff\n## Contracts: none\n"
  );
  const v = evaluate({ act: "bash", root, command: "git push origin feature/fix-d-hole" }, config);
  check("fix-d-hole-runtime-line:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 11b. `## Code review:` (empty) + `Contracts: none` as next line ⇒ DENY
// (Contracts line is also a labelled stamp line).
{
  const root = rootOnBranch("feature/fix-d-contracts");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "fix-d-contracts_review.md"),
    "## Code review:\n## Contracts: none\n"
  );
  const v = evaluate({ act: "bash", root, command: "git push origin feature/fix-d-contracts" }, config);
  check("fix-d-contracts-line:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 12. DIAGNOSTIC DRIFT GUARD — "Required form" derives from parser constants ──────────
// Mechanical guard: every separator in STAMP_SEPARATORS and every label in STAMP_LABELS
// must appear in the diagnostic text. Adding/removing a separator or label fails the test
// until the message is regenerated from the updated constants.
console.log("DIAGNOSTIC DRIFT GUARD (constant values present in diagnostic):");

// 12a. Diagnostic mentions every separator constant value.
{
  const root = rootOnBranch("feature/diagsep");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  const v = evaluate({ act: "bash", root, command: "git push origin feature/diagsep" }, config);
  check("diag-verdict", v.verdict, "deny");
  // Every separator in STAMP_SEPARATORS must appear in the reason
  let allFound = true;
  for (const sep of STAMP_SEPARATORS) {
    if (!v.reason.includes(sep)) {
      allFound = false;
      fail++; console.log(`  ✗ diag-missing-separator: separator ${JSON.stringify(sep)} not in diagnostic`);
      break;
    }
  }
  if (allFound) { pass++; }
  fs.rmSync(root, { recursive: true, force: true });
}

// 12b. Diagnostic mentions every verdict label constant value.
{
  const root = rootOnBranch("feature/diaglabel");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  const v = evaluate({ act: "bash", root, command: "git push origin feature/diaglabel" }, config);
  check("diag-label-verdict", v.verdict, "deny");
  let allFound = true;
  for (const label of STAMP_LABELS.verdict) {
    if (!v.reason.includes(label)) {
      allFound = false;
      fail++; console.log(`  ✗ diag-missing-verdict-label: label ${JSON.stringify(label)} not in diagnostic`);
      break;
    }
  }
  if (allFound) { pass++; }
  fs.rmSync(root, { recursive: true, force: true });
}

// 12c. Diagnostic mentions the contracts label constant value.
{
  const root = rootOnBranch("feature/diagcontract");
  const dir = path.join(root, ".ai-dev", "reviews");
  fs.mkdirSync(dir, { recursive: true });
  const v = evaluate({ act: "bash", root, command: "git push origin feature/diagcontract" }, config);
  check("diag-contract-label", v.verdict, "deny");
  if (!v.reason.includes(STAMP_LABELS.contracts)) {
    fail++; console.log(`  ✗ diag-missing-contracts-label: label ${JSON.stringify(STAMP_LABELS.contracts)} not in diagnostic`);
  } else { pass++; }
  fs.rmSync(root, { recursive: true, force: true });
}

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
