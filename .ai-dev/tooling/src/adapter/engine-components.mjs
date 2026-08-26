// Multi-repo components + orchestrator-writable boundary logic for the enforcement
// engine. Holds the fail-CLOSED manifest loader/validator (componentRoots) — the
// riskiest logic on the security floor — plus the per-root `.ai-dev/tooling/` never-list
// carve-out and the orchestrator-authorable test. Reads fs; leans on engine-paths for
// the canonical-path predicates.

import fs from "node:fs";
import path from "node:path";
import { isInsideRoot, isFilesystemRoot, isAncestorOrEqual } from "./engine-paths.mjs";

// Match a root-relative path against a registry entry. An entry ending in "/"
// matches that dir and everything under it; otherwise the exact path or anything
// under it as a dir.
function relMatches(rel, entry) {
  if (entry.endsWith("/")) return rel === entry.slice(0, -1) || rel.startsWith(entry);
  return rel === entry || rel.startsWith(entry + "/");
}
// A resolved in-root path the orchestrator MAY author, per the registry's
// orchestrator_writable (allow_prefixes minus never). DATA-DRIVEN — no hard-coded
// paths; deny-rules.json is the single home for the writable set.
function isOrchestratorAuthorable(root, resolved, ow) {
  const rel = path.relative(path.resolve(root), resolved);
  if (rel === "" || rel.startsWith("..")) return false;
  if ((ow?.never || []).some((e) => relMatches(rel, e))) return false;
  return (ow?.allow_prefixes || []).some((e) => relMatches(rel, e));
}
function writesIntoNever(root, resolved, ow) {
  const rel = path.relative(path.resolve(root), resolved);
  return (ow?.never || []).some((e) => relMatches(rel, e));
}
// The `.ai-dev/tooling/` carve-out is the UNCONDITIONAL enforcer-source deny
// (invariant 2). It must hold under EVERY declared component root, not just the
// session root: a manifest must never widen INTO a sibling's tooling dir and
// re-expose that sibling's enforcer. So this tests the never-list against EACH
// root in the validated component set — true iff `resolved` is inside ANY root's
// never-list (tooling) subtree. Used by (a) the self-patch WRITE deny
// (writesIntoTooling), so a write into ANY root's tooling reports the meaningful
// `self-patch-enforcer` ruleId per-root; and (b) the read/find boundary predicates
// (pathOutsideRoot / findTargetOutsideRoot), which have no dedicated tooling rule
// and so apply this carve-out themselves — tooling deny outranks the component-set
// allow. With an invalid manifest componentRoots collapses to [sessionRoot], so the
// sibling is wholly denied as out-of-boundary anyway; this stays fail-safe.
function writesIntoAnyNever(root, resolved, ow) {
  return componentRoots(root).some((r) => writesIntoNever(r, resolved, ow));
}

// ── multi-repo components: the fail-CLOSED manifest loader/validator ──────────
// Reads .ai-dev/components.json from the session root and returns the set of
// canonical absolute roots an agent may touch — ALWAYS including the session root.
// This is the riskiest logic on the security floor: a crafted manifest must NEVER
// widen the boundary to an attacker-chosen path. So it fails CLOSED — on ANY doubt
// the set collapses to the single session root (byte-identical to today's behaviour).
//
// The three project-boundary predicates (pathOutsideRoot / findTargetOutsideRoot /
// writeTargetOutsideRoot) consult this set via isInsideAnyComponent; isInsideRoot
// itself stays single-root, and the tooling/stamp/merge/truncation/orchestrator-content
// denies stay session-root-anchored (only the boundary read/find/write denies widen).
//
// The manifest schema (one home: docs/architecture.md `## Components`): a non-empty
// JSON array of objects, each { "root": "<rel path>", "role": "<str>", "stack": "<str>" }.
// `root` is resolved RELATIVE TO the manifest's own directory (the session root),
// canonicalised with path.resolve + fs.realpathSync (defeats symlink escapes).
//
// Fail-closed rules (every one collapses to the single root):
//   • absent / unreadable file                         ⇒ single root
//   • JSON parse failure                               ⇒ single root
//   • shape not a non-empty array of valid root objects⇒ single root
//   • a declared root that does not realpath to an
//     EXISTING directory                               ⇒ WHOLE manifest rejected
//   • an OVERBROAD root — resolves to a filesystem root,
//     or to an ancestor of / the session root itself   ⇒ WHOLE manifest rejected
// Rejection is ALL-OR-NOTHING: one bad entry rejects the whole manifest, never a
// partial honour (a partial honour is a fail-open seam — plan-adversary inversion).
function componentRoots(root) {
  const sessionRoot = path.resolve(root);
  const single = [sessionRoot];
  let text;
  try {
    text = fs.readFileSync(path.join(sessionRoot, ".ai-dev", "components.json"), "utf8");
  } catch { return single; } // absent / unreadable ⇒ fail closed
  let parsed;
  try { parsed = JSON.parse(text); } catch { return single; } // unparseable ⇒ fail closed
  if (!Array.isArray(parsed) || parsed.length === 0) return single; // wrong/empty shape ⇒ fail closed
  const set = new Set([sessionRoot]);
  for (const entry of parsed) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return single; // not a root object
    if (typeof entry.root !== "string" || entry.root.length === 0) return single; // missing/blank root
    // Resolve RELATIVE TO the manifest dir (the session root), then canonicalise
    // through realpath so a symlinked declared root cannot escape to /etc.
    let canon;
    try { canon = fs.realpathSync(path.resolve(sessionRoot, entry.root)); }
    catch { return single; } // does not resolve to an existing path ⇒ reject whole manifest
    let stat;
    try { stat = fs.statSync(canon); } catch { return single; }
    if (!stat.isDirectory()) return single; // must be an existing DIRECTORY
    // OVERBROAD guards — reject the whole manifest (never partially honour):
    if (isFilesystemRoot(canon)) return single; // a filesystem root
    if (isAncestorOrEqual(canon, sessionRoot)) return single; // ancestor of / equal to the session root
    set.add(canon);
  }
  return [...set];
}
// Boundary membership across the validated component SET (Step 2 wiring). A target
// is inside the boundary iff it is inside ANY validated declared root. With no/invalid
// manifest, componentRoots returns just [sessionRoot], so this is byte-identical to the
// single-root isInsideRoot — the no-manifest regression tripwire. ONLY the three
// project-boundary denies (pathOutsideRoot / findTargetOutsideRoot / writeTargetOutsideRoot)
// call this; the stamp/merge/truncation/orchestrator-content denies stay anchored
// to the session root via isInsideRoot (`.ai-dev/plans/multi-repo-components.md`).
//
// Tooling carve-out — NOT folded into this allow. Writes into ANY root's
// `.ai-dev/tooling/` are owned by the dedicated self-patch deny (writesIntoTooling
// → writesIntoAnyNever), so a tooling write reports the meaningful `self-patch-enforcer`
// ruleId, per-root. Reads/finds have no dedicated tooling rule, so the read/find
// boundary predicates apply the per-root tooling carve-out themselves
// (writesIntoAnyNever) BEFORE the component-set allow — tooling deny outranks the
// allow, so a declared sibling's tooling is denied on read/find exactly like a write.
function isInsideAnyComponent(root, resolved) {
  return componentRoots(root).some((r) => isInsideRoot(r, resolved));
}

export {
  relMatches,
  isOrchestratorAuthorable,
  writesIntoNever,
  writesIntoAnyNever,
  componentRoots,
  isInsideAnyComponent,
};
