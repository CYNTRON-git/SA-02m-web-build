// Self-test for the whole-set quality runner (run.mjs). Drives run() against
// SYNTHETIC temp registries — never the real tools.json (that would recurse: the
// real registry's run-quality row IS this test). Every call passes registryPath
// EXPLICITLY: the runner's default now resolves beside run.mjs itself (the real
// co-located tools.json), so leaning on the default would read — and recurse on
// — the real registry. Covers the four contract paths: all-pass ⇒ 0, a failing
// row ⇒ 1, zero matching rows ⇒ 0, malformed ⇒ 1. Also covers scope-mode
// (covers matching / non-matching / no-covers / malformed) and the
// fileMatchesCovers and filterByScope functions in isolation.
//
// Run: node src/quality/run.test.mjs

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { run, resolveRegistry, coversToRegex, fileMatchesCovers, filterByScope } from "./run.mjs";

let pass = 0;
const fails = [];
function check(name, got, want) {
  if (got === want) { pass++; return; }
  fails.push(`  ✗ ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

// A temp dir holding a synthetic tools.json with the given content (a JS object
// ⇒ stringified, or a raw string ⇒ written verbatim for the malformed case).
// Returns { root, registryPath } — root is the execSync cwd, registryPath is the
// explicit registry passed to run() so the default (beside the real run.mjs) is
// never consulted.
function rootWith(content) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qrun-"));
  const text = typeof content === "string" ? content : JSON.stringify(content);
  const registryPath = path.join(root, "tools.json");
  fs.writeFileSync(registryPath, text);
  return { root, registryPath };
}

// Silence the runner's per-row chatter so the test output stays clean; the
// child commands themselves inherit stdio, so a real failure still surfaces.
const realLog = console.log;
const quiet = (fn) => { console.log = () => {}; try { return fn(); } finally { console.log = realLog; } };

// 1. All matched rows pass ⇒ exit 0.
{
  const { root, registryPath } = rootWith({ tools: [
    { id: "ok-a", run: "node -e \"process.exit(0)\"", beat: "build" },
    { id: "ok-b", run: "node -e \"process.exit(0)\"", beat: "build" },
    { id: "other-beat", run: "node -e \"process.exit(1)\"", beat: "review" },
  ] });
  check("all-pass build ⇒ 0", quiet(() => run("build", root, registryPath)), 0);
}

// 2. A matched row fails ⇒ exit 1.
{
  const { root, registryPath } = rootWith({ tools: [
    { id: "ok", run: "node -e \"process.exit(0)\"", beat: "build" },
    { id: "boom", run: "node -e \"process.exit(1)\"", beat: "build" },
  ] });
  check("failing row ⇒ 1", quiet(() => run("build", root, registryPath)), 1);
}

// 3. Zero rows match the beat ⇒ no-op success, exit 0.
{
  const { root, registryPath } = rootWith({ tools: [
    { id: "only-build", run: "node -e \"process.exit(0)\"", beat: "build" },
  ] });
  check("zero matches (ship) ⇒ 0", quiet(() => run("ship", root, registryPath)), 0);
}

// 4. Malformed registry ⇒ fail closed, exit 1.
{
  const badJson = rootWith("{ this is not json");
  check("unparseable JSON ⇒ 1", quiet(() => run("build", badJson.root, badJson.registryPath)), 1);

  const noArray = rootWith({ notTools: [] });
  check("missing tools array ⇒ 1", quiet(() => run("build", noArray.root, noArray.registryPath)), 1);
}

// 5. Absent registry ⇒ no-op success (a downstream may define no checks). The
// explicit registryPath points at a file that does not exist — so the default
// (beside the real run.mjs) is still never consulted.
{
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "qrun-empty-"));
  check("absent tools.json ⇒ 0", quiet(() => run("build", empty, path.join(empty, "tools.json"))), 0);
}

// 6. Unknown beat arg ⇒ non-zero (and never interpolated into a command).
{
  const { root, registryPath } = rootWith({ tools: [{ id: "x", run: "node -e \"process.exit(0)\"", beat: "build" }] });
  check("unknown beat ⇒ non-zero", quiet(() => run("bogus", root, registryPath)) !== 0, true);
}

// 7. Project-vs-template resolution: when a project tools.json exists at
// `<root>/src/quality/tools.json`, the runner resolves THAT path (not the
// co-located one beside run.mjs). Tested via resolveRegistry (no command exec).
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qrun-resolve-"));
  const srcQ = path.join(root, "src", "quality");
  fs.mkdirSync(srcQ, { recursive: true });
  const projectTools = path.join(srcQ, "tools.json");
  fs.writeFileSync(projectTools, JSON.stringify({ tools: [] }));
  const resolved = resolveRegistry(root);
  check("project tools preferred", resolved, projectTools);
}

// 8. Template fallback: when no project tools exist, the runner falls back
// to the co-located file beside this runner.
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qrun-fb-"));
  const expected = path.join(path.dirname(new URL(import.meta.url).pathname), "tools.json");
  check("fallback to co-located", resolveRegistry(root), expected);
}

// 9. Explicit registryPath overrides both project and co-located resolution.
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qrun-explicit-"));
  const srcQ = path.join(root, "src", "quality");
  fs.mkdirSync(srcQ, { recursive: true });
  fs.writeFileSync(path.join(srcQ, "tools.json"), JSON.stringify({ tools: [] }));
  const explicit = path.join(root, "my-custom-tools.json");
  fs.writeFileSync(explicit, JSON.stringify({ tools: [] }));
  check("explicit registryPath overrides", resolveRegistry(root, explicit), explicit);
}

// --- Scope-mode tests ---

// 10. coversToRegex: basic patterns.
{
  const re = coversToRegex("src/adapter/install-core.mjs");
  check("coversToRegex exact match", re.test("src/adapter/install-core.mjs"), true);
  check("coversToRegex exact no-match", re.test("src/adapter/other.mjs"), false);
}

// 11. coversToRegex: ** glob.
{
  const re = coversToRegex("src/adapter/**");
  check("coversToRegex ** deep match", re.test("src/adapter/sub/deep/file.mjs"), true);
  check("coversToRegex ** shallow match", re.test("src/adapter/foo.mjs"), true);
  check("coversToRegex ** no-match", re.test("src/quality/foo.mjs"), false);
}

// 12. coversToRegex: * glob (single segment).
{
  const re = coversToRegex("src/adapter/*.test.mjs");
  check("coversToRegex * match", re.test("src/adapter/foo.test.mjs"), true);
  check("coversToRegex * no-match deep", re.test("src/adapter/sub/foo.test.mjs"), false);
  check("coversToRegex * no-match ext", re.test("src/adapter/foo.mjs"), false);
}

// 13. coversToRegex: special characters are escaped — never throws, never null.
// A bracket, paren, etc. are literal-matched after escaping; there is no
// "malformed" glob that breaks the converter (fail-safe by construction).
{
  const re = coversToRegex("[unclosed");
  check("coversToRegex special chars escaped", re instanceof RegExp, true);
  check("coversToRegex literal bracket match", re.test("[unclosed"), true);
}

// 14. fileMatchesCovers: one glob matches.
{
  const ok = fileMatchesCovers("src/adapter/install-core.mjs", ["src/adapter/install-core.mjs"]);
  check("fileMatchesCovers single exact", ok, true);
}

// 15. fileMatchesCovers: none match.
{
  const ok = fileMatchesCovers("src/quality/run.mjs", ["src/adapter/**", "docs/**"]);
  check("fileMatchesCovers none match", ok, false);
}

// 16. fileMatchesCovers: one of several matches.
{
  const ok = fileMatchesCovers("src/adapter/install-core.mjs", [
    "src/quality/**",
    "src/adapter/install-core.mjs",
  ]);
  check("fileMatchesCovers one of many", ok, true);
}

// 17. fileMatchesCovers: empty covers → false (no pattern to match).
{
  const ok = fileMatchesCovers("src/foo.mjs", []);
  check("fileMatchesCovers empty covers", ok, false);
}

// 17b. fileMatchesCovers: array of files — matches if any file matches any pattern.
{
  const ok = fileMatchesCovers(["docs/foo.md", "src/adapter/install-core.mjs"], ["src/adapter/**"]);
  check("fileMatchesCovers array of files", ok, true);
}

// 18. filterByScope: row with covers matching a touched file → kept.
{
  const rows = [
    { id: "a", beat: "build", run: "node -e 0", covers: ["src/adapter/**"] },
  ];
  const filtered = filterByScope(rows, ["src/adapter/install-core.mjs"]);
  check("filterByScope matching covers → kept", filtered.length, 1);
}

// 19. filterByScope: row with covers NOT matching any touched file → dropped.
{
  const rows = [
    { id: "a", beat: "build", run: "node -e 0", covers: ["docs/**"] },
  ];
  const filtered = filterByScope(rows, ["src/adapter/install-core.mjs"]);
  check("filterByScope non-matching covers → dropped", filtered.length, 0);
}

// 20. filterByScope: row WITHOUT covers → always kept (fail-safe).
{
  const rows = [
    { id: "a", beat: "build", run: "node -e 0" },
  ];
  const filtered = filterByScope(rows, ["docs/readme.md"]);
  check("filterByScope no covers → kept", filtered.length, 1);
}

// 21. filterByScope: row with empty covers array → always kept (fail-safe).
{
  const rows = [
    { id: "a", beat: "build", run: "node -e 0", covers: [] },
  ];
  const filtered = filterByScope(rows, ["docs/readme.md"]);
  check("filterByScope empty covers → kept", filtered.length, 1);
}

// 22. filterByScope: null/undefined touchedFiles → all rows kept (full run).
{
  const rows = [
    { id: "a", beat: "build", run: "node -e 0", covers: ["src/adapter/**"] },
  ];
  check("filterByScope null touched → all kept", filterByScope(rows, null).length, 1);
  check("filterByScope empty touched → all kept", filterByScope(rows, []).length, 1);
}

// 23. E2E scope mode: touched file matches row's covers → row runs (exit 0).
{
  const { root, registryPath } = rootWith({ tools: [
    { id: "in-scope", run: "node -e \"process.exit(0)\"", beat: "build", covers: ["src/adapter/**"] },
  ] });
  check("scope e2e matching row runs", quiet(() => run("build", root, registryPath, ["src/adapter/foo.mjs"])), 0);
}

// 24. E2E scope mode: touched file does NOT match row's covers → row skipped, exit 0.
{
  const { root, registryPath } = rootWith({ tools: [
    { id: "out-of-scope", run: "node -e \"process.exit(1)\"", beat: "build", covers: ["src/adapter/**"] },
  ] });
  check("scope e2e non-matching row skipped", quiet(() => run("build", root, registryPath, ["docs/readme.md"])), 0);
}

// 25. E2E scope mode: no-covers row always runs even when touched set is unrelated.
{
  const { root, registryPath } = rootWith({ tools: [
    { id: "always", run: "node -e \"process.exit(0)\"", beat: "build" },
  ] });
  check("scope e2e no-covers always runs", quiet(() => run("build", root, registryPath, ["docs/readme.md"])), 0);
}

// 26. E2E scope mode: malformed covers → fails safe to always-run (row runs, not skipped).
{
  const { root, registryPath } = rootWith({ tools: [
    { id: "malformed-covers", run: "node -e \"process.exit(0)\"", beat: "build", covers: ["[bad" ] },
  ] });
  check("scope e2e malformed covers → runs", quiet(() => run("build", root, registryPath, ["unrelated/file.md"])), 0);
}

// 27. E2E scope mode: mix of scoped and unscoped rows — only matching scoped runs.
{
  const { root, registryPath } = rootWith({ tools: [
    { id: "always-a", run: "node -e \"process.exit(0)\"", beat: "build" },
    { id: "in-scope", run: "node -e \"process.exit(0)\"", beat: "build", covers: ["src/adapter/**"] },
    { id: "out-of-scope", run: "node -e \"process.exit(1)\"", beat: "build", covers: ["docs/**"] },
    { id: "always-b", run: "node -e \"process.exit(0)\"", beat: "build" },
  ] });
  // touched is src/adapter/* → in-scope runs, out-of-scope skipped, always-a/b run.
  check("scope e2e mixed set", quiet(() => run("build", root, registryPath, ["src/adapter/foo.mjs"])), 0);
}

if (fails.length) {
  console.log("RUN-QUALITY SELF-TEST:");
  fails.forEach((f) => console.log(f));
  console.log(`\nFAIL — ${fails.length} case(s) failed`);
  process.exit(1);
}
console.log(`PASS — ${pass} passed`);
