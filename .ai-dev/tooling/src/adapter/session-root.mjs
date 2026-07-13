// Session-root + target-repo resolution for the platform shims. This is fs/git-BOUND
// glue (it shells out to `git` and stats the tree), the shim's job — NOT the engine's:
// the engine stays pure and consumes the resolved boolean (`input.targetsSessionRepo`),
// it never resolves a repo itself. One home so both platform shims resolve identically.
//
// The bug this closes: the old root resolution was `git rev-parse --show-toplevel` from
// cwd, which flips to a NESTED repo's toplevel when cwd is inside one (e.g. a separate
// repo bootstrapped under `_scratch/proj/.git`). That mis-anchored the write-boundary,
// the stamp read, and the merge-gate onto the nested repo — false positives on a
// legitimate separate-repo bootstrap. The session is anchored instead to the nearest
// `.ai-dev/config.json` marker (the protocol/downstream root carries it; a nested repo
// does not), and the git-targeting denies are scoped to the SESSION repo's git db.
//
// Also: `extractGitEffectiveCwd` (#383 Feature A) resolves the directory a `git -C <path>`
// command runs in, so the gate checks the CORRECT target repo instead of the shell cwd
// (which `git -C` does not change). Lives here (not in engine-bash.mjs) because it needs
// `fs.realpathSync` — the engine must stay pure, no fs calls.

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { maskQuotedSpans } from "./engine-bash.mjs";

// Walk up from `cwd` to the nearest ancestor dir carrying `.ai-dev/config.json` (the
// session marker). Returns that dir, or null when none is found (an unconfigured
// project — the caller falls back to the git toplevel). The NEAREST (deepest) marker
// wins: a configured project nested under another resolves to its own root, not the
// parent's.
export function findSessionMarkerRoot(cwd) {
  let dir = path.resolve(cwd);
  for (;;) {
    if (fs.existsSync(path.join(dir, ".ai-dev", "config.json"))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null; // filesystem root reached, no marker
    dir = parent;
  }
}

// The git work-tree toplevel of `cwd`, or null when cwd is not in a work tree / git
// errors / git is absent. POSITIVE resolution only — a null means "could not be
// positively resolved", which every caller treats as the fail-CLOSED direction.
export function gitToplevel(cwd) {
  try {
    const top = execFileSync("git", ["rev-parse", "--show-toplevel"],
      { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    return top || null;
  } catch { return null; }
}

// Resolve the SESSION root for a hook whose working dir is `cwd`. Priority:
//   1. the nearest `.ai-dev/config.json` marker dir (the session anchor — a nested
//      `.git` under it never flips the root);
//   2. the git toplevel (an unconfigured project still gets its repo root — the old
//      behaviour, unchanged);
//   3. cwd (no marker, no git — the last-resort fallback, unchanged).
// NORMAL case is byte-identical to the old git-toplevel resolution: at a real session
// root the marker dir IS the git toplevel; only the nested-cwd case is corrected.
export function resolveSessionRoot(cwd) {
  const base = path.resolve(cwd);
  return findSessionMarkerRoot(base) || gitToplevel(base) || base;
}

// Does the command's working dir target the SESSION repo's git db? Compares cwd's git
// toplevel against the SESSION repo's git toplevel (the git root containing the session
// root) — NOT the marker dir, so a configured project that is itself a subdir of a
// larger git repo is still correctly recognised as the session repo (its commands are
// NOT exempted).
//
// FAIL-CLOSED — load-bearing for the floor: returns TRUE (the deny applies) unless cwd
// is POSITIVELY confirmed a DIFFERENT repo. Any doubt — cwd's toplevel unresolvable, the
// session repo's toplevel unresolvable — defaults TRUE. It returns FALSE only when BOTH
// toplevels resolve and differ: cwd is provably a separate nested repo, whose `add -A` /
// `push main` target a different git db + origin and can never move the protocol's main.
export function targetsSessionRepo(cwd, sessionRoot) {
  const cwdTop = gitToplevel(cwd);
  if (cwdTop === null) return true; // cwd repo unresolvable ⇒ fail-closed (deny applies)
  const sessionTop = gitToplevel(sessionRoot);
  if (sessionTop === null) return true; // session repo unresolvable ⇒ fail-closed
  return path.resolve(cwdTop) === path.resolve(sessionTop);
}

// Extract the effective git working-directory from a command string's `-C <path>`
// global flag. Called by the shim's main() before computing targetsSessionRepo, so a
// `git -C /other-repo commit|push origin main` targeting a provably-different repo is
// no longer falsely blocked by the SESSION repo's HEAD state (#383 Feature A).
//
// Returns the resolved canonical path when EXACTLY ONE `-C` flag is present and its
// target exists on disk; returns `cwd` unchanged in EVERY other case (FAIL-CLOSED).
//
// Security contract:
//   • No `-C` flag → `cwd` (fast path, unchanged behavior — common case)
//   • Exactly one `-C <path>` → path.resolve(cwd, rawPath) then realpathSync it
//   • Multiple `-C` flags (chained `git -C a -C b`) → `cwd` (fail-closed)
//   • realpathSync throws (non-existent or inaccessible path) → `cwd` (fail-closed)
//   • `-c` lowercase is `git -c key=val` (config option) — NOT a cwd flag; this
//     function is CASE-SENSITIVE and only extracts uppercase `-C`
//   • Quoted spans masked first: a `-C /path` inside a commit message is data, not a flag
//   • A realpath that resolves back to the session repo: targetsSessionRepo returns true
//     → deny still fires (symlink safety; the check lives upstream, not here)
//   • Error path: any exception returns `cwd` → targetsSessionRepo computes as before
//     → the function CANNOT produce a false ALLOW through error
export function extractGitEffectiveCwd(command, cwd) {
  if (typeof command !== "string" || !command) return cwd;
  // Mask quoted spans: a `-C /path` inside `git commit -m "use -C /other"` is
  // data in the message, not a flag. maskQuotedSpans replaces "…" and '…' with blanks.
  const masked = maskQuotedSpans(command);
  // Collect all `-C <value>` occurrences, case-sensitive (uppercase -C only).
  // `-C` must be preceded by start, whitespace, or a shell separator — never the
  // interior of a long option like `--no-color` (which has 'o' before the 'C').
  // Two accepted forms:
  //   • `-C value`  (space-separated, captured in group 1)
  //   • `-Cvalue`   (glued, no space, rare but git-valid, captured in group 2)
  const re = /(?:^|[\s;&|(])-C(?:\s+(\S+)|(\S+))/g;
  let m;
  const hits = [];
  while ((m = re.exec(masked)) !== null) {
    hits.push(m[1] ?? m[2]);
  }
  if (hits.length !== 1) return cwd; // zero or multiple flags → fail-closed
  const rawPath = hits[0];
  try {
    const resolved = path.resolve(cwd, rawPath);
    return fs.realpathSync(resolved); // throws when path does not exist
  } catch {
    return cwd; // non-existent path or any fs error → fail-closed
  }
}
