// Installer test — platform-agnostic install behaviour. One of the two cohesive halves
// the former monolithic install.test.mjs was split into (audit LOW-2); the Claude-wiring
// self-verify + ensureConfig + launch-env half lives in install-claude-wiring.test.mjs,
// the shared helpers in install-shared.mjs. Guards the one-command-install contract
// (docs/contracts/one-command-install.md): the protocol installs into a temp target dir
// created INSIDE the repo root (the out-of-root deny blocks an external sibling, which is
// exactly why the target sits inside the root), then asserts the adapter is vendored
// (self-sufficiently — the vendored installer can re-run), both platforms are wired, the
// version is stamped (with the UPGRADING handoff marker on a version change), the inactive
// platform gets its breadcrumb, the F3 pre-push hook installs/never-clobbers/idempotent,
// the loud version banner fires, and a SECOND run is idempotent (identical tree, no
// duplication, stale ai-dev hook groups replaced).
//
// Run: node src/adapter/install-core.test.mjs

import { install } from "./install.mjs";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { isStaleNpxReRun } from "./install.mjs";
import { ROOT, PKG_VERSION, makeChecker, snapshot, freshTarget, registerStragglerSweep } from "./install-shared.mjs";

const { check, report } = makeChecker();
registerStragglerSweep();

// ── Claude install ────────────────────────────────────────────────────────────
function testPlatform(platform, assertWiring) {
  const target = freshTarget(platform);
  try {
    const resolved = install(target, platform);
    check(`[${platform}] install returns the resolved platform`, resolved === platform);

    // 1. adapter vendored
    check(`[${platform}] adapter engine vendored`, fs.existsSync(path.join(target, ".ai-dev", "tooling", "src", "adapter", "engine.mjs")));
    check(`[${platform}] deny-rules vendored`, fs.existsSync(path.join(target, ".ai-dev", "tooling", "src", "adapter", "deny-rules.json")));
    check(`[${platform}] neutral role bodies vendored beside the adapter`, fs.existsSync(path.join(target, ".ai-dev", "tooling", "src", "agents", "builder.md")));
    check(`[${platform}] modules registry vendored`, fs.existsSync(path.join(target, ".ai-dev", "tooling", "src", "modules", "registry.json")));
    check(`[${platform}] node_modules NOT vendored`, !fs.existsSync(path.join(target, ".ai-dev", "tooling", "src", "adapter", "node_modules")));

    // 2. core laid down
    check(`[${platform}] PROTOCOL.md laid down at .ai-dev/PROTOCOL.md`, fs.existsSync(path.join(target, ".ai-dev", "PROTOCOL.md")));
    check(`[${platform}] PROTOCOL.md NOT at project root`, !fs.existsSync(path.join(target, "PROTOCOL.md")));
    check(`[${platform}] quality runner laid down at .ai-dev/quality/`, fs.existsSync(path.join(target, ".ai-dev", "quality", "run.mjs")));
    check(`[${platform}] src/agents/ NOT at project root`, !fs.existsSync(path.join(target, "src", "agents")));
    check(`[${platform}] docs/ NOT created by installer`, !fs.existsSync(path.join(target, "docs")));

    // 2d. on-demand procedure bodies deployed to a READ-ALLOWED home. The latent bug
    // this guards: a procedure vendored ONLY under .ai-dev/tooling/ (read-denied,
    // invariant 2) is unreadable by the orchestrator whose own trigger names it. The
    // fix deploys a readable copy at .ai-dev/procedures/<x>.md (the stable read path in
    // both dogfood and downstream), byte-identical to its src/agents/procedures/ source.
    const deployedProc = path.join(target, ".ai-dev", "procedures", "parallel-work.md");
    check(`[${platform}] on-demand procedure deployed at the READABLE .ai-dev/procedures/`, fs.existsSync(deployedProc));
    check(
      `[${platform}] deployed procedure path is OUTSIDE the read-denied .ai-dev/tooling/`,
      !path.relative(target, deployedProc).split(path.sep).includes("tooling"),
    );
    check(
      `[${platform}] deployed procedure is byte-identical to its src/agents/procedures/ source`,
      fs.existsSync(deployedProc)
        && fs.readFileSync(deployedProc, "utf8") === fs.readFileSync(path.join(ROOT, "src", "agents", "procedures", "parallel-work.md"), "utf8"),
    );
    // 2e. runtime-read module bodies deployed to the same READ-ALLOWED home. Same latent
    // bug as the procedures above: the elicitation catalog vendored ONLY under
    // .ai-dev/tooling/ (read-denied, invariant 2) is unreadable by the elicitation
    // side-tool / builder plan-round whose own reference names it. The fix deploys a
    // readable copy at .ai-dev/modules/elicitation/catalog.md, byte-identical to source.
    // Guards against a regression dropping deployModules from the downstream branch.
    const deployedMod = path.join(target, ".ai-dev", "modules", "elicitation", "catalog.md");
    check(`[${platform}] runtime-read module deployed at the READABLE .ai-dev/modules/`, fs.existsSync(deployedMod));
    check(
      `[${platform}] deployed module path is OUTSIDE the read-denied .ai-dev/tooling/`,
      !path.relative(target, deployedMod).split(path.sep).includes("tooling"),
    );
    check(
      `[${platform}] deployed module is byte-identical to its src/modules/ source`,
      fs.existsSync(deployedMod)
        && fs.readFileSync(deployedMod, "utf8") === fs.readFileSync(path.join(ROOT, "src", "modules", "elicitation", "catalog.md"), "utf8"),
    );
    // the quality SHAPE, not this repo's own tool rows
    const tools = JSON.parse(fs.readFileSync(path.join(target, ".ai-dev", "quality", "tools.json"), "utf8"));
    check(`[${platform}] quality registry is the template shape, not this repo's rows`, tools.tools.length === 1 && tools.tools[0].id === "example-lint");

    // 2b. version stamp + migration notes (the upgrade channel's mechanical half)
    check(`[${platform}] .ai-dev/VERSION stamped with the source version`, fs.readFileSync(path.join(target, ".ai-dev", "VERSION"), "utf8").trim() === PKG_VERSION);
    check(`[${platform}] tooling carries its own VERSION`, fs.readFileSync(path.join(target, ".ai-dev", "tooling", "VERSION"), "utf8").trim() === PKG_VERSION);
    check(`[${platform}] migration notes laid down at .ai-dev/upgrades.md`, fs.existsSync(path.join(target, ".ai-dev", "upgrades.md")));
    check(`[${platform}] no UPGRADING marker on a fresh install`, !fs.existsSync(path.join(target, ".ai-dev", "UPGRADING.md")));

    // 2c. cross-platform breadcrumb: the INACTIVE platform's load surface exists
    const inactiveFile = platform === "claude" ? "AGENTS.md" : "CLAUDE.md";
    const inactivePlatform = platform === "claude" ? "opencode" : "claude";
    const bc = fs.readFileSync(path.join(target, inactiveFile), "utf8");
    check(`[${platform}] inactive-platform breadcrumb written to ${inactiveFile}`, bc.includes("ai-dev:breadcrumb") && bc.includes(`--platform ${inactivePlatform}`));

    // 3. config present inside .ai-dev/
    check(`[${platform}] config written at .ai-dev/config.json with the resolved platform`, JSON.parse(fs.readFileSync(path.join(target, ".ai-dev", "config.json"), "utf8")).platform === platform);

    // 3b. .gitignore excludes the local-only transients (state + raw feedback reports
    // + the personal launch override config.local.json + the personal routes override
    // model-routes.local.json)
    const gi = fs.existsSync(path.join(target, ".gitignore")) ? fs.readFileSync(path.join(target, ".gitignore"), "utf8") : "";
    check(`[${platform}] .gitignore excludes .ai-dev/state/`, gi.includes(".ai-dev/state/"));
    check(`[${platform}] .gitignore excludes .ai-dev/feedback/`, gi.includes(".ai-dev/feedback/"));
    check(`[${platform}] .gitignore excludes .ai-dev/worktrees/`, gi.includes(".ai-dev/worktrees/"));
    check(`[${platform}] .gitignore excludes the personal .ai-dev/config.local.json`, gi.includes(".ai-dev/config.local.json"));
    check(`[${platform}] .gitignore excludes the personal .ai-dev/model-routes.local.json`, gi.includes(".ai-dev/model-routes.local.json"));

    // 3e. the OPTIONAL project launcher .ai-dev/launch — generated ALWAYS (so enabling
    // routing/configDir later needs no re-install), platform-shaped, and executable.
    const launchPath = path.join(target, ".ai-dev", "launch");
    check(`[${platform}] .ai-dev/launch generated`, fs.existsSync(launchPath));
    if (fs.existsSync(launchPath)) {
      const lmode = fs.statSync(launchPath).mode;
      check(`[${platform}] .ai-dev/launch is executable (owner +x)`, (lmode & 0o100) !== 0);
    }

    // 3c. the gitignored session-state dir is CREATED, not just ignored (the
    // orchestrator's first current.md write expects the parent to exist).
    const stateDir = path.join(target, ".ai-dev", "state");
    check(`[${platform}] .ai-dev/state/ created`, fs.existsSync(stateDir) && fs.statSync(stateDir).isDirectory());

    // 3e. .ai-dev/notes/ is created and seeded with a README — the committed durable-
    // notes home (#316). Notes are NOT gitignored (they are team-shared knowledge);
    // the README seed is written only where absent (a re-run never clobbers it).
    const notesDir = path.join(target, ".ai-dev", "notes");
    check(`[${platform}] .ai-dev/notes/ created`, fs.existsSync(notesDir) && fs.statSync(notesDir).isDirectory());
    const notesReadme = path.join(notesDir, "README.md");
    check(`[${platform}] .ai-dev/notes/README.md seeded`, fs.existsSync(notesReadme));
    if (fs.existsSync(notesReadme)) {
      const readme = fs.readFileSync(notesReadme, "utf8");
      check(`[${platform}] notes README explains the taxonomy`, readme.includes("checkpoint") && readme.includes("durable notes"));
    }

    // 3d. DOWNSTREAM-LAYOUT regression guard (8D quality-gate-silent-bypass): the
    // laid-down runner must LOCATE the laid-down registry beside it. The temp
    // target has no src/quality/ — exactly the layout where the old root-relative
    // default failed open. `ship` matches zero template rows, so nothing executes;
    // "no ship-beat tools" means the registry was found and zero matched, while
    // "no tools.json" would mean the registry was never located (the bug signature).
    const runner = path.join(target, ".ai-dev", "quality", "run.mjs");
    const qr = spawnSync("node", [runner, "ship"], { encoding: "utf8" });
    const qrOut = (qr.stdout || "") + (qr.stderr || "");
    check(`[${platform}] laid-down runner LOCATES the laid-down registry (no ship-beat tools)`, qrOut.includes("no ship-beat tools"));
    check(`[${platform}] runner does NOT report a missing registry (the silent-bypass signature)`, !qrOut.includes("no tools.json"));

    // 4. platform wiring
    assertWiring(target);

    // 5. IDEMPOTENCE — a second run yields an identical tree
    const before = snapshot(target);
    install(target, platform);
    const after = snapshot(target);
    const beforeKeys = Object.keys(before).sort();
    const afterKeys = Object.keys(after).sort();
    check(`[${platform}] second run adds/removes no files`, JSON.stringify(beforeKeys) === JSON.stringify(afterKeys));
    const allIdentical = beforeKeys.every((k) => before[k] === after[k]);
    check(`[${platform}] second run leaves every file byte-identical`, allIdentical);
    check(`[${platform}] same-version re-run writes no UPGRADING marker`, !fs.existsSync(path.join(target, ".ai-dev", "UPGRADING.md")));
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

testPlatform("claude", (target) => {
  check("[claude] agents assembled", fs.existsSync(path.join(target, ".claude", "agents", "dev-planner.md")) && fs.existsSync(path.join(target, ".claude", "agents", "dev-builder.md")) && fs.existsSync(path.join(target, ".claude", "agents", "dev-reviewer.md")));
  // the orchestrator load surface lands OUTSIDE .claude/agents/ (it is the session's
  // @imported instructions, not a spawnable subagent — a file in agents/ would auto-register).
  check("[claude] orchestrator load surface assembled at .claude/ai-dev.md", fs.existsSync(path.join(target, ".claude", "ai-dev.md")));
  check("[claude] orchestrator is NOT registered as a spawnable subagent", !fs.existsSync(path.join(target, ".claude", "agents", "ai-dev.md")));
  // the surface is the platform:claude-FILTERED body — the inactive opencode caveat is gone.
  const orch = fs.readFileSync(path.join(target, ".claude", "ai-dev.md"), "utf8");
  check("[claude] orchestrator surface drops the platform:opencode block", !orch.includes("no cross-model reviewer is realisable") && !orch.includes("platform:opencode"));
  check("[claude] orchestrator surface keeps the neutral floor", orch.includes("Read `.ai-dev/config.json` `roles` for the seat"));
  check("[claude] /dev-setup command assembled", fs.existsSync(path.join(target, ".claude", "commands", "dev-setup.md")));
  const settings = JSON.parse(fs.readFileSync(path.join(target, ".claude", "settings.json"), "utf8"));
  const cmds = (settings.hooks.PreToolUse || []).flatMap((g) => g.hooks.map((h) => h.command));
  check("[claude] deny hook wired into settings.json", cmds.some((c) => c.includes("shim.mjs")));
  // idempotence of the hook merge: exactly one PreToolUse + one UserPromptSubmit group
  check("[claude] hook merge did not duplicate (one PreToolUse group)", settings.hooks.PreToolUse.length === 1);
  // .ai-dev/launch wraps the router-launch engine at the DOWNSTREAM (vendored) path,
  // passing claude args through; no shell-injection surface (exec ... "$@", argv passthrough).
  const launch = fs.readFileSync(path.join(target, ".ai-dev", "launch"), "utf8");
  check("[claude] .ai-dev/launch wraps the vendored router-launch engine", launch.includes('exec node ".ai-dev/tooling/src/adapter/router-launch.mjs" "$@"'));
  check("[claude] .ai-dev/launch carries no sh -c untrusted-eval surface", !launch.includes("sh -c"));
  const claudeMd = fs.readFileSync(path.join(target, "CLAUDE.md"), "utf8");
  check("[claude] CLAUDE.md imports the constitution + the filtered orchestrator surface", claudeMd.includes("@.ai-dev/PROTOCOL.md") && claudeMd.includes("@.claude/ai-dev.md"));
  // the raw orchestrator import is REPLACED, never left dangling beside the new one.
  check("[claude] CLAUDE.md carries no stale raw-orchestrator import", !claudeMd.includes("@.ai-dev/tooling/src/agents/orchestrator.md") && !claudeMd.includes("@src/agents/orchestrator.md"));
  // import appears exactly once (idempotent line append)
  check("[claude] constitution import appears once", claudeMd.split("@.ai-dev/PROTOCOL.md").length - 1 === 1);
});

testPlatform("opencode", (target) => {
  check("[opencode] four agents assembled", ["ai-dev", "dev-planner", "dev-builder", "dev-reviewer"].every((id) => fs.existsSync(path.join(target, ".opencode", "agents", `${id}.md`))));
  check("[opencode] /dev-setup command assembled", fs.existsSync(path.join(target, ".opencode", "commands", "dev-setup.md")));
  const plugin = path.join(target, ".opencode", "plugins", "ai-dev.mjs");
  check("[opencode] plugin generated", fs.existsSync(plugin));
  // downstream layout: the plugin keeps the tooling-submodule import path (adapter vendored there)
  check("[opencode] plugin uses the downstream (tooling-submodule) adapter path", fs.readFileSync(plugin, "utf8").includes(".ai-dev/tooling/src/adapter"));
  const oc = JSON.parse(fs.readFileSync(path.join(target, ".opencode", "opencode.json"), "utf8"));
  check("[opencode] opencode.json wires the orchestrator primary + constitution", oc.default_agent === "ai-dev" && oc.instructions.includes(".ai-dev/PROTOCOL.md"));
  check("[opencode] generic build/plan primaries disabled", oc.agent.build.disable === true && oc.agent.plan.disable === true);
  // RATCHET (opencode-plugin-registration): the boundary-deny plugin MUST be
  // registered in the `plugin` key — without it OpenCode 1.17.8 never loads the
  // plugin and the whole [mechanical] floor is silently absent. RED on pre-fix
  // install.mjs (no plugin key emitted), GREEN after.
  check("[opencode] opencode.json registers the boundary plugin in the plugin key", Array.isArray(oc.plugin) && oc.plugin.includes("./plugins/ai-dev.mjs"));
  // the spec is .opencode/-relative (opencode resolves it against the dir of
  // opencode.json), NOT project-root-relative — `./.opencode/plugins/...` would
  // double-resolve and silently fail to load.
  check("[opencode] plugin spec is .opencode/-relative, not project-root-relative", oc.plugin.includes("./plugins/ai-dev.mjs") && !oc.plugin.some((p) => p.includes(".opencode/plugins/")));
  // .ai-dev/launch on OpenCode is the HONEST stub: routing is Claude-Code only, so it
  // prints that and execs opencode (no proxy, no router-launch engine).
  const launch = fs.readFileSync(path.join(target, ".ai-dev", "launch"), "utf8");
  check("[opencode] .ai-dev/launch is the Claude-only stub that execs opencode", launch.includes('exec opencode "$@"'));
  check("[opencode] .ai-dev/launch prints the Claude-only routing note", launch.includes("multi-model routing is Claude-Code only"));
  check("[opencode] .ai-dev/launch does NOT wrap the router-launch engine (routing N/A)", !launch.includes("router-launch.mjs"));
  const agentsMd = fs.readFileSync(path.join(target, "AGENTS.md"), "utf8");
  check("[opencode] AGENTS.md imports the constitution once", agentsMd.split("@.ai-dev/PROTOCOL.md").length - 1 === 1);
});

// ── RATCHET (install-self-verify): the installer FAILS LOUDLY when the deployed ─
// plugin cannot load, instead of printing success over a silently-off [mechanical]
// floor. Three incidents shipped enforcement-off this way (5.17.4 import-depth,
// 5.17.5 registration drop, the noos stale-cache) — common thread: the install
// reported success while the plugin it just wrote could not import(). The plugin
// install step (install-plugin.mjs install()) now import-verifies the just-written
// file. RED without the self-verify (the install would pass over a broken plugin),
// GREEN with it.
//
// Driven through the standalone plugin-install CLI (the same entry the unified
// installer spawns as a child, so the child's non-zero exit propagation is exercised).
// GOOD arm: an intact downstream tree → exit 0. BROKEN arm: the vendored adapter
// removed after vendoring → the deployed plugin's top-level import throws →
// the install exits non-zero with the path-naming, hint-bearing error.
{
  const pluginCli = path.join(ROOT, "src", "adapter", "opencode", "install-plugin.mjs");
  const runPluginInstall = (target) =>
    spawnSync(
      "node",
      [pluginCli, path.join(target, ".opencode", "plugins", "ai-dev.mjs"), "--root", target],
      { encoding: "utf8" },
    );

  // GOOD arm (precondition — also catches a future vendor/wire reorder that would
  // write the plugin before its adapter exists, turning a false-fail into a caught
  // regression rather than a shipped one).
  const good = freshTarget("selfverify-good");
  try {
    install(good, "opencode"); // vendors the adapter THEN wires (incl. the self-verify)
    const r = runPluginInstall(good);
    check("[selfverify] GOOD install: plugin re-install on an intact tree exits 0", r.status === 0);
  } finally {
    fs.rmSync(good, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // BROKEN arm (the ratchet) — break the deployed plugin's load surface, then run
  // the plugin install step and assert it fails loudly.
  const broken = freshTarget("selfverify-broken");
  try {
    install(broken, "opencode");
    // remove the vendored adapter the deployed plugin imports → its top-level
    // import resolves to a missing module → import() throws on load.
    fs.rmSync(path.join(broken, ".ai-dev", "tooling", "src", "adapter", "engine.mjs"), { force: true });
    const r = runPluginInstall(broken);
    const err = (r.stderr || "") + (r.stdout || "");
    check("[selfverify] BROKEN plugin: install exits NON-ZERO (fail-closed)", r.status !== 0);
    check("[selfverify] BROKEN plugin: error names the deployed plugin path", err.includes(path.join(broken, ".opencode", "plugins", "ai-dev.mjs")));
    check("[selfverify] BROKEN plugin: error carries the enforcement-off hint", err.includes("enforcement would be silently off"));
    check("[selfverify] BROKEN plugin: error names the underlying load error", err.includes("Underlying load error:"));
  } finally {
    fs.rmSync(broken, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── opencode plugin registration: de-dupe, never-clobber, idempotent ─────────
// The boundary-deny plugin is registered in opencode.json `plugin`. A downstream
// project may carry its OWN plugin entries — the merge must preserve them (never
// clobber) AND never duplicate ours across re-runs (Set de-dupe). This guards the
// exact regression: enforcement silently off because the plugin was never registered.
{
  const target = freshTarget("ocplugin");
  try {
    // seed a downstream opencode.json that already lists the project's own plugin
    fs.mkdirSync(path.join(target, ".opencode"), { recursive: true });
    fs.writeFileSync(
      path.join(target, ".opencode", "opencode.json"),
      JSON.stringify({ plugin: ["./plugins/my-own.mjs"] }, null, 2) + "\n",
    );
    install(target, "opencode");
    const oc1 = JSON.parse(fs.readFileSync(path.join(target, ".opencode", "opencode.json"), "utf8"));
    check("[ocplugin] project's own plugin entry preserved (never clobbered)", oc1.plugin.includes("./plugins/my-own.mjs"));
    check("[ocplugin] our boundary plugin entry added alongside", oc1.plugin.includes("./plugins/ai-dev.mjs"));

    // a second run must not duplicate ours (Set idempotency)
    install(target, "opencode");
    const oc2 = JSON.parse(fs.readFileSync(path.join(target, ".opencode", "opencode.json"), "utf8"));
    check("[ocplugin] re-install does not duplicate our entry (idempotent)", oc2.plugin.filter((p) => p === "./plugins/ai-dev.mjs").length === 1);
    check("[ocplugin] re-install keeps the project's own entry once", oc2.plugin.filter((p) => p === "./plugins/my-own.mjs").length === 1);
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── platform resolution: a clear error when unresolvable, validation of the flag ─
{
  const target = freshTarget("resolve");
  try {
    let threw = false;
    try { install(target, "borkcode"); } catch { threw = true; }
    check("[resolve] unknown --platform is a hard error", threw);

    let threw2 = false;
    try { install(target, undefined); } catch { threw2 = true; }
    check("[resolve] no flag + no config platform is a hard error", threw2);
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── F4 migration: installer over prior-version artifacts ─────────────────────
// Verifies the installer succeeds and lays the new structure when a target already
// carries old-protocol artifacts (WORKFLOW.md, .ai-pm/ state dir) without deleting them.
{
  const target = freshTarget("f4migration");
  try {
    fs.mkdirSync(path.join(target, ".ai-pm", "state"), { recursive: true });
    fs.writeFileSync(path.join(target, ".ai-pm", "state", "current.md"), "# old state\n");
    fs.writeFileSync(path.join(target, "WORKFLOW.md"), "# old workflow\n");
    install(target, "claude");
    check("[f4] installer succeeds over old-protocol artifacts", fs.existsSync(path.join(target, ".ai-dev", "PROTOCOL.md")));
    check("[f4] old artifacts not deleted by installer", fs.existsSync(path.join(target, "WORKFLOW.md")) && fs.existsSync(path.join(target, ".ai-pm", "state", "current.md")));
    check("[f4] new structure laid down alongside old", fs.existsSync(path.join(target, ".ai-dev", "tooling", "src", "adapter", "engine.mjs")));
    check("[f4] .gitignore excludes .ai-dev/state/ after migration install", fs.readFileSync(path.join(target, ".gitignore"), "utf8").includes(".ai-dev/state/"));
    check("[f4] version stamped on a migration install", fs.readFileSync(path.join(target, ".ai-dev", "VERSION"), "utf8").trim() === PKG_VERSION);
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── version-change detection: the UPGRADING handoff marker ───────────────────
{
  const target = freshTarget("upgrade");
  try {
    install(target, "claude");
    fs.writeFileSync(path.join(target, ".ai-dev", "VERSION"), "1.0.0\n"); // simulate an older install
    install(target, "claude");
    const markerPath = path.join(target, ".ai-dev", "UPGRADING.md");
    check("[upgrade] marker written on a version-change re-run", fs.existsSync(markerPath));
    const marker = fs.existsSync(markerPath) ? fs.readFileSync(markerPath, "utf8") : "";
    check("[upgrade] marker names old → new", marker.includes("1.0.0") && marker.includes(PKG_VERSION));
    check("[upgrade] marker points at the notes + the upgrade check", marker.includes(".ai-dev/upgrades.md") && marker.includes(".ai-dev/procedures/upgrade.md"));
    check("[upgrade] stamp updated to the new version", fs.readFileSync(path.join(target, ".ai-dev", "VERSION"), "utf8").trim() === PKG_VERSION);

    // a chained bump with the marker still unconsumed keeps the ORIGIN version —
    // the (old, new] notes range must not shrink to the last unmigrated install
    fs.writeFileSync(path.join(target, ".ai-dev", "VERSION"), "1.5.0\n");
    install(target, "claude");
    check("[upgrade] unconsumed marker keeps its origin across a chained bump", fs.readFileSync(markerPath, "utf8").includes("Upgraded 1.0.0 →"));
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── pre-stamp detection: an existing .ai-dev/ tree without a VERSION ─────────
{
  const target = freshTarget("prestamp");
  try {
    fs.mkdirSync(path.join(target, ".ai-dev"), { recursive: true });
    fs.writeFileSync(path.join(target, ".ai-dev", "backlog.md"), "# existing pre-stamp install\n");
    install(target, "claude");
    const marker = path.join(target, ".ai-dev", "UPGRADING.md");
    check("[prestamp] existing stampless .ai-dev/ tree detected as pre-5.10 upgrade", fs.existsSync(marker) && fs.readFileSync(marker, "utf8").includes("pre-5.10"));
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── hook prune: a stale ai-dev hook group is replaced, foreign groups kept ───
{
  const target = freshTarget("hookprune");
  try {
    const settingsPath = path.join(target, ".claude", "settings.json");
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
    fs.writeFileSync(settingsPath, JSON.stringify({
      hooks: {
        PreToolUse: [
          // a stale ai-dev group — the old vendor location (its command CHANGED)
          { matcher: "Read|Write", hooks: [{ type: "command", command: "node \"$CLAUDE_PROJECT_DIR/.ai-pm/tooling/src/adapter/claude/shim.mjs\"" }] },
          // a foreign group the merge must never touch
          { matcher: "Bash", hooks: [{ type: "command", command: "my-own-linter --check" }] },
        ],
      },
    }, null, 2));
    install(target, "claude");
    const settings = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    const cmds = settings.hooks.PreToolUse.flatMap((g) => g.hooks.map((h) => h.command));
    check("[prune] stale ai-dev hook group (changed command) pruned", !cmds.some((c) => c.includes(".ai-pm/")));
    check("[prune] exactly one ai-dev shim group remains", cmds.filter((c) => c.includes("adapter/claude/shim.mjs")).length === 1);
    check("[prune] foreign hook group preserved", cmds.includes("my-own-linter --check"));
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── breadcrumb: never clobbers a real file; replaced by full wiring ──────────
{
  const target = freshTarget("breadcrumb");
  try {
    fs.writeFileSync(path.join(target, "CLAUDE.md"), "# My real project notes — keep me\n");
    install(target, "opencode");
    const withBc = fs.readFileSync(path.join(target, "CLAUDE.md"), "utf8");
    check("[breadcrumb] real CLAUDE.md content preserved", withBc.includes("keep me"));
    check("[breadcrumb] breadcrumb merged into the existing file", withBc.includes("ai-dev:breadcrumb") && withBc.includes("--platform claude"));

    // the platform becomes active ⇒ its breadcrumb is replaced by the full wiring
    install(target, "claude");
    const wired = fs.readFileSync(path.join(target, "CLAUDE.md"), "utf8");
    check("[breadcrumb] replaced by full wiring when the platform becomes active", !wired.includes("ai-dev:breadcrumb") && wired.includes("@.ai-dev/PROTOCOL.md") && wired.includes("keep me"));
    // the other surface is REALLY wired (a prior install) ⇒ no false breadcrumb on it
    const agentsMd = fs.readFileSync(path.join(target, "AGENTS.md"), "utf8");
    check("[breadcrumb] no false breadcrumb on an already-wired surface", !agentsMd.includes("ai-dev:breadcrumb") && agentsMd.includes("@.ai-dev/PROTOCOL.md"));
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── vendored installer self-re-run — the documented upgrade command ──────────
// INSTALL.md's upgrade path is `node .ai-dev/tooling/src/adapter/install.mjs <target>`;
// it used to ENOENT because layDownCore's sources (PROTOCOL.md, src/quality/,
// src/templates/) were never vendored (docs/decisions/upgrade-migration.md). This
// pins the regression: the vendored tree must be self-sufficient.
{
  const target = freshTarget("vendored");
  try {
    install(target, "claude");
    const vendoredInstaller = path.join(target, ".ai-dev", "tooling", "src", "adapter", "install.mjs");
    const r = spawnSync("node", [vendoredInstaller, target, "--platform", "claude"], { encoding: "utf8" });
    check("[vendored] self-re-run exits 0 (no ENOENT)", r.status === 0);
    const protocolSrc = fs.readFileSync(path.join(ROOT, "PROTOCOL.md"), "utf8");
    check("[vendored] PROTOCOL.md intact after self-re-run", fs.existsSync(path.join(target, ".ai-dev", "PROTOCOL.md")) && fs.readFileSync(path.join(target, ".ai-dev", "PROTOCOL.md"), "utf8") === protocolSrc);
    const engineSrc = fs.readFileSync(path.join(ROOT, "src", "adapter", "engine.mjs"), "utf8");
    check("[vendored] tooling not corrupted by the self-copy", fs.readFileSync(path.join(target, ".ai-dev", "tooling", "src", "adapter", "engine.mjs"), "utf8") === engineSrc);
    check("[vendored] same-version self-re-run leaves no upgrade marker", !fs.existsSync(path.join(target, ".ai-dev", "UPGRADING.md")));
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── never clobber a real doc ─────────────────────────────────────────────────
{
  const target = freshTarget("clobber");
  try {
    fs.mkdirSync(path.join(target, "docs"), { recursive: true });
    const realDoc = path.join(target, "docs", "architecture.md");
    fs.writeFileSync(realDoc, "# the project's REAL architecture — keep me\n");
    install(target, "claude");
    check("[clobber] an existing real doc is never overwritten", fs.readFileSync(realDoc, "utf8").includes("keep me"));
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}


// ── dogfood (self-host) mode — idempotent against the committed source tree ──
// The flag wires the three tracked surfaces to src/, skips vendoring + stamping,
// and converges to the committed bytes. The contract's falsifiable form is the
// real-layer scenario (git status clean after a reinstall in THIS repo); here we
// drive install() against the live repo root and assert it MUTATES NOTHING — the
// surface files keep their committed bytes, no .ai-dev/tooling/ is created, no
// VERSION/UPGRADING is written, and a second run is byte-identical. A snapshot of
// the touched surfaces is restored at the end so the test never dirties the repo
// even if an assertion regresses mid-run.
{
  // .ai-dev/launch is in the snapshot set: a dogfood OpenCode install below would write
  // the opencode stub form over the committed claude form, so it MUST be restored (the
  // finally block restores every SURFACE byte-for-byte regardless of outcome).
  const SURFACES = ["CLAUDE.md", "AGENTS.md", ".claude/settings.json", ".opencode/opencode.json", ".ai-dev/launch"];
  // A dogfood re-bake writes the BARE tier alias into .claude/agents/<seat>.md (the committed
  // bake is the CONCRETE native id) — snapshot + restore them too, or the test dirties the
  // working tree every run (the recurring drift that turns install-drift red on the next
  // build). Walk the dir so a seat added later is covered too (install-drift is the backstop).
  // NOTE: agents are EXCLUDED from the allSame "unchanged" assertion below — they DO change
  // under dogfood by design; we only snapshot+restore them (undo the change), not assert it.
  const agentDir = path.join(ROOT, ".claude", "agents");
  const agentFiles = fs.existsSync(agentDir) ? fs.readdirSync(agentDir).map((f) => path.join(".claude", "agents", f)) : [];
  const surfaces = [...SURFACES, ...agentFiles];
  const before = {};
  for (const s of surfaces) before[s] = fs.readFileSync(path.join(ROOT, s), "utf8");
  const toolingExisted = fs.existsSync(path.join(ROOT, ".ai-dev", "tooling"));
  const versionExisted = fs.existsSync(path.join(ROOT, ".ai-dev", "VERSION"));
  try {
    // dogfood claude: the tracked surfaces stay at their committed (source-form) bytes
    install(ROOT, "claude", { dogfood: true });
    check("[dogfood] CLAUDE.md unchanged (already source-form)", fs.readFileSync(path.join(ROOT, "CLAUDE.md"), "utf8") === before["CLAUDE.md"]);
    check("[dogfood] CLAUDE.md imports the SOURCE constitution + the unified orchestrator surface", before["CLAUDE.md"].includes("@PROTOCOL.md") && before["CLAUDE.md"].includes("@.claude/ai-dev.md"));
    check("[dogfood] CLAUDE.md does NOT import the vendored layout", !before["CLAUDE.md"].includes("@.ai-dev/PROTOCOL.md"));
    check("[dogfood] CLAUDE.md carries no stale raw-orchestrator import", !before["CLAUDE.md"].includes("@src/agents/orchestrator.md") && !before["CLAUDE.md"].includes("@.ai-dev/tooling/src/agents/orchestrator.md"));
    // the dogfood orchestrator surface converges to its committed bytes (drift-guarded separately)
    check("[dogfood] .claude/ai-dev.md present after dogfood install", fs.existsSync(path.join(ROOT, ".claude", "ai-dev.md")));
    const settings = fs.readFileSync(path.join(ROOT, ".claude", "settings.json"), "utf8");
    check("[dogfood] settings.json unchanged", settings === before[".claude/settings.json"]);
    check("[dogfood] hook command points at src/adapter/claude/shim.mjs", settings.includes("src/adapter/claude/shim.mjs") && !settings.includes(".ai-dev/tooling/src/adapter/claude/shim.mjs"));

    // dogfood opencode: opencode.json stays at its committed canonical shape
    install(ROOT, "opencode", { dogfood: true });
    check("[dogfood] opencode.json unchanged (canonical self-host shape)", fs.readFileSync(path.join(ROOT, ".opencode", "opencode.json"), "utf8") === before[".opencode/opencode.json"]);
    check("[dogfood] opencode.json instructions = source PROTOCOL.md", JSON.parse(before[".opencode/opencode.json"]).instructions.includes("PROTOCOL.md"));
    check("[dogfood] AGENTS.md unchanged (no @-import appended)", fs.readFileSync(path.join(ROOT, "AGENTS.md"), "utf8") === before["AGENTS.md"]);
    check("[dogfood] AGENTS.md carries NO @-import line (loads via opencode.json instructions)", !before["AGENTS.md"].includes("@.ai-dev/PROTOCOL.md") && !before["AGENTS.md"].includes("@PROTOCOL.md"));

    // no vendored / stamped debris created by either dogfood run (only if absent before)
    if (!toolingExisted) check("[dogfood] no .ai-dev/tooling/ vendored copy created", !fs.existsSync(path.join(ROOT, ".ai-dev", "tooling")));
    if (!versionExisted) {
      check("[dogfood] no .ai-dev/VERSION stamped", !fs.existsSync(path.join(ROOT, ".ai-dev", "VERSION")));
      check("[dogfood] no .ai-dev/UPGRADING.md marker written", !fs.existsSync(path.join(ROOT, ".ai-dev", "UPGRADING.md")));
    }

    // second dogfood run is a no-op — byte-identical surfaces
    install(ROOT, "claude", { dogfood: true });
    const allSame = SURFACES.every((s) => fs.readFileSync(path.join(ROOT, s), "utf8") === before[s]);
    check("[dogfood] a second dogfood run leaves every tracked surface byte-identical", allSame);
  } finally {
    // restore the committed bytes regardless of outcome (the test must never dirty the repo)
    for (const s of surfaces) fs.writeFileSync(path.join(ROOT, s), before[s]);
    if (!toolingExisted) fs.rmSync(path.join(ROOT, ".ai-dev", "tooling"), { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
    if (!versionExisted) {
      fs.rmSync(path.join(ROOT, ".ai-dev", "VERSION"), { force: true });
      fs.rmSync(path.join(ROOT, ".ai-dev", "UPGRADING.md"), { force: true });
    }
  }
}

// ── dogfood fail-closed (symmetric) — both misuse directions are hard errors ──
{
  // (a) --dogfood against a NON-source temp target (no src/adapter/install.mjs)
  const target = freshTarget("dogfood-nonsource");
  try {
    let threw = false;
    try { install(target, "claude", { dogfood: true }); } catch { threw = true; }
    check("[dogfood] --dogfood against a non-source target throws (fail-closed)", threw);
    check("[dogfood] failed --dogfood wired nothing into the non-source target", !fs.existsSync(path.join(target, ".claude")) && !fs.existsSync(path.join(target, ".ai-dev")));
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
  // (b) NO flag but the target IS the source repo → hard error (the footgun made loud)
  let threw2 = false;
  try { install(ROOT, "claude"); } catch { threw2 = true; }
  check("[dogfood] downstream-mode install against the source repo throws (no silent churn)", threw2);
  // assert nothing leaked: the source repo's committed surfaces are untouched
  check("[dogfood] refused source-repo install left CLAUDE.md untouched", fs.readFileSync(path.join(ROOT, "CLAUDE.md"), "utf8").includes("@.claude/ai-dev.md"));
}

// ── F3 pre-push quality hook: installs, never-clobbers, idempotent ───────────
// The installer drops a git pre-push hook running the registry runner WHOLESALE
// (`node .ai-dev/quality/run.mjs build`) so a push of build-beat-failing code is
// blocked locally before it leaves the machine (the local complement to remote CI).
// Default-ON; never-clobber a project's own pre-push hook (fail-safe); idempotent.
// RED without installPrePushHook: no hook file is written ⇒ the existence/exec/
// contents assertions fail; the never-clobber + idempotence arms pin the safety.
{
  // a target WITH a .git/ dir (the hook only writes where a repo exists)
  const target = freshTarget("prepush");
  try {
    fs.mkdirSync(path.join(target, ".git"), { recursive: true });
    install(target, "claude");
    const hookPath = path.join(target, ".git", "hooks", "pre-push");
    check("[prepush] pre-push hook written when .git exists", fs.existsSync(hookPath));
    const body = fs.existsSync(hookPath) ? fs.readFileSync(hookPath, "utf8") : "";
    check("[prepush] hook runs the registry runner WHOLESALE (not a tool subset)", body.includes("node .ai-dev/quality/run.mjs build"));
    check("[prepush] hook carries the ai-dev marker (for self-recognition on re-run)", body.includes("# ai-dev:pre-push"));
    // executable bit set (best-effort on platforms that honour it)
    if (fs.existsSync(hookPath)) {
      const mode = fs.statSync(hookPath).mode;
      check("[prepush] hook is executable (owner +x)", (mode & 0o100) !== 0);
    }

    // idempotence: a second install leaves OUR marker hook byte-identical
    const firstBytes = fs.readFileSync(hookPath, "utf8");
    install(target, "claude");
    check("[prepush] second install leaves our hook byte-identical (idempotent)", fs.readFileSync(hookPath, "utf8") === firstBytes);
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// never-clobber arm: a project's OWN pre-push hook (no ai-dev marker) is left
// UNTOUCHED and a warning is surfaced. Driven through the CLI so the warning on
// stdout is observable (the install() return is the platform string, by contract).
{
  const target = freshTarget("prepush-foreign");
  try {
    const hooksDir = path.join(target, ".git", "hooks");
    fs.mkdirSync(hooksDir, { recursive: true });
    const foreign = path.join(hooksDir, "pre-push");
    const foreignBody = "#!/bin/sh\n# my project's own pre-push hook\nmy-own-check --strict\n";
    fs.writeFileSync(foreign, foreignBody, { mode: 0o755 });
    const cli = path.join(ROOT, "src", "adapter", "install.mjs");
    const r = spawnSync("node", [cli, target, "--platform", "claude"], { encoding: "utf8" });
    const out = (r.stdout || "") + (r.stderr || "");
    check("[prepush] foreign pre-push hook left UNTOUCHED (never-clobber)", fs.readFileSync(foreign, "utf8") === foreignBody);
    check("[prepush] foreign hook collision surfaces a warning", out.includes("pre-push hook already exists") && out.includes("not ai-dev"));
    check("[prepush] warning still leaves the install succeeding (exit 0)", r.status === 0);
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// no-.git arm: a target without a repo gets NO hook and NO throw (the install
// already warns about the missing repo; the hook step skips silently).
{
  const target = freshTarget("prepush-norepo");
  try {
    install(target, "claude");
    check("[prepush] no hook written when .git is absent", !fs.existsSync(path.join(target, ".git", "hooks", "pre-push")));
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

// ── item 2: the loud version banner + the stale-npx-cache caveat ─────────────
// The CLI prints the EXACT version being installed FIRST (so a stale-cache no-op is
// visible). On a same-version re-run (prior === installed) AND an npx source path,
// it adds the cache-clear caveat. RED without the banner: the version line is absent.
{
  const cli = path.join(ROOT, "src", "adapter", "install.mjs");
  // fresh install ⇒ the loud version line appears unconditionally
  const target = freshTarget("banner");
  try {
    const r = spawnSync("node", [cli, target, "--platform", "claude"], { encoding: "utf8" });
    const out = (r.stdout || "") + (r.stderr || "");
    check("[banner] fresh install prints the loud version line", out.includes(`Installing ai-dev-protocol v${PKG_VERSION}`));

    // same-version re-run: the heuristic fires only with an _npx source segment.
    // This run's SOURCE is the repo (no _npx), so the caveat must NOT appear — the
    // no-false-alarm guarantee. The version line still appears (loud every time).
    const r2 = spawnSync("node", [cli, target, "--platform", "claude"], { encoding: "utf8" });
    const out2 = (r2.stdout || "") + (r2.stderr || "");
    check("[banner] same-version re-run still prints the version line", out2.includes(`Installing ai-dev-protocol v${PKG_VERSION}`));
    check("[banner] same-version re-run notes the prior version", out2.includes(`previously installed: v${PKG_VERSION}`));
    check("[banner] no _npx source ⇒ NO stale-cache caveat (no false alarm)", !out2.includes("npx cache may be STALE"));
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }

  // The stale-cache heuristic itself is unit-tested as a pure predicate — a full _npx
  // tree on disk is unworkable in-test because ES-module resolution follows symlinks
  // (node resolves import.meta.url through a symlink, so a symlinked `_npx` path
  // collapses to the real repo path). The predicate carries the actual decision; the
  // CLI banner-line integration above proves it is wired. Falsifiable, deterministic.
  const v = PKG_VERSION;
  check("[banner] heuristic: _npx source + same version ⇒ stale-cache caveat", isStaleNpxReRun(path.join("/home/u/.npm/_npx/abc123/src/adapter"), v, v) === true);
  check("[banner] heuristic: non-_npx source (git clone) ⇒ no caveat (no false alarm)", isStaleNpxReRun(path.join("/home/u/dev/ai-dev-protocol/src/adapter"), v, v) === false);
  check("[banner] heuristic: a version BUMP ⇒ no caveat (a real update, not a no-op)", isStaleNpxReRun(path.join("/home/u/.npm/_npx/abc/src"), "5.17.0", v) === false);
  check("[banner] heuristic: a fresh install (no prior) ⇒ no caveat", isStaleNpxReRun(path.join("/home/u/.npm/_npx/abc/src"), null, v) === false);
}

// ── RATCHET (#400 quality-runner-path split): assembled agent path === deployed path ──
// Root cause of #400: the assembled downstream Builder/Reviewer named `src/quality/run.mjs`
// (the SOURCE path, absent downstream) while the installer deployed the runner to
// `.ai-dev/quality/run.mjs`. The mismatch was dogfood-blind (source repo has src/quality/).
//
// This test pins the contract: the runner path the assembled downstream Builder agent names
// must be the path the installer ACTUALLY deploys the runner to in a real downstream install.
// RED before the fix (agents named src/quality/run.mjs, deployed path was .ai-dev/quality/run.mjs).
// GREEN after (both say .ai-dev/quality/run.mjs).
//
// Implementation: do a real non-dogfood install into a temp dir (inside the root —
// the boundary allows .tmp-install* under root; .gitignore excludes them). Then:
//   1. Read the assembled Builder body and extract the `node … run.mjs build` invocation.
//   2. Parse the path from that invocation.
//   3. Assert that path exists inside the installed target.
//   4. Confirm the assembled Reviewer names the same path for the review beat.
{
  const target = freshTarget("runner-path");
  try {
    install(target, "claude");

    // 1. Read the assembled downstream Builder
    const builderBody = fs.readFileSync(path.join(target, ".claude", "agents", "dev-builder.md"), "utf8");
    // Extract the `node <path> build` invocation — the literal the Builder follows.
    // Accept `build --touched` (scope mode) as well as plain `build` (full mode).
    const buildMatch = builderBody.match(/`node ([^\s`]+) build(?: --touched)?`/);
    check("[runner-path] assembled Builder names a run.mjs invocation", buildMatch !== null);
    const builderRunnerPath = buildMatch ? buildMatch[1] : null;

    // 2. Assert the path the agent names resolves to a REAL file in the install target
    if (builderRunnerPath) {
      const resolvedRunner = path.join(target, builderRunnerPath);
      check(
        "[runner-path] the runner path the Builder names exists in the installed target",
        fs.existsSync(resolvedRunner),
      );
      check(
        "[runner-path] the deployed runner is at .ai-dev/quality/run.mjs (the canonical runtime path)",
        builderRunnerPath === ".ai-dev/quality/run.mjs",
      );
    }

    // 3. Assembled Reviewer names the same path for the review beat
    const reviewerBody = fs.readFileSync(path.join(target, ".claude", "agents", "dev-reviewer.md"), "utf8");
    const reviewMatch = reviewerBody.match(/`node ([^\s`]+) review(?: --touched)?`/);
    check("[runner-path] assembled Reviewer names a run.mjs invocation", reviewMatch !== null);
    if (reviewMatch) {
      check(
        "[runner-path] Reviewer's run.mjs path matches Builder's (one canonical path)",
        reviewMatch[1] === builderRunnerPath,
      );
    }

    // 4. Same contract holds for the fixup reviewer (also assembled downstream)
    const fixupBody = fs.readFileSync(path.join(target, ".claude", "agents", "dev-reviewer-fixup.md"), "utf8");
    const fixupMatch = fixupBody.match(/`node ([^\s`]+) review(?: --touched)?`/);
    check("[runner-path] assembled fixup Reviewer names a run.mjs invocation", fixupMatch !== null);
    if (fixupMatch) {
      check(
        "[runner-path] fixup Reviewer's path matches the canonical runtime path",
        fixupMatch[1] === builderRunnerPath,
      );
    }
  } finally {
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  }
}

report("INSTALL-CORE", "PASS — installer vendors the adapter, wires both platforms, and is idempotent");
