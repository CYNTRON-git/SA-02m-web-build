// Merge-gate: trunk push, no-slash HEAD branch, worktree checkouts, git global
// flags, trunk --ff-only sync carve-out, and force-push-with-lease scope.
//
//   10.  `git push origin main` left the topic unresolvable (bare `main` has no
//        slash) → fell through to the ask rule → SILENT PASS on a no-ask platform
//        (OpenCode). An explicit unstamped trunk push now DENIES on both platforms.
//   11.  `git push origin <branch>` for a no-slash branch name while ON that branch
//        once mis-fired merge-topic-unresolvable ASK. The explicit ref EQUALS the
//        HEAD branch now resolves via HEAD.
//   12.  in a git worktree `.git` is a FILE carrying `gitdir: <path>`, not a
//        directory. The engine once read `root/.git/HEAD` literally, threw, and
//        returned null ⇒ merge-topic-unresolvable (the parallel-work friction, #335).
//   13.  `git -C <path> (push|merge)` carried no resolvable ref ⇒ a parallel-work
//        push fell through to the session-root HEAD and false-BLOCKed; and
//        `git -C <path> push origin main` was INVISIBLE to the trunk-ref guard.
//   14.  `git merge --ff-only origin/main` while ON main is the post-squash-merge
//        trunk sync, NOT a feature merge. The gate once DENIED it.
//   15.  `git push --force-with-lease origin feature/x` to a non-trunk branch IS
//        safe (the lease protects) and no longer asks. Bare --force/-f still asks.
//
// Run: node src/adapter/merge-gate-trunk-and-worktree.test.mjs

import { evaluate, loadConfig, _internals } from "./engine.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { rootOnBranch, stamp, rootOnWorktree } from "./merge-gate-helpers.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const config = loadConfig(HERE);
const { resolveMergeTopic, pushExplicitTrunkRef } = _internals;

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; }
  else { fail++; console.log(`  ✗ ${name}: got ${got}, want ${want}`); }
}

// ── 10. EXPLICIT TRUNK PUSH DENIES, NOT ASKS (F1) ─────────────────────────────
// Guards the F1 escape (backlog "Persona floor collapses…"): `git push origin main`
// left the topic unresolvable (bare `main` has no slash) → fell through to the ask
// rule → SILENT PASS on a no-ask platform (OpenCode). An explicit unstamped trunk push
// now DENIES on both platforms (deny holds where ask degrades). The trunk ref IS the
// stamp topic, so a reviewed main-named change still ships.
console.log("EXPLICIT TRUNK PUSH (denies, not asks — F1):");

// DENY cases (the fix — were ask/silent-pass). On a feature/x checkout with no stamp.
for (const [name, cmd] of [
  ["bare-main", "git push origin main"],
  ["bare-master", "git push origin master"],
  ["refspec-dst-main", "git push uni HEAD:main"],
  ["force-marker-main", "git push origin +main"],
]) {
  const root = rootOnBranch("feature/x");
  const v = evaluate({ act: "bash", root, command: cmd }, config);
  check(`trunk-push-${name}:denies`, v.verdict, "deny");
  check(`trunk-push-${name}:ruleId`, v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// ALLOW cases (no real push newly blocked).
{
  // A reviewed trunk-named change (main_review.md present) still ships.
  const root = rootOnBranch("feature/x");
  stamp(root, "main");
  const v = evaluate({ act: "bash", root, command: "git push origin main" }, config);
  check("trunk-push-stamped:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
{
  // Tag push still wins the early-out (not read as a trunk push).
  const root = rootOnBranch("feature/x");
  const v = evaluate({ act: "bash", root, command: "git push origin v5.3.1" }, config);
  check("trunk-push-tag:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
{
  // Feature push unaffected, when stamped.
  const root = rootOnBranch("feature/foo");
  stamp(root, "foo");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/foo" }, config);
  check("trunk-fix-feature-push-stamped:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
{
  // `maintenance` must NOT be mis-classified as trunk (whole-token equality, not
  // substring `main`): it resolves via the normal unparsed-ref path ⇒ ask, NOT the
  // trunk deny. The helper returns null for it.
  check("maintenance-not-trunk", pushExplicitTrunkRef("git push origin maintenance"), null);
  check("mainline-not-trunk", pushExplicitTrunkRef("git push origin mainline"), null);
  check("main-is-trunk", pushExplicitTrunkRef("git push origin main"), "main");
  check("master-is-trunk", pushExplicitTrunkRef("git push origin master"), "master");
  const root = rootOnBranch("feature/x");
  const v = evaluate({ act: "bash", root, command: "git push origin maintenance" }, config);
  check("maintenance-push:asks-not-trunk-deny", v.verdict, "ask");
  fs.rmSync(root, { recursive: true, force: true });
}
{
  // yolo profile turns the gate off — even a trunk push allows (gate-off consistency).
  const root = rootOnBranch("feature/x");
  fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(root, ".ai-dev", "config.json"), '{ "profile": "yolo" }');
  const v = evaluate({ act: "bash", root, command: "git push origin main" }, config);
  check("trunk-push-yolo:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
{
  // Quoted prose is masked before the trunk scan — a `-m` message mentioning a trunk
  // push is never read as one. Asserted at the helper (mirrors block 5d's resolver-level
  // quoted-prose assertion): a real `git push origin main` inside a quoted span yields no
  // trunk ref. (NOTE: the full `git commit -m "git push origin main"` still trips the
  // PRE-EXISTING merge-gate guard quirk — the raw guard regex sees `push` in the message
  // and the checkout topic resolves; that is unchanged by this batch, verified against
  // HEAD. The trunk predicate itself correctly masks.)
  check("trunk-push-quoted-prose-helper", pushExplicitTrunkRef('git commit -m "git push origin main"'), null);
}

// ── 11. EXPLICIT NO-SLASH HEAD BRANCH — push the branch you're on ─────────────
// Guards the friction behind hook-mode (#2/#335 family): `git push origin <branch>`
// for a no-slash branch name, while ON that branch, once mis-fired the
// merge-topic-unresolvable ASK — topicFromRefToken only slash-parses, so the bare
// name was "unparseable" and the HEAD fallback was wrongly skipped. The explicit
// ref EQUALS the HEAD branch now resolves via HEAD: the branch IS the topic. The
// floor still holds — an UNSTAMPED such push now DENIES (the topic resolves, so the
// stamp is CHECKED, not asked around), and a cross-checkout push of a DIFFERENT
// no-slash branch still asks (can't syntactically tell a non-numeric tag from a
// branch, and the pushed ref outranks HEAD).
console.log("EXPLICIT NO-SLASH HEAD BRANCH (push the branch you're on):");

// 11a. STAMPED push of the current no-slash branch ⇒ ALLOW (the fix — was ASK).
{
  const root = rootOnBranch("hook-mode-strict-light");
  stamp(root, "hook-mode-strict-light");
  const v = evaluate({ act: "bash", root, command: "git push origin hook-mode-strict-light" }, config);
  check("head-branch-stamped:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 11b. UNSTAMPED push of the current no-slash branch ⇒ DENY merge-while-unstamped
// (the floor HOLDS — was an ASK because the topic was unresolvable; now the topic
// resolves and the missing stamp denies, the correct strict side, not an ask-around).
{
  const root = rootOnBranch("hook-mode-strict-light");
  const v = evaluate({ act: "bash", root, command: "git push origin hook-mode-strict-light" }, config);
  check("head-branch-unstamped:denies", v.verdict, "deny");
  check("head-branch-unstamped:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 11c. Cross-checkout: push a DIFFERENT no-slash branch from another checkout ⇒
// still ASK (the ref outranks HEAD; a non-numeric tag can't be told from a branch,
// so the safe path is ask — never a silent pass, never a guess).
{
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git push origin hook-mode-strict-light" }, config);
  check("head-branch-cross-checkout:asks", v.verdict, "ask");
  check("head-branch-cross-checkout:ruleId", v.ruleId, "merge-topic-unresolvable");
  fs.rmSync(root, { recursive: true, force: true });
}

// 11d. Resolver unit: the explicit no-slash current-branch ref resolves to the
// branch name (prefix-stripped), not null.
{
  const root = rootOnBranch("mybranch");
  check("head-branch-resolver", resolveMergeTopic("git push origin mybranch", root), "mybranch");
  fs.rmSync(root, { recursive: true, force: true });
}

// 11e. Refspec form `<branch>:<branch>` on the current no-slash branch, stamped ⇒
// ALLOW (the HEAD-equality check tests EITHER side of the refspec).
{
  const root = rootOnBranch("mybranch");
  stamp(root, "mybranch");
  const v = evaluate({ act: "bash", root, command: "git push origin mybranch:mybranch" }, config);
  check("head-branch-refspec:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 11f. Force-marker form resolves at the resolver level (the + is stripped before
// the HEAD-equality check). The full command ALSO trips the force-push ASK — that
// is correct and independent of this fix; asserted at the resolver layer only.
{
  const root = rootOnBranch("mybranch");
  check("head-branch-force-resolver", resolveMergeTopic("git push origin +mybranch", root), "mybranch");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 12. WORKTREE CHECKOUT — `.git` is a gitdir-file, HEAD lives elsewhere ─────
// Guards #335 (parallel-work's per-feature checkout): in a git worktree `.git` is a
// FILE carrying `gitdir: <path>`, not a directory. The engine once read `root/.git/HEAD`
// literally, threw, and returned null ⇒ a push/merge from a worktree mis-fired
// merge-topic-unresolvable (the parallel-work Reviewer-stamp friction). resolveGitDir
// follows the pointer; headBranch reads HEAD from the real git dir.
console.log("WORKTREE CHECKOUT (.git is a gitdir-file — #335):");

// 12a. STAMPED push from a worktree, no-slash branch ⇒ ALLOW (the fix — was ASK:
// HEAD returned null and the topic was unresolvable).
{
  const root = rootOnWorktree("parallel-feature");
  stamp(root, "parallel-feature");
  const v = evaluate({ act: "bash", root, command: "git push origin parallel-feature" }, config);
  check("worktree-stamped:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 12b. UNSTAMPED push from a worktree ⇒ DENY merge-while-unstamped (the floor HOLDS —
// the worktree's HEAD now resolves, so the stamp is checked, not asked around).
{
  const root = rootOnWorktree("parallel-feature");
  const v = evaluate({ act: "bash", root, command: "git push origin parallel-feature" }, config);
  check("worktree-unstamped:denies", v.verdict, "deny");
  check("worktree-unstamped:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 12c. Resolver unit: the topic resolves from the worktree's HEAD via the gitdir pointer.
{
  const root = rootOnWorktree("my-wt-branch");
  check("worktree-resolver", resolveMergeTopic("git push origin my-wt-branch", root), "my-wt-branch");
  check("worktree-bare-push-resolver", resolveMergeTopic("git push", root), "my-wt-branch");
  fs.rmSync(root, { recursive: true, force: true });
}

// 12d. A RELATIVE `gitdir:` pointer resolves too (target resolved against the checkout
// root — git writes absolute, but the resolver handles both).
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-wt-rel-"));
  fs.mkdirSync(path.join(root, "wt-private")); // the private git dir, RELATIVE to root
  fs.writeFileSync(path.join(root, "wt-private", "HEAD"), "ref: refs/heads/rel-wt\n");
  fs.writeFileSync(path.join(root, ".git"), "gitdir: wt-private\n"); // relative pointer
  check("worktree-relative-gitdir", resolveMergeTopic("git push", root), "rel-wt");
  fs.rmSync(root, { recursive: true, force: true });
}

// 12e. commit-on-main in a worktree: headBranch now resolves the worktree's HEAD, so the
// gate fires (was a false-PASS — headBranch returned null and commitOnUnstampedMain
// short-circuited to false). Configured + a has-history marker set so the carve-outs
// don't apply; no main stamp ⇒ DENY.
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-wt-main-"));
  const gitDir = path.join(root, "wt-git");
  fs.mkdirSync(path.join(gitDir, "logs"), { recursive: true });
  fs.writeFileSync(path.join(gitDir, "HEAD"), "ref: refs/heads/main\n");
  fs.writeFileSync(path.join(gitDir, "logs", "HEAD"), "0000 commit\n"); // has-history marker
  fs.writeFileSync(path.join(root, ".git"), `gitdir: ${gitDir}\n`);
  fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(root, ".ai-dev", "config.json"), '{ "profile": "solo" }');
  const v = evaluate({ act: "bash", root, command: "git commit -m x" }, config);
  check("worktree-commit-on-main:denies", v.verdict, "deny");
  check("worktree-commit-on-main:ruleId", v.ruleId, "commit-on-unstamped-main");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 13. `git -C <path> (push|merge)` — a GLOBAL flag between `git` and the subcommand ─
// once broke the adjacency regex every parser keyed on. `git -C <worktree> push`
// carried no resolvable ref ⇒ a parallel-work push fell through to the session-root
// HEAD and false-BLOCKed (availability); and `git -C <path> push origin main` was
// INVISIBLE to the trunk-ref guard ⇒ the [mechanical] trunk deny leaked (security).
// normalizeGitInvocation strips the global-flag span before the adjacency match.
console.log("GLOBAL-FLAG PREFIX git -C / -c / --git-dir / --work-tree (push|merge):");

// 13a. STAMPED `git -C <path> push origin feature/x` ⇒ ALLOW (the fix — was BLOCK:
// the command resolved no ref, fell through to HEAD, checked the wrong stamp).
{
  const root = rootOnBranch("feature/x");
  stamp(root, "x");
  const v = evaluate({ act: "bash", root, command: "git -C /tmp/ai-dev-wt push origin feature/x" }, config);
  check("git-C-stamped:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 13b. UNSTAMPED `git -C <path> push` ⇒ DENY merge-while-unstamped (the floor HOLDS —
// the topic now resolves ⇒ the stamp is checked, not bypassed).
{
  const root = rootOnBranch("feature/x");
  const v = evaluate({ act: "bash", root, command: "git -C /tmp/ai-dev-wt push origin feature/x" }, config);
  check("git-C-unstamped:denies", v.verdict, "deny");
  check("git-C-unstamped:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 13c. SECURITY: `git -C <path> push origin main` ⇒ DENY (the trunk-ref guard was
// BLIND to the -C form ⇒ the [mechanical] trunk deny leaked; it now fires). Unit:
// pushExplicitTrunkRef now sees the trunk ref THROUGH the -C prefix.
{
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git -C /tmp/ai-dev-wt push origin main" }, config);
  check("git-C-trunk-push:denies", v.verdict, "deny");
  check("git-C-trunk-push:ruleId", v.ruleId, "merge-while-unstamped");
  check("git-C-trunk-ref-unit", pushExplicitTrunkRef("git -C /tmp/ai-dev-wt push origin main"), "main");
  fs.rmSync(root, { recursive: true, force: true });
}

// 13d. Resolver unit: the global-flag span is stripped, then the ref resolves (push
// and merge); the other value-taking globals (-c, --git-dir, --work-tree; space-,
// =-, glue-, and multi-forms) resolve too.
{
  const root = rootOnBranch("feature/x");
  check("git-C-push-resolver", resolveMergeTopic("git -C /tmp/wt push origin feature/x", root), "x");
  check("git-C-merge-resolver", resolveMergeTopic("git -C /tmp/wt merge --ff-only feature/x", root), "x");
  check("git-c-resolver", resolveMergeTopic("git -c core.bare=false push origin feature/x", root), "x");
  check("git-gitdir-eq-resolver", resolveMergeTopic("git --git-dir=/tmp/g push origin feature/x", root), "x");
  check("git-worktree-space-resolver", resolveMergeTopic("git --work-tree /tmp/w push origin feature/x", root), "x");
  check("git-multi-global-resolver", resolveMergeTopic("git -C /tmp/a -c k=v push origin feature/x", root), "x");
  check("git-C-glued-resolver", resolveMergeTopic("git -C/tmp/wt push origin feature/x", root), "x");
  fs.rmSync(root, { recursive: true, force: true });
}

// 13e. REGRESSION: a non-`-C` command is unchanged through the normalizer (no flag
// span ⇒ no match ⇒ returned as-is); the trunk-ref guard returns null for a feature.
{
  const root = rootOnBranch("feature/x");
  stamp(root, "x");
  check("no-global-resolver", resolveMergeTopic("git push origin feature/x", root), "x");
  check("no-global-stamped-allows", evaluate({ act: "bash", root, command: "git push origin feature/x" }, config).verdict, "allow");
  check("no-global-trunk-null", pushExplicitTrunkRef("git push origin feature/x"), null);
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 14. trunk `--ff-only` sync — `git merge --ff-only origin/main` while ON main is the ──
// post-squash-merge trunk sync (the git flow's `git pull`), NOT a feature merge. The gate
// once DENIED it (topic resolved to `main`, no main_review.md), forcing the destructive
// `git reset --hard origin/main`. isTrunkFastForward carves it out — strict 4-guard.
console.log("TRUNK --ff-only SYNC CARVE-OUT (git merge --ff-only <trunk-upstream>):");

// 14a. `git merge --ff-only origin/main` on main ⇒ ALLOW (the fix — was DENY).
{
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git merge --ff-only origin/main" }, config);
  check("trunk-ff-origin-main:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 14b. `git merge --ff-only main` on main ⇒ ALLOW (bare trunk ref).
{
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git merge --ff-only main" }, config);
  check("trunk-ff-main:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 14c. `git merge --ff-only origin/master` on master ⇒ ALLOW (master trunk).
{
  const root = rootOnBranch("master");
  const v = evaluate({ act: "bash", root, command: "git merge --ff-only origin/master" }, config);
  check("trunk-ff-master:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 14d. REGRESSION: on a FEATURE branch ⇒ DENY (guard 4 — checkout not trunk).
{
  const root = rootOnBranch("feature/x");
  const v = evaluate({ act: "bash", root, command: "git merge --ff-only origin/main" }, config);
  check("trunk-ff-on-feature:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 14e. REGRESSION: a non-trunk ref ⇒ DENY (guard 3 — `origin/feature-x` not in the trunk set).
{
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git merge --ff-only origin/feature-x" }, config);
  check("trunk-ff-non-trunk-ref:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 14f. REGRESSION: `--ff` WITHOUT -only ⇒ DENY (guard 2 — bare --ff can still create a merge commit).
{
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git merge --ff origin/main" }, config);
  check("trunk-ff-bare-ff:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 14g. REGRESSION: the `feature/main` collision ⇒ DENY (guard 3 rejects a non-`origin` prefix).
{
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git merge --ff-only feature/main" }, config);
  check("trunk-ff-feature-main-collision:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 14h. REGRESSION: a trunk PUSH is unaffected — still DENY (the carve-out is merge-only).
{
  const root = rootOnBranch("main");
  const v = evaluate({ act: "bash", root, command: "git push origin main" }, config);
  check("trunk-push-still-denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// ── 15. FORCE-PUSH SCOPE — lease+non-trunk allowed, bare force asks ─────────────
// Guards the parallel-work rebase-before-merge friction: `git push --force-with-lease
// origin feature/x` to a non-trunk branch IS safe (the lease protects) and no longer
// asks. Bare --force/-f (no lease) still asks. Trunk is fully protected by the
// merge-gate deny rule and still asks.
console.log("FORCE-PUSH SCOPE (lease+non-trunk allowed, bare force asks):");

// 15a. --force-with-lease to a non-trunk feature branch ⇒ ALLOW (the fix — was ASK).
{
  const root = rootOnBranch("feature/x");
  stamp(root, "x");
  const v = evaluate({ act: "bash", root, command: "git push --force-with-lease origin feature/x" }, config);
  check("fwl-feature-branch:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 15b. --force-with-lease to trunk ⇒ ASK (trunk is fully protected).
{
  const root = rootOnBranch("feature/x");
  stamp(root, "x");
  const v = evaluate({ act: "bash", root, command: "git push --force-with-lease origin main" }, config);
  check("fwl-trunk:asks", v.verdict, "ask");
  check("fwl-trunk:ruleId", v.ruleId, "force-push");
  fs.rmSync(root, { recursive: true, force: true });
}

// 15c. Bare --force to a feature branch ⇒ ASK (no lease protection).
{
  const root = rootOnBranch("feature/x");
  stamp(root, "x");
  const v = evaluate({ act: "bash", root, command: "git push --force origin feature/x" }, config);
  check("bare-force-feature:asks", v.verdict, "ask");
  check("bare-force-feature:ruleId", v.ruleId, "force-push");
  fs.rmSync(root, { recursive: true, force: true });
}

// 15d. Bare -f to a feature branch ⇒ ASK (no lease protection).
{
  const root = rootOnBranch("feature/x");
  stamp(root, "x");
  const v = evaluate({ act: "bash", root, command: "git push -f origin feature/x" }, config);
  check("short-f-feature:asks", v.verdict, "ask");
  check("short-f-feature:ruleId", v.ruleId, "force-push");
  fs.rmSync(root, { recursive: true, force: true });
}

// 15e. Bare --force to trunk ⇒ ASK (trunk + bare force is the worst case).
{
  const root = rootOnBranch("feature/x");
  stamp(root, "x");
  const v = evaluate({ act: "bash", root, command: "git push --force origin main" }, config);
  check("bare-force-trunk:asks", v.verdict, "ask");
  check("bare-force-trunk:ruleId", v.ruleId, "force-push");
  fs.rmSync(root, { recursive: true, force: true });
}

// 15f. REGRESSION: --force-with-lease to master trunk ⇒ ASK (not just main).
{
  const root = rootOnBranch("feature/x");
  stamp(root, "x");
  const v = evaluate({ act: "bash", root, command: "git push --force-with-lease origin master" }, config);
  check("fwl-master-trunk:asks", v.verdict, "ask");
  fs.rmSync(root, { recursive: true, force: true });
}

// 15g. REGRESSION: non-force push still allows (no behavior change).
{
  const root = rootOnBranch("feature/foo");
  stamp(root, "foo");
  const v = evaluate({ act: "bash", root, command: "git push origin feature/foo" }, config);
  check("normal-push:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 15h. REGRESSION: --force-with-lease with -C global flag (worktree push) ⇒ ALLOW.
{
  const root = rootOnBranch("feature/x");
  stamp(root, "x");
  const v = evaluate({ act: "bash", root, command: "git -C .ai-dev/worktrees/x push --force-with-lease origin feature/x" }, config);
  check("fwl-worktree:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 15i. Compound command: multiple pushes, first one is --force-with-lease to non-trunk ⇒ ALLOW
// (the predicate reads the FIRST push invocation's flag).
{
  const root = rootOnBranch("feature/x");
  stamp(root, "x");
  const v = evaluate({ act: "bash", root, command: "git push --force-with-lease origin feature/x && git push origin main" }, config);
  check("compound-first-fwl:allows", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 15j. Unit: pushExplicitTrunkRef sees main/master through -C prefix (re-verify).
check("fwl-trunk-ref-unit-C", pushExplicitTrunkRef("git -C /tmp/wt push --force-with-lease origin main"), "main");

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
