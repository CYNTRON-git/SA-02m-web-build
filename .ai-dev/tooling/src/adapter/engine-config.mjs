// Config + filesystem-state reads for the enforcement engine. Fixed root-relative
// reads within invariant 2, never a write: repo history markers, the rigor profile,
// the disabled-safeguards set, the product-brief fill check, and the plain-language
// guard registry. Every read fails SAFE on doubt (absent / unreadable / malformed) —
// each function's comment names its safe direction.

import fs from "node:fs";
import path from "node:path";

function fileNonEmpty(p) {
  try { return fs.statSync(p).size > 0; } catch { return false; }
}
// Resolve the checkout's REAL git directory — ONE home for "where is this checkout's
// .git", shared by every HEAD/ref read. In a MAIN checkout `.git` is a directory and it
// IS the git dir. In a git WORKTREE (parallel-work's per-feature checkout) `.git` is a
// FILE carrying `gitdir: <path>` — the real git dir (the worktree's private dir under
// the main repo's `.git/worktrees/<name>/`) is that pointer's target, which git writes
// ABSOLUTE but may be RELATIVE (resolved against the checkout root). null on no `.git`
// or an unreadable/malformed pointer. Pure fs reads, no shell — within invariant 2.
function resolveGitDir(root) {
  const dotGit = path.join(path.resolve(root), ".git");
  let st;
  try { st = fs.statSync(dotGit); } catch { return null; }
  if (st.isDirectory()) return dotGit;
  if (st.isFile()) {
    try {
      const m = fs.readFileSync(dotGit, "utf8").trim().match(/^gitdir:\s*(.+)$/);
      if (!m) return null;
      const target = m[1].trim();
      return path.isAbsolute(target) ? target : path.resolve(path.resolve(root), target);
    } catch { return null; }
  }
  return null;
}
// The checkout's current branch name (the FULL name, prefix NOT stripped), read from the
// real git dir's HEAD — WORKTREE-aware via resolveGitDir (the 5.52.1 reader returned null
// in a worktree, so a parallel-work push mis-fired merge-topic-unresolvable — #335). Null
// on a detached HEAD or an unreadable/missing HEAD. Used by the commit-to-trunk deny
// (commitOnUnstampedMain): a `git commit` names no branch, so the branch is the checkout,
// exactly the signal resolveMergeTopic's HEAD fallback reads — but here we need the WHOLE
// name (`main`, not a stripped topic) to compare against trunk. Also the ONE home
// engine-git's resolveMergeTopic imports (the 5.52.1 local headBranchName duplicate is
// retired — invariant 6).
function headBranch(root) {
  const gitDir = resolveGitDir(root);
  if (!gitDir) return null;
  try {
    const head = fs.readFileSync(path.join(gitDir, "HEAD"), "utf8").trim();
    const hm = head.match(/^ref:\s*refs\/heads\/(.+)$/);
    return hm ? hm[1].trim() : null;
  } catch { return null; }
}
// Does the repo already have at least one commit? True iff a packed-ref or a loose ref
// exists, or a reflog records history — i.e. NOT a freshly-`git init`'d repo with no
// commit yet. This is the carve-out signal for the commit-to-main deny: the only
// legitimate direct commit to main is the day-zero bootstrap (Setup step 0: `git init`
// + initial commit of the existing tree), where there is no history to bypass. A repo
// with ANY commit history is past that day-zero moment. Cheapest reliable check: a
// HEAD ref pointing at a branch whose loose-ref file exists, OR any packed-refs, OR a
// populated logs/HEAD. Fail toward "has commits" (deny-side) on doubt — a false
// "no commits" would WIDEN the carve-out (allow a commit that should deny), the wrong
// direction; so an unreadable .git makes this true (assume history ⇒ no carve-out).
function repoHasCommits(root) {
  const git = resolveGitDir(root); // worktree-aware — a worktree's history markers live in its real git dir, not root/.git (a file)
  if (!git) return false; // no .git at all ⇒ no history (carve-out applies; headBranch is null too)
  try {
    const branch = headBranch(root);
    if (branch && fs.existsSync(path.join(git, "refs", "heads", branch))) return true;
    if (fileNonEmpty(path.join(git, "packed-refs"))) return true;
    if (fileNonEmpty(path.join(git, "logs", "HEAD"))) return true;
    return false; // none of the history markers present ⇒ fresh init (carve-out applies)
  } catch { return true; } // unreadable ⇒ assume history (deny-side: no carve-out)
}
const TRUNK_BRANCHES = new Set(["main", "master"]);
// Is the project configured? True iff .ai-dev/config.json exists. Root-relative,
// fs-checked — the lazy-setup predicate reads only this presence (it adds context,
// never executes), within invariant 2.
function projectConfigured(root) {
  return fs.existsSync(path.join(path.resolve(root), ".ai-dev", "config.json"));
}
// Does the project have a FILLED product brief? False when docs/product.md is
// absent OR still the install-landed template — the installer copies the
// template verbatim, so presence alone proved nothing and the discovery nudge
// could never fire on a real install (the 4.18.0 fix). Template detection is
// TWO literal-substring layers (includes() on fixed text, never a regex — no
// prompt data reaches this read, no backtracking surface):
//   1. the sentinel `<!-- ai-dev:template -->` the template carries as line 1
//      (forward-looking; discovery deletes it on fill),
//   2. the §0 placeholder line, byte-identical in every template version ever
//      shipped (legacy; catches installs that copied a pre-sentinel template).
// Same fixed root-relative read as projectConfigured, within invariant 2.
const BRIEF_TEMPLATE_MARKERS = [
  "<!-- ai-dev:template -->",
  "<one plain sentence: what this product is and what it does",
];
function productBriefFilled(root) {
  let text;
  try { text = fs.readFileSync(path.join(path.resolve(root), "docs", "product.md"), "utf8"); }
  catch { return false; } // absent or unreadable ⇒ no brief
  return !BRIEF_TEMPLATE_MARKERS.some((m) => text.includes(m));
}
// The project's hook mode (.ai-dev/config.json `hookMode`). Controls how ask-class
// rules are realised: "strict" (default, fail-safe) = ask the Operator where the
// platform supports it; "light" = ask-class rules become deny + an informative
// message (no Operator interruption). Overridable via .ai-dev/config.local.json
// (the personal/shared split, mirroring launch). Absent / unreadable / malformed /
// unknown value ⇒ "strict" (fail-safe: the Operator is always asked).
function projectHookMode(root) {
  try {
    // Read shared config.
    const sharedPath = path.join(path.resolve(root), ".ai-dev", "config.json");
    const shared = JSON.parse(fs.readFileSync(sharedPath, "utf8"));
    // Read personal override (gitignored config.local.json).
    const localPath = path.join(path.resolve(root), ".ai-dev", "config.local.json");
    let local;
    try { local = JSON.parse(fs.readFileSync(localPath, "utf8")); }
    catch { local = null; }
    // hookMode is a top-level scalar — local wins over shared.
    const mode = local && typeof local.hookMode === "string" && local.hookMode.trim()
      ? local.hookMode.trim()
      : shared && typeof shared.hookMode === "string" && shared.hookMode.trim()
        ? shared.hookMode.trim()
        : "";
    if (mode === "light") return "light";
    return "strict"; // absent / unrecognised / malformed ⇒ strict
  } catch { return "strict"; }
}

// The project's rigor profile (.ai-dev/config.json `profile`). Defaults to "solo"
// on absent / unreadable / malformed / unknown value — proportionality by default
// (protocol-reference.md `## Project config`), a deliberate Operator decision, not fail-strict.
// Predicates that read the profile:
//   - orchestrator-content deny: relaxed on lite/solo/yolo (the original gate).
//   - merge-gate: short-circuits (returns false) on yolo — the explicit gate-off.
// All other floor predicates (tooling, boundary, truncation, stamp-write) do NOT
// read the profile — the default ("solo") widens none of those floors.
// Only an explicit "full" keeps the strict orchestrator-content lane.
// Same presence/value read within invariant 2 as projectConfigured — never a write.
function projectProfile(root) {
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(path.resolve(root), ".ai-dev", "config.json"), "utf8"));
    if (cfg.profile === "full" || cfg.profile === "lite") return cfg.profile;
    if (cfg.profile === "yolo") return "yolo"; // only an explicit value enters the escape hatch
    return "solo"; // absent / unrecognised / malformed ⇒ solo (never yolo by default)
  } catch { return "solo"; }
}

// The set of rule-ids the project has consciously disabled (.ai-dev/config.json
// `safeguards`). The GENERAL toggle: an explicit `safeguards.<id>: "off"` opts that
// one guard out — read once per evaluate and consulted in the rule loop, where the
// skip is ALSO gated on `rule.toggleable === true` (a deny/merge-gate rule carries no
// such flag, so it can never be skipped however config reads — the mechanical floor).
// Same fixed root-relative read shape as projectProfile (never a write; within
// invariant 2) and the SAME fail-safe discipline: ONLY the exact string "off" on a
// well-formed object disables. Fail-CLOSED to the EMPTY set (every guard stays ON) on
// any doubt — absent / unreadable / malformed JSON / a non-object `safeguards` / a
// non-string value.
function disabledSafeguards(root) {
  const off = new Set();
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(path.resolve(root), ".ai-dev", "config.json"), "utf8"));
    const sg = cfg.safeguards;
    if (!sg || typeof sg !== "object" || Array.isArray(sg)) return off; // non-object ⇒ empty
    for (const [id, val] of Object.entries(sg)) {
      if (val === "off") off.add(id); // only an explicit "off" string disables
    }
  } catch { /* absent / unreadable / malformed ⇒ empty set (all guards on) */ }
  return off;
}

// The plain-language guard registry — every rule's {id, class, label, toggleable},
// for the orchestrator's `## Safeguards` query/toggle and the setup dialog. Reads the
// loaded config (deny-rules.json); a rule with no `label` falls back to its id. Pure
// data shaping, no fs read.
function safeguardRegistry(config) {
  return (config.rules || []).map((r) => ({
    id: r.id,
    class: r.class,
    label: r.label || r.id,
    toggleable: r.toggleable === true,
  }));
}

export {
  fileNonEmpty,
  resolveGitDir,
  headBranch,
  repoHasCommits,
  TRUNK_BRANCHES,
  projectConfigured,
  productBriefFilled,
  projectProfile,
  projectHookMode,
  disabledSafeguards,
  safeguardRegistry,
};
