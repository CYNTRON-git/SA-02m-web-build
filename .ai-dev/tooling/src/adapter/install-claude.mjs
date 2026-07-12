// Claude platform wiring: assemble the agents + the setup command, merge the deny
// hooks into .claude/settings.json (keyed so a re-run never duplicates), import the
// constitution via CLAUDE.md, and self-verify the deny wiring just written.

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";
import { runScript, ensureLine } from "./install-fs.mjs";
import { stripBreadcrumbFile } from "./install-breadcrumb.mjs";
import { loadClaudeModelPolicy, aliasOf } from "./claude/install-agents.mjs";
import { loadConfigWithLocal } from "./router-launch.mjs";

// A hook group is OURS when any of its commands targets the protocol shim. The
// marker is the shim's stable path tail — it survives a vendor-location change
// (.ai-pm/tooling/ → .ai-dev/tooling/) where an exact-command compare would not.
const HOOK_MARKER = "adapter/claude/shim.mjs";

// The tools the mechanical floor RIDES — the PreToolUse matcher MUST route them to the
// shim or that part of the deny layer goes silently off. verifyClaudeWiring step 1b
// asserts every one stays covered. The map doubles as the per-tool failure description, so
// a dropped-`Read` error names the boundary denies it disabled, not Bash's merge-gate.
const FLOOR_TOOL_DENIES = {
  Bash: "the merge-gate, remote-edit, and git-add-all denies", // push/commit/ssh are Bash ops
  Task: "the role/seat spawn-deny",
  Read: "the boundary read/find-outside-root deny",
  Write: "the boundary write-outside-root, truncation, and orchestrator-authors-content denies",
  Edit: "the boundary write-outside-root, truncation, and orchestrator-authors-content denies",
};
const FLOOR_TOOLS = Object.keys(FLOOR_TOOL_DENIES); // ["Bash","Task","Read","Write","Edit"]

// 5a. Wire Claude: assemble the agents + the setup command against the target root,
// merge the deny hooks into .claude/settings.json (keyed so a re-run never
// duplicates), and import the constitution + the orchestrator procedure via CLAUDE.md.
export function wireClaude(target, dogfood) {
  const cfg = { AI_DEV_CONFIG: path.join(target, ".ai-dev", "config.json") };
  // Children run from the vendored tooling copy downstream, the source tree in
  // dogfood mode (they self-locate ROOT three dirs up — the source copies point
  // ROOT at the repo root, correct for self-host).
  const scriptBase = dogfood ? path.join(target, "src") : path.join(target, ".ai-dev", "tooling", "src");
  runScript(scriptBase, path.join("adapter", "claude", "install-agents.mjs"), [path.join(target, ".claude", "agents")], cfg);
  runScript(scriptBase, path.join("adapter", "claude", "install-commands.mjs"), [path.join(target, ".claude", "commands")], cfg);

  // Merge the two deny hooks into .claude/settings.json idempotently. The hook
  // fragment is the claude/hooks.json (the single home of the hook shape) — read
  // from the source tree in dogfood mode, the vendored copy downstream; we merge
  // its PreToolUse/UserPromptSubmit arrays, de-duping by command.
  const hooksFragment = JSON.parse(
    fs.readFileSync(path.join(scriptBase, "adapter", "claude", "hooks.json"), "utf8"),
  );
  // The hooks.json fragment carries the DOWNSTREAM shim path
  // (.ai-dev/tooling/src/adapter/claude/shim.mjs). In dogfood mode the active shim
  // is the source copy — retarget the command to src/adapter/claude/shim.mjs so the
  // wired settings.json matches the committed self-host form. The HOOK_MARKER (the
  // path tail) is unchanged, so the merge still prunes a prior group correctly.
  if (dogfood) {
    for (const groups of Object.values(hooksFragment.hooks)) {
      for (const g of groups) {
        for (const h of g.hooks || []) {
          if (typeof h.command === "string") {
            h.command = h.command.split(".ai-dev/tooling/src/adapter/claude/shim.mjs").join("src/adapter/claude/shim.mjs");
          }
        }
      }
    }
  }
  const settingsPath = path.join(target, ".claude", "settings.json");
  const settings = fs.existsSync(settingsPath) ? JSON.parse(fs.readFileSync(settingsPath, "utf8")) : {};
  settings.hooks = mergeHooks(settings.hooks || {}, hooksFragment.hooks);
  // Deny the built-in Workflow tool by default (#368). CC ships Workflow's full JSON
  // schema resident in the system prompt (~5k tokens) whether it is used or not; the
  // protocol orchestrates through the Agent tool and NEVER uses Workflow, so a BARE
  // tool deny drops the schema from context entirely (not just blocks invocation) —
  // ~5k reclaimed for every downstream Claude project, re-paid per compaction on a
  // small-window model. Merge idempotently: never clobber a user's own permissions or
  // deny entries, never duplicate Workflow on a re-run. NOT Agent/Task or the dev tools.
  settings.permissions = settings.permissions || {};
  const deny = Array.isArray(settings.permissions.deny) ? settings.permissions.deny : [];
  if (!deny.includes("Workflow")) deny.push("Workflow");
  settings.permissions.deny = deny;
  // Disable auto-memory — protocol state lives in project artifacts (.ai-dev/notes/
  // for durable knowledge, .ai-dev/state/current.md for volatile session state), not in
  // the external per-profile store. Set unconditionally: `autoMemoryDirectory` has no CC
  // env var form (settings.json-only) and an absolute in-project path in a committed
  // settings.json would be machine-specific, so disabling is cleaner than diverting
  // (#316 P2c decision — docs/decisions/out-of-root-scratch-allow.md Residual).
  settings.autoMemoryEnabled = false;
  // The wrapper-less auto-apply of the launch-time models: write the config `launch`
  // section into settings.json `env`, which Claude Code reads AT STARTUP — so a routed
  // project needs no personal export wrapper for the env (the proxy PROCESS is still
  // started separately). Merges OUR two keys only (never clobbers a user-set env key);
  // an empty/absent launch section writes nothing and prunes any key we previously set,
  // so a non-routing project's settings.json is byte-unchanged (dogfood stays clean).
  const launchEnv = mergeLaunchEnv(settings.env, readLaunchModels(target));
  // Non-empty ⇒ write it; empty (our keys pruned to nothing) or null ⇒ drop the `env`
  // key entirely — never leave a cosmetic `env: {}` (and never a stale our-key).
  if (launchEnv && Object.keys(launchEnv).length > 0) settings.env = launchEnv;
  else delete settings.env;
  fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
  fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n");

  // Load instructions: the orchestrator session loads the constitution + its
  // procedure via CLAUDE.md imports — appended only if not already present. A
  // breadcrumb left from when this platform was inactive is replaced by this
  // full wiring. The constitution path is layout-dependent (source root in dogfood,
  // vendored downstream); the orchestrator import is UNIFIED — both modes @import the
  // committed platform:claude-FILTERED artifact .claude/ai-dev.md that install-agents
  // assembled above (so the session loads the filtered body, not the raw source that
  // re-injects the inactive platform's caveats every turn).
  const claudeMd = path.join(target, "CLAUDE.md");
  stripBreadcrumbFile(claudeMd);
  ensureLine(claudeMd, dogfood ? "@PROTOCOL.md" : "@.ai-dev/PROTOCOL.md");
  repointOrchestratorImport(claudeMd);

  // Self-verify the Claude wiring just written (item 3) — the symmetric twin of the
  // opencode plugin self-verify. A broken Claude deny path otherwise goes SILENTLY
  // off at the first tool call under a printed success; this FAILS THE INSTALL loudly.
  verifyClaudeWiring(target, settingsPath);

  // Routing self-verify — the twin for the CROSS-ENDPOINT class. A baked seat routed to a
  // foreign provider that silently baked a concrete native id (or a bound tier whose env
  // var did not land) runs NATIVE without complaint — the exact silent class papercut 13
  // fixes. This static config-intent ↔ baked-artifact ↔ settings.env consistency check
  // catches it at APPLY time, no live proxy needed; FAILS THE INSTALL loudly on a mismatch.
  verifyRoutingConsistency(target, settingsPath);
}

// Verify the Claude deny wiring just written actually holds: settings.json parses and
// carries the PreToolUse ai-dev hook entry, AND the shim it points at is loadable.
// THROWS (⇒ install exits non-zero) on any failure, naming the cause + the
// enforcement-off hint — mirroring the opencode plugin self-verify's message
// discipline. Honest scope: this proves the shim LOADS (syntax + its import chain),
// NOT that Claude invokes the hook at runtime (the registration class, the same
// residual the opencode plugin self-verify carries).
export function verifyClaudeWiring(target, settingsPath) {
  // 1. settings.json parses + carries the expected PreToolUse ai-dev hook entry.
  let settings;
  try {
    settings = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
  } catch (e) {
    throw new Error(
      `Claude wiring self-verify FAILED: ${settingsPath} is not valid JSON — ` +
        "the deny path would be silently off at the first tool call. " +
        `Underlying parse error: ${e && e.message ? e.message : String(e)}`,
      { cause: e },
    );
  }
  const preGroups = (settings.hooks && settings.hooks.PreToolUse) || [];
  const shimCommand = preGroups
    .flatMap((g) => (g.hooks || []).map((h) => (typeof h.command === "string" ? h.command : "")))
    .find((c) => c.includes(HOOK_MARKER));
  if (!shimCommand) {
    throw new Error(
      `Claude wiring self-verify FAILED: ${settingsPath} carries no PreToolUse hook ` +
        `referencing the deny shim (${HOOK_MARKER}) — the deny path would be silently ` +
        "off at the first tool call.",
    );
  }

  // 1b. the matcher of OUR shim-routing group(s) still COVERS the floor's tools. The
  // mechanical floor RIDES specific tools (FLOOR_TOOLS): the merge-gate, remote-edit, and
  // git-add-all denies fire on `Bash` (push/commit/ssh are Bash ops); the role/seat
  // spawn-deny fires on `Task`; the boundary (read/find/write-outside-root), truncation,
  // and orchestrator-authors-content denies fire on `Read`/`Write`/`Edit`. A PreToolUse
  // matcher that has dropped ANY of these routes those tool calls AROUND the shim, silently
  // disabling that part of the deny layer — the entry-exists check above does NOT catch it.
  // Assert each floor tool is a whole-token member of some shim-routing group's matcher;
  // fail-closed (an absent/empty matcher covers nothing). Honest scope: mergeHooks REPLACES
  // our group on every install, so this primarily guards a hooks.json source-regression that
  // drops a floor tool (a downstream's between-run manual matcher edit auto-heals on the next
  // install) plus fail-closed defense-in-depth. Persona sibling: the audit
  // verification-coverage sweep (src/agents/orchestrator.md `## Audit`).
  const shimMatchers = preGroups
    .filter((g) => (g.hooks || []).some((h) => typeof h.command === "string" && h.command.includes(HOOK_MARKER)))
    .map((g) => g.matcher);
  for (const tool of FLOOR_TOOLS) {
    if (!shimMatchers.some((m) => matcherCoversTool(m, tool))) {
      throw new Error(
        `Claude wiring self-verify FAILED: the PreToolUse deny-shim matcher in ${settingsPath} ` +
          `does not cover \`${tool}\` — the mechanical floor that rides ${tool} ` +
          `(${FLOOR_TOOL_DENIES[tool]}) ` +
          "would be silently off at the first tool call.",
      );
    }
  }

  // 2. the shim itself loads. Resolve its on-disk path from the WIRED command (so the
  // check works in both downstream and dogfood layouts), substituting the harness's
  // $CLAUDE_PROJECT_DIR for the real target root.
  const shimPath = resolveShimPath(target, shimCommand);
  if (!fs.existsSync(shimPath)) {
    throw new Error(
      `Claude wiring self-verify FAILED: the wired deny shim does not exist at ${shimPath} ` +
        "— the deny path would be silently off at the first tool call.",
    );
  }
  // `node --check` = a SYNTAX check that does NOT execute the shim (it reads stdin/argv
  // when run, so executing it here would hang); a child import() then catches a broken
  // dependency chain (the shim's top-level imports of engine.mjs / deny-rules.json).
  // Both via execFileSync with an argv ARRAY (no shell) — consistent with runScript.
  try {
    execFileSync("node", ["--check", shimPath], { stdio: "pipe" });
    execFileSync(
      "node",
      ["--input-type=module", "-e", `await import(${JSON.stringify(pathToFileURL(shimPath).href)})`],
      { stdio: "pipe" },
    );
  } catch (e) {
    const detail = e && e.stderr ? e.stderr.toString() : e && e.message ? e.message : String(e);
    throw new Error(
      `Claude wiring self-verify FAILED: the deny shim at ${shimPath} does not load — ` +
        `the deny path would be silently off at the first tool call. Underlying load error: ${detail}`,
      { cause: e },
    );
  }
}

// The BAKED seats whose routing this check covers. Session + guard are launch-env concrete
// ids (never tier-routed), so they carry no silent-native class — only the baked seats do.
// `planner` is a first-class routable baked seat (papercut 14 C): a foreign-pinned planner
// that baked native is the same silent-native class as builder/reviewer, so it is verified
// too; an unpinned planner (inherit session) has no tier intent and is reported, never failed.
const ROUTED_SEATS = ["planner", "builder", "reviewer"];

// Pure routing-consistency check: config INTENT ↔ baked ACTUAL ↔ settings.json env — the
// static apply-time guard against the silent-native cross-endpoint class (docs/decisions/
// multi-model-setup-ux.md papercut 13), no live proxy needed. Inputs are already parsed:
//   config          — .ai-dev/config.json
//   seatModelLines  — { planner, builder, reviewer } → the baked `model:` value, or null for no line
//   env             — .claude/settings.json `env` (or {})
//   policy          — loadClaudeModelPolicy()
// Intent per seat: a bare tier alias whose tier is bound in launch.aliases ⇒ FOREIGN (must
// bake the BARE alias AND carry ANTHROPIC_DEFAULT_<TIER>_MODEL=<that id>); a bare tier
// unbound, or a concrete claude-<tier>-* id ⇒ NATIVE (must bake the concrete id); anything
// else (session/auto/absent) ⇒ inherit (no tier routing to check). Returns { ok, reports,
// failures }: reports are the success confirmation lines, failures name the seat + intended
// vs actual. ok === (failures.length === 0). Pure — the file-reading half is
// verifyRoutingConsistency below.
//
// `localAliases` — the tier bindings that live in the PERSONAL, gitignored config.local.json
// (a subset of the merged `config.launch.aliases`). A tier bound there is applied by the
// LAUNCHER at startup and is DELIBERATELY absent from the committed settings.json env
// (mergeLaunchEnv writes only shared config.json values), so its missing env var is EXPECTED,
// not a silent-native failure. A tier bound in the SHARED config.json still MUST land in
// settings.json env. Absent ⇒ {} ⇒ every bound tier is treated as shared (the pre-config.local
// behaviour, byte-identical). See docs/decisions/personal-multi-model-setup.md.
export function checkRouting({ config, seatModelLines, env, policy, localAliases = {} }) {
  const roles = (config && config.roles) || {};
  const launch = (config && typeof config.launch === "object" && config.launch) || {};
  const aliases = (typeof launch.aliases === "object" && launch.aliases) || {};
  const localAliasObj = localAliases && typeof localAliases === "object" ? localAliases : {};
  const envObj = env && typeof env === "object" ? env : {};
  const reports = [];
  const failures = [];
  const boundId = (tier) => (typeof aliases[tier] === "string" ? aliases[tier].trim() : "");
  // A tier whose binding lives in config.local ⇒ the launcher exports its env var at startup,
  // so an absent settings.json env var for it is expected (never committed).
  const launcherApplied = (tier) => typeof localAliasObj[tier] === "string" && localAliasObj[tier].trim() !== "";

  for (const seat of ROUTED_SEATS) {
    const role = roles[seat];
    if (!role || !role.agent) continue; // an unstaffed baked seat (rigor profile) — nothing baked
    const wish = role.model;
    const actual = seatModelLines[seat] ?? null; // the baked model: value, or null for no line
    const tier = aliasOf(wish, policy); // a bare tier OR a claude-<tier>-* id → its tier; else null
    if (!tier) {
      // session / auto / absent / off-allowlist — an inherit/auto seat, no tier routing intent.
      reports.push(actual
        ? `routing: ${seat} → ${actual} (auto / no tier routing)`
        : `routing: ${seat} → inherits the session model (no pin)`);
      continue;
    }
    const isConcrete = typeof wish === "string" && wish.startsWith("claude-");
    const foreignId = boundId(tier);
    if (foreignId && !isConcrete) {
      // INTENDED FOREIGN — must bake the BARE alias AND carry the tier env var, UNLESS the
      // binding is personal (config.local): then the launcher exports the env var at startup,
      // so its absence from the committed settings.json is expected (never a failure).
      const envKey = LAUNCH_ALIAS_ENV_KEYS[tier];
      const personal = launcherApplied(tier);
      const bakeOk = actual === tier;
      const envOk = envObj[envKey] === foreignId || personal;
      if (!bakeOk) {
        failures.push(
          `seat ${seat}: intended FOREIGN (${tier} tier → ${foreignId}) but the assembled agent baked ` +
            `\`model: ${actual === null ? "<none>" : actual}\` — routing needs the bare \`${tier}\` alias (resolves through ` +
            `${envKey}); \`${actual === null ? "<none>" : actual}\` runs NATIVE, silently ignoring the binding.`);
      }
      if (!envOk) {
        failures.push(
          `seat ${seat}: the ${tier} tier is bound to ${foreignId} but settings.json env ${envKey} is ` +
            `${envObj[envKey] === undefined ? "MISSING" : `\`${envObj[envKey]}\``} — the ${tier} alias would not resolve to the foreign model.`);
      }
      if (bakeOk && envOk) {
        reports.push(personal
          ? `routing: ${seat} → ${tier} tier via ${foreignId} (foreign, applied by the launcher from config.local)`
          : `routing: ${seat} → ${tier} tier via ${foreignId} (foreign)`);
      }
    } else {
      // INTENDED NATIVE — must bake the CONCRETE id (passthrough, immune to any alias env).
      const expected = isConcrete ? wish : policy.ids[tier];
      if (actual !== expected) {
        failures.push(
          `seat ${seat}: intended NATIVE (${expected}) but the assembled agent baked ` +
            `\`model: ${actual === null ? "<none>" : actual}\` — a native seat must bake the concrete id.`);
      } else {
        reports.push(`routing: ${seat} → ${expected} (native)`);
      }
    }
  }

  // A bound tier whose env var did not land — independent of which seat uses it (the
  // mergeLaunchEnv contract). De-duped against a per-seat env failure already raised above.
  // A PERSONAL (config.local) binding is skipped: the launcher exports its env var at
  // startup, so its absence from the committed settings.json is expected, not a failure.
  for (const [tier, envKey] of Object.entries(LAUNCH_ALIAS_ENV_KEYS)) {
    const foreignId = boundId(tier);
    if (foreignId && !launcherApplied(tier) && envObj[envKey] !== foreignId && !failures.some((f) => f.includes(envKey))) {
      failures.push(
        `the ${tier} tier is bound to ${foreignId} in launch.aliases but settings.json env ${envKey} is ` +
          `${envObj[envKey] === undefined ? "MISSING" : `\`${envObj[envKey]}\``} — the binding would not apply.`);
    }
  }

  return { ok: failures.length === 0, reports, failures };
}

// Pure cross-model independence check: detects when the reviewer's effective model resolves
// to the same model as the session, meaning the protocol's `auto` cross-model promise is
// hollow — no blind spot exists. Non-blocking (advisory warnings only); the Operator may
// consciously accept same-model review on a single-provider proxy. Returns { warnings: [...] }.
// One home: install-claude.mjs — the routing-verify path (docs/decisions/cross-model-honesty.md).
export function checkCrossModelIndependence({ config, seatModelLines, policy }) {
  const launch = (config && typeof config.launch === "object" && config.launch) || {};
  const aliases = (typeof launch.aliases === "object" && launch.aliases) || {};
  const sessionModel = typeof launch.sessionModel === "string" ? launch.sessionModel.trim() : "";
  const warnings = [];

  // No explicit session model → the session's effective model is unknown (harness default),
  // so the collision can't be detected. Skip silently — no false positive.
  if (!sessionModel) return { warnings };

  // Resolve the reviewer's EFFECTIVE runtime model:
  //   bare alias (e.g. "sonnet")  → foreign-bound tier id (launch.aliases) or native id
  //   concrete id (e.g. "claude-sonnet-4-6") → verbatim
  //   null (no model line, session inherit) → same as session, but HONEST (no false claim)
  const reviewerBaked = seatModelLines.reviewer ?? null;
  if (reviewerBaked === null) return { warnings }; // honest inherit — no false claim

  const tier = aliasOf(reviewerBaked, policy);
  let reviewerEffective;
  if (tier) {
    const foreignId = typeof aliases[tier] === "string" ? aliases[tier].trim() : "";
    reviewerEffective = foreignId || policy.ids[tier];
  } else {
    reviewerEffective = reviewerBaked; // concrete id (possibly non-allowlist)
  }

  if (reviewerEffective && reviewerEffective === sessionModel) {
    warnings.push(
      `cross-model independence: reviewer resolves to the same model as the session ` +
        `(${sessionModel}) — no cross-model blind spot. The reviewer contract says "a ` +
        `different context than the Builder," but this configuration runs both on the ` +
        `same brain. To restore independence: pin the reviewer to a different model ` +
        `or set a different tier alias.`,
    );
  }

  return { warnings };
}

// Read the apply artifacts (config, baked agents, settings.json env) and run checkRouting.
// THROWS (⇒ install exits non-zero) with a per-seat report on any mismatch — the loud twin
// of verifyClaudeWiring for the ROUTING class. On all-consistent, prints the per-seat
// confirmation lines (the "seat → model" trace). A malformed config is itself a failure
// (the routing intent cannot be read, so it cannot be trusted).
export function verifyRoutingConsistency(target, settingsPath) {
  const policy = loadClaudeModelPolicy();
  const configPath = path.join(target, ".ai-dev", "config.json");
  // Read the config as the INSTALLER sees it: config.json merged with the gitignored
  // config.local.json's `launch` (loadConfigWithLocal, the one home shared with the
  // launcher + the bake). So a foreign tier bound ONLY in config.local is recognised here —
  // the bake produced the bare alias for it, and this check accepts its settings.json env
  // absence as launcher-applied (docs/decisions/personal-multi-model-setup.md).
  let config, shared, local;
  try {
    ({ config, shared, local } = loadConfigWithLocal(configPath));
  } catch (e) {
    throw new Error(
      `Routing self-verify FAILED: cannot read the config at ${configPath} — ${e && e.message ? e.message : String(e)}`,
      { cause: e },
    );
  }
  if (!shared || typeof shared !== "object") {
    throw new Error(`Routing self-verify FAILED: cannot read the config at ${configPath} — file missing or empty`);
  }
  const localAliases = local && typeof local.launch === "object" && local.launch && typeof local.launch.aliases === "object"
    ? local.launch.aliases
    : {};
  let env = {};
  try {
    const s = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    env = s && typeof s.env === "object" && s.env ? s.env : {};
  } catch {
    // no settings/env ⇒ keep the {} default; a foreign seat then fails loud on the missing var
  }
  const seatModelLines = {};
  for (const seat of ROUTED_SEATS) {
    const agentId = config.roles?.[seat]?.agent;
    seatModelLines[seat] = agentId
      ? readBakedModelLine(path.join(target, ".claude", "agents", `${agentId}.md`))
      : null;
  }
  const { ok, reports, failures } = checkRouting({ config, seatModelLines, env, policy, localAliases });
  if (!ok) {
    throw new Error(
      "Routing self-verify FAILED — the config's routing intent does not match the assembled agents / settings.json env:\n" +
        failures.map((f) => `  • ${f}`).join("\n") +
        "\n  Fix the config and re-run the installer: a foreign-routed seat must bake the bare tier alias AND carry its ANTHROPIC_DEFAULT_<TIER>_MODEL binding.",
    );
  }
  for (const r of reports) console.log(r);

  // Cross-model independence check (non-blocking advisory): warn when the reviewer
  // resolves to the same model as the session — no blind spot despite the `auto` promise.
  const { warnings } = checkCrossModelIndependence({ config, seatModelLines, policy });
  for (const w of warnings) process.stderr.write(`WARNING: ${w}\n`);
}

// Extract the frontmatter `model:` value from an assembled agent file, or null when it
// carries no `model:` line (an inherit seat) or the file is absent.
function readBakedModelLine(agentPath) {
  if (!fs.existsSync(agentPath)) return null;
  const fm = fs.readFileSync(agentPath, "utf8").split("\n---")[0];
  const m = fm.match(/^model:\s*(.+)$/m);
  return m ? m[1].trim() : null;
}

// Resolve the shim's real on-disk path from the wired hook command. The command is
// `node "$CLAUDE_PROJECT_DIR/<rel>/shim.mjs"` (downstream) or `node "src/.../shim.mjs"`
// (dogfood, retargeted). Pull the token ending in the marker, strip the
// $CLAUDE_PROJECT_DIR prefix to the real target root, and resolve.
function resolveShimPath(target, command) {
  // The marker (HOOK_MARKER) is the path tail; the wired path is everything from the
  // start of the quoted/space-delimited shim token up to and including the marker.
  const tokens = command.match(/\S+/g) || [];
  const raw = tokens
    .map((t) => t.replace(/^["']|["']$/g, ""))
    .find((t) => t.includes(HOOK_MARKER));
  if (!raw) return path.join(target, ".ai-dev", "tooling", "src", HOOK_MARKER);
  const rel = raw
    .replace(/^["']/, "")
    .replace(/^\$CLAUDE_PROJECT_DIR\//, "")
    .replace(/^\$\{CLAUDE_PROJECT_DIR\}\//, "");
  return path.isAbsolute(rel) ? rel : path.join(target, rel);
}

// True when `tool` is a WHOLE alternation member of the matcher string (a regex-ish
// `A|B|C` Claude matches against the tool name). Whole-token membership via word
// boundaries — never a brittle exact-string compare — so a reordered, extended, or
// parenthesis-grouped (`(Read|Bash)`) matcher with the tool still present passes, while
// a substring like `BashTool`/`Bashful` does NOT count as coverage. `\b` is chosen over
// `|`-only anchors precisely because it also tolerates grouping (a trailing `)` defeats
// a `($|\|)` anchor); Claude's tool namespace is alphanumeric with no sub-word collision
// for `Bash`/`Task`, so `\b` carries no false positive here. Fail-closed: a non-string or
// empty matcher covers nothing. `tool` is only ever a fixed FLOOR_TOOLS literal (no regex
// metacharacters), so no escaping is required at this call site.
function matcherCoversTool(matcher, tool) {
  if (typeof matcher !== "string" || !matcher) return false;
  return new RegExp(`\\b${tool}\\b`).test(matcher);
}

// Repoint the orchestrator @import to the unified, platform-filtered load surface
// (.claude/ai-dev.md). The surface USED to be the raw orchestrator source, wired by a
// layout-dependent line: @src/agents/orchestrator.md (dogfood) or
// @.ai-dev/tooling/src/agents/orchestrator.md (downstream). A downstream UPGRADE from a
// pre-this-version install therefore carries the old vendored line — so we STRIP any stale
// raw-orchestrator import first, then ensure the new unified line. Strip-then-ensure (not a
// bare append) is what guarantees the old line is REPLACED, never left dangling beside the
// new one. Idempotent: on a tree already at the new form, the strip is a no-op (the file is
// not rewritten) and ensureLine no-ops — so a dogfood reinstall converges to committed bytes.
const ORCHESTRATOR_IMPORT = "@.claude/ai-dev.md";
const STALE_ORCHESTRATOR_IMPORTS = [
  "@src/agents/orchestrator.md",
  "@.ai-dev/tooling/src/agents/orchestrator.md",
];
function repointOrchestratorImport(claudeMd) {
  if (fs.existsSync(claudeMd)) {
    const lines = fs.readFileSync(claudeMd, "utf8").split("\n");
    const kept = lines.filter((l) => !STALE_ORCHESTRATOR_IMPORTS.includes(l.trim()));
    if (kept.length !== lines.length) fs.writeFileSync(claudeMd, kept.join("\n"));
  }
  ensureLine(claudeMd, ORCHESTRATOR_IMPORT);
}

// The two env keys the launch-time models own in settings.json (G1: the guard maps to
// the DEPRECATED `ANTHROPIC_SMALL_FAST_MODEL` — the only var that sets the background
// model independently of the haiku slot; tracked in the backlog for its eventual
// removal. See docs/decisions/multi-model-setup-ux.md `## Requirement 7`).
const LAUNCH_ENV_KEYS = {
  sessionModel: "ANTHROPIC_MODEL",
  guardModel: "ANTHROPIC_SMALL_FAST_MODEL",
};

// The tier-alias bindings (nested config.launch.aliases.{fable,opus,sonnet,haiku}) → the
// ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL vars Claude Code resolves a tier through
// — the FOUR remappable aliases (strongest→weakest fable·opus·sonnet·haiku; the fixed set,
// no custom aliases). The cross-endpoint lever: bind a Claude tier to a foreign model id.
// Same launch-env class (startup-read, restart-applied) and same prune semantics as
// LAUNCH_ENV_KEYS above. Mirror of router-launch.mjs `launchModelEnv` ALIAS_ENV (the
// launcher-side export) — the two MUST stay in sync tier-for-tier.
const LAUNCH_ALIAS_ENV_KEYS = {
  fable: "ANTHROPIC_DEFAULT_FABLE_MODEL",
  opus: "ANTHROPIC_DEFAULT_OPUS_MODEL",
  sonnet: "ANTHROPIC_DEFAULT_SONNET_MODEL",
  haiku: "ANTHROPIC_DEFAULT_HAIKU_MODEL",
};

// Read the config `launch` section ({ sessionModel, guardModel }) from the target's
// .ai-dev/config.json — the same file in dogfood (the source repo's own root config) and
// downstream. Absent file / section / non-object ⇒ {} (a non-routing project, no env).
// Best-effort: a malformed config must NOT abort the install — the deny wiring above is
// the load-bearing write; the launch env is an optional convenience layered on top.
function readLaunchModels(target) {
  const configPath = path.join(target, ".ai-dev", "config.json");
  if (!fs.existsSync(configPath)) return {};
  let config;
  try {
    config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  } catch {
    return {};
  }
  return config && typeof config.launch === "object" && config.launch ? config.launch : {};
}

// Merge the launch-time model values into an existing settings.json `env` object,
// touching ONLY our keys (LAUNCH_ENV_KEYS + LAUNCH_ALIAS_ENV_KEYS) — a foreign env key is
// never clobbered.
// A non-empty trimmed value is written; an empty/absent value PRUNES our key (so clearing
// the config field clears the env, never leaving a stale value). Returns the merged env
// object, or `null` when the result would be empty AND there was no pre-existing env (so
// the caller writes no `env` key at all — byte-unchanged for a non-routing project).
export function mergeLaunchEnv(existingEnv, launch) {
  const env = existingEnv && typeof existingEnv === "object" ? { ...existingEnv } : {};
  const hadEnv = existingEnv !== undefined && existingEnv !== null;
  for (const [field, key] of Object.entries(LAUNCH_ENV_KEYS)) {
    const raw = launch && typeof launch[field] === "string" ? launch[field].trim() : "";
    if (raw) env[key] = raw;
    else delete env[key];
  }
  // Tier-alias bindings (nested launch.aliases.{fable,opus,sonnet,haiku}) → ANTHROPIC_DEFAULT_*.
  // Same per-key prune: a non-empty value is written, an empty/absent one clears our key.
  const aliases = launch && typeof launch.aliases === "object" && launch.aliases ? launch.aliases : {};
  for (const [tier, key] of Object.entries(LAUNCH_ALIAS_ENV_KEYS)) {
    const raw = typeof aliases[tier] === "string" ? aliases[tier].trim() : "";
    if (raw) env[key] = raw;
    else delete env[key];
  }
  if (Object.keys(env).length === 0 && !hadEnv) return null;
  return env;
}

// Merge a hooks fragment into an existing hooks object: foreign groups are kept
// untouched; every group recognised as ours (HOOK_MARKER) is REPLACED by the
// fragment's current group. Replace-not-append is both the idempotence guarantee
// (a re-run converges to the same bytes) and the prune: a stale ai-dev group whose
// command changed no longer accumulates beside the new one.
function mergeHooks(existing, fragment) {
  const merged = { ...existing };
  for (const [event, groups] of Object.entries(fragment)) {
    const foreign = (Array.isArray(merged[event]) ? merged[event] : []).filter(
      (g) => !(g.hooks || []).some((h) => typeof h.command === "string" && h.command.includes(HOOK_MARKER)),
    );
    merged[event] = [...foreign, ...groups];
  }
  return merged;
}

// Standalone: `node <this> --verify-routing [target]` runs ONLY the routing self-check
// against an already-applied tree (the same check wireClaude runs post-apply) — a cheap,
// re-runnable probe. Exits 0 (consistent, prints the seat→model trace) / 1 (loud failure).
// Guarded so importing this module (the install path) never triggers it.
if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  const args = process.argv.slice(2);
  if (args.includes("--verify-routing")) {
    const target = args.find((a) => !a.startsWith("--")) || process.cwd();
    try {
      verifyRoutingConsistency(target, path.join(target, ".claude", "settings.json"));
      console.log("routing self-verify: OK");
    } catch (e) {
      process.stderr.write((e && e.message ? e.message : String(e)) + "\n");
      process.exit(1);
    }
  }
}
