// Multi-repo boundary — END-TO-END through the REAL installed entry point.
//
// WHAT THIS COVERS (the gap parity.test.mjs leaves open):
//   parity.test.mjs `## 1c COMPONENTS MATRIX` tests the multi-repo deny matrix
//   exhaustively, but by calling `decide(payload, root, config)` with an EXPLICIT
//   `root` — it bypasses `resolveRoot`, the one piece of the real hook path that
//   resolves the session root from the filesystem (`git rev-parse --show-toplevel`,
//   cwd fallback). So the deny ENGINE is well-tested; the *resolution + subprocess*
//   seam — a real `node shim.mjs` reading a hook payload on stdin and resolving the
//   root from a real on-disk multi-repo layout — is NOT. This file closes that gap:
//   it builds a REAL multi-repo fixture on disk (git-init'd hub + siblings) and runs
//   the REAL `node src/adapter/claude/shim.mjs` subprocess for each scenario, reading
//   the verdict the platform would actually get from stdout.
//
// WHAT THIS DOES *NOT* COVER (honesty bar — auditor fold):
//   • It validates the MECHANISM (the deny engine reaching the right verdict on a
//     real layout, through the real subprocess entry, with the root resolved by the
//     real resolveRoot). It does NOT validate the orchestrator's multi-repo
//     COORDINATION — which sibling to touch, when to declare a component. That is
//     `[persona]` prose in the agent definitions and is not mechanically testable.
//     The E2E proves the enforcement the coordination relies on, never the
//     coordination itself.
//   • OpenCode asymmetry is two-layered and NOT faked into subprocess parity:
//       (1) OpenCode has no subprocess entry — its real entry is the in-process
//           plugin function AiPmEnforcement(ctx), and the root is supplied by the
//           runtime (ctx.directory), never resolved from git by our code.
//       (2) plugin-entry.mjs is not even loadable from the source tree: it imports
//           the engine via the DEPLOYED path (.ai-dev/tooling/...), which exists only
//           in an installed downstream project. (install-plugin.test.mjs covers the
//           install-time path rewrite that makes it loadable.)
//     So the closest source-tree-exercisable REAL OpenCode entry is the pure
//     `decide()` in opencode/normalise.mjs (which the plugin wraps unchanged). We
//     drive THAT against the SAME on-disk fixture + the SAME git-resolved hub root,
//     proving the same layout reaches the same verdicts through OpenCode's real
//     decision code. There is no OpenCode root-resolution seam to E2E — the runtime
//     supplies the root — so the asymmetry is recorded, not papered over.
//
// Run: node src/adapter/multirepo-e2e.test.mjs

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadConfig } from "./engine.mjs";
import { decide as ocDecide } from "./opencode/normalise.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHIM = path.join(HERE, "claude", "shim.mjs");
const config = loadConfig(HERE);

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; }
  else { fail++; console.log(`  ✗ ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
}

// ── REAL fixture: a git-init'd hub + 3 sibling repos on disk ──────────────────
// The hub is the session root: resolveRoot runs `git rev-parse --show-toplevel`
// from cwd=hub, so the hub MUST be a git repo whose toplevel is itself — hence
// `git init` per repo (siblings are SEPARATE repos so a payload cwd in the hub
// never walks up into a sibling). The parent is realpath'd: the manifest's
// declared roots are canonicalised via realpathSync in componentRoots, and git's
// --show-toplevel returns a canonical path, so the fixture must align (mirrors
// parity.test.mjs compWorkspace). All build inputs are literals — no shell, no
// untrusted data, no network.
function gitInit(dir) {
  execFileSync("git", ["init", "-q"], { cwd: dir, stdio: ["ignore", "ignore", "ignore"] });
}
function buildFixture() {
  const parent = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-mre2e-")));
  const hub = path.join(parent, "hub");
  const owner = path.join(parent, "owner");
  const worker = path.join(parent, "worker");
  const outsider = path.join(parent, "outsider");
  for (const d of [hub, owner, worker, outsider]) fs.mkdirSync(d, { recursive: true });
  for (const d of [hub, owner, worker, outsider]) gitInit(d);

  fs.mkdirSync(path.join(hub, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(hub, "README.md"), "readme");

  // owner: the seam-contract OWNER. Its manifest entry carries the advisory
  // `contracts` field (D6) naming the seam contract; the file exists on disk.
  fs.mkdirSync(path.join(owner, "docs", "contracts"), { recursive: true });
  fs.writeFileSync(path.join(owner, "docs", "contracts", "seam.md"), "# Seam contract\n");
  fs.writeFileSync(path.join(owner, "src.js"), "// owner source\n");

  // worker: a declared sibling carrying its OWN `.ai-dev/tooling/` dir (the
  // per-root carve-out target — a manifest must never widen into a sibling's
  // enforcer source).
  fs.mkdirSync(path.join(worker, ".ai-dev", "tooling"), { recursive: true });
  fs.writeFileSync(path.join(worker, ".ai-dev", "tooling", "engine.mjs"), "// worker tooling\n");
  fs.writeFileSync(path.join(worker, "src.js"), "// worker source\n");

  // outsider: a REAL sibling that is NOT declared in the manifest (deny target).
  fs.writeFileSync(path.join(outsider, "src.py"), "# outsider source\n");

  // The manifest at the hub: declares owner + worker by RELATIVE path; the owner
  // entry carries the advisory `contracts` field (D6, an owner-named seam).
  const manifest = [
    { root: "../owner", role: "owner", stack: "node", contracts: ["docs/contracts/seam.md"] },
    { root: "../worker", role: "worker", stack: "node" },
  ];
  writeManifest(hub, manifest);

  // resolveRoot resolves cwd=hub → this exact path; assert the fixture's git
  // toplevel IS the hub before any scenario (a fixture-vs-production-path sanity).
  const resolvedHub = execFileSync("git", ["rev-parse", "--show-toplevel"],
    { cwd: hub, encoding: "utf8" }).trim();
  check("fixture:git-toplevel-is-hub", resolvedHub, hub);

  return { parent, hub, owner, worker, outsider };
}
function writeManifest(hub, manifest) {
  fs.writeFileSync(path.join(hub, ".ai-dev", "components.json"), JSON.stringify(manifest));
}

// ── the REAL Claude entry: `node shim.mjs`, payload on stdin, verdict on stdout ─
// cwd in the payload is set to the hub so resolveRoot resolves the SAME root the
// production hook would. allow ⇒ empty stdout (the shim prints nothing); deny/ask ⇒
// stdout JSON carrying hookSpecificOutput.permissionDecision.
function shimVerdict(payload) {
  const out = execFileSync("node", [SHIM], { input: JSON.stringify(payload), encoding: "utf8" });
  const trimmed = out.trim();
  if (trimmed === "") return "allow";
  try { return JSON.parse(trimmed).hookSpecificOutput.permissionDecision; }
  catch { return `non-json:${trimmed.slice(0, 60)}`; }
}
function readPayload(filePath, hub) {
  return { tool_name: "Read", tool_input: { file_path: filePath }, cwd: hub };
}
function writePayload(filePath, hub) {
  return { tool_name: "Write", tool_input: { file_path: filePath, content: "x" }, cwd: hub };
}

const fx = buildFixture();
try {
  console.log("CLAUDE REAL SHIM SUBPROCESS (node shim.mjs, root resolved from the on-disk hub):");

  // 1. declared sibling read ⇒ allow (the widening, through the real entry).
  check("claude:declared-sibling-read",
    shimVerdict(readPayload(path.join(fx.owner, "src.js"), fx.hub)), "allow");

  // 2. declared sibling write ⇒ allow.
  check("claude:declared-sibling-write",
    shimVerdict(writePayload(path.join(fx.owner, "src.js"), fx.hub)), "allow");

  // 3. non-declared sibling read ⇒ deny.
  check("claude:non-declared-sibling-read",
    shimVerdict(readPayload(path.join(fx.outsider, "src.py"), fx.hub)), "deny");

  // 4. seam-contract read from the hub session ⇒ allow. This is the D6 claim — the
  //    widened boundary IS the transport for a cross-repo seam contract — proven
  //    END-TO-END through the real subprocess.
  check("claude:seam-contract-read-D6",
    shimVerdict(readPayload(path.join(fx.owner, "docs", "contracts", "seam.md"), fx.hub)), "allow");

  // 5. declared sibling's `.ai-dev/tooling/` WRITE ⇒ deny (per-root carve-out;
  //    a manifest must never widen into a sibling's enforcer source).
  check("claude:sibling-tooling-write",
    shimVerdict(writePayload(path.join(fx.worker, ".ai-dev", "tooling", "engine.mjs"), fx.hub)), "deny");

  // 6. declared sibling's `.ai-dev/tooling/` READ ⇒ deny (the read/find boundary
  //    predicate applies the per-root tooling carve-out before the allow).
  check("claude:sibling-tooling-read",
    shimVerdict(readPayload(path.join(fx.worker, ".ai-dev", "tooling", "engine.mjs"), fx.hub)), "deny");

  // 7. session-root read ⇒ allow (the root is always in the set).
  check("claude:session-root-read",
    shimVerdict(readPayload(path.join(fx.hub, "README.md"), fx.hub)), "allow");

  // 8. manifest REMOVED ⇒ the same declared-sibling read now DENIES — the
  //    no-manifest single-root regression tripwire, through the REAL path. Remove
  //    the manifest and re-run the real subprocess; the boundary collapses to the
  //    single session root and the sibling falls outside it.
  fs.rmSync(path.join(fx.hub, ".ai-dev", "components.json"));
  check("claude:no-manifest-sibling-read-denies",
    shimVerdict(readPayload(path.join(fx.owner, "src.js"), fx.hub)), "deny");
  // Sanity: the session root itself still reads with no manifest (single-root).
  check("claude:no-manifest-session-root-read",
    shimVerdict(readPayload(path.join(fx.hub, "README.md"), fx.hub)), "allow");
  // Restore the manifest for the OpenCode block below.
  writeManifest(fx.hub, [
    { root: "../owner", role: "owner", stack: "node", contracts: ["docs/contracts/seam.md"] },
    { root: "../worker", role: "worker", stack: "node" },
  ]);

  // ── OpenCode real-entry (in-process), SAME on-disk fixture + git-resolved root ──
  // See the header: OpenCode has no subprocess entry and plugin-entry.mjs is not
  // source-loadable (deployed-path import). The closest REAL source-tree entry is
  // the pure decide() in normalise.mjs, which the plugin wraps unchanged. We drive
  // it against the SAME on-disk fixture and the SAME root git resolved for the
  // Claude path — proving the same layout reaches the same widened-boundary verdicts
  // through OpenCode's real decision code. This is NOT a subprocess and NOT a
  // root-resolution E2E (OpenCode's runtime supplies the root; our adapter never
  // resolves it). The asymmetry is the honest finding, not faked parity.
  console.log("OPENCODE REAL decide() (in-process; same fixture + git-resolved root; runtime supplies root):");
  const hub = fx.hub; // the root OpenCode's runtime would pass as ctx.directory
  const ocV = (tool, args) => ocDecide(tool, args, hub, false, config).verdict;
  check("opencode:declared-sibling-read",
    ocV("read", { filePath: path.join(fx.owner, "src.js") }), "allow");
  check("opencode:non-declared-sibling-read",
    ocV("read", { filePath: path.join(fx.outsider, "src.py") }), "deny");
  check("opencode:seam-contract-read-D6",
    ocV("read", { filePath: path.join(fx.owner, "docs", "contracts", "seam.md") }), "allow");
  check("opencode:sibling-tooling-write",
    ocV("write", { filePath: path.join(fx.worker, ".ai-dev", "tooling", "engine.mjs"), content: "x" }), "deny");

  // Asymmetry guard: prove plugin-entry.mjs is genuinely NOT source-loadable (so the
  // claim above is not a lazy excuse — it is a real, current property of the tree).
  // A future install-layout change that makes it loadable should make THIS flip and
  // prompt a re-decision about subprocess-shaping the OpenCode entry.
  let pluginSourceLoadable = false;
  try {
    await import("./opencode/plugin-entry.mjs");
    pluginSourceLoadable = true;
  } catch { /* expected: deployed-path import throws from the source tree */ }
  check("opencode:plugin-entry-not-source-loadable", pluginSourceLoadable, false);
} finally {
  fs.rmSync(fx.parent, { recursive: true, force: true });
}

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
