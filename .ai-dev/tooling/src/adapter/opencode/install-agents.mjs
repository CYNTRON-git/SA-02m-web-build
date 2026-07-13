// OpenCode realisation of the adapter's load-instructions + spawn-a-sub-agent. Assembles
// each neutral role body (src/agents/<role>.md) + the OpenCode per-role frontmatter
// (src/adapter/opencode/agents/<role>.fm) into an OpenCode agent file in **.opencode/agents/**
// (PLURAL — opencode 1.17 loads `.opencode/agents/` and `.opencode/plugins/`, not the
// singular forms). Concatenation, not a generator: the neutral body is the single source
// (shared with the Claude adapter); the .fm adds only OpenCode's frontmatter (description +
// `mode` + `tools` OBJECT map, no `name` key — the filename is the agent id).
//
// UNLIKE Claude (where the orchestrator IS the session, held by CLAUDE.md), OpenCode runs
// the session as a PRIMARY agent — so here the orchestrator gets its own agent file
// (`mode: primary`, from orchestrator.fm), wired as opencode.json `default_agent`. The
// Builder and Reviewer are `mode: subagent`. The agent id comes from .ai-dev/config.json
// `roles` (its single home): orchestrator→ai-dev, builder→dev-builder, reviewer→dev-reviewer.
//
// Run from the repo root: node src/adapter/opencode/install-agents.mjs [outDir]
//   outDir defaults to .opencode/agents/ (pass a temp dir to dry-run).

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadRegistry, composeBody, composeFloorOnly } from "../modules.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const ROLES = ["orchestrator", "planner", "builder", "reviewer"];

// Resolve a role's configured model — on OpenCode this ALWAYS returns null: we
// never bake a `model:` line. The OpenCode `task` runtime parses a subagent's
// `model:` frontmatter but does NOT apply it at execution — a cluster of open
// upstream bugs (#21632 / #17870 / #18615; fix PR #14961 closed unmerged), with no
// fix shipped through our 1.17.7 (research: docs/decisions/opencode-task-capabilities.md
// Q1). Baking a concrete pin would therefore claim a cross-model reviewer the
// runtime silently swallows. So a concrete OpenCode pin is treated exactly like
// `auto`/`session`/absent — no `model:` line, the subagent inherits the session
// model, and the install log makes no false claim. The WHY a cross-model reviewer
// is unavailable on OpenCode is documented for the Operator in three durable homes:
// the orchestrator honesty note (`## Your seat`), tool-map.json `models.opencode._note`,
// and the research doc above. (Claude DOES bake a `model:` line — its `task` runtime
// honours a baked subagent model: — so its resolveModelPin returns a real pin; the two
// same-named exports differ precisely because the platforms differ.)
export function resolveModelPin() {
  return null; // OpenCode `task` ignores subagent model: at runtime — never bake one
}

// Assemble every role agent file into outDir from the given config. Returns the
// agentId→path map written, so a test can read the result without re-deriving it.
export function install(outDir, config) {
  fs.mkdirSync(outDir, { recursive: true });
  const registry = loadRegistry(ROOT);
  const written = {};
  for (const role of ROLES) {
    const agentId = config.roles?.[role]?.agent;
    if (!agentId) throw new Error(`.ai-dev/config.json roles.${role}.agent is missing`);
    const fm = fs.readFileSync(path.join(ROOT, "src", "adapter", "opencode", "agents", `${role}.fm`), "utf8").trim();
    // FLOOR body + the enabled capability modules' fragments, composed at the marker
    // by the SHARED assembler (one home with the Claude shim; never copied here).
    const floor = fs.readFileSync(path.join(ROOT, "src", "agents", `${role}.md`), "utf8").trimStart();
    const body = composeBody(ROOT, floor, role, registry, config, "opencode");
    // OpenCode never bakes a model line (resolveModelPin always null — the runtime
    // ignores subagent model: pins; see its comment). Kept as a call so the no-bake
    // contract has one named home the test pins.
    const modelPin = resolveModelPin(config.roles?.[role]?.model);
    const modelLine = modelPin ? `model: ${modelPin}\n` : "";
    const out = `---\n${fm}\n${modelLine}---\n\n${body}`;
    const outPath = path.join(outDir, `${agentId}.md`);
    fs.writeFileSync(outPath, out);
    written[agentId] = outPath;
    console.log(`wrote ${path.relative(ROOT, outPath)}  (role ${role} -> ${agentId}${modelPin ? `, model ${modelPin}` : ""})`);
  }
  // Emit the fixup Reviewer variant (floor-only body) for fixup-grade spawns. The fixup
  // agent id is derived as `{reviewerAgentId}-fixup` (naming convention — the single
  // derivation home: orchestrator.md `## Your seat`). No model pin — OpenCode never
  // bakes a model line (see resolveModelPin above).
  const reviewerAgentId = config.roles?.reviewer?.agent;
  if (reviewerAgentId) {
    const fixupFm = fs.readFileSync(path.join(ROOT, "src", "adapter", "opencode", "agents", "reviewer-fixup.fm"), "utf8").trim();
    const fixupFloor = fs.readFileSync(path.join(ROOT, "src", "agents", "reviewer.md"), "utf8").trimStart();
    const fixupBody = composeFloorOnly(ROOT, fixupFloor, "reviewer", registry, "opencode");
    const fixupAgentId = `${reviewerAgentId}-fixup`;
    const fixupOut = `---\n${fixupFm}\n---\n\n${fixupBody}`;
    const fixupOutPath = path.join(outDir, `${fixupAgentId}.md`);
    fs.writeFileSync(fixupOutPath, fixupOut);
    written[fixupAgentId] = fixupOutPath;
    console.log(`wrote ${path.relative(ROOT, fixupOutPath)}  (reviewer-fixup -> ${fixupAgentId})`);
  }
  return written;
}

// Run only when invoked directly (not when imported by a test for resolveModelPin /
// install). Config path: .ai-dev/config.json by default; AI_DEV_CONFIG lets a test
// drive a fixture config without mutating the repo's real one.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const configPath = process.env.AI_DEV_CONFIG
    ? path.resolve(process.env.AI_DEV_CONFIG)
    : path.join(ROOT, ".ai-dev", "config.json");
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const outDir = process.argv[2] ? path.resolve(process.argv[2]) : path.join(ROOT, ".opencode", "agents");
  install(outDir, config);
}
