// Installer test — Claude wiring self-verify, the no-repo predicate, ensureConfig, and the
// launch-time-model → settings.json env auto-apply. One of the two cohesive halves the
// former monolithic install.test.mjs was split into (audit LOW-2); the platform-agnostic
// install behaviour lives in install-core.test.mjs, the shared helpers in
// install-shared.mjs. After wiring Claude, the installer verifies settings.json parses +
// carries the PreToolUse deny-shim entry, its matcher covers the floor tools, AND the shim
// itself loads — a broken Claude deny path FAILS THE INSTALL loudly instead of going
// silently off at the first tool call.
//
// Run: node src/adapter/install-claude-wiring.test.mjs

import { install, hasGitRepo, verifyClaudeWiring } from "./install.mjs";
import { ensureConfig } from "./install-core.mjs";
import { mergeLaunchEnv } from "./install-claude.mjs";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { ROOT, makeChecker, freshTarget, registerStragglerSweep } from "./install-shared.mjs";

const { check, report } = makeChecker();
registerStragglerSweep();

// ── item 3: Claude hook wiring self-verify (GOOD / BROKEN-shim / BROKEN-settings) ─
// After wiring Claude, the installer verifies settings.json parses + carries the
// PreToolUse deny-shim entry AND the shim itself loads (syntax + import chain). A
// broken Claude deny path FAILS THE INSTALL loudly (throw ⇒ non-zero, named cause)
// instead of going silently off at the first tool call — the symmetric twin of the
// opencode plugin self-verify.
//
// The broken arms drive verifyClaudeWiring DIRECTLY against a clean install whose
// wiring is then corrupted — because a full re-install RE-VENDORS the adapter and
// would repair any break before the verify runs (same reason the opencode broken arm
// drives the standalone plugin-install step, not a full install). RED proof: without
// the verifyClaudeWiring call wired into wireClaude, a broken-shim install would exit
// 0; the unit here proves the gate that closes that.
{
  const cli = path.join(ROOT, "src", "adapter", "install.mjs");

  // GOOD arm — an intact Claude install exits 0 (the self-verify, now wired into
  // wireClaude, passes on intact wiring). This is the end-to-end half: the verify
  // runs as part of a real CLI install and does NOT spuriously fail.
  const good = freshTarget("claudeverify-good");
  try {
    const r = spawnSync("node", [cli, good, "--platform", "claude"], { encoding: "utf8" });
    check("[claudeverify] GOOD install exits 0 (self-verify passes on intact wiring)", r.status === 0);
    // and the verify is idempotently re-runnable on the intact tree (no throw)
    let threw = false;
    try { verifyClaudeWiring(good, path.join(good, ".claude", "settings.json")); } catch { threw = true; }
    check("[claudeverify] verify on an intact tree does NOT throw", !threw);
  } finally {
    fs.rmSync(good, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // WIRED-IN arm (proves verifyClaudeWiring is actually CALLED by wireClaude, not just
  // correct in isolation): a full downstream install always vendors a HEALTHY shim, so
  // to make the install itself produce a broken shim we install from a COPY of the
  // source tree whose shim is corrupted — the installer vendors the broken shim, then
  // its wired self-verify must FAIL the install loudly. RED proof: without the wired
  // call, this install would exit 0 over a silently-off deny path.
  const srcCopy = fs.mkdtempSync(path.join(ROOT, ".tmp-srccopy-"));
  const copyMin = (rel) => {
    const from = path.join(ROOT, rel), to = path.join(srcCopy, rel);
    fs.cpSync(from, to, { recursive: true });
  };
  try {
    // the minimal source surface the installer reads (vendor + lay-down + claude wire)
    fs.mkdirSync(path.join(srcCopy, "src"), { recursive: true });
    copyMin("PROTOCOL.md");
    copyMin("protocol-reference.md");
    copyMin("package.json");
    for (const d of ["adapter", "agents", "modules", "quality", "templates"]) copyMin(path.join("src", d));
    // corrupt the shim the installer will vendor + wire
    fs.writeFileSync(path.join(srcCopy, "src", "adapter", "claude", "shim.mjs"), "broken ( syntax {\n");
    const target = freshTarget("claudeverify-wired");
    try {
      const r = spawnSync("node", [path.join(srcCopy, "src", "adapter", "install.mjs"), target, "--platform", "claude"], { encoding: "utf8" });
      const err = (r.stderr || "") + (r.stdout || "");
      check("[claudeverify] WIRED-IN: a full install with a broken shim EXITS NON-ZERO", r.status !== 0);
      check("[claudeverify] WIRED-IN: the install error carries the enforcement-off hint", err.includes("silently off at the first tool call"));
    } finally {
      fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
    }
  } finally {
    fs.rmSync(srcCopy, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // BROKEN-shim arm (the ratchet) — a clean install, then break the shim's import
  // chain by removing engine.mjs (the shim's top-level import). verifyClaudeWiring
  // MUST throw, naming the shim path + the enforcement-off hint.
  const brokenShim = freshTarget("claudeverify-brokenshim");
  try {
    install(brokenShim, "claude");
    const settingsPath = path.join(brokenShim, ".claude", "settings.json");
    fs.rmSync(path.join(brokenShim, ".ai-dev", "tooling", "src", "adapter", "engine.mjs"), { force: true });
    let threw = false, msg = "";
    try { verifyClaudeWiring(brokenShim, settingsPath); } catch (e) { threw = true; msg = e.message; }
    check("[claudeverify] BROKEN shim (import chain): verify THROWS (fail-closed)", threw);
    check("[claudeverify] BROKEN shim: error names the shim path", msg.includes("shim.mjs"));
    check("[claudeverify] BROKEN shim: error carries the enforcement-off hint", msg.includes("silently off at the first tool call"));
  } finally {
    fs.rmSync(brokenShim, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // BROKEN-shim (syntax) arm — corrupt the shim file into a syntax error ⇒ the
  // `node --check` probe catches it before any import.
  const brokenSyntax = freshTarget("claudeverify-syntax");
  try {
    install(brokenSyntax, "claude");
    const settingsPath = path.join(brokenSyntax, ".claude", "settings.json");
    fs.writeFileSync(path.join(brokenSyntax, ".ai-dev", "tooling", "src", "adapter", "claude", "shim.mjs"), "this is ( not valid javascript {\n");
    let threw = false, msg = "";
    try { verifyClaudeWiring(brokenSyntax, settingsPath); } catch (e) { threw = true; msg = e.message; }
    check("[claudeverify] BROKEN shim syntax: verify THROWS", threw);
    check("[claudeverify] BROKEN shim syntax: error names the deny path going off", msg.includes("silently off at the first tool call"));
  } finally {
    fs.rmSync(brokenSyntax, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // MISSING-ENTRY arm — settings.json parses but carries NO PreToolUse deny-shim
  // entry (the hook silently dropped) ⇒ verify throws naming settings.json.
  const missingEntry = freshTarget("claudeverify-noentry");
  try {
    install(missingEntry, "claude");
    const settingsPath = path.join(missingEntry, ".claude", "settings.json");
    fs.writeFileSync(settingsPath, JSON.stringify({ hooks: { PreToolUse: [{ matcher: "Bash", hooks: [{ type: "command", command: "other --tool" }] }] } }, null, 2) + "\n");
    let threw = false, msg = "";
    try { verifyClaudeWiring(missingEntry, settingsPath); } catch (e) { threw = true; msg = e.message; }
    check("[claudeverify] MISSING deny-shim entry: verify THROWS", threw);
    check("[claudeverify] missing-entry error names settings.json", msg.includes("settings.json"));
  } finally {
    fs.rmSync(missingEntry, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // BROKEN-settings (malformed JSON) arm — settings.json is not valid JSON ⇒ verify
  // throws naming settings.json (the parse-failure path).
  const brokenSettings = freshTarget("claudeverify-settings");
  try {
    install(brokenSettings, "claude");
    const settingsPath = path.join(brokenSettings, ".claude", "settings.json");
    fs.writeFileSync(settingsPath, "{ this is not json\n");
    let threw = false, msg = "";
    try { verifyClaudeWiring(brokenSettings, settingsPath); } catch (e) { threw = true; msg = e.message; }
    check("[claudeverify] malformed settings.json: verify THROWS (fail-closed)", threw);
    check("[claudeverify] malformed-settings error names settings.json", msg.includes("settings.json"));
  } finally {
    fs.rmSync(brokenSettings, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // MATCHER-COVERAGE arms — the deny-shim entry is present AND the shim loads, but the
  // PreToolUse matcher of OUR group has lost a floor tool. The mechanical floor RIDES
  // specific tools: the merge-gate + remote-edit + git-add-all denies fire on `Bash`
  // (push/commit/ssh are Bash ops), the role/seat spawn-deny on `Task` — a matcher that
  // omits either silently disables that whole half of the deny layer. verifyClaudeWiring
  // (step 1b) MUST throw, naming the missing tool + the enforcement-off hint; a reordered
  // or extended SUPERSET still passes (membership is whole-token, never an exact-string
  // compare). Honest scope: mergeHooks REPLACES our group on every install, so the durable
  // catch is a hooks.json source-regression that dropped a floor tool (a downstream's
  // between-run manual matcher edit auto-heals on the next install) + fail-closed
  // defense-in-depth — which is exactly why these arms drive verifyClaudeWiring DIRECTLY
  // against a tampered settings.json, mirroring the MISSING-ENTRY arm.
  //
  // Each crafted settings.json keeps a FOREIGN group whose matcher DOES carry `Bash` but
  // routes elsewhere (my-own-linter, no shim marker): the check must scope to OUR group
  // and must NOT be fooled into thinking Bash is covered by a foreign Bash matcher.
  const SHIM_CMD = 'node "$CLAUDE_PROJECT_DIR/.ai-dev/tooling/src/adapter/claude/shim.mjs"';
  const writeMatcher = (settingsPath, matcher) =>
    fs.writeFileSync(
      settingsPath,
      JSON.stringify(
        {
          hooks: {
            PreToolUse: [
              { matcher, hooks: [{ type: "command", command: SHIM_CMD }] },
              // foreign group: a Bash matcher that routes to a non-shim command — the
              // check must ignore it (it does not protect the floor).
              { matcher: "Bash", hooks: [{ type: "command", command: "my-own-linter --check" }] },
            ],
          },
        },
        null,
        2,
      ) + "\n",
    );

  // missing Bash ⇒ THROWS, error names Bash + the enforcement-off hint
  const noBash = freshTarget("claudeverify-nobash");
  try {
    install(noBash, "claude");
    const settingsPath = path.join(noBash, ".claude", "settings.json");
    writeMatcher(settingsPath, "Read|Write|Edit|Task|Skill");
    let threw = false, msg = "";
    try { verifyClaudeWiring(noBash, settingsPath); } catch (e) { threw = true; msg = e.message; }
    check("[claudeverify] matcher MISSING Bash: verify THROWS (fail-closed)", threw);
    check("[claudeverify] missing-Bash error names the missing tool (Bash)", msg.includes("Bash"));
    check("[claudeverify] missing-Bash error carries the enforcement-off hint", msg.includes("silently off at the first tool call"));
  } finally {
    fs.rmSync(noBash, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // missing Task ⇒ THROWS, error names Task
  const noTask = freshTarget("claudeverify-notask");
  try {
    install(noTask, "claude");
    const settingsPath = path.join(noTask, ".claude", "settings.json");
    writeMatcher(settingsPath, "Read|Write|Edit|Bash|Skill");
    let threw = false, msg = "";
    try { verifyClaudeWiring(noTask, settingsPath); } catch (e) { threw = true; msg = e.message; }
    check("[claudeverify] matcher MISSING Task: verify THROWS (fail-closed)", threw);
    check("[claudeverify] missing-Task error names the missing tool (Task)", msg.includes("Task"));
  } finally {
    fs.rmSync(noTask, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // missing Read/Write/Edit ⇒ THROWS, error names the dropped boundary-floor tool. These
  // ride the boundary read/find/write-outside-root, truncation, and orchestrator-content
  // denies — a matcher that drops one routes those AROUND the shim (LOW-1, audit 2026-06-27).
  for (const [tool, matcher] of [
    ["Read", "Write|Edit|Bash|Task|Skill"],
    ["Write", "Read|Edit|Bash|Task|Skill"],
    ["Edit", "Read|Write|Bash|Task|Skill"],
  ]) {
    const dropped = freshTarget(`claudeverify-no${tool.toLowerCase()}`);
    try {
      install(dropped, "claude");
      const settingsPath = path.join(dropped, ".claude", "settings.json");
      writeMatcher(settingsPath, matcher);
      let threw = false, msg = "";
      try { verifyClaudeWiring(dropped, settingsPath); } catch (e) { threw = true; msg = e.message; }
      check(`[claudeverify] matcher MISSING ${tool}: verify THROWS (fail-closed)`, threw);
      check(`[claudeverify] missing-${tool} error names the missing tool (${tool})`, msg.includes(tool));
    } finally {
      fs.rmSync(dropped, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
    }
  }

  // reordered + extended SUPERSET ⇒ PASSES (whole-token membership, not exact compare)
  const reordered = freshTarget("claudeverify-reordered");
  try {
    install(reordered, "claude");
    const settingsPath = path.join(reordered, ".claude", "settings.json");
    writeMatcher(settingsPath, "Task|Bash|Read|Write|Edit|Skill|NewTool");
    let threw = false;
    try { verifyClaudeWiring(reordered, settingsPath); } catch { threw = true; }
    check("[claudeverify] reordered/extended matcher superset still PASSES (no brittle exact compare)", !threw);
  } finally {
    fs.rmSync(reordered, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // empty matcher on OUR group ⇒ THROWS (fail-closed: coverage is not provable)
  const emptyMatcher = freshTarget("claudeverify-emptymatcher");
  try {
    install(emptyMatcher, "claude");
    const settingsPath = path.join(emptyMatcher, ".claude", "settings.json");
    writeMatcher(settingsPath, "");
    let threw = false;
    try { verifyClaudeWiring(emptyMatcher, settingsPath); } catch { threw = true; }
    check("[claudeverify] empty matcher on our group: verify THROWS (fail-closed)", threw);
  } finally {
    fs.rmSync(emptyMatcher, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── hasGitRepo — the CLI's no-repo warning predicate ─────────────────────────
{
  const target = freshTarget("gitcheck");
  try {
    check("[git] repo-less target detected", hasGitRepo(target) === false);
    fs.mkdirSync(path.join(target, ".git"), { recursive: true });
    check("[git] .git presence detected", hasGitRepo(target) === true);
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── ensureConfig: honest write-vs-kept reporting + no orchestrator model pin ──
// (docs/decisions/multi-model-setup-ux.md papercuts 9 + 2). The return flag is what
// lets the install summary print the truth instead of an unconditional "wrote config".
{
  const target = freshTarget("ensureconfig");
  try {
    fs.mkdirSync(path.join(target, ".ai-dev"), { recursive: true });
    const wroteFresh = ensureConfig(target, "claude");
    check("[ensureConfig] returns TRUE on a fresh write", wroteFresh === true);
    const cfg = JSON.parse(fs.readFileSync(path.join(target, ".ai-dev", "config.json"), "utf8"));
    check("[ensureConfig] default carries NO roles.orchestrator.model (papercut 2)",
      !("model" in cfg.roles.orchestrator));
    check("[ensureConfig] default still pins the reviewer to auto", cfg.roles.reviewer.model === "auto");

    // A second call must NOT clobber and must report the no-op honestly.
    fs.writeFileSync(path.join(target, ".ai-dev", "config.json"), '{"platform":"claude","mode":"autonomous"}\n');
    const wroteAgain = ensureConfig(target, "claude");
    check("[ensureConfig] returns FALSE when a config already exists (kept)", wroteAgain === false);
    const kept = JSON.parse(fs.readFileSync(path.join(target, ".ai-dev", "config.json"), "utf8"));
    check("[ensureConfig] existing config is untouched (no clobber)", kept.mode === "autonomous");
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── ensureConfig: BACKWARD-COMPAT backfill of a newly-added seat (the planner) ──────
// A config that predates a new SPAWNABLE seat lacks `roles.planner`; without a heal the
// next install's bake throws `roles.planner.agent is missing`. ensureConfig must
// additively fill the missing seat, preserve every user value, and stay return-FALSE
// (the config existed). RED before the backfill (install() threw); GREEN after.
{
  const target = freshTarget("planner-backfill-unit");
  try {
    fs.mkdirSync(path.join(target, ".ai-dev"), { recursive: true });
    // An OLD config: no planner seat, a user builder MODEL pin, a non-default mode.
    const old = { mode: "autonomous", profile: "lite", platform: "claude", kind: "mixed",
      roles: { orchestrator: { agent: "ai-dev" }, builder: { agent: "dev-builder", model: "sonnet" }, reviewer: { agent: "dev-reviewer", model: "auto" } } };
    fs.writeFileSync(path.join(target, ".ai-dev", "config.json"), JSON.stringify(old, null, 2) + "\n");
    const ret = ensureConfig(target, "claude");
    check("[ensureConfig] backfill returns FALSE (config EXISTED — papercut 9)", ret === false);
    const healed = JSON.parse(fs.readFileSync(path.join(target, ".ai-dev", "config.json"), "utf8"));
    check("[ensureConfig] backfill adds the missing planner seat", healed.roles.planner && healed.roles.planner.agent === "dev-planner");
    check("[ensureConfig] backfill NEVER overwrites a user's builder model pin", healed.roles.builder.model === "sonnet");
    check("[ensureConfig] backfill preserves the existing reviewer seat", healed.roles.reviewer.model === "auto");
    check("[ensureConfig] backfill preserves other user values (mode/profile/kind)",
      healed.mode === "autonomous" && healed.profile === "lite" && healed.kind === "mixed");
    // Idempotent: a second call finds nothing missing and leaves the file byte-identical.
    const before = fs.readFileSync(path.join(target, ".ai-dev", "config.json"), "utf8");
    const ret2 = ensureConfig(target, "claude");
    check("[ensureConfig] second call is a no-op (nothing missing) and still FALSE", ret2 === false);
    check("[ensureConfig] second call leaves the file byte-identical", fs.readFileSync(path.join(target, ".ai-dev", "config.json"), "utf8") === before);
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// End-to-end: the REAL downstream upgrade path. A pre-existing planner-less config →
// full install() must NOT throw (ensureConfig heals before wireClaude bakes) and the
// planner agent must actually assemble.
{
  const target = freshTarget("planner-backfill-e2e");
  try {
    fs.mkdirSync(path.join(target, ".ai-dev"), { recursive: true });
    const old = { mode: "interactive", profile: "solo", platform: "claude", kind: "code",
      roles: { orchestrator: { agent: "ai-dev" }, builder: { agent: "dev-builder" }, reviewer: { agent: "dev-reviewer", model: "auto" } } };
    fs.writeFileSync(path.join(target, ".ai-dev", "config.json"), JSON.stringify(old, null, 2) + "\n");
    let threw = false;
    try { install(target, "claude"); } catch { threw = true; }
    check("[backfill-e2e] a planner-less config no longer throws on install (bake heals first)", threw === false);
    check("[backfill-e2e] the planner agent assembled after the heal", fs.existsSync(path.join(target, ".claude", "agents", "dev-planner.md")));
    const healed = JSON.parse(fs.readFileSync(path.join(target, ".ai-dev", "config.json"), "utf8"));
    check("[backfill-e2e] the healed config carries the planner seat", healed.roles.planner.agent === "dev-planner");
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── launch-time models → settings.json `env` (the wrapper-less auto-apply) ──────────
// The installer writes config.launch.{sessionModel,guardModel} into .claude/settings.json
// `env` (read by Claude Code at startup), touching ONLY our two keys and never clobbering
// a user-set env key. Pure-unit the merge (mergeLaunchEnv) + end-to-end the write through
// install(). Source: docs/decisions/multi-model-setup-ux.md `## Requirement 7`.

// Pure merge — both launch values populate ANTHROPIC_MODEL / ANTHROPIC_SMALL_FAST_MODEL.
{
  const env = mergeLaunchEnv(undefined, { sessionModel: "glm-5.2", guardModel: "deepseek-v4" });
  check("[launch-env] sessionModel → ANTHROPIC_MODEL", env.ANTHROPIC_MODEL === "glm-5.2");
  check("[launch-env] guardModel → ANTHROPIC_SMALL_FAST_MODEL (G1, deprecated var)", env.ANTHROPIC_SMALL_FAST_MODEL === "deepseek-v4");
}
// Empty/absent launch ⇒ NULL (no env key written at all) when there was no pre-existing env.
check("[launch-env] empty launch + no prior env ⇒ null (byte-unchanged project)", mergeLaunchEnv(undefined, {}) === null);
check("[launch-env] absent launch (undefined) ⇒ null", mergeLaunchEnv(undefined, undefined) === null);
check("[launch-env] whitespace-only values are treated as empty ⇒ null", mergeLaunchEnv(undefined, { sessionModel: "  ", guardModel: "" }) === null);
// Our keys only — a user-set foreign env key is never clobbered.
{
  const env = mergeLaunchEnv({ MY_OWN: "keep", ANTHROPIC_BASE_URL: "http://proxy" }, { sessionModel: "glm-5.2", guardModel: "" });
  check("[launch-env] foreign env key preserved", env.MY_OWN === "keep");
  check("[launch-env] user's ANTHROPIC_BASE_URL preserved (out of our scope)", env.ANTHROPIC_BASE_URL === "http://proxy");
  check("[launch-env] our session key set alongside foreign keys", env.ANTHROPIC_MODEL === "glm-5.2");
  check("[launch-env] empty guard prunes our guard key (not written)", !("ANTHROPIC_SMALL_FAST_MODEL" in env));
}
// Idempotent prune — a previously-written key is removed when its config value clears,
// without disturbing a foreign key (so a routed→non-routed flip leaves no stale env).
{
  const env = mergeLaunchEnv({ ANTHROPIC_MODEL: "stale", ANTHROPIC_SMALL_FAST_MODEL: "stale", MY_OWN: "keep" }, {});
  check("[launch-env] clearing launch prunes both our stale keys", !("ANTHROPIC_MODEL" in env) && !("ANTHROPIC_SMALL_FAST_MODEL" in env));
  check("[launch-env] prune keeps foreign keys ⇒ env object retained (not null)", env.MY_OWN === "keep");
}
// Tier-alias bindings (nested launch.aliases) → ANTHROPIC_DEFAULT_*_MODEL, same write+prune.
{
  const env = mergeLaunchEnv(undefined, { aliases: { opus: "claude-opus-4-8", sonnet: "glm-4.6", haiku: "deepseek-chat" } });
  check("[launch-env] aliases.opus → ANTHROPIC_DEFAULT_OPUS_MODEL", env.ANTHROPIC_DEFAULT_OPUS_MODEL === "claude-opus-4-8");
  check("[launch-env] aliases.sonnet → ANTHROPIC_DEFAULT_SONNET_MODEL", env.ANTHROPIC_DEFAULT_SONNET_MODEL === "glm-4.6");
  check("[launch-env] aliases.haiku → ANTHROPIC_DEFAULT_HAIKU_MODEL", env.ANTHROPIC_DEFAULT_HAIKU_MODEL === "deepseek-chat");
}
// Empty/whitespace alias tiers prune their key; a foreign ANTHROPIC_DEFAULT_* is OURS to manage.
{
  const env = mergeLaunchEnv({ ANTHROPIC_DEFAULT_SONNET_MODEL: "stale", MY_OWN: "keep" }, { aliases: { sonnet: "", haiku: "  " } });
  check("[launch-env] clearing an alias tier prunes its stale key", !("ANTHROPIC_DEFAULT_SONNET_MODEL" in env));
  check("[launch-env] alias prune keeps foreign keys", env.MY_OWN === "keep");
}
// Absent aliases section ⇒ no alias keys written (byte-unchanged for a non-routing project).
check("[launch-env] absent aliases ⇒ null (no env)", mergeLaunchEnv(undefined, { aliases: {} }) === null);
check("[launch-env] only aliases set ⇒ env carries just the alias keys", (() => {
  const env = mergeLaunchEnv(undefined, { aliases: { sonnet: "glm-4.6" } });
  return env && env.ANTHROPIC_DEFAULT_SONNET_MODEL === "glm-4.6" && !("ANTHROPIC_MODEL" in env);
})());
// Non-object aliases (a malformed value) ⇒ ignored, no alias keys, no crash (fail-safe).
check("[launch-env] non-object aliases ⇒ ignored (null, no env)", mergeLaunchEnv(undefined, { aliases: "nope" }) === null);
check("[launch-env] non-object aliases ⇒ no alias key written", (() => {
  const env = mergeLaunchEnv({ MY_OWN: "keep" }, { aliases: 42 });
  return env.MY_OWN === "keep" && !("ANTHROPIC_DEFAULT_SONNET_MODEL" in env);
})());

// End-to-end: install() with a populated launch section writes the env into settings.json;
// a non-routing install leaves NO env block.
{
  const target = freshTarget("launchenv-e2e");
  try {
    install(target, "claude");
    const settingsPath = path.join(target, ".claude", "settings.json");
    // Default config has an empty launch section ⇒ no env block (byte-unchanged class).
    const sDefault = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    check("[launch-env e2e] default (empty launch) writes NO env block", sDefault.env === undefined);

    const cfgPath = path.join(target, ".ai-dev", "config.json");
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
    cfg.launch = { sessionModel: "glm-5.2", guardModel: "deepseek-v4" };
    fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2) + "\n");
    // seed a user env key to prove it survives the re-install
    const sSeed = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    sSeed.env = { ...(sSeed.env || {}), USER_SET: "keep" };
    fs.writeFileSync(settingsPath, JSON.stringify(sSeed, null, 2) + "\n");
    install(target, "claude");
    const s1 = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    check("[launch-env e2e] populated launch writes ANTHROPIC_MODEL into env", s1.env.ANTHROPIC_MODEL === "glm-5.2");
    check("[launch-env e2e] populated launch writes ANTHROPIC_SMALL_FAST_MODEL into env", s1.env.ANTHROPIC_SMALL_FAST_MODEL === "deepseek-v4");
    check("[launch-env e2e] user-set env key survives the re-install", s1.env.USER_SET === "keep");

    // Clearing the launch section prunes our keys on the next install (no stale env).
    const cfg2 = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
    cfg2.launch = { sessionModel: "", guardModel: "" };
    fs.writeFileSync(cfgPath, JSON.stringify(cfg2, null, 2) + "\n");
    install(target, "claude");
    const s2 = JSON.parse(fs.readFileSync(path.join(target, ".claude", "settings.json"), "utf8"));
    check("[launch-env e2e] cleared launch prunes our keys", s2.env && !("ANTHROPIC_MODEL" in s2.env) && !("ANTHROPIC_SMALL_FAST_MODEL" in s2.env));
    check("[launch-env e2e] clearing leaves the user key intact", s2.env.USER_SET === "keep");
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// NIT (cosmetic): when `env` held ONLY our launch keys (no foreign key), clearing the
// launch section must REMOVE the `env` key entirely — never leave a cosmetic `env: {}`.
{
  const target = freshTarget("launchenv-onlyours");
  try {
    const cfgPath = path.join(target, ".ai-dev", "config.json");
    const settingsPath = path.join(target, ".claude", "settings.json");
    install(target, "claude");
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
    cfg.launch = { sessionModel: "glm-5.2", guardModel: "deepseek-v4" };
    fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2) + "\n");
    install(target, "claude"); // env now holds ONLY our two keys (no foreign key seeded)
    const cfg2 = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
    cfg2.launch = { sessionModel: "", guardModel: "" };
    fs.writeFileSync(cfgPath, JSON.stringify(cfg2, null, 2) + "\n");
    install(target, "claude"); // cleared → our keys pruned → was only-ours ⇒ drop `env`
    const sFinal = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    check("[launch-env e2e] env held only our keys + cleared ⇒ `env` key removed (no cosmetic {})", sFinal.env === undefined);
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── SessionStart inject — the crash-resume FIRST-action backstop ──────────────
// SessionStart is a pure inject (no engine evaluation): the shim short-circuits it and emits
// the fixed reminder that reading `.ai-dev/state/current.md` is the FIRST action. Fires on
// every source (startup/resume/clear/compact) — the recovery cases. RED without the shim's
// SessionStart branch (the event would fall through to decide(), normalise returns null for
// it, no output). Driven against the REAL shim (SessionStart carries no act, so no fixture is
// needed — it never reaches config/deny-rules).
{
  const shim = path.join(ROOT, "src", "adapter", "claude", "shim.mjs");
  for (const source of ["startup", "resume", "clear", "compact"]) {
    const r = spawnSync("node", [shim], {
      input: JSON.stringify({ hook_event_name: "SessionStart", source, cwd: ROOT }),
      encoding: "utf8",
    });
    let out = null;
    try { out = JSON.parse(r.stdout); } catch { /* not JSON → leave out null */ }
    check(`[session-start] ${source}: shim emits a SessionStart inject`, !!out && out.hookSpecificOutput?.hookEventName === "SessionStart");
    check(`[session-start] ${source}: inject names the pointer path + the FIRST action`,
      !!out && typeof out.hookSpecificOutput?.additionalContext === "string"
        && out.hookSpecificOutput.additionalContext.includes(".ai-dev/state/current.md")
        && /FIRST action/i.test(out.hookSpecificOutput.additionalContext));
  }
  // A UserPromptSubmit payload is NOT hijacked by the SessionStart branch (it still reaches
  // the engine): confirms the short-circuit is scoped to SessionStart only.
  {
    const r = spawnSync("node", [shim], {
      input: JSON.stringify({ hook_event_name: "UserPromptSubmit", prompt: "good morning", cwd: ROOT }),
      encoding: "utf8",
    });
    let out = null;
    try { out = JSON.parse(r.stdout); } catch { /* not JSON → leave out null */ }
    check("[session-start] a UserPromptSubmit payload is NOT hijacked by the SessionStart branch (event name stays UserPromptSubmit, not SessionStart)",
      !out || out.hookSpecificOutput?.hookEventName !== "SessionStart");
  }
}

// ── PostToolUse / Task checkpoint inject ─────────────────────────────────────
// PostToolUse is a pure inject (no engine evaluation): the shim emits the checkpoint
// nudge after a Task-tool return (a spawned-role handoff = the canonical significant
// step the crash-resume checkpoint rule names). Scoped to Task only (matcher + defensive
// tool-name guard). Claude-only mechanical; OpenCode is persona-only (inject-class was
// dropped — M18). Sibling of the SessionStart inject family.
{
  const shim = path.join(ROOT, "src", "adapter", "claude", "shim.mjs");

  // PostToolUse + Task → emits a PostToolUse checkpoint inject
  {
    const r = spawnSync("node", [shim], {
      input: JSON.stringify({
        hook_event_name: "PostToolUse",
        tool_name: "Task",
        tool_input: { subagent_type: "dev-builder" },
        tool_response: { output: "done" },
        cwd: ROOT,
      }),
      encoding: "utf8",
    });
    let out = null;
    try { out = JSON.parse(r.stdout); } catch { /* not JSON → leave out null */ }
    check("[checkpoint-inject] PostToolUse+Task: shim emits a PostToolUse inject",
      !!out && out.hookSpecificOutput?.hookEventName === "PostToolUse");
    check("[checkpoint-inject] PostToolUse+Task: inject additionalContext mentions plan progress note",
      !!out && typeof out.hookSpecificOutput?.additionalContext === "string"
        && /progress note/i.test(out.hookSpecificOutput.additionalContext));
  }

  // PostToolUse + non-Task (e.g., Bash) → no output (silent no-op)
  {
    const r = spawnSync("node", [shim], {
      input: JSON.stringify({
        hook_event_name: "PostToolUse",
        tool_name: "Bash",
        tool_input: { command: "ls" },
        tool_response: { output: "file.txt" },
        cwd: ROOT,
      }),
      encoding: "utf8",
    });
    check("[checkpoint-inject] PostToolUse+non-Task (Bash): no output (no nudge)",
      r.stdout.trim() === "");
  }

  // PreToolUse / UserPromptSubmit / SessionStart payloads are NOT hijacked by the
  // PostToolUse branch — the branch is scoped to hook_event_name === "PostToolUse" only.
  for (const [label, payload] of [
    ["PreToolUse+Task", { hook_event_name: "PreToolUse", tool_name: "Task", tool_input: { subagent_type: "dev-builder" }, cwd: ROOT }],
    ["UserPromptSubmit", { hook_event_name: "UserPromptSubmit", prompt: "good morning", cwd: ROOT }],
    ["SessionStart", { hook_event_name: "SessionStart", source: "startup", cwd: ROOT }],
  ]) {
    const r = spawnSync("node", [shim], { input: JSON.stringify(payload), encoding: "utf8" });
    let out = null;
    try { out = JSON.parse(r.stdout); } catch { /* ... */ }
    check(`[checkpoint-inject] ${label} payload is NOT treated as a PostToolUse inject`,
      !out || out.hookSpecificOutput?.hookEventName !== "PostToolUse");
  }
}

// ── PostToolUse hook wired into settings.json after install ───────────────────
// After a Claude install, .claude/settings.json carries the PostToolUse Task hook
// referencing the shim — the checkpoint nudge's mechanical wiring.
{
  const cli = path.join(ROOT, "src", "adapter", "install.mjs");
  const target = freshTarget("post-tool-use-hook");
  try {
    const r = spawnSync("node", [cli, target, "--platform", "claude"], { encoding: "utf8" });
    check("[post-tool-use-wiring] install exits 0", r.status === 0);
    const settings = JSON.parse(fs.readFileSync(path.join(target, ".claude", "settings.json"), "utf8"));
    const postGroups = (settings.hooks && settings.hooks.PostToolUse) || [];
    const shimCommand = postGroups
      .flatMap((g) => (g.hooks || []).map((h) => (typeof h.command === "string" ? h.command : "")))
      .find((c) => c.includes("shim.mjs"));
    check("[post-tool-use-wiring] settings.json carries a PostToolUse hook referencing the shim",
      !!shimCommand);
    // The shim-routing group's matcher covers Task
    const taskMatcher = postGroups
      .filter((g) => (g.hooks || []).some((h) => typeof h.command === "string" && h.command.includes("shim.mjs")))
      .map((g) => g.matcher)
      .find((m) => typeof m === "string" && /\bTask\b/.test(m));
    check("[post-tool-use-wiring] PostToolUse hook matcher covers Task", !!taskMatcher);
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── Workflow tool deny by default (#368) ──────────────────────────────────────
// The Claude install writes permissions.deny:["Workflow"] so CC drops the built-in
// Workflow tool's ~5k-token schema from the resident prompt (the protocol orchestrates
// via Agent, never Workflow). The merge is idempotent and preserves a user's own denies.
{
  const cli = path.join(ROOT, "src", "adapter", "install.mjs");
  const readDeny = (t) => {
    const s = JSON.parse(fs.readFileSync(path.join(t, ".claude", "settings.json"), "utf8"));
    return (s.permissions && Array.isArray(s.permissions.deny)) ? s.permissions.deny : [];
  };

  // Fresh install ⇒ Workflow denied by default.
  const fresh = freshTarget("workflow-deny-fresh");
  spawnSync("node", [cli, fresh, "--platform", "claude"], { encoding: "utf8" });
  check("[workflow-deny] fresh install denies Workflow by default", readDeny(fresh).includes("Workflow"));
  check("[workflow-deny] does NOT deny Agent/Task or the dev tools",
    !readDeny(fresh).some((d) => ["Agent", "Task", "Read", "Edit", "Write", "Bash", "Grep", "Glob"].includes(d)));

  // A pre-existing user deny is PRESERVED and Workflow appended; a second install is
  // idempotent (no duplicate Workflow entry, user deny still there).
  const preseed = freshTarget("workflow-deny-preseed");
  fs.mkdirSync(path.join(preseed, ".claude"), { recursive: true });
  fs.writeFileSync(path.join(preseed, ".claude", "settings.json"),
    JSON.stringify({ permissions: { allow: ["Bash(ls *)"], deny: ["SomeUserTool"] } }, null, 2) + "\n");
  spawnSync("node", [cli, preseed, "--platform", "claude"], { encoding: "utf8" });
  const preseedSettings = JSON.parse(fs.readFileSync(path.join(preseed, ".claude", "settings.json"), "utf8"));
  check("[workflow-deny] pre-existing user deny preserved", readDeny(preseed).includes("SomeUserTool"));
  check("[workflow-deny] pre-existing permissions.allow preserved (whole object not clobbered)",
    Array.isArray(preseedSettings.permissions.allow) && preseedSettings.permissions.allow.includes("Bash(ls *)"));
  check("[workflow-deny] Workflow appended alongside the user deny", readDeny(preseed).includes("Workflow"));
  spawnSync("node", [cli, preseed, "--platform", "claude"], { encoding: "utf8" });
  const twice = readDeny(preseed);
  check("[workflow-deny] idempotent — single Workflow entry after a second install",
    twice.filter((d) => d === "Workflow").length === 1 && twice.includes("SomeUserTool"));

  // Coherence guard: no assembled agent's `tools:` frontmatter lists Workflow — the
  // deny must never contradict a tool the protocol actually grants a role.
  const agentsDir = path.join(fresh, ".claude", "agents");
  const usesWorkflow = fs.existsSync(agentsDir) && fs.readdirSync(agentsDir)
    .filter((f) => f.endsWith(".md"))
    .some((f) => /^tools:.*\bWorkflow\b/m.test(fs.readFileSync(path.join(agentsDir, f), "utf8")));
  check("[workflow-deny] no assembled agent grants the Workflow tool (deny is coherent)", !usesWorkflow);
}

report("INSTALL-CLAUDE-WIRING", "PASS — Claude wiring self-verify, ensureConfig, and launch-env all hold");
