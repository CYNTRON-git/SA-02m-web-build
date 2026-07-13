// The downstream target's filesystem setup steps — platform-agnostic: vendor the
// shared adapter, lay down the core + quality shape, deploy the readable procedure
// bodies, ensure the config + gitignore, and install the local pre-push quality gate.
// The platform wiring (install-claude / install-opencode) runs AFTER these.

import fs from "node:fs";
import path from "node:path";
import { SOURCE, copyTree, copyFile, copyIfAbsent, ensureLine } from "./install-fs.mjs";

// ── steps ──────────────────────────────────────────────────────────────────

// 1. Vendor the shared adapter AND the neutral bodies the assembler reads, into the
// target's tooling location. The vendored install-*.mjs self-locate their ROOT as
// .ai-dev/tooling/ (three dirs up from the script), and read src/agents, src/modules,
// src/adapter from there — so all three must sit under .ai-dev/tooling/src/.
// Also vendored: everything the vendored installer ITSELF reads on the upgrade
// re-run (PROTOCOL.md, src/quality/, src/templates/ — layDownCore's sources) plus
// its own VERSION (resolveSourceVersion's vendored branch + the audit's skew check)
// — without these the documented upgrade command ENOENTs.
export function vendorTooling(target, version) {
  const toolingRoot = path.join(target, ".ai-dev", "tooling");
  const tooling = path.join(toolingRoot, "src");
  copyTree(path.join(SOURCE, "src", "adapter"), path.join(tooling, "adapter"));
  copyTree(path.join(SOURCE, "src", "agents"), path.join(tooling, "agents"));
  copyTree(path.join(SOURCE, "src", "modules"), path.join(tooling, "modules"));
  copyTree(path.join(SOURCE, "src", "quality"), path.join(tooling, "quality"));
  copyTree(path.join(SOURCE, "src", "templates"), path.join(tooling, "templates"));
  copyFile(path.join(SOURCE, "PROTOCOL.md"), path.join(toolingRoot, "PROTOCOL.md"));
  copyFile(path.join(SOURCE, "protocol-reference.md"), path.join(toolingRoot, "protocol-reference.md"));
  fs.writeFileSync(path.join(toolingRoot, "VERSION"), version + "\n");
}

// 2. Lay down the core the downstream loads + the quality-layer shape.
// agents and modules are already vendored under .ai-dev/tooling/ (step 1);
// the downstream does NOT get a bare src/agents/ or src/modules/ at its root.
// Doc templates are NOT copied to the project root — they are accessible via
// .ai-dev/tooling/src/templates/ when product discovery / doc bootstrap run.
export function layDownCore(target) {
  // The constitution lives inside .ai-dev/ — not at the project root.
  copyFile(path.join(SOURCE, "PROTOCOL.md"), path.join(target, ".ai-dev", "PROTOCOL.md"));
  // The reference companion mirrors the constitution's deploy — sibling of PROTOCOL.md.
  copyFile(path.join(SOURCE, "protocol-reference.md"), path.join(target, ".ai-dev", "protocol-reference.md"));

  // The quality layer: ship the SHAPE (the registry format + the runner),
  // NOT this repo's own tool rows. tools.json is laid down only where absent
  // so a project's own registry is never clobbered; the runner is the shared
  // mechanism and is overwritten.
  copyFile(path.join(SOURCE, "src", "quality", "run.mjs"), path.join(target, ".ai-dev", "quality", "run.mjs"));
  copyIfAbsent(path.join(SOURCE, "src", "templates", "tools.json"), path.join(target, ".ai-dev", "quality", "tools.json"));

  // The per-version migration notes, readable by the session (the tooling dir is
  // agent-read-denied). Overwritten every run — it is always the NEW version's copy.
  copyFile(path.join(SOURCE, "src", "adapter", "upgrades.md"), path.join(target, ".ai-dev", "upgrades.md"));

  // The session-state dir: gitignored (ensureTransientsGitignore) but the
  // orchestrator's first current.md write expects the parent to exist — create it
  // here so a fresh install is write-ready. Only state/; feedback/ and worktrees/
  // are created on demand by their own use.
  fs.mkdirSync(path.join(target, ".ai-dev", "state"), { recursive: true });

  // The durable-notes home: .ai-dev/notes/ is COMMITTED (team-shared; changes via PR).
  // The orchestrator reads it on the understand beat for cross-session project knowledge.
  // Seed a README only where absent — a project's own notes are never clobbered on
  // re-install. Content is generated inline (the npm package does not ship .ai-dev/).
  const notesDir = path.join(target, ".ai-dev", "notes");
  fs.mkdirSync(notesDir, { recursive: true });
  const notesReadme = path.join(notesDir, "README.md");
  if (!fs.existsSync(notesReadme)) {
    fs.writeFileSync(notesReadme, notesReadmeBody());
  }
}

// The seed README for .ai-dev/notes/ — the taxonomy reference every session reads.
// Generated inline so it stays in the npm package without a separate .ai-dev/ entry
// in package.json `files`. The content mirrors docs/decisions/out-of-root-scratch-allow.md.
function notesReadmeBody() {
  return (
    "# .ai-dev/notes/ — durable project knowledge\n\n" +
    "**Committed, team-shared.** Each change rides a PR (invariant 4). Read by the\n" +
    "orchestrator on the understand beat alongside `docs/architecture.md`.\n\n" +
    "## What belongs here\n\n" +
    "- Known project quirks the next session should know (\"why we do X here\")\n" +
    "- Architectural context not yet formalized into `docs/architecture.md`\n" +
    "- Open decisions pending a `docs/decisions/` record\n" +
    "- Agent conventions specific to this project that have not earned a canonical home yet\n\n" +
    "## What does NOT belong here\n\n" +
    "| Content | Correct home |\n" +
    "|---|---|\n" +
    "| Formal decisions | `docs/decisions/<topic>.md` |\n" +
    "| Contracts (API surface, schema, routes) | `docs/contracts/` |\n" +
    "| Product brief | `docs/product.md` |\n" +
    "| Architecture | `docs/architecture.md` |\n" +
    "| Session state (active branch, PR, cadence) | `.ai-dev/state/current.md` (gitignored) |\n" +
    "| Temp / ephemeral context | External scratch (outside the project) |\n" +
    "| Harness `#`/host memory | Never for project knowledge — unshared |\n\n" +
    "## Taxonomy\n\n" +
    "```\n" +
    "checkpoint    (.ai-dev/state/current.md)  volatile, gitignored, superseded on each update\n" +
    "durable notes (.ai-dev/notes/)            committed, team-shared, changes via PR\n" +
    "formal decisions (docs/decisions/)        committed, titled, sourced\n" +
    "external scratch                          outside project root, ephemeral\n" +
    "```\n\n" +
    "The harness `#`/host memory (external, per-profile, unshared with the team) is\n" +
    "**never** a project knowledge home. Write `.ai-dev/notes/<topic>.md` instead.\n"
  );
}

// 2b. Deploy the on-demand procedure bodies (src/agents/procedures/) to a READABLE,
// non-tooling home: .ai-dev/procedures/. The orchestrator reads a procedure when its
// trigger fires (e.g. parallel-work) via the stable path .ai-dev/procedures/<x>.md —
// the SAME path in dogfood and downstream. vendorTooling also copies them under
// .ai-dev/tooling/src/agents/procedures/ (for the upgrade re-run), but that subtree is
// read-denied unconditionally (invariant 2), so the AGENT cannot read them there; this
// readable copy is the one the trigger resolves to. Source of truth stays
// src/agents/procedures/ (one home); this is the deployed copy, committed + drift-guarded
// (install-drift.test.mjs), like the assembled .opencode/.claude artifacts. Runs in BOTH
// modes — in dogfood SOURCE===target so it converges to the committed bytes (git clean).
export function deployProcedures(target) {
  copyTree(
    path.join(SOURCE, "src", "agents", "procedures"),
    path.join(target, ".ai-dev", "procedures"),
  );
}

// 2c. Deploy the RUNTIME-READ module files (src/modules/**) to a READABLE, non-tooling
// home: .ai-dev/modules/. PREDICATE — a src/modules file gets a readable deploy iff a
// RUNTIME role reads it on-demand: it is referenced BY PATH from a runtime instruction (a
// deployed procedure body or a composed agent fragment) and resolved when a trigger fires.
// It does NOT get a deploy if it is ASSEMBLY-ONLY — consumed by the installer at assembly
// time and baked into an artifact the runtime role reads instead: registry.json (the
// assembler reads it) and every <id>/<role>.md FRAGMENT (composed into the role agents;
// the runtime role reads the assembled agent, never the fragment file). Today the ONLY
// runtime-read file is elicitation/catalog.md — referenced from elicitation.md /
// product-discovery.md (deployed procedures) and elicitation/builder.md (composed builder
// fragment), NONE of which any assembler composes. Like deployProcedures, the source of
// truth stays src/modules/ (one home); this readable copy is what the runtime reference
// (.ai-dev/modules/<rel>) resolves to in BOTH dogfood and downstream, since the vendored
// .ai-dev/tooling/src/modules/ copy is read-denied unconditionally (invariant 2). Committed
// + drift-guarded (install-drift.test.mjs). Runs in BOTH modes — in dogfood SOURCE===target
// so it converges to the committed bytes (git clean). Adding a runtime-read file = one line
// here + a repoint; over-deploying the whole tree would duplicate every assembly-only
// fragment into a second readable home (an invariant-6 smell), so we deploy the allow-list only.
export const RUNTIME_READ_MODULE_FILES = [
  path.join("elicitation", "catalog.md"),
];

export function deployModules(target) {
  for (const rel of RUNTIME_READ_MODULE_FILES) {
    copyFile(
      path.join(SOURCE, "src", "modules", rel),
      path.join(target, ".ai-dev", "modules", rel),
    );
  }
}

// 3. Ensure a config exists so the agent assembly has its `roles` bindings. A real
// project configures via /dev-setup; pre-setup we write a minimal default carrying
// the resolved platform + the standard seat ids. Written ONLY where absent — a
// re-run never overwrites the Operator's configured file. Config lives inside
// .ai-dev/ — not at the project root.
// Returns TRUE when it actually wrote the default, FALSE when an existing config was
// kept — so the install summary reports the truth instead of an unconditional
// "wrote config" that scares a re-installing Operator into thinking their pins were
// clobbered (docs/decisions/multi-model-setup-ux.md papercut 9).
// The default carries NO roles.orchestrator.model: the orchestrator is the running
// session, its model is the launch model, never a baked pin (papercut 2).
// The standard seat bindings a fresh config carries AND the source the backfill (below)
// heals a pre-existing config from. One home for "the default roles" — a newly added
// seat (e.g. `planner` in 5.48.0) is added here and both the fresh-write and the
// upgrade-heal pick it up automatically.
const DEFAULT_ROLES = {
  orchestrator: { agent: "ai-dev" },
  planner: { agent: "dev-planner" },
  builder: { agent: "dev-builder" },
  reviewer: { agent: "dev-reviewer", model: "auto" },
};

export function ensureConfig(target, platform) {
  const dest = path.join(target, ".ai-dev", "config.json");
  if (fs.existsSync(dest)) {
    // BACKWARD-COMPAT: a config that predates a newly-added seat (its `roles` lacks,
    // e.g., `planner`) would make the bake throw `roles.<seat>.agent is missing`. Heal
    // it — additively fill any WHOLLY-MISSING default seat, never touching an existing
    // seat's agent/model or any other user value. Return FALSE regardless: the config
    // EXISTED before this run (papercut 9 — the return means "existed", healed or not;
    // the install summary's wrote-vs-kept pre-check is independent of this).
    backfillMissingSeats(dest);
    return false;
  }
  const config = {
    mode: "interactive",
    profile: "solo",
    platform,
    kind: "code",
    roles: { ...DEFAULT_ROLES },
  };
  fs.writeFileSync(dest, JSON.stringify(config, null, 2) + "\n");
  return true;
}

// Additively fill any role seat present in DEFAULT_ROLES but WHOLLY MISSING from the
// config's `roles`, writing the file back only if something was added. General, not
// seat-hardcoded: every future default seat a config lacks auto-heals on the next
// install. Guarantees:
//   • never overwrites an existing seat (a present `roles.planner` — even a customised
//     one — is left byte-untouched), nor any other config value;
//   • a seat present but agent-less is NOT a "missing seat" — left as-is (a malformed
//     user binding the bake rightly rejects, not ours to guess);
//   • a malformed (unparseable) config is left untouched — the Operator's to fix, never
//     clobbered here.
function backfillMissingSeats(dest) {
  let config;
  try {
    config = JSON.parse(fs.readFileSync(dest, "utf8"));
  } catch {
    return; // malformed config — never clobber; the bake will surface it honestly
  }
  const roles = config.roles && typeof config.roles === "object" ? config.roles : {};
  let added = false;
  for (const [seat, binding] of Object.entries(DEFAULT_ROLES)) {
    if (!roles[seat]) {
      roles[seat] = { ...binding };
      added = true;
    }
  }
  if (!added) return; // nothing missing — leave the file byte-untouched
  config.roles = roles;
  fs.writeFileSync(dest, JSON.stringify(config, null, 2) + "\n");
}

// 4. Ensure the local-only transient dirs are gitignored: state (the session
// pointer), feedback (the self-report holds RAW pre-leak-sweep context — a
// crash before its delete, or a blind `git add -A`, must not commit it), and
// worktrees (parallel-feature checkouts live inside the root so the
// project-boundary deny still covers them — but they are never repo content).
// Idempotent: appends only the lines not already present.
export function ensureTransientsGitignore(target) {
  ensureLine(path.join(target, ".gitignore"), ".ai-dev/state/");
  ensureLine(path.join(target, ".gitignore"), ".ai-dev/feedback/");
  ensureLine(path.join(target, ".gitignore"), ".ai-dev/worktrees/");
  // config.local.json is the gitignored personal home for the per-machine launch.*
  // values (a configDir, a personal launch model) — never shared, never forced on a
  // teammate (docs/decisions/launcher-ux.md). Like the state pointer, it lives in the
  // tree but is git-excluded.
  ensureLine(path.join(target, ".gitignore"), ".ai-dev/config.local.json");
  // model-routes.local.json is the gitignored personal home for the per-machine
  // `proxyUrl` and personal route entries — never shared, never landed in the committed
  // routes table (docs/decisions/personal-multi-model-setup.md). Same split pattern as
  // config.local.json.
  ensureLine(path.join(target, ".gitignore"), ".ai-dev/model-routes.local.json");
}

// 5. Generate the OPTIONAL project-local launcher .ai-dev/launch — a drop-in for
// `claude` run from the project root, framed optional but generated ALWAYS (so enabling
// routing or a configDir later needs no re-install; docs/decisions/launcher-ux.md).
//
//   Claude   → wraps the router-launch engine: `exec node <engine> "$@"`. The engine
//              decides direct-vs-router and layers the launch env (configDir included),
//              so a plain single-model project just launches claude through it.
//   OpenCode → routing is not realisable there (tool-map.json opencode._note), so the
//              launch is an HONEST stub: it prints the Claude-only note and execs
//              `opencode "$@"` (no proxy).
//
// Layout-aware engine path: dogfood wires to src/adapter/, downstream to the vendored
// .ai-dev/tooling/src/adapter/ — the SAME split every wiring step uses. The committed
// dogfood form is the single home the drift guard locks (install-drift.test.mjs).
//
// Security (semgrep / shell-exec surface): the script is a fixed template — the only
// interpolated value is the engine path, a literal the installer controls (never user
// input). It uses `exec ... "$@"` (argv passthrough, no `sh -c "$untrusted"`), so no
// argument is ever re-parsed by a shell. The file is written 0755.
function launchScriptBody(platform, dogfood) {
  if (platform === "opencode") {
    return (
      "#!/bin/sh\n" +
      "# ai-dev:launch — OPTIONAL project launcher. Generated by the installer.\n" +
      "# Multi-model routing is Claude-Code only (the harness must forward distinct\n" +
      "# per-subagent model ids; OpenCode's task runtime does not — tool-map.json\n" +
      "# opencode._note). On OpenCode this launcher is an honest stub: it says so and\n" +
      "# runs opencode normally, no proxy.\n" +
      'echo "[ai-dev] multi-model routing is Claude-Code only — launching opencode normally (no proxy)." >&2\n' +
      'exec opencode "$@"\n'
    );
  }
  const engine = dogfood
    ? "src/adapter/router-launch.mjs"
    : ".ai-dev/tooling/src/adapter/router-launch.mjs";
  return (
    "#!/bin/sh\n" +
    "# ai-dev:launch — OPTIONAL project launcher (a drop-in for `claude`, run from the\n" +
    "# project root). Generated by the installer. You only NEED it for multi-model +\n" +
    "# proxy, or a per-project claude profile (launch.configDir) — a plain single-model\n" +
    "# project can keep launching `claude` (or your own wrapper) directly. The engine\n" +
    "# decides direct-vs-router and layers the launch env; see README `## Multi-model\n" +
    "# routing` and src/adapter/README.md `### The launcher`.\n" +
    "#   ./.ai-dev/launch [claude args…]   normal launch (direct, or through the proxy)\n" +
    "#   ./.ai-dev/launch --proxy          foreground proxy only — prints the URL, no claude\n" +
    `exec node "${engine}" "$@"\n`
  );
}

// Write .ai-dev/launch (always). Overwritten every run — it is a generated artifact
// (the layout-correct engine path), drift-guarded like the assembled agents. 0755 so it
// is directly runnable.
export function generateLaunchScript(target, platform, dogfood) {
  const dest = path.join(target, ".ai-dev", "launch");
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, launchScriptBody(platform, dogfood), { mode: 0o755 });
  fs.chmodSync(dest, 0o755); // ensure +x even if the file pre-existed
}

// 6. Lay down the LOCAL pre-push quality gate (F3): a git pre-push hook that runs
// the registry runner WHOLESALE (`node .ai-dev/quality/run.mjs build`) and blocks a
// push of build-beat-failing code before it leaves the machine — the local
// complement to the once-per-project remote CI offer (orchestrator `## Your seat`).
// Single-home discipline: it invokes the runner, never a re-listed tool subset (same
// rule as the CI wiring). Default-ON downstream; never written in dogfood mode (the
// source repo carries its own committed hooks). Returns one of:
//   "written"     — a fresh hook was written (or our own prior hook refreshed)
//   "foreign"     — a non-ai-dev pre-push hook exists; left UNTOUCHED, warning surfaced
//   "no-repo"     — no <target>/.git ⇒ skipped silently (the install already warns)
// The marker line lets a re-run recognise its own hook (overwrite ⇒ idempotent
// refresh) while NEVER clobbering a project's own pre-push hook (fail-safe).
const PREPUSH_MARKER = "# ai-dev:pre-push";
const PREPUSH_BODY =
  "#!/bin/sh\n" +
  PREPUSH_MARKER + "\n" +
  "# Local quality gate — blocks a push of build-beat-failing code (the registry\n" +
  "# runner, WHOLESALE — never a re-listed tool subset). Bypass with --no-verify.\n" +
  "# Remove this file to opt out. The merge-gate + remote CI are the other two layers.\n" +
  "node .ai-dev/quality/run.mjs build\n";

export function installPrePushHook(target) {
  const gitDir = path.join(target, ".git");
  if (!fs.existsSync(gitDir)) return "no-repo"; // no repo ⇒ skip (install already warns)
  const hooksDir = path.join(gitDir, "hooks");
  const hookPath = path.join(hooksDir, "pre-push");
  if (fs.existsSync(hookPath)) {
    const existing = fs.readFileSync(hookPath, "utf8");
    if (!existing.includes(PREPUSH_MARKER)) {
      // A project's OWN pre-push hook — never clobber it (fail-safe). Warn + proceed.
      console.log(
        `\n⚠ A pre-push hook already exists at ${hookPath} and is not ai-dev's — left UNTOUCHED.`,
      );
      console.log("  To get the local quality gate, add this line to it:  node .ai-dev/quality/run.mjs build");
      return "foreign";
    }
    // Our own prior hook ⇒ overwrite (idempotent refresh, converges to identical bytes).
  }
  fs.mkdirSync(hooksDir, { recursive: true });
  fs.writeFileSync(hookPath, PREPUSH_BODY, { mode: 0o755 });
  fs.chmodSync(hookPath, 0o755); // ensure executable even if the file pre-existed
  return "written";
}
