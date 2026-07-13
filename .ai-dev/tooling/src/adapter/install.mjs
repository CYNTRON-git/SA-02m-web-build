#!/usr/bin/env node
// The unified installer — the one realisation of the adapter's "install into a
// project" contract point (protocol-reference.md `## Core and adapter`,
// docs/contracts/one-command-install.md). It automates exactly the manual
// procedure src/adapter/INSTALL.md documents: vendor the shared adapter, lay down
// the core, and wire the active platform — idempotently.
//
// This file is the ENTRY POINT: it orchestrates the steps and carries the CLI. The
// steps themselves live in cohesive sibling modules (one home each), composed here:
//   install-fs.mjs        — SOURCE/PLATFORMS + the idempotent fs/process helpers
//   install-version.mjs   — version resolve/stamp + the UPGRADING marker + npx heuristic
//   install-core.mjs      — the downstream filesystem steps (vendor, lay-down, config,
//                           gitignore, the local pre-push gate)
//   install-breadcrumb.mjs— the cross-platform inactive-load-surface breadcrumb
//   install-claude.mjs    — Claude wiring + the deny-wiring self-verify
//   install-opencode.mjs  — OpenCode wiring + the boundary plugin
//
// This is ADAPTER-LAYER code: platform-specific by nature, so concrete platform
// names (claude / opencode, .claude/, .opencode/) are allowed here. The neutral
// core (PROTOCOL.md, the role bodies, docs/architecture.md prose) names none of them.
//
// Usage:  node src/adapter/install.mjs <target-dir> [--platform claude|opencode] [--dogfood]
//   platform: the --platform flag, else the target's .ai-dev/config.json `platform`,
//   else a clear error (never a silent guess).
//   --dogfood: SELF-HOST mode — only valid when the target IS the protocol's own
//   source repo. It wires the three tracked surfaces to the source tree (src/...),
//   skips vendoring .ai-dev/tooling/ and skips the .ai-dev/VERSION stamp + UPGRADING
//   marker, so a reinstall into this repo converges to the committed bytes
//   (git status clean) instead of churning tracked files to the downstream layout.
//   Fail-closed, symmetric: --dogfood against a non-source target, OR its absence
//   when the target IS the source repo, is a hard error (the footgun made loud).
//
// Security (threat-model): the two untrusted inputs are the target path and the
// platform string, both validated AT entry (the boundary). The platform is checked
// against a literal allow-list before any filesystem or process use. Every write
// lands beneath the RESOLVED target root; the project-boundary deny (invariant 2)
// is the mechanical floor under that — installing into an external sibling from
// the protocol repo is blocked, so this is tested against a temp dir INSIDE the
// root. Child scripts are invoked via execFileSync with an argv ARRAY (no shell),
// so no path is ever parsed by a shell. No network, no secrets, no new dependency
// (Node stdlib only).

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SOURCE, PLATFORMS, copyFile } from "./install-fs.mjs";
import { resolveSourceVersion, readPriorVersion, stampVersion, isStaleNpxReRun } from "./install-version.mjs";
import { vendorTooling, layDownCore, deployProcedures, deployModules, ensureConfig, ensureTransientsGitignore, generateLaunchScript, installPrePushHook } from "./install-core.mjs";
import { writeInactiveBreadcrumb } from "./install-breadcrumb.mjs";
import { wireClaude, verifyClaudeWiring } from "./install-claude.mjs";
import { wireOpenCode } from "./install-opencode.mjs";

// Re-exported so the test imports the whole public surface from this one entry
// (the four exports the one-command-install net drives) regardless of which sibling
// module now defines each. install + hasGitRepo are defined below.
export { verifyClaudeWiring, isStaleNpxReRun };

// ── orchestration ────────────────────────────────────────────────────────────

// Resolve the active platform: --platform flag, else the target config's `platform`,
// else a hard error (fail-closed — never wire a guessed platform).
function resolvePlatform(target, flag) {
  if (flag) {
    if (!PLATFORMS.includes(flag)) {
      throw new Error(`unknown --platform "${flag}" — expected one of ${PLATFORMS.join(" | ")}`);
    }
    return flag;
  }
  const cfgPath = path.join(target, ".ai-dev", "config.json");
  if (fs.existsSync(cfgPath)) {
    const platform = JSON.parse(fs.readFileSync(cfgPath, "utf8")).platform;
    if (PLATFORMS.includes(platform)) return platform;
  }
  throw new Error(
    `cannot resolve the platform — pass --platform ${PLATFORMS.join("|")} ` +
      `or set "platform" in ${path.join(target, ".ai-dev", "config.json")}`,
  );
}

// Is `target` the protocol's OWN source repo? The sentinel is the installer's own
// source presence at <target>/src/adapter/install.mjs (the same SOURCE===target
// signal install-plugin.mjs uses with plugin-entry.mjs). A real downstream never
// carries the installer source at its root.
function isSourceRepo(target) {
  return fs.existsSync(path.join(target, "src", "adapter", "install.mjs"));
}

// Install the protocol into `targetDir`. Returns the resolved platform. Exported so
// the test drives it directly against a temp dir. `opts.dogfood` selects SELF-HOST
// mode (wire to src/, skip vendoring + stamping) — fail-closed and symmetric: it is
// valid ONLY against the protocol's own source repo, and its ABSENCE against that
// repo is equally a hard error (the footgun made loud, the Operator's call).
export function install(targetDir, platformFlag, opts = {}) {
  const target = path.resolve(targetDir);
  const dogfood = !!opts.dogfood;
  const source = isSourceRepo(target);
  if (dogfood && !source) {
    throw new Error(
      "--dogfood is only valid when installing into the protocol's own source repo " +
        `(no src/adapter/install.mjs found under ${target}) — drop the flag for a downstream install`,
    );
  }
  if (!dogfood && source) {
    throw new Error(
      `target ${target} IS the protocol's own source repo — pass --dogfood to wire it to its own src/ ` +
        "(a downstream-mode install here would churn the tracked source-mode files)",
    );
  }

  const platform = resolvePlatform(target, platformFlag);

  if (dogfood) {
    // SELF-HOST: skip vendoring AND skip layDownCore — the self-host repo carries its
    // core at the root (PROTOCOL.md, src/quality/, src/templates/), so it needs no
    // copy under .ai-dev/ (a copy would be untracked debris that breaks idempotency).
    // No version stamp / UPGRADING marker either — the repo's authoritative version is
    // its own package.json (read live). config.json + .gitignore are committed and
    // present, so ensureConfig / ensureTransientsGitignore are no-ops; only the
    // gitignored session-state dir is created for write-readiness. This is the path
    // that converges to the committed bytes (git status clean).
    //
    // Exception: deploy the quality runner to .ai-dev/quality/run.mjs so the dogfood
    // repo finds it at the same canonical path assembled agents name. The runner is
    // gitignored (.ai-dev/quality/ is in .gitignore), so this is a build artifact, not
    // a committed duplicate of src/quality/run.mjs.
    fs.mkdirSync(path.join(target, ".ai-dev", "state"), { recursive: true });
    fs.mkdirSync(path.join(target, ".ai-dev", "quality"), { recursive: true });
    copyFile(
      path.join(SOURCE, "src", "quality", "run.mjs"),
      path.join(target, ".ai-dev", "quality", "run.mjs"),
    );
    deployProcedures(target); // readable .ai-dev/procedures/ — converges to committed bytes
    deployModules(target); // readable .ai-dev/modules/ (runtime-read module files) — converges to committed bytes
    ensureConfig(target, platform);
    ensureTransientsGitignore(target);
    generateLaunchScript(target, platform, true); // .ai-dev/launch — converges to committed bytes
    if (platform === "claude") wireClaude(target, true);
    else wireOpenCode(target, true);
    return platform;
  }

  const version = resolveSourceVersion();
  const prior = readPriorVersion(target); // BEFORE any write creates .ai-dev/

  vendorTooling(target, version);
  layDownCore(target);
  deployProcedures(target); // readable .ai-dev/procedures/ (the tooling copy is read-denied)
  deployModules(target); // readable .ai-dev/modules/ runtime-read module files (the tooling copy is read-denied)
  ensureConfig(target, platform);
  ensureTransientsGitignore(target);
  generateLaunchScript(target, platform, false); // .ai-dev/launch (always — optional, but generated)
  if (platform === "claude") wireClaude(target, false);
  else wireOpenCode(target, false);
  writeInactiveBreadcrumb(target, platform, false);
  installPrePushHook(target); // F3 local quality gate (default-ON; never-clobber)
  stampVersion(target, version, prior);

  return platform;
}

// Does the target carry a git repository? The protocol's loop stands on git
// (branches, commits, the merge-gate reads pushes) — the CLI warns when none
// exists. A check, never an init: a one-shot script must not mutate the
// target's VCS state; the interactive offer lives in setup's repo check
// (src/agents/orchestrator.md `## Setup` step 0). Exported for the test.
export function hasGitRepo(targetDir) {
  return fs.existsSync(path.join(path.resolve(targetDir), ".git"));
}

// CLI entry — only when invoked directly, not when imported by the test. argv[1] is
// realpath'd because an npm bin shim invokes this file through a symlink, while the
// loaded module URL is the real path — without it the npx run would silently no-op.
if (process.argv[1] && fs.realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const flagIdx = args.indexOf("--platform");
  const platformFlag = flagIdx >= 0 ? args[flagIdx + 1] : undefined;
  const dogfood = args.includes("--dogfood");
  const targetDir = args.find((a, i) => !a.startsWith("--") && (flagIdx < 0 || i !== flagIdx + 1));

  if (!targetDir) {
    console.error("Usage: node src/adapter/install.mjs <target-dir> [--platform claude|opencode] [--dogfood]");
    process.exit(2);
  }

  try {
    // Loud version banner (item 2) — print the EXACT version being installed FIRST,
    // before the per-step lines, so a stale-cache no-op is visible. Skipped in dogfood
    // (the source repo's version is its live package.json, not an install stamp). The
    // resolve is best-effort: a failure must not abort the install, so guard it.
    let bannerVersion = null, priorBeforeInstall = null;
    // Did a config already exist BEFORE this run? ensureConfig writes only where
    // absent, so this single pre-check tells the summary the truth (wrote vs kept)
    // without changing install()'s return contract (papercut 9).
    const configExistedBefore = fs.existsSync(path.join(path.resolve(targetDir), ".ai-dev", "config.json"));
    if (!dogfood) {
      try {
        bannerVersion = resolveSourceVersion();
        priorBeforeInstall = readPriorVersion(path.resolve(targetDir));
        console.log(`\n→ Installing ai-dev-protocol v${bannerVersion}`);
        if (priorBeforeInstall && priorBeforeInstall !== "pre-5.10") {
          console.log(`  (previously installed: v${priorBeforeInstall})`);
        }
      } catch { /* a version-resolve failure is non-fatal to the banner */ }
    }

    const platform = install(targetDir, platformFlag, { dogfood });
    const rel = path.relative(process.cwd(), path.resolve(targetDir)) || ".";
    if (dogfood) {
      console.log(`\nInstalled the ai-dev protocol into ${rel} in DOGFOOD (self-host) mode (platform: ${platform}).`);
      console.log("  • wired the tracked surfaces to the source tree (src/...) — no .ai-dev/tooling/ vendored copy");
      console.log("  • skipped the .ai-dev/VERSION stamp + UPGRADING marker (the repo's version is its own package.json)");
      console.log(`  • wired ${platform} (deny hooks, agents, the /dev-setup command${platform === "opencode" ? ", the plugin" : ""}) against src/`);
      console.log("  • a reinstall here converges to the committed bytes — git status stays clean");
      process.exit(0);
    }
    console.log(`\nInstalled the ai-dev protocol into ${rel} (platform: ${platform}).`);
    console.log("  • vendored the shared adapter into .ai-dev/tooling/ (self-sufficient for the upgrade re-run)");
    console.log("  • laid down .ai-dev/PROTOCOL.md, .ai-dev/quality/ (the quality-runner shape), and .ai-dev/upgrades.md");
    console.log(
      configExistedBefore
        ? "  • kept existing .ai-dev/config.json (your roles/models/mode untouched)"
        : "  • wrote .ai-dev/config.json (minimal default — run /dev-setup to configure)",
    );
    console.log(`  • wired ${platform} (deny hooks, agents, the /dev-setup command${platform === "opencode" ? ", the plugin" : ""})`);
    console.log("  • stamped .ai-dev/VERSION and left a breadcrumb load-surface for the inactive platform");
    if (!hasGitRepo(targetDir)) {
      console.log("\n⚠ No git repository found — the protocol's loop (branches, reviews, merges) requires one.");
      console.log("  Initialize before the first feature:  git init -b main && git add . && git commit -m 'init'");
      console.log("  (setup will also offer this; the remote — gh repo create / git remote add — is yours.)");
    }
    // Stale-npx-cache caveat (item 2, HEURISTIC) — a re-run that did NOT bump the
    // version (prior === installed) is the stale-no-op signature WHEN the Operator
    // believed they were updating. Fired only when the source was fetched via npx
    // (an `_npx` segment in the installer's own source path) — absent ⇒ no caveat,
    // no false alarm. The installer runs FROM the (possibly stale) source, so it
    // CANNOT know it is stale; this makes the version loud so the Operator can judge.
    if (isStaleNpxReRun(SOURCE, priorBeforeInstall, bannerVersion)) {
      console.log(`\n⚠ Same version as before (v${bannerVersion}) — if you expected a newer one, the npx cache may be STALE.`);
      console.log("  Clear it and re-run:  rm -rf \"$(npm config get cache)/_npx\"   (or use the git-clone path)");
      console.log("  See README `## Updating an existing install`.");
    }
    console.log("\nNext: run /dev-setup to configure roles, models, mode, and the module kit.");
  } catch (e) {
    console.error(`install failed: ${e.message}`);
    process.exit(1);
  }
}
