// Merge-gate: reference parsing — pushed ref outranks HEAD, heredoc stripping,
// path-traversal in the topic, and merge-* plumbing false-positives.
//
//   5.   the checkout (.git/HEAD) once OUTRANKED the command's ref — pushing
//        branch A from branch B's checkout read B's stamp (4.19.x conveyor).
//   6.   the verb rules once matched INSIDE heredoc bodies — a python3 heredoc
//        whose prose mentioned pushes was denied as a push (4.19.x).
//   8.   a crafted ref (`feature/../EVIL`) once resolved topic `../EVIL` and the
//        stamp path ESCAPED reviews/ — a planted stamp satisfied the floor. The
//        topic is now validated as a single clean segment at the stamp boundary.
//   9.   `git merge\b` matched `git merge-base`/`merge-tree`/`merge-file`/
//        `mergetool` (the word boundary treats `-` as a separator) — read-only
//        plumbing was DENIED as a merge. The predicate now uses `merge(?![-\w])`.
//
// Run: node src/adapter/merge-gate-ref-parsing.test.mjs

import { evaluate, loadConfig, _internals } from "./engine.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { rootOnBranch, stamp } from "./merge-gate-helpers.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const config = loadConfig(HERE);
const { resolveMergeTopic, bashWriteTargets } = _internals;

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; }
  else { fail++; console.log(`  ✗ ${name}: got ${got}, want ${want}`); }
}

// ── 5. PUSHED REF OUTRANKS HEAD — cross-branch push/merge ─────────────────────
// Guards the 4.19.x live failure: pushing branch A from branch B's checkout read
// B's stamp (HEAD was consulted first). The ref named in the command now wins;
// HEAD remains the bare-push fallback (block 1b in stamp-resolution test).
console.log("PUSHED REF OUTRANKS HEAD (cross-branch push/merge):");

// 5a. On checkout feature/beta, pushing feature/alpha with ALPHA's stamp ⇒ ALLOW
// (the old order read beta's absent stamp ⇒ false deny).
{
  const root = rootOnBranch("feature/beta");
  stamp(root, "alpha");
  const v = evaluate({ act: "bash", root, command: "git push uni feature/alpha" }, config);
  check("cross-branch-stamped:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 5b. Same checkout with only BETA stamped ⇒ DENY — the gate reads the PUSHED
// branch's stamp, never the checkout's (the false-allow direction, the security case).
{
  const root = rootOnBranch("feature/beta");
  stamp(root, "beta");
  const v = evaluate({ act: "bash", root, command: "git push uni feature/alpha" }, config);
  check("cross-branch-unstamped:denies", v.verdict, "deny");
  check("cross-branch-unstamped:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 5c. `git merge <branch>` resolves the MERGED branch: on checkout main, merging
// stamped feature/alpha ⇒ ALLOW (the old order resolved topic `main` ⇒ false
// deny); unstamped ⇒ DENY.
{
  const root = rootOnBranch("main");
  stamp(root, "alpha");
  check("merge-branch-stamped:allows",
    evaluate({ act: "bash", root, command: "git merge feature/alpha" }, config).verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
{
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git merge feature/alpha" }, config);
  check("merge-branch-unstamped:denies", v.verdict, "deny");
  check("merge-branch-unstamped:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 5d. Command-ref parsing forms (no .git/HEAD in this root — the command alone
// must resolve): a flag-bearing push; refspec dst (`HEAD:feature/x` ⇒ the remote
// side, its only named ref); refspec src (`feature/foo:main` ⇒ the local
// feature); quoted prose masked (a `-m` message never resolves).
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-mergegate-ref-"));
  check("flag-form", resolveMergeTopic("git push --force-with-lease uni feature/alpha", root), "alpha");
  check("refspec-dst", resolveMergeTopic("git push uni HEAD:feature/x", root), "x");
  check("refspec-src", resolveMergeTopic("git push origin feature/foo:main", root), "foo");
  check("quoted-prose-masked", resolveMergeTopic('git merge -m "take sprint/3 work" feature/alpha', root), "alpha");
  fs.rmSync(root, { recursive: true, force: true });
}

// 5e. A slashed PATH outside the push's own argument span never resolves — the
// bare push falls back to HEAD (the checkout), exactly as before.
{
  const root = rootOnBranch("feature/beta");
  check("compound-path-not-a-ref", resolveMergeTopic("cd src/foo && git push", root), "beta");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 6. HEREDOC BODIES ARE DATA — unless a shell executes them ─────────────────
// Guards the 4.19.x live false positive: a python3 heredoc whose PROSE mentioned
// pushes was denied as a push. A non-shell heredoc body is stripped before any
// rule pattern runs; a shell-interpreted body (bash <<EOF) and a malformed body
// (no closing delimiter) stay fully matched — fail toward deny.
console.log("HEREDOC BODIES (data stripped; shell/malformed kept):");

// 6a. python3 heredoc with push PROSE in the body, unstamped checkout ⇒ ALLOW
// (the live-failure replay — the old code denied this).
{
  const root = rootOnBranch("feature/beta");
  const v = evaluate({ act: "bash", root,
    command: "python3 <<'EOF'\nprint(\"git push origin main\")\nEOF" }, config);
  check("python-heredoc-prose:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 6b. Shell heredoc — the body EXECUTES as shell: a real unstamped push in it
// ⇒ DENY, including the path-qualified form (the bypass-shaped cases).
{
  const root = rootOnBranch("feature/beta");
  const v = evaluate({ act: "bash", root,
    command: "bash <<'EOF'\ngit push origin feature/beta\nEOF" }, config);
  check("bash-heredoc-push:denies", v.verdict, "deny");
  check("bash-heredoc-push:ruleId", v.ruleId, "merge-while-unstamped");
  const pv = evaluate({ act: "bash", root,
    command: "/bin/bash <<'EOF'\ngit push origin feature/beta\nEOF" }, config);
  check("path-qualified-shell-heredoc:denies", pv.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 6c. A real push AFTER a heredoc in the same compound stays caught.
{
  const root = rootOnBranch("feature/beta");
  const v = evaluate({ act: "bash", root,
    command: "python3 <<'EOF'\nprint(\"all about pushes\")\nEOF\ngit push uni feature/beta" }, config);
  check("push-after-heredoc:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 6d. MALFORMED heredoc (no closing delimiter) ⇒ nothing stripped ⇒ the verb
// words still match ⇒ DENY (fail toward the strict side).
{
  const root = rootOnBranch("feature/beta");
  const v = evaluate({ act: "bash", root,
    command: "python3 <<'EOF'\ngit push origin feature/beta" }, config);
  check("malformed-heredoc:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 8. PATH-TRAVERSAL IN THE TOPIC — the stamp path can't escape reviews/ ──────
// Guards the HIGH the Reviewer drove end-to-end: a crafted ref `feature/../EVIL`
// resolves topic `../EVIL`, and `.ai-dev/reviews/../EVIL_review.md` collapses to
// `.ai-dev/EVIL_review.md` — OUTSIDE reviews/. A planted stamp there once ALLOWED
// an unstamped push. The topic is now validated as a single clean segment at the
// stamp boundary (the one choke point every source funnels through); an unclean
// topic leaves the stamp unsatisfiable ⇒ DENY (fail toward deny).
console.log("PATH-TRAVERSAL (topic stays inside reviews/):");

// 8a. THE REVIEWER SCENARIO: a stamp planted at the ESCAPED path + a crafted ref
// ⇒ DENY (was ALLOW — the bypass). The checkout itself is unstamped.
{
  const root = rootOnBranch("feature/beta");
  fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(root, ".ai-dev", "EVIL_review.md"), "## Code review: APPROVED\n");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/../EVIL" }, config);
  check("traversal-planted-stamp:denies", v.verdict, "deny");
  check("traversal-planted-stamp:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 8b. Resolution stays SYNTACTIC — the crafted topic resolves NON-null (`../EVIL`)
// so the gate denies it; nulling it here would fall back to HEAD and could read a
// stamped checkout's stamp (a bypass). Pins the no-second-entry-path design.
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-mergegate-trav-"));
  check("traversal-topic-syntactic", resolveMergeTopic("git push origin feature/../EVIL", root), "../EVIL");
  fs.rmSync(root, { recursive: true, force: true });
}

// 8c. NESTED branch name ⇒ DENY (the pinned choice: reject path separators
// outright — the convention is a single `<prefix>/<topic>`, not `feature/sub/topic`).
{
  const root = rootOnBranch("feature/sub/topic");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/sub/topic" }, config);
  check("nested-branch:denies", v.verdict, "deny");
  check("nested-branch:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 8d. A CLEAN topic still resolves and satisfies the gate (the guard rejects only
// unclean segments, never a normal topic).
{
  const root = rootOnBranch("feature/clean");
  stamp(root, "clean");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/clean" }, config);
  check("clean-topic:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 8e. SIBLING WRITE-TARGET (the touched scan): a heredoc-borne escaping write
// still DENIES write-outside-root — the phantom-`<<EOF` strip removed only the
// opener noise, never weakened the escape detection.
{
  const root = rootOnBranch("feature/beta");
  const v = evaluate({ act: "bash", root, command: "tee ../../escape.txt <<EOF\nx\nEOF" }, config);
  check("heredoc-write-escape:denies", v.verdict, "deny");
  check("heredoc-write-escape:ruleId", v.ruleId, "write-outside-root");
  fs.rmSync(root, { recursive: true, force: true });
}

// 8f. The LOW: `tee file <<EOF` no longer extracts a phantom `<<EOF` write target
// (the opener is an operator, never a target).
check("heredoc-phantom-gone", JSON.stringify(bashWriteTargets("tee /tmp/out <<EOF")), JSON.stringify(["/tmp/out"]));

// ── 9. MERGE-* PLUMBING IS NOT A MERGE ────────────────────────────────────────
// Guards the live false positive (2026-06-16): the gate predicate matched
// `git merge\b`, and `\b` treats the hyphen as a word boundary — so a read-only
// `git merge-base`/`merge-tree`/`merge-file`/`mergetool` was DENIED as if it were a
// real merge. The predicate now uses `merge(?![-\w])`: a hyphen/word-char immediately
// after `merge` marks a plumbing subcommand and falls through; a REAL merge always has
// whitespace/EOL after `merge`, so the floor is unchanged.
console.log("MERGE-* PLUMBING (read-only merge-* is not a merge):");

// ALLOW cases (the fix — were DENY). Run on an UNSTAMPED feature/x checkout: under
// the old regex each resolved a topic and denied; now they fall through to allow.
for (const [name, cmd] of [
  ["merge-base", "git merge-base --is-ancestor feature/x main"],
  ["merge-tree", "git merge-tree main feature/x"],
  ["merge-file", "git merge-file a b c"],
  ["mergetool", "git mergetool"],
]) {
  const root = rootOnBranch("feature/x");
  const v = evaluate({ act: "bash", root, command: cmd }, config);
  check(`plumbing-${name}:allows`, v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// DENY cases (the real merge floor MUST still hold under the new regex).
{
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git merge feature/alpha" }, config);
  check("real-merge-space:denies", v.verdict, "deny");
  check("real-merge-space:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}
{
  // Tab separator after `merge` — the negative lookahead allows whitespace, so a
  // tab-separated real merge still matches and denies.
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git merge\tfeature/alpha" }, config);
  check("real-merge-tab:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}
{
  // The push alternation is untouched by the merge tightening.
  const root = rootOnBranch("feature/foo");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/foo" }, config);
  check("push-untouched-by-merge-fix:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// Resolver unit: a `merge-base` span must NOT resolve a bogus topic from the
// `merge-base` arguments. On a feature/x checkout the HEAD topic `x` is fine;
// the assertion is that it never yields `base`/`tree` parsed from the subcommand.
{
  const root = rootOnBranch("feature/x");
  const topic = resolveMergeTopic("git merge-base x main", root);
  check("merge-base-resolver:no-bogus-topic", topic === "x" || topic === null, true);
  check("merge-base-resolver:not-base", topic === "base", false);
  check("merge-base-resolver:not-tree", topic === "tree", false);
  fs.rmSync(root, { recursive: true, force: true });
}

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
