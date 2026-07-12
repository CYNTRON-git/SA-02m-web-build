// Git / merge-topic + review-stamp logic for the enforcement engine. Resolves the
// merge-gate topic from a push/merge command (or the .git/HEAD fallback), classifies
// tag and trunk pushes, and reads the review stamp. Pure regex reads — no shell, no
// interpolation. The traversal guard (isCleanTopic) is the choke point every topic
// source funnels through before a stamp path is built.

import fs from "node:fs";
import path from "node:path";
import { maskQuotedSpans } from "./engine-bash.mjs";
import { headBranch } from "./engine-config.mjs";

// Preprocess a git command so the adjacency regex (\bgit\s+push|merge) matches even
// when a GLOBAL flag sits between `git` and the subcommand. The standard
// `git -C <path> push …` (and `-c <k=v>`, `--git-dir`, `--work-tree`) breaks the
// adjacency every parser predicate keys on: the command then carries no resolvable
// ref ⇒ a parallel-work `git -C <worktree> push` falls through to the session-root
// HEAD and false-BLOCKs a legitimately-stamped push (availability), AND
// `pushExplicitTrunkRef`'s first-line guard is blind to `git -C <path> push origin
// main` ⇒ the unconditional trunk deny leaks (security — a [mechanical] floor
// reached through the wrong door). Strip the global-flag span (each flag WITH its
// value) from the QUOTE-MASKED command: `git -C path push origin feature/x` ⇒
// `git push origin feature/x`, and the existing adjacency regex + refspec semantics
// run UNCHANGED on the remainder. Value-taking globals only (the four the live
// report names); boolean globals never intervene before a push/merge subcommand.
// Pure regex on the masked string — no shell, no interpolation.
function normalizeGitInvocation(command) {
  const masked = maskQuotedSpans(command);
  // One global flag WITH its value: `-C <v>` | `-C<v>` | `-c <v>` | `-c<v>`
  // | `--git-dir <v>` | `--git-dir=<v>` | `--work-tree <v>` | `--work-tree=<v>`.
  // A quoted value is already masked to underscores by maskQuotedSpans, so it stays
  // a single `\S+` token. Long flags first (defensive — `-c`/`-C` can't prefix-match
  // `--git-dir`/`--work-tree`, the 2nd char differs — but it reads cleanest).
  const flag = "(?:--git-dir(?:=\\S+|\\s+\\S+)|--work-tree(?:=\\S+|\\s+\\S+)|-C(?:\\S+|\\s+\\S+)|-c(?:\\S+|\\s+\\S+))";
  // `git` + whitespace + first flag + (whitespace + flag)* + the whitespace before
  // the subcommand. A non-`-C` command has no flag span ⇒ no match ⇒ returned
  // unchanged (regression-safe). Replace with `git ` so the subcommand is adjacent.
  const span = new RegExp("\\bgit\\s+" + flag + "(?:\\s+" + flag + ")*\\s+", "g");
  return masked.replace(span, "git ");
}

// Resolve the merge-gate topic from ANY branch, prefix stripped: the topic is the
// branch name with its leading work-prefix dropped (feature/foo→foo, fix/bar→bar,
// hotfix/x→x, a bare `topic`→topic). The ref named in the push/merge command
// OUTRANKS the checkout — pushing branch A from branch B's checkout must read A's
// stamp, not B's (live 4.19.x failure); .git/HEAD is the fallback for a bare
// `git push`/`git merge` naming no ref. A non-feature prefix (fix/…) must resolve
// too, or its unstamped push escapes the floor. Pure regex reads — no
// interpolation, no shell.
function stripPrefix(branch) {
  const slash = branch.indexOf("/");
  return slash === -1 ? branch : branch.slice(slash + 1);
}
// One refspec token → topic. Only a slashed token is a branch ref (a remote name
// has no slash). In a <src>:<dst> refspec the slashed side carries the topic —
// src preferred (the local feature being shipped); `HEAD:feature/x` resolves the
// dst side, its only named ref. The safe ref character class must hold over the
// WHOLE side (plus `<>` excluded — redirect chars, never a ref the gate trusts);
// a dirty token resolves nothing rather than mis-parse.
const REF_SIDE = /^[^\s;&|"':~^@<>]+$/;
function topicFromRefToken(token) {
  if (token.startsWith("+")) token = token.slice(1); // refspec force marker
  const sides = token.split(":");
  if (sides.length > 2) return null; // not a refspec shape
  for (const side of sides) {
    if (!REF_SIDE.test(side)) continue;
    const slash = side.indexOf("/");
    if (slash > 0 && slash < side.length - 1) return side.slice(slash + 1);
  }
  return null;
}
// The ref named in the command, read from each `git push|merge` invocation's OWN
// argument span (to the next shell separator) — never an unrelated slashed token
// elsewhere in a compound command (`cd src/foo && git push` resolves no ref).
// Quoted spans are masked first, so `-m "mentions feature/x"` prose never
// resolves. push's first non-flag argument is the <remote> (possibly a slashed
// URL — never a topic) and is skipped; merge's arguments are all candidates.
function refTopicFromCommand(command) {
  const masked = normalizeGitInvocation(command);
  // `merge(?![-\w])` rejects `merge-base`/`merge-tree`/`merge-file`/`mergetool` — the
  // hyphen/word-char after `merge` marks a plumbing subcommand, not a real merge whose
  // ref we'd parse. `push\b` keeps its boundary (no `push-*` plumbing to confuse). A
  // real merge always has whitespace after `merge`, so none is newly excluded.
  const inv = /\bgit\s+(push\b|merge(?![-\w]))([^;&|\n]*)/g;
  let m;
  while ((m = inv.exec(masked)) !== null) {
    const args = m[2].split(/\s+/).filter((t) => t && !t.startsWith("-"));
    const candidates = m[1] === "push" ? args.slice(1) : args;
    for (const token of candidates) {
      const topic = topicFromRefToken(token);
      if (topic) return topic;
    }
  }
  return null;
}
// Tag pushes (--tags flag, refs/tags/ prefix, or vN.N.N-style token) never
// need a review stamp. Evaluated per git-push invocation so a compound
// command mixing a feature push and a tag push (`push feature/foo && push
// v1.0`) is NOT treated as a tag push — only when ALL push invocations in
// the command are tag pushes does this return true.
function isTagPush(command) {
  const masked = normalizeGitInvocation(command || "");
  const inv = /\bgit\s+push\b([^;&|\n]*)/g;
  let hasPush = false;
  let m;
  while ((m = inv.exec(masked)) !== null) {
    hasPush = true;
    const span = m[1];
    if (!/\s--tags\b/.test(span) && !/\srefs\/tags\//.test(span) && !/\sv\d+\.\d+/.test(span)) {
      return false; // this invocation is not a tag push → compound is not all-tag
    }
  }
  return hasPush; // at least one push, and every one is a tag push
}
// True when a `git push` command carries an EXPLICIT non-flag, non-remote ref
// token that `topicFromRefToken` could not parse as a slashed branch (e.g. a
// tag name or a trunk branch name). In that case the HEAD fallback would
// resolve the wrong topic — the explicit ref IS the target, we just can't read
// a topic from it. A bare `git push origin` has no such token (only the remote)
// and correctly falls through to HEAD.
function pushHasUnparsedExplicitRef(command) {
  const masked = normalizeGitInvocation(command);
  if (!/\bgit\s+push\b/.test(masked)) return false;
  const inv = /\bgit\s+push\b([^;&|\n]*)/g;
  let m;
  while ((m = inv.exec(masked)) !== null) {
    const args = m[1].split(/\s+/).filter((t) => t && !t.startsWith("-"));
    // args[0] = remote (skip); args[1+] = explicit refs
    for (const token of args.slice(1)) {
      if (!topicFromRefToken(token)) return true; // explicit but unresolvable
    }
  }
  return false;
}
// True when a `git push` command carries an EXPLICIT trunk ref (`main` or `master`)
// as a pushed branch — the F1 case the merge-gate otherwise escaped: `git push origin
// main` left the topic unresolvable (a bare `main` has no slash), so the gate fell
// through to the ask rule, which degrades to a SILENT PASS on a platform with no
// ask-return (OpenCode). In this protocol main moves ONLY via PR squash-merge on the
// forge, never a direct push from a session (PROTOCOL.md `## Git flow`), so a direct
// trunk push is already a violation — deny it mechanically on BOTH platforms.
// Whole-token equality (NOT includes) so `maintenance`/`mainline` never match. Both
// sides of a <src>:<dst> refspec are tested (`HEAD:main` is a trunk push too); the
// `+` force marker is stripped. A tag token (`vN.N`, `refs/tags/…`) never equals a
// bare `main`, so tag pushes are excluded by construction; isTagPush is the explicit
// early-out at the call site for defense in depth.
// Returns the trunk ref name (`main`/`master`) when found, else null. The found name
// IS the stamp topic the gate checks — a project that legitimately reviewed a change
// branched/named `main` would carry `main_review.md`, so a *stamped* trunk push still
// passes; only the unstamped one denies (symmetry with the bare-push path).
function pushExplicitTrunkRef(command) {
  const masked = normalizeGitInvocation(command || "");
  if (!/\bgit\s+push\b/.test(masked)) return null;
  const inv = /\bgit\s+push\b([^;&|\n]*)/g;
  const trunkOf = (side) => (side === "main" || side === "master" ? side : null);
  let m;
  while ((m = inv.exec(masked)) !== null) {
    const args = m[1].split(/\s+/).filter((t) => t && !t.startsWith("-"));
    // args[0] = remote (skip); args[1+] = explicit refs.
    for (let token of args.slice(1)) {
      if (token.startsWith("+")) token = token.slice(1); // refspec force marker
      for (const side of token.split(":")) { // either side of <src>:<dst>
        const t = trunkOf(side);
        if (t) return t;
      }
    }
  }
  return null;
}

// True when a `git push` names the CURRENT HEAD branch as an explicit ref token
// that topicFromRefToken could not slash-parse — the common `git push origin
// <branch>` for a no-slash branch name (push the branch you're on). There the
// HEAD fallback is correct and must NOT be skipped: skipping it mis-fires
// merge-topic-unresolvable on every such push (the friction of pushing a branch
// like hook-mode-strict-light). Whole-token equality (force marker stripped,
// either side of a <src>:<dst> refspec); a slashed HEAD branch is already resolved
// by refTopicFromCommand upstream, so this only ever decides the no-slash case.
// Parallels pushExplicitTrunkRef / pushHasUnparsedExplicitRef.
function pushExplicitHeadBranch(command, head) {
  if (!head) return false;
  const masked = normalizeGitInvocation(command || "");
  if (!/\bgit\s+push\b/.test(masked)) return false;
  const inv = /\bgit\s+push\b([^;&|\n]*)/g;
  let m;
  while ((m = inv.exec(masked)) !== null) {
    const args = m[1].split(/\s+/).filter((t) => t && !t.startsWith("-"));
    for (let token of args.slice(1)) {
      if (token.startsWith("+")) token = token.slice(1); // refspec force marker
      for (const side of token.split(":")) { // either side of <src>:<dst>
        if (side === head) return true;
      }
    }
  }
  return false;
}
// A trunk fast-forward sync — `git merge --ff-only <trunk-upstream>` while ON that trunk —
// is NOT a feature merge, so the merge-gate must not require a review stamp for it. It is the
// post-squash-merge sync the git flow needs (`git checkout main && git pull`); without this
// carve-out the gate DENIED it (the topic resolves to `main`, no `main_review.md`), forcing
// the destructive `git reset --hard origin/main` workaround — a worse surface than the ff it
// blocked. STRICT (all four guards must hold — bypass-safe for the [mechanical] floor):
//   1. a `git merge` (not push, not `merge-*` plumbing);
//   2. an EXPLICIT `--ff-only` (guarantees no merge commit — bare `--ff` can still merge);
//   3. the merged ref is `main`/`master`/`origin/main`/`origin/master` (the trunk or its
//      conventional remote-tracking ref — `feature/main` is REJECTED; a non-`origin` remote
//      gets no carve-out, byte-identical to today, fail-safe);
//   4. the checkout (headBranch) is ON that same trunk (a feature branch is never on a trunk).
const TRUNK_FF_REF = /^(main|master|origin\/main|origin\/master)$/;
function isTrunkFastForward(command, root) {
  const cmd = normalizeGitInvocation(command || "");
  if (!/\bgit\s+merge(?![-\w])/.test(cmd)) return false;   // guard 1: a `git merge`
  if (!/\s--ff-only\b/.test(cmd)) return false;            // guard 2: explicit --ff-only only
  const inv = /\bgit\s+merge(?![-\w])([^;&|\n]*)/g;
  let m, refTrunk = null;
  while ((m = inv.exec(cmd)) !== null) {
    for (let tok of m[1].split(/\s+/).filter((t) => t && !t.startsWith("-"))) {
      if (tok.startsWith("+")) tok = tok.slice(1);          // refspec force marker
      tok = tok.split(":")[0];                              // src side of a <src>:<dst> refspec
      if (TRUNK_FF_REF.test(tok)) { refTrunk = tok.split("/").pop(); break; } // guard 3
    }
    if (refTrunk) break;
  }
  if (!refTrunk) return false;
  return headBranch(root) === refTrunk;                     // guard 4: checkout on that same trunk
}

// Resolution stays SYNTACTIC — it returns the extracted topic as-is, even an
// unclean one. Traversal validation lives downstream at the stamp boundary
// (isCleanTopic in reviewStampSatisfied), NOT here: nulling an unclean
// command-ref topic here would fall back to HEAD and could read a stamped
// checkout's stamp instead — a bypass. An unclean topic must stay non-null so
// the gate denies it, never resolves around it.
function resolveMergeTopic(command, root) {
  if (typeof command !== "string" || !command) return null;
  const fromCommand = refTopicFromCommand(command);
  if (fromCommand) return fromCommand;
  // Skip HEAD fallback when the push names an explicit ref we can't slash-parse
  // (a tag, a trunk, or ANOTHER branch): falling back to HEAD would resolve the
  // WRONG topic. EXCEPT when that explicit ref IS the current HEAD branch — the
  // common `git push origin <branch>` for a no-slash branch name (push the branch
  // you're on). There HEAD is correct (the branch IS the topic); skipping the
  // fallback mis-fires merge-topic-unresolvable on every such push (pushing a
  // branch like hook-mode-strict-light). HEAD is always a branch, so resolving it
  // can't confuse a tag/trunk push — isTagPush + pushExplicitTrunkRef handle those
  // upstream. A bare `git push origin` has no explicit token and falls through to
  // HEAD unchanged.
  if (pushHasUnparsedExplicitRef(command)) {
    const head = headBranch(root);
    if (head && pushExplicitHeadBranch(command, head)) {
      const topic = stripPrefix(head);
      if (topic) return topic;
    }
    return null;
  }
  const head = headBranch(root);
  if (head) {
    const topic = stripPrefix(head);
    if (topic) return topic;
  }
  return null;
}
// A merge-gate topic becomes a path SEGMENT (`<topic>_review.md`), so it must be
// a single clean segment. Anything carrying a path separator (`/` / `\`), a NUL,
// or a bare dot-segment (`.` / `..`) is not a usable topic — it could traverse
// out of reviews/. Plain character tests, no regex (no backtracking surface in
// the deny layer).
function isCleanTopic(topic) {
  return typeof topic === "string" && topic.length > 0 &&
    !topic.includes("/") && !topic.includes("\\") && !topic.includes("\0") &&
    topic !== "." && topic !== "..";
}
// The CHOKE POINT every topic source (command ref, HEAD, any future source)
// funnels through before a stamp path is built — so the traversal guard here
// cannot be bypassed by a second entry path. A crafted ref `feature/../EVIL`
// resolves topic `../EVIL`; left unguarded, `.ai-dev/reviews/../EVIL_review.md`
// escapes reviews/ and a planted stamp would satisfy the [mechanical] floor. An
// unclean topic leaves the stamp UNsatisfiable ⇒ the merge-gate DENIES (fail
// toward deny). A nested branch name (feature/sub/topic → `sub/topic`) is
// rejected by the same rule: the convention is a single `<prefix>/<topic>`.
function reviewStampSatisfied(root, topic) {
  if (!isCleanTopic(topic)) return false;
  const file = path.join(path.resolve(root), ".ai-dev", "reviews", topic + "_review.md");
  let text;
  try { text = fs.readFileSync(file, "utf8"); } catch { return false; }
  const stampOK = (label) => {
    // Heading level is incidental — the gate reads the verdict's PRESENCE, not
    // the markdown level. Accept any level (#…######): a reviewer authoring a
    // fresh file naturally opens with an H1 title, and pinning ## only cost a
    // blocked push + a wasted re-review (8D reviewer-stamp-heading-level).
    const m = text.match(new RegExp("^#{1,6}[ \\t]+" + label + ":[ \\t]*(.*)$", "m"));
    if (!m) return false;
    let content = m[1].trim();
    // Also accept the verdict on the very next non-blank line after the heading
    // (resilient to reviewers that split "## Code review:" and "APPROVED").
    if (!content) {
      const after = text.slice(text.indexOf(m[0]) + m[0].length);
      const next = after.match(/^\r?\n([^\r\n#][^\r\n]*)/);
      if (next) content = next[1].trim();
    }
    if (!content || /^NOT YET RUN$/i.test(content)) return false;
    return true;
  };
  // The accepted heading labels are exactly the Reviewer's documented stamp
  // forms (src/agents/reviewer.md, Verdict).
  return stampOK("Code review") || stampOK("Doc review");
}

export {
  stripPrefix,
  topicFromRefToken,
  normalizeGitInvocation,
  refTopicFromCommand,
  isTagPush,
  pushHasUnparsedExplicitRef,
  pushExplicitTrunkRef,
  pushExplicitHeadBranch,
  isTrunkFastForward,
  resolveMergeTopic,
  isCleanTopic,
  reviewStampSatisfied,
};
