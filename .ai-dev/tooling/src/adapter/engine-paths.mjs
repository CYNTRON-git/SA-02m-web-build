// Path / root resolution helpers for the enforcement engine. Pure path math —
// no fs, no platform names. One home for "is this resolved path inside that root"
// and the canonical-path predicates the component-set validator leans on.

import path from "node:path";

export function isInsideRoot(root, resolved) {
  const r = path.resolve(root);
  return resolved === r || resolved.startsWith(r + path.sep);
}
// A resolved path is inside a sanctioned out-of-root scratch set (the agent's OWN
// harness-assigned scratchpad / tool-result overflow store — derived fail-closed by the
// adapter, see claude/sanctioned-scratch.mjs). `paths` is an array of canonical absolute
// roots (possibly empty/undefined). Pure lexical match (mirrors isInsideRoot) — the
// derivation canonicalises via realpath, the caller passes the same-shape resolved target.
// One predicate, two call sites with DIFFERENT sets: the read-family boundary predicates
// consult it with the full set (input.sanctionedScratch) ALONGSIDE isInsideAnyComponent;
// the write boundary predicate consults it with the narrower writable-only subset
// (input.sanctionedScratchWritable — the per-session temp root, never the overflow store).
export function isInsideSanctioned(paths, resolved) {
  if (!Array.isArray(paths) || paths.length === 0) return false;
  return paths.some((p) => resolved === p || resolved.startsWith(p + path.sep));
}
export function resolveTarget(root, target) {
  if (typeof target !== "string" || target.length === 0) return null;
  return path.resolve(root, target);
}
export function isFilesystemRoot(p) {
  return path.dirname(p) === p; // "/" on POSIX, "C:\\" on Windows — dirname is itself
}
// `ancestor` is an ancestor of (or equal to) `descendant`. Both must be canonical
// absolute paths. Used to reject a declared root that contains the session root —
// widening to a parent would re-expose everything above the work, including the
// enforcer's own tree.
export function isAncestorOrEqual(ancestor, descendant) {
  return descendant === ancestor || descendant.startsWith(ancestor + path.sep);
}
