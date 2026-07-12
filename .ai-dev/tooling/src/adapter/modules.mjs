// The shared capability-module assembler. ONE copy of the compose logic, imported
// by BOTH install-agents shims (Claude + OpenCode) — mirrors how engine.mjs is the
// one home shared by both deny shims. A role agent is no longer a hand-authored
// monolith: it is a lean FLOOR body carrying a single marker (`<!-- ai-dev:modules -->`),
// and this helper REPLACES that marker with the enabled modules' fragments, in
// registry order. Toggle a module off and its fragment is simply not composed in.
//
// What is whose home (PROTOCOL.md manifesto, no duplication):
//   • the floor PROSE (always-on role text, e.g. the Reviewer's security item) lives
//     in src/agents/<role>.md and is NEVER a module fragment — the marker only ADDS;
//   • src/modules/registry.json carries the catalog (toggle shape · per-kind defaults · targets ·
//     fragment pointers), never floor prose;
//   • src/modules/<id>/<role>.md carries the DEEPENING a module adds to a role.
//
// HONESTY: a capability module is `[persona]` prompt content — it sharpens what the
// Builder/Reviewer THINK about; it blocks nothing at the tool-call layer (no deny
// rule). The architecture and registry label it so; this helper just assembles text.
//
// THREE security checks the assembler enforces (its own threat model):
//   1. a fragment pointer that escapes the repo root (absolute, or `..`-bearing) is
//      REJECTED — a typo'd/hostile pointer in the registry cannot read outside root
//      (invariant 2; mirrors the engine's isInsideRoot);
//   2. a fragment file NAMED by an ENABLED module but MISSING on disk is a HARD ERROR
//      — never silently ship a role missing its security section (the dangerous drop);
//   3. the fail-safe direction is toward MORE rigor: an unknown/malformed CONFIG
//      toggle, or an unknown/absent project kind, resolves the module ON with the
//      strict-side defaults — a bad config can never silently DISABLE the rigor.
//      The ONE default-OFF path is REGISTRY-authored: a per-kind default of literal
//      `false` (the registry is our data; the config is the untrusted input), and it
//      applies only while the config never names the module — any named config value
//      overrides it.

import fs from "node:fs";
import path from "node:path";

// ── path safety (mirrors engine.mjs isInsideRoot) ─────────────────────────────
function isInsideRoot(root, resolved) {
  const r = path.resolve(root);
  return resolved === r || resolved.startsWith(r + path.sep);
}

// Resolve a registry-relative fragment pointer against the root, REJECTING any
// pointer that is absolute or escapes the root. A fragment pointer is data in
// the registry; this is the trust boundary for it.
export function resolveFragmentPath(root, pointer) {
  if (typeof pointer !== "string" || pointer.length === 0) {
    throw new Error(`module fragment pointer is empty or not a string: ${JSON.stringify(pointer)}`);
  }
  if (path.isAbsolute(pointer)) {
    throw new Error(`module fragment pointer escapes root (absolute path): ${pointer}`);
  }
  const resolved = path.resolve(root, pointer);
  if (!isInsideRoot(root, resolved)) {
    throw new Error(`module fragment pointer escapes root: ${pointer}`);
  }
  return resolved;
}

// ── registry ──────────────────────────────────────────────────────────────────
export function loadRegistry(root) {
  return JSON.parse(fs.readFileSync(path.join(path.resolve(root), "src", "modules", "registry.json"), "utf8"));
}

// ── enabled-resolution (fail-safe to ON / strict side) ────────────────────────
// The project's kind (config.kind). Unknown/absent ⇒ the strict side: the kind
// whose defaults this module declares first, falling to "code" — mirrors the
// absent-`mode` ⇒ `interactive` fail-safe. A module's defaults map names the
// per-kind default toggle; an unknown kind takes the strictest declared default.
function strictKind(mod, kind) {
  const defaults = mod.defaults || {};
  if (kind && Object.prototype.hasOwnProperty.call(defaults, kind)) return kind;
  // Unknown/absent kind ⇒ the strict side. "code" is the attack-surface-heavy
  // kind; prefer it when declared, else the first declared default.
  if (Object.prototype.hasOwnProperty.call(defaults, "code")) return "code";
  const keys = Object.keys(defaults);
  return keys.length ? keys[0] : null;
}

// Is a config toggle value an explicit OFF? Only a literal `false` (or an object
// `{ enabled: false }`) disables. Every other NAMED value — true, an object, a
// malformed value — resolves the module ON (fail-safe to MORE rigor); an ABSENT key
// falls to the registry's per-kind default (defaultOffForKind). A bad/typo'd toggle
// can never silently disable the rigor.
function isExplicitlyOff(toggle) {
  if (toggle === false) return true;
  if (toggle && typeof toggle === "object" && toggle.enabled === false) return true;
  return false;
}

// Is the REGISTRY's per-kind default for this config an explicit OFF? A kind default
// of literal `false` (e.g. a docs project gets no UI module) defaults the module off
// for that kind — the ONE default-OFF path, and it is registry-authored (our data,
// never the untrusted config). Resolved through strictKind, so an unknown/absent kind
// takes the strict-side (code-preferred) default, never another kind's `false`.
function defaultOffForKind(mod, config) {
  const sk = strictKind(mod, config && config.kind);
  return sk !== null && (mod.defaults || {})[sk] === false;
}

// Compute the set of ENABLED module ids for this config, against the registry.
// Returns an array of { id, mod } in REGISTRY ORDER (stable, declared — not config
// order), so assembly is deterministic and reviewable.
export function enabledModules(registry, config) {
  const toggles = (config && config.modules) || {};
  const out = [];
  for (const mod of registry.modules || []) {
    const toggle = toggles[mod.id]; // undefined when the project never names it
    if (isExplicitlyOff(toggle)) continue; // the ONLY config-side way off; every other named value ⇒ on
    if (toggle === undefined && defaultOffForKind(mod, config)) continue; // registry kind-default OFF, config silent
    out.push({ id: mod.id, mod });
  }
  return out;
}

// Resolve the effective toggle VALUE a module runs with (its depth / nonSecurity /
// …), merging the per-kind default with any config override. Fail-safe: unknown
// kind ⇒ strict-side defaults. Exposed for setup and for tests.
export function effectiveToggle(mod, config) {
  const kind = config && config.kind;
  const sk = strictKind(mod, kind);
  const base = (mod.defaults && sk && mod.defaults[sk]) || {};
  const override = ((config && config.modules) || {})[mod.id];
  if (override && typeof override === "object") return { ...base, ...override };
  return { ...base };
}

// ── platform filtering ───────────────────────────────────────────────────────
// A single-platform project re-injects the role bodies every turn; content that is
// operating guidance for the OTHER adapter is pure noise there. The assembler strips
// it at assembly: a block delimited by own-line markers
//   <!-- platform:opencode -->  …OpenCode-only guidance…  <!-- /platform:opencode -->
// (and `platform:claude`) is DROPPED when assembling for a different recognised
// adapter, KEPT (markers stripped) for its own, and untagged/neutral content always
// survives. One home for the syntax + rule; each shim passes its own platform literal.
//
// FAIL-SAFE (never a silent drop — the strict side is KEEP):
//   • an unknown/missing assembling platform ⇒ NO filtering at all (text returned
//     as-is) — we drop only on a CONFIDENT other-platform match;
//   • a block whose tag is not a recognised adapter (a typo) ⇒ never dropped, kept
//     everywhere. Both directions fail toward keeping content.
export const PLATFORMS = new Set(["claude", "opencode"]);

// Match a tagged block: own-line opening marker, lazy inner, matching own-line closing
// marker (the \1 backreference pins open==close, so an unbalanced or foreign comment is
// left untouched). Leading horizontal whitespace (indentation) on a marker line is
// consumed; the inner content keeps its own newlines.
const PLATFORM_BLOCK_RE =
  /[^\S\n]*<!--\s*platform:([a-z0-9-]+)\s*-->[^\S\n]*\n([\s\S]*?)[^\S\n]*<!--\s*\/platform:\1\s*-->[^\S\n]*(?:\n|$)/g;

export function filterPlatform(text, platform) {
  // Unknown/missing assembling platform ⇒ no filtering (fail-safe keep-all, markers intact).
  if (!PLATFORMS.has(platform)) return text;
  let changed = false;
  const out = text.replace(PLATFORM_BLOCK_RE, (_full, tag, inner) => {
    changed = true;
    // Drop ONLY on a confident other-platform match (both sides recognised, differ).
    if (PLATFORMS.has(tag) && tag !== platform) return "";
    // Own platform (or an unrecognised tag): keep content, strip the marker lines.
    return inner;
  });
  // Collapse a blank-line run a dropped block may have left between two kept lines.
  return changed ? out.replace(/\n{3,}/g, "\n\n") : out;
}

// ── compose ─────────────────────────────────────────────────────────────────
export const MARKER = "<!-- ai-dev:modules -->";
export const INVARIANTS_MARKER = "<!-- ai-dev:invariants -->";

// Extract the ## Invariants section from PROTOCOL.md — the single source of truth
// for the invariants that bind every spawned role. Returns the heading + body up to
// (not including) the next ## heading, with trailing blank lines and the ---
// section-separator trimmed, so the composed block is self-contained in each agent.
export function extractInvariantsSection(root) {
  const protocol = fs.readFileSync(path.join(path.resolve(root), "PROTOCOL.md"), "utf8");
  const lines = protocol.split("\n");
  let start = -1, end = lines.length;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i] === "## Invariants") { start = i; continue; }
    if (start >= 0 && /^## /.test(lines[i])) { end = i; break; }
  }
  if (start === -1) throw new Error("PROTOCOL.md: ## Invariants section not found");
  // Trim trailing blank lines then the --- section-separator then trailing blank lines.
  let trimEnd = end;
  while (trimEnd > start && !lines[trimEnd - 1].trim()) trimEnd--;
  if (trimEnd > start && lines[trimEnd - 1] === "---") trimEnd--;
  while (trimEnd > start && !lines[trimEnd - 1].trim()) trimEnd--;
  return lines.slice(start, trimEnd).join("\n");
}

// Apply the resolved DEPTH to a fragment's tagged checklist. A fragment marks each
// item with a leading code-span tag — `[light]` (the core subset every depth keeps)
// or `[rich]` (kept only at full depth). At light the rich lines are STRIPPED, so a
// light project gets
// genuinely less text — not the same prose with a label. The depth tags are then
// removed from the kept lines (they are an authoring signal, not reader prose), and a
// one-line banner names the resolved depth so the role knows which set it is reading.
// Fail-safe: anything other than the literal "light" ⇒ rich (the stricter side),
// mirroring the module's fail-safe-to-ON — a malformed depth can never silently thin
// the checklist.
function applyDepth(text, depth) {
  const light = depth === "light";
  const kept = [];
  for (const line of text.split("\n")) {
    const m = line.match(/^(\s*[-*]\s*)`\[(light|rich)\]`\s*(.*)$/);
    if (!m) { kept.push(line); continue; }
    if (light && m[2] === "rich") continue; // rich-only item dropped at light depth
    kept.push(`${m[1]}${m[3]}`); // strip the authoring tag from a kept item
  }
  const banner = `> Depth: **${light ? "light" : "rich"}** — ${light ? "the core subset" : "the full enumeration"}.`;
  return kept.join("\n").replace(/\n\n+/g, "\n\n") + "\n\n" + banner;
}

// Read the fragment text a module contributes to a role, or null when the module
// does not target that role, with the module's resolved DEPTH applied. MISSING
// fragment for a targeted+enabled module is a HARD ERROR (never a silent drop).
// Path safety is enforced by resolveFragmentPath.
function fragmentFor(root, mod, role, config) {
  const pointer = mod.fragments && mod.fragments[role];
  if (!pointer) return null; // this module does not target this role
  const fpath = resolveFragmentPath(root, pointer);
  if (!fs.existsSync(fpath)) {
    throw new Error(
      `enabled module "${mod.id}" names a fragment for role "${role}" that is missing on disk: ${pointer}`
    );
  }
  const raw = fs.readFileSync(fpath, "utf8").trim();
  // A fragment carries depth tags only when its module declares a `depth` toggle; a
  // tag-free fragment is returned as-is (applyDepth is a no-op on untagged lines).
  return applyDepth(raw, effectiveToggle(mod, config).depth);
}

// Compose a role's FLOOR body with ALL modules disabled — the floor-only variant used
// for fixup-grade baked agents (e.g. dev-reviewer-fixup). The §Invariants block is
// composed in verbatim (identical to the full body); the <!-- ai-dev:modules --> marker
// resolves to empty (no module fragment). A floor body without a modules marker is
// unchanged. The `allOff` config is built from the registry's own module ID list (our
// data, trusted), never from untrusted input.
export function composeFloorOnly(root, floorBody, role, registry, platform) {
  const allOff = {
    modules: Object.fromEntries((registry.modules || []).map((m) => [m.id, false])),
  };
  return composeBody(root, floorBody, role, registry, allOff, platform);
}

// Compose a role's FLOOR body with the enabled modules' fragments for that role,
// then strip any block tagged for a DIFFERENT platform than `platform` (filterPlatform).
// Two markers are replaced in order:
//   1. INVARIANTS_MARKER — the PROTOCOL.md §Invariants block (verbatim, same for every role).
//   2. MARKER             — the enabled capability-module fragments (registry order).
// A floor body WITHOUT a marker simply omits it — a role a module does not target
// omits MARKER; a role that does not compose invariants omits INVARIANTS_MARKER.
// `platform` is the ASSEMBLING adapter's name; missing/unknown ⇒ no platform filtering
// (fail-safe keep-all — filterPlatform).
export function composeBody(root, floorBody, role, registry, config, platform) {
  // 1. Replace the invariants marker with the PROTOCOL.md §Invariants block.
  //    The extraction reads PROTOCOL.md once per call, lazily — most bodies carry no marker.
  let body = floorBody;
  if (body.includes(INVARIANTS_MARKER)) {
    const invariants = extractInvariantsSection(root);
    const invRe = new RegExp(INVARIANTS_MARKER.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\n?");
    body = body.replace(invRe, invariants + "\n\n");
  }
  // 2. Replace the modules marker with the enabled capability-module fragments.
  //    Replace the marker AND its trailing newline. A non-empty block lands as its own
  //    section followed by a blank line; an empty block (no enabled module targets this
  //    role) collapses the marker to nothing, leaving the floor's own spacing intact.
  const fragments = [];
  for (const { mod } of enabledModules(registry, config)) {
    const text = fragmentFor(root, mod, role, config);
    if (text) fragments.push(text);
  }
  if (body.includes(MARKER)) {
    const block = fragments.join("\n\n");
    const markerRe = new RegExp(MARKER.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\n?");
    body = body.replace(markerRe, block ? block + "\n\n" : "");
  }
  // Strip inactive-platform blocks last, so it covers both floor and composed content.
  return filterPlatform(body, platform);
}
