// Claude adapter — derive the agent's OWN sanctioned out-of-root scratch roots from
// the harness environment, fail-CLOSED. The boundary deny (invariant 2) is anchored to
// the session root (+ declared components) to keep an agent out of OTHER projects. But it
// also false-blocks the agent's OWN workspaces the harness itself hands it — the tool-result
// overflow store (a fetched doc/image body that exceeded the inline cap), the per-session
// temp root (the scratchpad, staged pasted files, the tasks/ dir), and the image-cache
// (screenshots cached in sibling directories of CLAUDE_CONFIG_DIR, read-only). This module
// derives those roots from the Claude Code env so the boundary predicates can carve them out.
// CC env conventions live HERE (the adapter), never in the neutral engine.
//
// Fail-closed throughout: a path is admitted ONLY if it is built from present env, realpath-
// resolves to an existing directory, and passes the overbroad/ancestor guard (mirrors
// componentRoots in engine-components.mjs — the riskiest widening logic). Bad/absent env ⇒
// empty set ⇒ byte-identical to today's strict behaviour. The three paths are derived
// INDEPENDENTLY — one failing its check does not poison the others.
//
// Read vs write scope: all three roots are read-allowed (consulted by the read-family boundary
// predicates). Only the per-session temp root is WRITE-allowed — the tool-result overflow
// store and the proxy image-cache stay write-denied forever (they are the harness/proxy's own
// artifacts, never the agent's to mutate). `deriveSanctionedScratch` returns all roots (reads); `deriveSanctionedScratchWritable`
// returns only the writable subset (the temp root) for the write boundary predicate. Both
// delegate to one internal tagged derivation — no logic duplicated. See
// docs/decisions/out-of-root-scratch-allow.md.

import path from "node:path";
import fs from "node:fs";
import os from "node:os";
import { isFilesystemRoot, isAncestorOrEqual } from "../engine-paths.mjs";

// realpath or null — a missing / unreadable path yields null (fail-closed skip), never throws.
function realDir(p) {
  try {
    const real = fs.realpathSync(p);
    // Confirm it resolved to a directory (a stale file at the path is not a usable scratch root).
    const st = fs.statSync(real);
    return st.isDirectory() ? real : null;
  } catch {
    return null;
  }
}

// CC's project-slug convention: the session-root absolute path with `/` → `-`
// (verified empirically: /home/.../ai-pm-protocol → -home-...-ai-pm-protocol; case preserved,
// no lowercasing). Matches the `~/.claude/projects/<slug>/` dir CC itself creates.
function projectSlug(root) {
  return path.resolve(root).replace(/\//g, "-");
}

// The overbroad guard, shared with componentRoots' discipline: a sanctioned root must NOT
// contain the session root (widening to a parent re-exposes everything above the work,
// including the enforcer's own tree) and must NOT be a filesystem root. It may be INSIDE the
// session root (harmless — already allowed by the component set), though in practice both
// derived roots are outside it. `boundary` is the parent the sanctioned root must stay a
// proper descendant of (the config dir for the tool-results store; the per-project temp base
// for the session root) — a symlink that realpath-escaped the boundary is rejected.
function admissible(sanctionedReal, rootReal, boundaryReal) {
  if (!sanctionedReal) return false;
  if (isFilesystemRoot(sanctionedReal)) return false;
  if (isAncestorOrEqual(sanctionedReal, rootReal)) return false; // would contain the session root
  if (boundaryReal && !isAncestorOrEqual(boundaryReal, sanctionedReal)) return false; // escaped its parent
  if (boundaryReal && sanctionedReal === boundaryReal) return false; // the parent itself (too broad)
  return true;
}

// Derive the TAGGED sanctioned-scratch roots for THIS session from env — the one internal
// computation both public derivations delegate to (invariant 1: no logic duplicated).
// Returns `{ path, writable }[]` — canonical (realpath'd) absolute paths, possibly empty.
// `env` defaults to process.env so the shim can call it with no args; tests pass a mock
// env + a real tmp tree.
function deriveTagged(env = process.env, root = process.cwd()) {
  const out = [];
  const rootReal = (() => { try { return fs.realpathSync(path.resolve(root)); } catch { return path.resolve(root); } })();
  const slug = projectSlug(root);

  // (1) tool-result overflow store: <CLAUDE_CONFIG_DIR>/projects/<slug>/tool-results/
  // The harness's own artifact — read-only forever, never writable by the agent.
  const configDir = typeof env.CLAUDE_CONFIG_DIR === "string" && env.CLAUDE_CONFIG_DIR.trim()
    ? env.CLAUDE_CONFIG_DIR
    : null;
  if (configDir) {
    const boundaryReal = realDir(configDir);
    if (boundaryReal) {
      const p = realDir(path.join(configDir, "projects", slug, "tool-results"));
      if (admissible(p, rootReal, boundaryReal)) out.push({ path: p, writable: false });
    }

    // (3) universal image-cache carve-out: scan siblings of CLAUDE_CONFIG_DIR for any
    // directory containing an image-cache/ subdirectory. Works regardless of sibling
    // name (not just .claude-proxy). Fail-closed: no matching sibling ⇒ nothing added;
    // a sibling's image-cache/ is read-only (the proxy owns it, the agent only reads).
    const configParent = path.dirname(configDir);
    const configParentReal = realDir(configParent);
    if (configParentReal) {
      try {
        const siblings = fs.readdirSync(configParent, { withFileTypes: true });
        for (const entry of siblings) {
          if (!entry.isDirectory()) continue;
          if (entry.name === path.basename(configDir)) continue; // skip self
          const cachePath = realDir(path.join(configParent, entry.name, "image-cache"));
          if (cachePath && admissible(cachePath, rootReal, configParentReal)) {
            out.push({ path: cachePath, writable: false });
          }
        }
      } catch {
        // readdirSync failed ⇒ fail-closed, nothing added
      }
    }
  }

  // (2) per-session harness temp root: <TMPDIR|/tmp>/claude-<uid>/<slug>/<CLAUDE_CODE_SESSION_ID>/
  // The agent's OWN workspace (the scratchpad the harness system prompt directs it to write
  // temp files to) — writable.
  const sid = typeof env.CLAUDE_CODE_SESSION_ID === "string" && env.CLAUDE_CODE_SESSION_ID.trim()
    ? env.CLAUDE_CODE_SESSION_ID
    : null;
  if (sid) {
    const tmp = (typeof env.TMPDIR === "string" && env.TMPDIR.trim()) ? env.TMPDIR : os.tmpdir();
    const uid = typeof process.getuid === "function" ? process.getuid() : null;
    const segs = [tmp, uid !== null ? `claude-${uid}` : "claude", slug, sid];
    const base = path.join(...segs.slice(0, 3)); // <tmp>/claude-<uid>/<slug> — the boundary
    const boundaryReal = realDir(base);
    if (boundaryReal) {
      const p = realDir(path.join(...segs));
      if (admissible(p, rootReal, boundaryReal)) out.push({ path: p, writable: true });
    }
  }

  return out;
}

// All sanctioned roots (reads) — signature + output byte-identical to before the write
// carve-out. Consulted by the read-family boundary predicates.
export function deriveSanctionedScratch(env = process.env, root = process.cwd()) {
  return deriveTagged(env, root).map((e) => e.path);
}

// Only the WRITABLE sanctioned roots (the per-session temp root) — consulted by the write
// boundary predicate. The tool-result overflow store never appears here.
export function deriveSanctionedScratchWritable(env = process.env, root = process.cwd()) {
  return deriveTagged(env, root).filter((e) => e.writable).map((e) => e.path);
}

// Exported for the unit test (mirrors engine.mjs's `_internals` pattern) so the overbroad
// guard is exercised directly with controlled paths, not only through the env-derived
// black box (which cannot plausibly produce an overbroad path from real harness env).
export const _internals = { admissible, projectSlug, realDir, deriveTagged };
