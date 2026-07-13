// Multi-repo components — the fail-CLOSED manifest loader/validator
// (_internals.componentRoots in engine.mjs).
//
// This is the validator-level unit test (the loader/validator in isolation); the
// boundary-wiring + per-root tooling carve-out are covered by the parity matrix in
// parity.test.mjs (`.ai-dev/plans/multi-repo-components.md`).
//
// The load-bearing promise: on ANY doubt (absent / malformed / overbroad /
// non-existent / wrong-shape) the permitted set collapses to the SINGLE session
// root — never widen on doubt. Every fail-closed branch + the happy path below.
//
// Each case builds a tmp session root, optionally a sibling tree + a
// components.json, and asserts the canonical root SET componentRoots returns.
//
// Run: node src/adapter/components.test.mjs

import { _internals } from "./engine.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const { componentRoots } = _internals;

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; }
  else { fail++; console.log(`  ✗ ${name}: got ${got}, want ${want}`); }
}
// Compare two root sets order-independently (componentRoots uses a Set; order is
// insertion-dependent and not a contract).
function sameSet(name, got, want) {
  const g = [...got].sort();
  const w = [...want].sort();
  check(name, JSON.stringify(g), JSON.stringify(w));
}

// A fresh tmp workspace: a parent dir holding `host/` (the session root) plus any
// requested sibling dirs. realpath the parent so the expected canonical paths match
// what fs.realpathSync returns inside the validator (macOS /tmp → /private/tmp etc).
function workspace(siblings = []) {
  const parent = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-comp-")));
  const host = path.join(parent, "host");
  fs.mkdirSync(path.join(host, ".ai-dev"), { recursive: true });
  for (const s of siblings) fs.mkdirSync(path.join(parent, s), { recursive: true });
  return { parent, host };
}
// Write the manifest verbatim (a string lets us inject malformed bytes / wrong shapes).
function writeManifest(host, body) {
  fs.writeFileSync(path.join(host, ".ai-dev", "components.json"), body);
}
function cleanup(parent) { fs.rmSync(parent, { recursive: true, force: true }); }

console.log("COMPONENTS — fail-closed manifest validator:");

// ── happy path ───────────────────────────────────────────────────────────────
// A valid manifest listing two existing sibling dirs ⇒ the set is {host, A, B}.
{
  const { parent, host } = workspace(["frontend", "backend"]);
  writeManifest(host, JSON.stringify([
    { root: "../frontend", role: "frontend", stack: "react" },
    { root: "../backend", role: "backend", stack: "python" },
  ]));
  sameSet("happy:two-siblings", componentRoots(host), [
    host, path.join(parent, "frontend"), path.join(parent, "backend"),
  ]);
  cleanup(parent);
}

// The session root is ALWAYS in the set, even with a valid extra sibling.
{
  const { parent, host } = workspace(["frontend"]);
  writeManifest(host, JSON.stringify([{ root: "../frontend", role: "frontend", stack: "x" }]));
  const got = componentRoots(host);
  check("happy:session-root-always-present", got.includes(host), true);
  cleanup(parent);
}

// A declared root that IS the session root itself (".") is allowed — equal to the
// session root, not an ancestor of it; it adds nothing new but does not reject.
{
  const { parent, host } = workspace([]);
  writeManifest(host, JSON.stringify([{ root: ".", role: "self", stack: "x" }]));
  sameSet("happy:self-reference", componentRoots(host), [host]);
  cleanup(parent);
}

// ── advisory `contracts` field (seam-contract transport, D6) ───────────────────
// The optional `contracts` field is ADVISORY metadata the orchestrator reads; the
// security validator (componentRoots) must IGNORE it — it reads only `root`. These
// pin that the field is structurally inert: it neither breaks the happy path nor
// opens a fail-closed seam. (`docs/decisions/seam-contract-transport.md`.)

// A `contracts`-bearing entry STILL widens correctly — the advisory field is ignored,
// never rejecting the entry as malformed.
{
  const { parent, host } = workspace(["backend", "frontend"]);
  writeManifest(host, JSON.stringify([
    { root: "../backend", role: "backend", stack: "python",
      contracts: [{ path: "docs/contracts/api.md", consumers: ["frontend"] }] },
    { root: "../frontend", role: "frontend", stack: "react" },
  ]));
  sameSet("advisory:contracts-still-widens", componentRoots(host), [
    host, path.join(parent, "backend"), path.join(parent, "frontend"),
  ]);
  cleanup(parent);
}

// A `contracts`-bearing entry whose `root` is OVERBROAD still fails CLOSED to the
// single session root — the advisory field does not smuggle a path past the floor,
// and one bad root rejects the whole manifest (all-or-nothing) even with the field.
{
  const { parent, host } = workspace(["backend"]);
  writeManifest(host, JSON.stringify([
    { root: "/", role: "evil", stack: "x",
      contracts: [{ path: "docs/contracts/api.md", consumers: ["frontend"] }] },
  ]));
  sameSet("advisory:contracts-with-bad-root-fails-closed", componentRoots(host), [host]);
  cleanup(parent);
}

// A malformed `contracts` VALUE (a string, not an array) on an otherwise-valid entry
// is inert — the validator never touches the field, so any junk value is ignored and
// the valid `root` still widens. Proves no crash/reject from an unrecognised value.
{
  const { parent, host } = workspace(["backend"]);
  writeManifest(host, JSON.stringify([
    { root: "../backend", role: "backend", stack: "python", contracts: "not-an-array" },
  ]));
  sameSet("advisory:junk-contracts-value-ignored", componentRoots(host), [
    host, path.join(parent, "backend"),
  ]);
  cleanup(parent);
}

// ── fail-closed: absent / unreadable ───────────────────────────────────────────
{
  const { parent, host } = workspace([]); // no manifest written
  sameSet("closed:absent", componentRoots(host), [host]);
  cleanup(parent);
}

// ── fail-closed: JSON parse failure ────────────────────────────────────────────
{
  const { parent, host } = workspace(["frontend"]);
  writeManifest(host, "{ this is not valid json ][");
  sameSet("closed:malformed-json", componentRoots(host), [host]);
  cleanup(parent);
}

// ── fail-closed: wrong / empty shape ───────────────────────────────────────────
for (const [label, body] of [
  ["empty-array", "[]"],
  ["object", "{}"],
  ["null", "null"],
  ["string", "\"frontend\""],
  ["number", "42"],
  ["array-of-null", "[null]"],
  ["array-of-string", "[\"../frontend\"]"],
  ["array-of-array", "[[]]"],
  ["root-not-string", JSON.stringify([{ root: 1, role: "x", stack: "y" }])],
  ["root-empty-string", JSON.stringify([{ root: "", role: "x", stack: "y" }])],
  ["root-missing", JSON.stringify([{ role: "x", stack: "y" }])],
]) {
  const { parent, host } = workspace(["frontend"]);
  writeManifest(host, body);
  sameSet(`closed:shape:${label}`, componentRoots(host), [host]);
  cleanup(parent);
}

// ── fail-closed: non-existent declared root ⇒ WHOLE manifest rejected ───────────
{
  const { parent, host } = workspace([]); // no `ghost` sibling created
  writeManifest(host, JSON.stringify([{ root: "../ghost", role: "x", stack: "y" }]));
  sameSet("closed:nonexistent-root", componentRoots(host), [host]);
  cleanup(parent);
}

// A declared root pointing at an existing FILE (not a directory) ⇒ rejected.
{
  const { parent, host } = workspace([]);
  fs.writeFileSync(path.join(parent, "afile"), "x");
  writeManifest(host, JSON.stringify([{ root: "../afile", role: "x", stack: "y" }]));
  sameSet("closed:root-is-file", componentRoots(host), [host]);
  cleanup(parent);
}

// ── fail-closed: overbroad root (filesystem root) ⇒ WHOLE manifest rejected ─────
{
  const { parent, host } = workspace([]);
  writeManifest(host, JSON.stringify([{ root: "/", role: "x", stack: "y" }]));
  sameSet("closed:overbroad-fs-root", componentRoots(host), [host]);
  cleanup(parent);
}

// ── fail-closed: overbroad root (ancestor of the session root) ──────────────────
// The manifest's own parent dir CONTAINS the session root — declaring it would
// re-expose everything above the work. Rejected.
{
  const { parent, host } = workspace([]);
  writeManifest(host, JSON.stringify([{ root: "..", role: "x", stack: "y" }]));
  sameSet("closed:overbroad-ancestor", componentRoots(host), [host]);
  cleanup(parent);
}

// ── fail-closed: symlink escape ⇒ realpath canonicalises, then overbroad-check ──
// A declared root that is a symlink to a filesystem root resolves (via realpath)
// to "/" and is rejected as overbroad. Proves canonicalisation happens BEFORE the
// overbroad guard, so a symlink cannot smuggle an escape past it.
{
  const { parent, host } = workspace([]);
  try {
    fs.symlinkSync("/", path.join(parent, "escape"));
    writeManifest(host, JSON.stringify([{ root: "../escape", role: "x", stack: "y" }]));
    sameSet("closed:symlink-to-fs-root", componentRoots(host), [host]);
  } catch (e) {
    // Symlink creation may be denied in some sandboxes — skip rather than false-fail.
    if (e.code === "EPERM" || e.code === "EACCES") { console.log("  · symlink-to-fs-root: skipped (symlink not permitted here)"); }
    else throw e;
  }
  cleanup(parent);
}

// A symlink to a real sibling dir resolves to that sibling's CANONICAL path — the
// set carries the realpath target, not the symlink path (defeats a symlink that
// points INTO an allowed tree but whose name traverses).
{
  const { parent, host } = workspace(["realsib"]);
  try {
    fs.symlinkSync(path.join(parent, "realsib"), path.join(parent, "link"));
    writeManifest(host, JSON.stringify([{ root: "../link", role: "x", stack: "y" }]));
    sameSet("closed:symlink-canonicalised", componentRoots(host), [host, path.join(parent, "realsib")]);
  } catch (e) {
    if (e.code === "EPERM" || e.code === "EACCES") { console.log("  · symlink-canonicalised: skipped (symlink not permitted here)"); }
    else throw e;
  }
  cleanup(parent);
}

// ── fail-closed: partial-honour trap ⇒ one bad entry rejects the WHOLE manifest ─
// A manifest mixing one VALID sibling with one OVERBROAD root must NOT honour the
// valid one — all-or-nothing. This is the plan-adversary inversion finding.
{
  const { parent, host } = workspace(["frontend"]);
  writeManifest(host, JSON.stringify([
    { root: "../frontend", role: "frontend", stack: "x" }, // valid
    { root: "/", role: "evil", stack: "y" },               // overbroad
  ]));
  sameSet("closed:partial-honour-rejected", componentRoots(host), [host]);
  cleanup(parent);
}

// Valid sibling mixed with a NON-EXISTENT root ⇒ whole manifest rejected too.
{
  const { parent, host } = workspace(["frontend"]);
  writeManifest(host, JSON.stringify([
    { root: "../frontend", role: "frontend", stack: "x" },
    { root: "../ghost", role: "missing", stack: "y" },
  ]));
  sameSet("closed:partial-honour-nonexistent", componentRoots(host), [host]);
  cleanup(parent);
}

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
