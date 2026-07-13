// The agent's OWN out-of-root scratch allow-set — derivation (fail-closed) + the boundary
// carve-out, READ and WRITE. Units under test:
//   (1) deriveSanctionedScratch(env, root) — derives the tool-result overflow store,
//       the universal image-cache (sibling scan), and the per-session harness temp root from the
//       Claude env, fail-CLOSED (mirrors the componentRoots discipline:
//       bad/absent/non-existent/overbroad ⇒ empty set).
//   (1b) deriveSanctionedScratchWritable(env, root) — the WRITABLE subset of the same
//       tagged derivation: ONLY the per-session temp root, never the overflow store
//       (`#401`). Same fail-closed discipline as (1) — no logic duplicated between them.
//   (2) the engine read predicates consult input.sanctionedScratch ALONGSIDE the component
//       set — a sanctioned read is allowed, a non-sanctioned out-of-root read still denies.
//   (2b) the engine write predicate consults input.sanctionedScratchWritable — a write to
//       the per-session temp root allows, a write to the overflow store or any other
//       out-of-root/sibling-session path still denies.
//   (3) END-TO-END through the real `node shim.mjs` subprocess — the same real-write
//       carve-out and the same guards, driven through the actual hook entry.
//
// This is the validator-level unit test for the security-core widening. Run:
//   node src/adapter/claude/sanctioned-scratch.test.mjs

import { deriveSanctionedScratch, deriveSanctionedScratchWritable, _internals as scratchInternals } from "./sanctioned-scratch.mjs";
import { isInsideSanctioned } from "../engine-paths.mjs";
import { _internals } from "../engine.mjs";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHIM = path.join(HERE, "shim.mjs");

const { PREDICATES } = _internals;
const UID = typeof process.getuid === "function" ? process.getuid() : null;

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; }
  else { fail++; console.log(`  ✗ ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
}
function checkTrue(name, got) { check(name, !!got, true); }
function checkFalse(name, got) { check(name, !!got, false); }
function has(name, arr, val) {
  const ok = Array.isArray(arr) && arr.some((p) => p === val || p.startsWith(val + path.sep) || val.startsWith(p));
  if (ok) pass++; else { fail++; console.log(`  ✗ ${name}: ${JSON.stringify(arr)} missing ${val}`); }
}
function lacks(name, arr, fragment) {
  const ok = !Array.isArray(arr) || arr.every((p) => !p.includes(fragment));
  if (ok) pass++; else { fail++; console.log(`  ✗ ${name}: ${JSON.stringify(arr)} unexpectedly contains ${fragment}`); }
}

// realpath so expected paths match the derivation's realpathSync canonicalisation.
function ws() { return fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-scratch-"))); }

// Build a fake Claude env + the on-disk trees the derivation expects.
// `opts.{configDir, tmpBase, sid, makeToolResults, makeTemp}` control what exists.
function setup(opts = {}) {
  const root = ws();
  fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
  const slug = path.resolve(root).replace(/\//g, "-");
  const sid = opts.sid || "11111111-2222-3333-4444-555555555555";
  const configDir = opts.configDir || ws();
  const tmpBase = opts.tmpBase || ws();
  if (opts.makeToolResults !== false) {
    fs.mkdirSync(path.join(configDir, "projects", slug, "tool-results"), { recursive: true });
  }
  if (opts.makeTemp !== false && UID !== null) {
    fs.mkdirSync(path.join(tmpBase, `claude-${UID}`, slug, sid), { recursive: true });
  }
  const env = {
    CLAUDE_CONFIG_DIR: opts.omitConfig ? undefined : configDir,
    CLAUDE_CODE_SESSION_ID: opts.omitSid ? undefined : sid,
    TMPDIR: tmpBase,
    HOME: opts.home || "/home/fake-op",
  };
  return { root, slug, sid, configDir, tmpBase, env };
}

// ── (1) derivation ───────────────────────────────────────────────────────────

// happy path: both roots derived, slug correct, temp is session-id-narrow.
{
  const { root, slug, sid, configDir, tmpBase, env } = setup();
  const out = deriveSanctionedScratch(env, root);
  check("happy: 2 roots derived", out.length, 2);
  has("tool-results root derived", out, fs.realpathSync(path.join(configDir, "projects", slug, "tool-results")));
  has("per-session temp root derived", out, fs.realpathSync(path.join(tmpBase, `claude-${UID}`, slug, sid)));
  // session-id-narrow: the temp entry ends with THIS session's id (a sibling session's
  // <slug>/<other-sid>/ dir is NOT under it ⇒ its transcripts stay denied).
  const tempEntry = out.find((p) => p.includes(`claude-${UID}`));
  checkTrue("temp entry is sid-narrow (ends with the session id)", tempEntry && tempEntry.endsWith(path.sep + sid));
}

// missing CLAUDE_CONFIG_DIR ⇒ no tool-results entry (temp may still derive).
{
  const { root, env } = setup({ omitConfig: true });
  const out = deriveSanctionedScratch(env, root);
  lacks("no configDir ⇒ no tool-results", out, "tool-results");
}
// missing CLAUDE_CODE_SESSION_ID ⇒ no temp entry.
{
  const { root, configDir, env } = setup({ omitSid: true });
  const out = deriveSanctionedScratch(env, root);
  lacks("no sid ⇒ no temp", out, "11111111");
  has("no sid ⇒ tool-results still derived", out, fs.realpathSync(path.join(configDir, "projects")));
}

// derived dir does NOT exist ⇒ that entry absent (fail-closed), the other still derives.
{
  const { root, configDir, env } = setup({ makeTemp: false });
  const out = deriveSanctionedScratch(env, root);
  lacks("missing temp dir ⇒ no temp entry", out, "11111111");
  has("missing temp dir ⇒ tool-results still derived", out, fs.realpathSync(path.join(configDir, "projects")));
}
{
  const { root, tmpBase, env } = setup({ makeToolResults: false });
  const out = deriveSanctionedScratch(env, root);
  lacks("missing tool-results dir ⇒ no tool-results entry", out, "tool-results");
  has("missing tool-results dir ⇒ temp still derived", out, fs.realpathSync(path.join(tmpBase, `claude-${UID}`)));
}

// empty env ⇒ empty set (byte-identical to today's strict behaviour).
{
  const { root } = setup();
  check("empty env ⇒ []", deriveSanctionedScratch({}, root).length, 0);
  check("null-ish env ⇒ []", deriveSanctionedScratch({ CLAUDE_CONFIG_DIR: "  ", CLAUDE_CODE_SESSION_ID: "" }, root).length, 0);
}

// CLAUDE_CONFIG_DIR is a FILE (not a dir) ⇒ rejected (realDir → null), no tool-results.
{
  const { root } = setup();
  const fileAsDir = path.join(ws(), "notadir");
  fs.writeFileSync(fileAsDir, "x");
  const env = { CLAUDE_CONFIG_DIR: fileAsDir, CLAUDE_CODE_SESSION_ID: "sid", HOME: "/h" };
  lacks("configDir is a file ⇒ rejected", deriveSanctionedScratch(env, root), "notadir");
}

// a symlinked configDir resolves through realpath (the canonical path is admitted).
{
  const { root, slug } = setup({ makeToolResults: false });
  const real = ws();
  fs.mkdirSync(path.join(real, "projects", slug, "tool-results"), { recursive: true });
  const link = path.join(ws(), "config-link");
  fs.symlinkSync(real, link);
  const out = deriveSanctionedScratch({ CLAUDE_CONFIG_DIR: link, HOME: "/h" }, root);
  has("symlinked configDir resolves to realpath", out, fs.realpathSync(path.join(real, "projects", slug, "tool-results")));
}

// ── (1c) universal image-cache carve-out — sibling scan, read-only ────────────

// universal scan: sibling with image-cache/ is found regardless of sibling name.
{
  const parentDir = ws();
  const configDir = path.join(parentDir, ".claude");
  fs.mkdirSync(configDir, { recursive: true });
  const siblingDir = path.join(parentDir, ".my-custom-profile"); // not .claude-proxy — any name
  const imageCacheDir = path.join(siblingDir, "image-cache");
  fs.mkdirSync(imageCacheDir, { recursive: true });

  const { root } = setup({ configDir, makeToolResults: false, makeTemp: false });
  const env = { CLAUDE_CONFIG_DIR: configDir, HOME: "/home/fake-op" };
  const out = deriveSanctionedScratch(env, root);
  has("universal scan: image-cache found in any sibling", out, fs.realpathSync(imageCacheDir));
}

// no sibling with image-cache/ ⇒ fail-closed (nothing added for image-cache).
{
  const parentDir = ws();
  const configDir = path.join(parentDir, ".claude");
  fs.mkdirSync(configDir, { recursive: true });
  // Create a sibling WITHOUT image-cache/ — it should NOT be admitted.
  fs.mkdirSync(path.join(parentDir, ".some-sibling"), { recursive: true });

  const { root } = setup({ configDir, makeToolResults: false, makeTemp: false });
  const env = { CLAUDE_CONFIG_DIR: configDir, HOME: "/home/fake-op" };
  const out = deriveSanctionedScratch(env, root);
  lacks("no sibling with image-cache ⇒ not derived", out, "image-cache");
}

// multiple siblings with image-cache/ ⇒ all matching ones added.
{
  const parentDir = ws();
  const configDir = path.join(parentDir, ".claude");
  fs.mkdirSync(configDir, { recursive: true });
  const siblingA = path.join(parentDir, ".profile-a", "image-cache");
  const siblingB = path.join(parentDir, ".profile-b", "image-cache");
  fs.mkdirSync(siblingA, { recursive: true });
  fs.mkdirSync(siblingB, { recursive: true });

  const { root } = setup({ configDir, makeToolResults: false, makeTemp: false });
  const env = { CLAUDE_CONFIG_DIR: configDir, HOME: "/home/fake-op" };
  const out = deriveSanctionedScratch(env, root);
  has("multiple siblings: first image-cache found", out, fs.realpathSync(siblingA));
  has("multiple siblings: second image-cache found", out, fs.realpathSync(siblingB));
  // The config dir itself (no image-cache/) should NOT appear.
  lacks("multiple siblings: config dir itself not admitted", out, path.basename(configDir));
}

// image-cache is NEVER writable — it must not appear in deriveSanctionedScratchWritable.
{
  const parentDir = ws();
  const configDir = path.join(parentDir, ".claude");
  fs.mkdirSync(configDir, { recursive: true });
  const imageCacheDir = path.join(parentDir, ".some-profile", "image-cache");
  fs.mkdirSync(imageCacheDir, { recursive: true });

  const { root } = setup({ configDir, makeToolResults: false, makeTemp: false });
  const env = { CLAUDE_CONFIG_DIR: configDir, HOME: "/home/fake-op" };
  const writable = deriveSanctionedScratchWritable(env, root);
  lacks("image-cache NOT writable", writable, "image-cache");
}

// missing CLAUDE_CONFIG_DIR ⇒ no image-cache entry (fail-closed, mirrors tool-results).
{
  const { root, env } = setup({ omitConfig: true });
  const out = deriveSanctionedScratch(env, root);
  lacks("no configDir ⇒ no image-cache", out, "image-cache");
}

// ── (1b) deriveSanctionedScratchWritable — the WRITE-only subset (`#401`) ─────
// Same fail-closed derivation, exposed narrower: ONLY the per-session temp root, never
// the tool-result overflow store — the security invariant the whole write carve-out rests
// on.

// happy path: exactly the temp root, never the overflow store.
{
  const { root, slug, sid, tmpBase, env } = setup();
  const out = deriveSanctionedScratchWritable(env, root);
  check("writable: exactly 1 root", out.length, 1);
  has("writable: per-session temp root present", out, fs.realpathSync(path.join(tmpBase, `claude-${UID}`, slug, sid)));
  lacks("writable: overflow store NEVER present", out, "tool-results");
}

// missing CLAUDE_CODE_SESSION_ID ⇒ writable set is EMPTY (the overflow store never
// substitutes — there is nothing writable when the temp root cannot derive).
{
  const { root, env } = setup({ omitSid: true });
  check("writable: no sid ⇒ empty", deriveSanctionedScratchWritable(env, root).length, 0);
}

// missing CLAUDE_CONFIG_DIR (overflow store cannot derive) ⇒ writable set UNAFFECTED —
// the temp root still derives independently.
{
  const { root, slug, sid, tmpBase, env } = setup({ omitConfig: true });
  const out = deriveSanctionedScratchWritable(env, root);
  check("writable: no configDir ⇒ temp root still writable", out.length, 1);
  has("writable: no configDir ⇒ temp root present", out, fs.realpathSync(path.join(tmpBase, `claude-${UID}`, slug, sid)));
}

// empty/blank env ⇒ empty writable set (fail-closed, byte-identical to today's DENY for
// ANY out-of-root write).
{
  const { root } = setup();
  check("writable: empty env ⇒ []", deriveSanctionedScratchWritable({}, root).length, 0);
  check("writable: null-ish env ⇒ []", deriveSanctionedScratchWritable({ CLAUDE_CONFIG_DIR: "  ", CLAUDE_CODE_SESSION_ID: "" }, root).length, 0);
}

// session-id-narrow: a SIBLING session's temp dir (same slug, different sid) is NOT in
// the writable set derived for THIS session's sid — the derivation only ever builds ITS
// OWN sid's path, so a sibling path never appears regardless of what exists on disk.
{
  const { root, slug, tmpBase, env } = setup({ sid: "aaaa-1111" });
  const siblingSid = "bbbb-2222";
  fs.mkdirSync(path.join(tmpBase, `claude-${UID}`, slug, siblingSid), { recursive: true });
  const out = deriveSanctionedScratchWritable(env, root);
  check("writable: exactly this session's temp root", out.length, 1);
  lacks("writable: sibling sid NOT admitted", out, siblingSid);
  has("writable: this session's own sid admitted", out, fs.realpathSync(path.join(tmpBase, `claude-${UID}`, slug, "aaaa-1111")));
}

// derived temp dir does NOT exist ⇒ writable set empty (fail-closed — mirrors the read
// derivation's own missing-dir case).
{
  const { root } = setup({ makeTemp: false });
  check("writable: missing temp dir ⇒ []", deriveSanctionedScratchWritable({ CLAUDE_CODE_SESSION_ID: "sid-x", TMPDIR: ws(), HOME: "/h" }, root).length, 0);
}

// the overbroad guard applies identically to the writable derivation (shares `admissible`
// with the read derivation via the one internal deriveTagged — no separate, weaker guard).
{
  const { admissible } = scratchInternals;
  const root = fs.realpathSync(ws());
  const boundary = fs.realpathSync(ws());
  const sanctioned = path.join(boundary, "child");
  checkTrue("writable derivation shares the SAME admissible guard (happy path)", admissible(sanctioned, root, boundary));
  checkFalse("writable derivation shares the SAME admissible guard (ancestor rejected)", admissible(path.dirname(root), root, boundary));
}

// ── (2) isInsideSanctioned (pure) ────────────────────────────────────────────
{
  const S = ["/tmp/scratch"];
  checkTrue("inside (child)", isInsideSanctioned(S, "/tmp/scratch/foo.txt"));
  checkTrue("inside (equal)", isInsideSanctioned(S, "/tmp/scratch"));
  checkFalse("not inside (sibling prefix)", isInsideSanctioned(S, "/tmp/scratch-other/x"));
  checkFalse("not inside (no overlap)", isInsideSanctioned(S, "/etc/passwd"));
  checkFalse("empty set ⇒ false", isInsideSanctioned([], "/tmp/scratch/x"));
  checkFalse("undefined ⇒ false", isInsideSanctioned(undefined, "/tmp/scratch/x"));
  // prefix-collision guard: /tmp/scra must NOT match /tmp/scratch
  checkFalse("prefix collision (/tmp/scra ≠ /tmp/scratch)", isInsideSanctioned(["/tmp/scra"], "/tmp/scratch/foo"));
}

// ── (2b) admissible — the overbroad guard (security-critical narrowing) ───────
// Direct unit tests of the guard with controlled canonical paths — every overbroad
// shape rejects, the happy path admits. The env-derived black box above cannot plausibly
// PRODUCE an overbroad path from real harness env, so the guard's four reject-branches
// are exercised here in isolation (mirrors how components.test.mjs pins componentRoots).
{
  const { admissible } = scratchInternals;
  const root = fs.realpathSync(ws());            // a real session root
  const rootParent = path.dirname(root);         // an ancestor of the root
  const boundary = fs.realpathSync(ws());        // a specific boundary dir (NOT /tmp itself)
  const sanctioned = path.join(boundary, "child"); // a proper descendant of the boundary (happy)
  const escaped = fs.realpathSync(ws());         // a real dir OUTSIDE the boundary (escape shape)

  // (1) null/empty ⇒ rejected.
  checkFalse("admissible: null ⇒ rejected", admissible(null, root, boundary));
  checkFalse("admissible: empty ⇒ rejected", admissible("", root, boundary));
  // (2) a filesystem root ⇒ rejected.
  checkFalse("admissible: filesystem root ⇒ rejected", admissible("/", root, boundary));
  // (3) ancestor-of / equal-to the session root ⇒ rejected (would re-expose everything above the work).
  checkFalse("admissible: the session root itself ⇒ rejected", admissible(root, root, boundary));
  checkFalse("admissible: ancestor of session root ⇒ rejected", admissible(rootParent, root, boundary));
  // (4) a path that escaped its parent (realpath left the boundary) ⇒ rejected.
  checkFalse("admissible: outside the boundary (escape) ⇒ rejected", admissible(escaped, root, boundary));
  // (5) the boundary itself (the shared parent — too broad, would admit a sibling) ⇒ rejected.
  checkFalse("admissible: the boundary itself ⇒ rejected", admissible(boundary, root, boundary));
  // (6) the happy path: a proper descendant of the boundary, outside the root ⇒ admitted.
  checkTrue("admissible: proper descendant of boundary, outside root ⇒ admitted", admissible(sanctioned, root, boundary));
  // No boundary passed ⇒ only the (1)(2)(3) checks apply (the boundary branches are skipped).
  checkTrue("admissible: no boundary, outside root ⇒ admitted", admissible(sanctioned, root));
  checkFalse("admissible: no boundary, ancestor of root ⇒ rejected", admissible(rootParent, root));
}

// ── (3) engine read predicates consult sanctionedScratch ─────────────────────
// Real tmp root so componentRoots' realpath behaves; a real sanctioned dir.
{
  const root = ws();
  fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
  const san = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-san-")));
  const sanFile = path.join(san, "overflow.txt");
  const foreign = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-foreign-"))) + "/secret";

  // READ a sanctioned path ⇒ outside-root FALSE (allowed).
  checkFalse("read sanctioned ⇒ allowed", PREDICATES.pathOutsideRoot(
    { root, path: sanFile, sanctionedScratch: [san] }, null));
  // READ a non-sanctioned out-of-root path ⇒ TRUE (denied).
  checkTrue("read foreign ⇒ denied", PREDICATES.pathOutsideRoot(
    { root, path: foreign, sanctionedScratch: [san] }, null));
  // READ sanctioned but sanctionedScratch absent ⇒ TRUE (today's strict behaviour).
  checkTrue("read sanctioned but no set ⇒ denied (strict)", PREDICATES.pathOutsideRoot(
    { root, path: sanFile }, null));

  // WRITE to a read-sanctioned path with NO sanctionedScratchWritable ⇒ TRUE (denied) —
  // the pre-`#401` regression case: a path admitted for READ only (e.g. the overflow
  // store) never gets a write allow just because it is in the READ set.
  checkTrue("write to read-sanctioned (no writable set) ⇒ denied — overflow-store shape", PREDICATES.writeTargetOutsideRoot(
    { act: "write", root, path: sanFile, sanctionedScratch: [san] }));
  // WRITE to that SAME path when it is explicitly NOT in sanctionedScratchWritable (the
  // real overflow-store shape: readable, never writable) ⇒ TRUE (denied).
  checkTrue("write to overflow-store-shaped path (readable, not writable) ⇒ denied", PREDICATES.writeTargetOutsideRoot(
    { act: "write", root, path: sanFile, sanctionedScratch: [san], sanctionedScratchWritable: [] }));
  // WRITE to a path that IS in sanctionedScratchWritable (the temp-root shape) ⇒ FALSE
  // (allowed) — the `#401` carve-out itself.
  checkFalse("write to sanctionedScratchWritable path ⇒ allowed — temp-root shape (#401)", PREDICATES.writeTargetOutsideRoot(
    { act: "write", root, path: sanFile, sanctionedScratchWritable: [san] }));
  // WRITE to a DIFFERENT out-of-root path while sanctionedScratchWritable is set to
  // something else entirely ⇒ TRUE (denied) — the writable set does not blanket-allow.
  checkTrue("write to foreign path while an unrelated writable set exists ⇒ denied", PREDICATES.writeTargetOutsideRoot(
    { act: "write", root, path: foreign, sanctionedScratchWritable: [san] }));

  // FIND a sanctioned path ⇒ outside-root FALSE (allowed).
  checkFalse("find sanctioned ⇒ allowed", PREDICATES.findTargetOutsideRoot(
    { root, command: `find ${san} -name x`, sanctionedScratch: [san] }, null));
  // FIND a non-sanctioned out-of-root path ⇒ TRUE (denied).
  checkTrue("find foreign ⇒ denied", PREDICATES.findTargetOutsideRoot(
    { root, command: `find ${foreign} -name x`, sanctionedScratch: [san] }, null));

  // BASH-READ (`cat`) a sanctioned path with `~` expanded via input.home ⇒ allowed.
  const home = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-home-")));
  const sanViaHome = path.join(home, "scratch");
  fs.mkdirSync(sanViaHome, { recursive: true });
  checkFalse("bash cat ~/sanctioned (home set) ⇒ allowed", PREDICATES.bashReadTargetOutsideRoot(
    { root, command: `cat ~/scratch/foo`, sanctionedScratch: [sanViaHome], home }, null));
  // BASH-READ a `~`-path with NO home ⇒ TRUE (denied — unresolvable, original behaviour).
  checkTrue("bash cat ~/x (no home) ⇒ denied", PREDICATES.bashReadTargetOutsideRoot(
    { root, command: `cat ~/scratch/foo`, sanctionedScratch: [sanViaHome] }, null));
  // BASH-READ a non-sanctioned absolute path ⇒ TRUE (denied).
  checkTrue("bash cat foreign ⇒ denied", PREDICATES.bashReadTargetOutsideRoot(
    { root, command: `cat ${foreign}`, sanctionedScratch: [san] }, null));
}

// ── (4) END-TO-END through the REAL `node shim.mjs` subprocess ───────────────
// Mirrors multirepo-e2e.test.mjs's discipline: drive the REAL hook entry (stdin payload,
// stdout verdict), not the pure decide() function, over a REAL on-disk fixture that
// reproduces the harness's own env-derived layout — the overflow store, this session's
// temp root, and a SIBLING session's temp root (same slug, different sid). Proves the
// carve-out (and every guard) through the exact path a live Claude PreToolUse hook runs.
console.log("\nEND-TO-END (real node shim.mjs subprocess):");
{
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-scratch-e2e-")));
  fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
  const slug = path.resolve(root).replace(/\//g, "-");
  const sid = "e2e-1111-2222-3333";
  const siblingSid = "e2e-9999-8888-7777";
  const configDir = ws();
  // Deliberately NOT overriding TMPDIR in the child env: some Node builds/sandboxes
  // strip a caller-supplied TMPDIR from a spawned `node` subprocess's own process.env
  // (observed in this dev environment), which would silently break a fixture built
  // under a custom TMPDIR. The derivation's own fallback is `env.TMPDIR || os.tmpdir()`
  // (sanctioned-scratch.mjs) — so building the fixture under THIS process's real
  // `os.tmpdir()` (whatever it resolves to) matches what the child will independently
  // resolve too, with no env-var round-trip to depend on.
  const tmpBase = os.tmpdir();

  const tempRoot = path.join(tmpBase, `claude-${UID}`, slug, sid);
  const siblingTempRoot = path.join(tmpBase, `claude-${UID}`, slug, siblingSid);
  const overflowRoot = path.join(configDir, "projects", slug, "tool-results");
  fs.mkdirSync(tempRoot, { recursive: true });
  fs.mkdirSync(siblingTempRoot, { recursive: true });
  fs.mkdirSync(overflowRoot, { recursive: true });
  const foreign = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-e2e-foreign-")));

  const env = {
    ...process.env,
    CLAUDE_CONFIG_DIR: configDir,
    CLAUDE_CODE_SESSION_ID: sid,
    HOME: typeof process.env.HOME === "string" ? process.env.HOME : "/root",
  };

  // shimVerdict: run the REAL shim subprocess with the fixture env, exactly the shape a
  // live PreToolUse hook payload carries. allow ⇒ empty stdout; deny/ask ⇒ JSON verdict.
  function shimVerdict(payload) {
    const out = execFileSync("node", [SHIM], {
      input: JSON.stringify(payload),
      encoding: "utf8",
      env,
    });
    const trimmed = out.trim();
    if (trimmed === "") return "allow";
    try { return JSON.parse(trimmed).hookSpecificOutput.permissionDecision; }
    catch { return `non-json:${trimmed.slice(0, 60)}`; }
  }
  function writePayload(filePath) {
    return { tool_name: "Write", tool_input: { file_path: filePath, content: "x" }, cwd: root };
  }
  function readPayload(filePath) {
    return { tool_name: "Read", tool_input: { file_path: filePath }, cwd: root };
  }

  // RED→GREEN evidence lives in the git history of this change (the engine carve-out
  // commit) — these assertions are the GREEN state, proven against the real subprocess:

  // 1. A real WRITE to the derived temp root ⇒ ALLOWS — the carve-out itself (`#401`).
  check("e2e: write to own temp root ⇒ allow",
    shimVerdict(writePayload(path.join(tempRoot, "scratch.txt"))), "allow");

  // 2. A WRITE to the tool-result overflow store ⇒ DENIES — the overflow-read-only guard.
  check("e2e: write to overflow store ⇒ deny",
    shimVerdict(writePayload(path.join(overflowRoot, "spilled.txt"))), "deny");

  // 3. A WRITE to an arbitrary out-of-root path (no relation to either sanctioned root)
  //    ⇒ DENIES — the boundary floor is otherwise untouched.
  check("e2e: write to arbitrary out-of-root path ⇒ deny",
    shimVerdict(writePayload(path.join(foreign, "x.txt"))), "deny");

  // 4. A WRITE to a SIBLING session's temp root (same slug, different sid) ⇒ DENIES —
  //    the session-id-narrow guard, proven end-to-end.
  check("e2e: write to sibling-session temp root ⇒ deny",
    shimVerdict(writePayload(path.join(siblingTempRoot, "x.txt"))), "deny");

  // 5. READS to both sanctioned roots still ALLOW — the read widening is unchanged by
  //    this feature (regression, through the real subprocess).
  check("e2e: read own temp root ⇒ allow (regression)",
    shimVerdict(readPayload(path.join(tempRoot, "scratch.txt"))), "allow");
  check("e2e: read overflow store ⇒ allow (regression)",
    shimVerdict(readPayload(path.join(overflowRoot, "spilled.txt"))), "allow");

  // 6. A READ to the sibling session's temp root still DENIES (read-side session-id-narrow
  //    guard, unaffected by the write carve-out — regression).
  check("e2e: read sibling-session temp root ⇒ deny (regression)",
    shimVerdict(readPayload(path.join(siblingTempRoot, "x.txt"))), "deny");

  fs.rmSync(root, { recursive: true, force: true });
  fs.rmSync(configDir, { recursive: true, force: true });
  // tmpBase is the SHARED os.tmpdir(), not a dedicated fixture dir — remove only the
  // subtree this fixture created under it (`claude-<uid>/<slug>/`), never the shared root.
  fs.rmSync(path.join(tmpBase, `claude-${UID}`, slug), { recursive: true, force: true });
  fs.rmSync(foreign, { recursive: true, force: true });
}

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — sanctioned-scratch: ${pass} passed${fail ? `, ${fail} FAILED` : ""}`);
process.exit(fail === 0 ? 0 : 1);
