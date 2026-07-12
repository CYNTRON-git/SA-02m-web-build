// Capability-module assembly test — proves the SHARED assembler (src/adapter/modules.mjs)
// composes a module's fragment into a role agent, omits it when off, never drops the
// floor, fails SAFE to ON, and enforces its two security guards (missing-fragment
// hard error, root-escape rejection). The mechanism is BINARY — a module toggles and
// the assembled agent provably gains/loses its section — so this is the Slice-1 DoD.
// Both install shims (Claude + OpenCode) share the one compose helper, so the
// resolver is tested directly AND through one shim end-to-end.
//
// Run: node src/adapter/install-modules.test.mjs

import {
  loadRegistry, enabledModules, effectiveToggle,
  resolveFragmentPath, composeBody, filterPlatform, MARKER,
} from "./modules.mjs";
import { install as claudeInstall } from "./claude/install-agents.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const registry = loadRegistry(ROOT);

let pass = 0, fail = 0;
function check(name, cond) {
  if (cond) { pass++; }
  else { fail++; console.log(`  ✗ ${name}`); }
}

// The fragment text's distinguishing marker (present iff the module composed in).
const FRAGMENT_MARK = "The **threat-model** module is on";
// A floor-body distinguishing line — must be present under EVERY config.
const REVIEWER_FLOOR = "a security-relevant change names its threats";
// A `[rich]`-only checklist item — present at rich depth, STRIPPED at light depth.
const RICH_ONLY = "Supply chain";
// A `[light]`-core item — present at EVERY depth.
const LIGHT_CORE = "Attack surface";

// ── 1. enabledModules — the resolver's on/off decision ────────────────────────
console.log("RESOLVER — enabled/disabled + fail-safe-to-ON:");
check("on: an object toggle ⇒ enabled",
  enabledModules(registry, { modules: { "threat-model": { depth: "rich" } } }).some((m) => m.id === "threat-model"));
check("on: true ⇒ enabled",
  enabledModules(registry, { modules: { "threat-model": true } }).some((m) => m.id === "threat-model"));
check("on: absent modules key ⇒ enabled (per-kind default)",
  enabledModules(registry, { kind: "code" }).some((m) => m.id === "threat-model"));
check("off: literal false ⇒ disabled",
  !enabledModules(registry, { modules: { "threat-model": false } }).some((m) => m.id === "threat-model"));
check("off: { enabled: false } ⇒ disabled",
  !enabledModules(registry, { modules: { "threat-model": { enabled: false } } }).some((m) => m.id === "threat-model"));
// FAIL-SAFE: a malformed/unknown toggle value (string, number, null) ⇒ ON (strict side).
for (const bad of ["garbage", 42, null]) {
  check(`fail-safe-to-ON: malformed toggle ${JSON.stringify(bad)} ⇒ enabled`,
    enabledModules(registry, { modules: { "threat-model": bad } }).some((m) => m.id === "threat-model"));
}

// ── 2. effectiveToggle — per-kind defaults + strict fallback ──────────────────
console.log("RESOLVER — per-kind defaults:");
const tm = registry.modules.find((m) => m.id === "threat-model");
check("default: code ⇒ rich", effectiveToggle(tm, { kind: "code" }).depth === "rich");
check("default: docs ⇒ light", effectiveToggle(tm, { kind: "docs" }).depth === "light");
check("default: unknown kind ⇒ strict side (code/rich)", effectiveToggle(tm, { kind: "bogus" }).depth === "rich");
check("default: absent kind ⇒ strict side (code/rich)", effectiveToggle(tm, {}).depth === "rich");
check("override: config value overrides the kind default",
  effectiveToggle(tm, { kind: "code", modules: { "threat-model": { depth: "light" } } }).depth === "light");

// ── 3. composeBody — compose / omit / floor-always / no-marker ────────────────
console.log("COMPOSE — fragment in/out, floor always present:");
const reviewerFloor = fs.readFileSync(path.join(ROOT, "src", "agents", "reviewer.md"), "utf8");
const orchFloor = fs.readFileSync(path.join(ROOT, "src", "agents", "orchestrator.md"), "utf8");

const composedOn = composeBody(ROOT, reviewerFloor, "reviewer", registry, { modules: { "threat-model": { depth: "rich" } } });
check("compose: enabled ⇒ fragment text present", composedOn.includes(FRAGMENT_MARK));
check("compose: enabled ⇒ marker consumed (not left in output)", !composedOn.includes(MARKER));
check("compose: enabled ⇒ floor still present", composedOn.includes(REVIEWER_FLOOR));

const composedOff = composeBody(ROOT, reviewerFloor, "reviewer", registry, { modules: { "threat-model": false } });
check("omit: disabled ⇒ fragment text absent", !composedOff.includes(FRAGMENT_MARK));
check("omit: disabled ⇒ marker consumed (no leftover comment)", !composedOff.includes(MARKER));
check("omit: disabled ⇒ floor STILL present", composedOff.includes(REVIEWER_FLOOR));

// Floor always present under every config shape, including malformed.
for (const cfg of [{}, { modules: {} }, { modules: { "threat-model": "garbage" } }, { kind: "docs" }]) {
  check(`floor-always: ${JSON.stringify(cfg)} ⇒ floor present`,
    composeBody(ROOT, reviewerFloor, "reviewer", registry, cfg).includes(REVIEWER_FLOOR));
}

// ── 3b. DEPTH — rich composes the full enumeration, light the core subset only ──
console.log("DEPTH — rich vs light (light gets genuinely less):");
function reviewerComposed(modulesCfg) {
  return composeBody(ROOT, reviewerFloor, "reviewer", registry, modulesCfg);
}
const richBody = reviewerComposed({ kind: "code", modules: { "threat-model": { depth: "rich" } } });
check("rich: light-core item present", richBody.includes(LIGHT_CORE));
check("rich: rich-only item present", richBody.includes(RICH_ONLY));
check("rich: banner names rich depth", richBody.includes("Depth: **rich**"));
check("rich: no authoring tag leaks into composed prose", !/\[(light|rich)\]/.test(richBody));

const lightBody = reviewerComposed({ kind: "code", modules: { "threat-model": { depth: "light" } } });
check("light: light-core item present", lightBody.includes(LIGHT_CORE));
check("light: rich-only item STRIPPED", !lightBody.includes(RICH_ONLY));
check("light: banner names light depth", lightBody.includes("Depth: **light**"));
check("light: genuinely shorter than rich", lightBody.length < richBody.length);

// Per-kind default drives depth with no explicit override: docs ⇒ light.
const docDefault = reviewerComposed({ kind: "docs" });
check("depth-default: docs kind ⇒ light (rich-only stripped)", !docDefault.includes(RICH_ONLY));
const swDefault = reviewerComposed({ kind: "code" });
check("depth-default: code kind ⇒ rich (rich-only kept)", swDefault.includes(RICH_ONLY));

// FAIL-SAFE: a malformed/unknown depth ⇒ rich (the stricter side) — never silently thin.
for (const bad of ["garbage", "LIGHT", "", 42, null]) {
  const body = reviewerComposed({ kind: "code", modules: { "threat-model": { depth: bad } } });
  check(`depth fail-safe: depth ${JSON.stringify(bad)} ⇒ rich (rich-only kept)`, body.includes(RICH_ONLY));
}

// A floor body with NO marker (the orchestrator — no module targets it) is returned
// unchanged and does NOT error.
const orchOut = composeBody(ROOT, orchFloor, "orchestrator", registry, { modules: { "threat-model": true } });
check("no-marker: orchestrator body returned unchanged", orchOut === orchFloor);
check("no-marker: orchestrator gains no fragment", !orchOut.includes(FRAGMENT_MARK));

// ── 4. SECURITY: missing-fragment is a HARD ERROR (never a silent drop) ───────
console.log("SECURITY — missing fragment, root escape:");
{
  // A synthetic registry whose enabled module points at a fragment that does not exist.
  const badRegistry = { modules: [{ id: "ghost", fragments: { reviewer: "src/modules/ghost/reviewer.md" } }] };
  let threw = false;
  try { composeBody(ROOT, reviewerFloor, "reviewer", badRegistry, { modules: { ghost: true } }); }
  catch { threw = true; }
  check("missing-fragment: enabled module with absent fragment ⇒ throws", threw);
}

// ── 5. SECURITY: a root-escaping fragment pointer is REJECTED ─────────────────
check("root-escape: absolute pointer ⇒ throws", (() => {
  try { resolveFragmentPath(ROOT, "/etc/passwd"); return false; } catch { return true; }
})());
check("root-escape: `..`-bearing pointer ⇒ throws", (() => {
  try { resolveFragmentPath(ROOT, "../outside.md"); return false; } catch { return true; }
})());
check("root-escape: empty pointer ⇒ throws", (() => {
  try { resolveFragmentPath(ROOT, ""); return false; } catch { return true; }
})());
check("in-root pointer resolves", resolveFragmentPath(ROOT, "src/modules/threat-model/reviewer.md").startsWith(ROOT));
// A root-escaping pointer carried by an ENABLED module also fails at compose time.
{
  const escRegistry = { modules: [{ id: "esc", fragments: { reviewer: "../escape.md" } }] };
  let threw = false;
  try { composeBody(ROOT, reviewerFloor, "reviewer", escRegistry, { modules: { esc: true } }); }
  catch { threw = true; }
  check("root-escape: enabled module with escaping pointer ⇒ throws at compose", threw);
}

// ── 6. END-TO-END through a real shim (Claude) — the deployed agent gains/loses
//      the section, floor intact. Proves the shared helper is wired into install().
console.log("END-TO-END — Claude install composes the module:");
// orchestrator included: claudeInstall now also assembles the orchestrator load surface
// (.claude/ai-dev.md), so the fixture config must name every seat install() writes.
const baseRoles = { orchestrator: { agent: "ai-dev" }, planner: { agent: "dev-planner" }, builder: { agent: "dev-builder" }, reviewer: { agent: "dev-reviewer" } };
function reviewerAgentText(modulesCfg) {
  // nest the agents dir under a temp BASE: claudeInstall writes the orchestrator surface
  // to outDir's PARENT (.claude/ai-dev.md), so the parent must be inside a cleanable dir,
  // not the bare tmpdir (else ai-dev.md would leak into os.tmpdir()).
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-modtest-"));
  const outDir = path.join(base, "agents");
  const written = claudeInstall(outDir, { roles: baseRoles, kind: "code", ...modulesCfg });
  const text = fs.readFileSync(written["dev-reviewer"], "utf8");
  fs.rmSync(base, { recursive: true, force: true });
  return text;
}
const e2eOn = reviewerAgentText({ modules: { "threat-model": { depth: "rich" } } });
check("e2e: ON ⇒ assembled agent carries the fragment", e2eOn.includes(FRAGMENT_MARK));
check("e2e: ON ⇒ assembled agent carries the floor", e2eOn.includes(REVIEWER_FLOOR));
check("e2e: ON ⇒ no leftover marker in deployed agent", !e2eOn.includes(MARKER));
const e2eOff = reviewerAgentText({ modules: { "threat-model": false } });
check("e2e: OFF ⇒ assembled agent omits the fragment", !e2eOff.includes(FRAGMENT_MARK));
check("e2e: OFF ⇒ assembled agent keeps the floor", e2eOff.includes(REVIEWER_FLOOR));

// ── 7. SECOND MODULE: product-advocate — same binary behaviour, no threat-model
//      assertion weakened (added, never edited). Targets builder (plan) + reviewer
//      (review), declared AFTER threat-model so registry order = assembly order.
const plannerFloor = fs.readFileSync(path.join(ROOT, "src", "agents", "planner.md"), "utf8");
// The product-advocate builder fragment's distinguishing line, and a `[rich]`-only item.
const PA_FRAGMENT_MARK = "The **product-advocate** module is on";
const PA_RICH_ONLY = "The cheapest test that would tell us";
const PA_LIGHT_CORE = "Who is this for";
// The Builder plan-time product FLOOR line the module deepens — present under EVERY config.
const PLANNER_PRODUCT_FLOOR = "Product questions";

console.log("PRODUCT-ADVOCATE — resolver + per-kind defaults:");
const pa = registry.modules.find((m) => m.id === "product-advocate");
check("pa exists in registry after threat-model",
  registry.modules.findIndex((m) => m.id === "product-advocate") >
  registry.modules.findIndex((m) => m.id === "threat-model"));
check("pa on: object toggle ⇒ enabled",
  enabledModules(registry, { modules: { "product-advocate": { depth: "rich" } } }).some((m) => m.id === "product-advocate"));
check("pa on: true ⇒ enabled",
  enabledModules(registry, { modules: { "product-advocate": true } }).some((m) => m.id === "product-advocate"));
check("pa on: absent key ⇒ enabled (per-kind default)",
  enabledModules(registry, { kind: "code" }).some((m) => m.id === "product-advocate"));
check("pa off: literal false ⇒ disabled",
  !enabledModules(registry, { modules: { "product-advocate": false } }).some((m) => m.id === "product-advocate"));
check("pa off: { enabled: false } ⇒ disabled",
  !enabledModules(registry, { modules: { "product-advocate": { enabled: false } } }).some((m) => m.id === "product-advocate"));
for (const bad of ["garbage", 42, null]) {
  check(`pa fail-safe-to-ON: malformed toggle ${JSON.stringify(bad)} ⇒ enabled`,
    enabledModules(registry, { modules: { "product-advocate": bad } }).some((m) => m.id === "product-advocate"));
}
check("pa default: code ⇒ rich", effectiveToggle(pa, { kind: "code" }).depth === "rich");
check("pa default: docs ⇒ light", effectiveToggle(pa, { kind: "docs" }).depth === "light");
check("pa default: unknown kind ⇒ strict side (code/rich)", effectiveToggle(pa, { kind: "bogus" }).depth === "rich");
check("pa default: absent kind ⇒ strict side (code/rich)", effectiveToggle(pa, {}).depth === "rich");
check("pa override: config value overrides the kind default",
  effectiveToggle(pa, { kind: "code", modules: { "product-advocate": { depth: "light" } } }).depth === "light");

console.log("PRODUCT-ADVOCATE — compose into planner, floor always present:");
function plannerComposed(cfg) {
  return composeBody(ROOT, plannerFloor, "planner", registry, cfg);
}
const paOn = plannerComposed({ kind: "code", modules: { "product-advocate": { depth: "rich" }, "threat-model": false } });
check("pa compose: enabled ⇒ fragment text present", paOn.includes(PA_FRAGMENT_MARK));
check("pa compose: enabled ⇒ marker consumed", !paOn.includes(MARKER));
check("pa compose: enabled ⇒ Builder product FLOOR still present", paOn.includes(PLANNER_PRODUCT_FLOOR));
const paOff = plannerComposed({ modules: { "product-advocate": false, "threat-model": false } });
check("pa omit: disabled ⇒ fragment absent", !paOff.includes(PA_FRAGMENT_MARK));
check("pa omit: disabled ⇒ marker consumed", !paOff.includes(MARKER));
check("pa omit: disabled ⇒ Builder product FLOOR STILL present", paOff.includes(PLANNER_PRODUCT_FLOOR));

console.log("PRODUCT-ADVOCATE — depth (light gets genuinely less):");
const paRich = plannerComposed({ kind: "code", modules: { "product-advocate": { depth: "rich" }, "threat-model": false } });
const paLight = plannerComposed({ kind: "code", modules: { "product-advocate": { depth: "light" }, "threat-model": false } });
check("pa rich: light-core item present", paRich.includes(PA_LIGHT_CORE));
check("pa rich: rich-only item present", paRich.includes(PA_RICH_ONLY));
check("pa rich: banner names rich depth", paRich.includes("Depth: **rich**"));
check("pa rich: no authoring tag leaks", !/\[(light|rich)\]/.test(paRich));
check("pa light: light-core item present", paLight.includes(PA_LIGHT_CORE));
check("pa light: rich-only item STRIPPED", !paLight.includes(PA_RICH_ONLY));
check("pa light: banner names light depth", paLight.includes("Depth: **light**"));
check("pa light: genuinely shorter than rich", paLight.length < paRich.length);
for (const bad of ["garbage", "LIGHT", "", 42, null]) {
  const body = plannerComposed({ kind: "code", modules: { "product-advocate": { depth: bad }, "threat-model": false } });
  check(`pa depth fail-safe: depth ${JSON.stringify(bad)} ⇒ rich (rich-only kept)`, body.includes(PA_RICH_ONLY));
}

// HONESTY (Slice-1 caveat): the builder fragment must NOT claim it is an INDEPENDENT
// challenge — in this slice the questions are self-applied. The fragment explicitly
// disclaims that; assert it never asserts a fresh/independent voice.
// The Slice-1 caveat: the questions are SELF-applied, so the fragment must disclaim
// independence (a sharper self-check, not a separate disinterested voice) and must
// NOT present itself as that voice. Assert the disclaimer is present.
check("pa honesty: fragment disclaims independence (self-check, not a separate voice)",
  /self-check|self-apply/i.test(paRich) && /never as a verdict from a separate/i.test(paRich));
check("pa honesty: fragment points at the recorded-answer-or-descope floor rule",
  /descoped|recorded answer/i.test(paRich));

console.log("PRODUCT-ADVOCATE — reviewer dimension composes:");
const paRevOn = composeBody(ROOT, reviewerFloor, "reviewer", registry,
  { kind: "code", modules: { "product-advocate": true, "threat-model": false } });
check("pa reviewer: enabled ⇒ fragment present", paRevOn.includes(PA_FRAGMENT_MARK));
check("pa reviewer: enabled ⇒ Reviewer product FLOOR still present", paRevOn.includes("foundational product question"));

// ── 8. CO-EXISTENCE: BOTH modules ON ⇒ both fragments compose, in REGISTRY ORDER
//      (threat-model first, product-advocate second) and both floors survive. This
//      is coverage the single-module suite never exercised — the second module
//      unlocks it, proving registry-order composition with two modules.
console.log("CO-EXISTENCE — both modules on, registry order:");
const both = plannerComposed({ kind: "code", modules: { "threat-model": { depth: "rich" }, "product-advocate": { depth: "rich" } } });
check("both: threat-model fragment present", both.includes(FRAGMENT_MARK));
check("both: product-advocate fragment present", both.includes(PA_FRAGMENT_MARK));
check("both: Builder product FLOOR present", both.includes(PLANNER_PRODUCT_FLOOR));
check("both: registry order — threat-model precedes product-advocate",
  both.indexOf(FRAGMENT_MARK) < both.indexOf(PA_FRAGMENT_MARK));
check("both: no leftover marker", !both.includes(MARKER));

// ── 9. END-TO-END through the Claude shim — the deployed BUILDER agent gains/loses
//      the product-advocate fragment, floor intact.
console.log("END-TO-END — Claude install composes product-advocate into the planner:");
function plannerAgentText(modulesCfg) {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-pa-e2e-"));
  const written = claudeInstall(outDir, { roles: baseRoles, kind: "code", ...modulesCfg });
  const text = fs.readFileSync(written["dev-planner"], "utf8");
  fs.rmSync(outDir, { recursive: true, force: true });
  return text;
}
const paE2eOn = plannerAgentText({ modules: { "product-advocate": { depth: "rich" }, "threat-model": false } });
check("pa e2e: ON ⇒ assembled planner carries the fragment", paE2eOn.includes(PA_FRAGMENT_MARK));
check("pa e2e: ON ⇒ assembled planner carries the product floor", paE2eOn.includes(PLANNER_PRODUCT_FLOOR));
check("pa e2e: ON ⇒ no leftover marker", !paE2eOn.includes(MARKER));
const paE2eOff = plannerAgentText({ modules: { "product-advocate": false, "threat-model": false } });
check("pa e2e: OFF ⇒ assembled planner omits the fragment", !paE2eOff.includes(PA_FRAGMENT_MARK));
check("pa e2e: OFF ⇒ assembled planner keeps the product floor", paE2eOff.includes(PLANNER_PRODUCT_FLOOR));

// ── 10. KIND-DEFAULT OFF: a REGISTRY-authored per-kind default of literal `false`
//      defaults the module off for that kind — the "a docs project gets no UI module"
//      behaviour. Registry-authored ONLY: the config-side fail-safe is untouched — a
//      NAMED config value (even malformed) still resolves ON, explicit false still OFF,
//      and an unknown/absent kind takes the strict side (the code-preferred default).
//      Synthetic registry over a real on-disk fragment, so these cases hold regardless
//      of the live catalog's content.
console.log("KIND-DEFAULT OFF — registry false vs config override:");
const koRegistry = { modules: [{
  id: "ko",
  toggle: { depth: "rich | light" },
  defaults: { code: { depth: "rich" }, docs: false, mixed: { depth: "light" } },
  targets: [{ role: "reviewer", beat: "review" }],
  fragments: { reviewer: "src/modules/threat-model/reviewer.md" },
}] };
check("kind-default false: docs kind + silent config ⇒ disabled",
  !enabledModules(koRegistry, { kind: "docs" }).some((m) => m.id === "ko"));
check("kind-default false: docs kind ⇒ fragment ABSENT from composed body",
  !composeBody(ROOT, reviewerFloor, "reviewer", koRegistry, { kind: "docs" }).includes(FRAGMENT_MARK));
check("kind-default false: docs kind ⇒ floor still present",
  composeBody(ROOT, reviewerFloor, "reviewer", koRegistry, { kind: "docs" }).includes(REVIEWER_FLOOR));
check("kind-default non-false: code kind + silent config ⇒ enabled",
  enabledModules(koRegistry, { kind: "code" }).some((m) => m.id === "ko"));
// Explicit config value overrides the kind default BOTH ways.
check("override beats kind-default false: explicit object ⇒ enabled",
  enabledModules(koRegistry, { kind: "docs", modules: { ko: { depth: "light" } } }).some((m) => m.id === "ko"));
check("override beats kind-default false: literal true ⇒ enabled",
  enabledModules(koRegistry, { kind: "docs", modules: { ko: true } }).some((m) => m.id === "ko"));
check("override beats kind-default false: explicit enable ⇒ fragment composes",
  composeBody(ROOT, reviewerFloor, "reviewer", koRegistry, { kind: "docs", modules: { ko: { depth: "rich" } } }).includes(FRAGMENT_MARK));
check("override beats kind-default on: literal false on code kind ⇒ disabled",
  !enabledModules(koRegistry, { kind: "code", modules: { ko: false } }).some((m) => m.id === "ko"));
// FAIL-SAFE unchanged: a NAMED-but-malformed config toggle resolves ON even where the
// kind default is false — only the registry may default off, never a broken config.
for (const bad of ["garbage", 42, null]) {
  check(`fail-safe over kind-default false: malformed toggle ${JSON.stringify(bad)} ⇒ enabled`,
    enabledModules(koRegistry, { kind: "docs", modules: { ko: bad } }).some((m) => m.id === "ko"));
}
// Unknown/absent kind ⇒ strict side: the code-preferred default (non-false here), never
// another kind's `false`.
check("unknown kind ⇒ strict side (code default, non-false) ⇒ enabled",
  enabledModules(koRegistry, { kind: "bogus" }).some((m) => m.id === "ko"));
check("absent kind ⇒ strict side (code default, non-false) ⇒ enabled",
  enabledModules(koRegistry, {}).some((m) => m.id === "ko"));

// ── 11. PLATFORM FILTER — strip the inactive adapter's blocks at assembly ──────
//      A single-platform project must not carry the OTHER adapter's operating
//      caveats. The shared assembler drops a block tagged for a DIFFERENT recognised
//      adapter, keeps the active platform's (markers stripped), and never touches
//      neutral content. Fail-safe is KEEP: unknown platform ⇒ no filtering; a typo'd
//      tag ⇒ never dropped. Driven both on the REAL tagged orchestrator floor and on
//      synthetic fixtures (so the neutral/fail-safe cases hold regardless of catalog).
console.log("PLATFORM FILTER — drop the inactive adapter, keep the active + neutral:");

// The real OpenCode-only caveat tagged in src/agents/orchestrator.md.
const OC_CAVEAT = "no cross-model reviewer is realisable AT ALL";
// A neutral line right before the tagged block — must survive on BOTH platforms.
const OC_NEUTRAL = "where no second model exists";
const orchClaude = composeBody(ROOT, orchFloor, "orchestrator", registry, {}, "claude");
const orchOpencode = composeBody(ROOT, orchFloor, "orchestrator", registry, {}, "opencode");
check("filter: assembling for claude DROPS the platform:opencode block", !orchClaude.includes(OC_CAVEAT));
check("filter: assembling for opencode KEEPS the platform:opencode block", orchOpencode.includes(OC_CAVEAT));
check("filter: a kept block has its markers stripped", !orchOpencode.includes("<!-- platform:opencode -->"));
check("filter: a dropped block leaves no marker behind", !orchClaude.includes("platform:opencode"));
check("filter: neutral line survives on claude", orchClaude.includes(OC_NEUTRAL));
check("filter: neutral line survives on opencode", orchOpencode.includes(OC_NEUTRAL));
check("filter: claude assembly stays platform-neutral around the drop (floor intact)",
  orchClaude.includes("Read `.ai-dev/config.json` `roles` for the seat"));

// Synthetic fixtures: both-platform blocks + neutral, exercised both directions and fail-safe.
const fx = [
  "Neutral lead.",
  "<!-- platform:claude -->",
  "CLAUDE-ONLY-LINE",
  "<!-- /platform:claude -->",
  "<!-- platform:opencode -->",
  "OPENCODE-ONLY-LINE",
  "<!-- /platform:opencode -->",
  "Neutral tail.",
].join("\n");
const fxClaude = filterPlatform(fx, "claude");
const fxOpencode = filterPlatform(fx, "opencode");
check("synthetic claude: keeps claude block", fxClaude.includes("CLAUDE-ONLY-LINE"));
check("synthetic claude: drops opencode block", !fxClaude.includes("OPENCODE-ONLY-LINE"));
check("synthetic opencode: keeps opencode block", fxOpencode.includes("OPENCODE-ONLY-LINE"));
check("synthetic opencode: drops claude block", !fxOpencode.includes("CLAUDE-ONLY-LINE"));
check("synthetic: neutral survives both", fxClaude.includes("Neutral lead.") && fxClaude.includes("Neutral tail.")
  && fxOpencode.includes("Neutral lead.") && fxOpencode.includes("Neutral tail."));
check("synthetic: all markers stripped from kept output",
  !fxClaude.includes("<!-- platform:") && !fxOpencode.includes("<!-- platform:"));

// FAIL-SAFE: an unknown/missing assembling platform ⇒ NO filtering (everything kept, as-is).
for (const p of [undefined, null, "", "bogus"]) {
  const keptAll = filterPlatform(fx, p);
  check(`fail-safe: platform ${JSON.stringify(p)} ⇒ both blocks kept`,
    keptAll.includes("CLAUDE-ONLY-LINE") && keptAll.includes("OPENCODE-ONLY-LINE"));
  check(`fail-safe: platform ${JSON.stringify(p)} ⇒ returned byte-identical (no-op)`, keptAll === fx);
}
// FAIL-SAFE: a typo'd tag (not a recognised adapter) is NEVER dropped, on any platform.
const typo = "<!-- platform:opencod -->\nTYPO-LINE\n<!-- /platform:opencod -->";
check("fail-safe: unrecognised tag kept on claude", filterPlatform(typo, "claude").includes("TYPO-LINE"));
check("fail-safe: unrecognised tag kept on opencode", filterPlatform(typo, "opencode").includes("TYPO-LINE"));

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
