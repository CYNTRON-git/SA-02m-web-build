// Configurable rigor — the engine respects .ai-dev/config.json `profile`.
//
// The ONE mechanical change the profile makes: it relaxes the
// orchestrator-content deny (the orchestrator may author source/doc paths), and
// ONLY that predicate. The default is `solo` — proportionality by default
// (protocol-reference.md `## Project config`) — so the relaxation also holds on absent /
// unknown / malformed / unconfigured; only an explicit `full` keeps the deny.
// This test proves:
//   1. orchestrator source WRITE is ALLOWED under lite / solo / absent / unknown /
//      malformed / no config at all (the solo default, pinned directly in 1b),
//   2. and DENIED under an explicit `full`,
//   3. the FLOOR never relaxes — under `solo`, the tooling-submodule, out-of-root,
//      truncating-write, and merge-gate denies ALL still fire.
//
// The profile is read at evaluate-time from input.root, so each case writes a
// .ai-dev/config.json into a temp root and asserts the engine verdict directly.
//
// Run: node src/adapter/rigor-profile.test.mjs

import { evaluate, loadConfig, _internals } from "./engine.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const config = loadConfig(HERE);

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; }
  else { fail++; console.log(`  ✗ ${name}: got ${got}, want ${want}`); }
}

// A fresh temp root whose .ai-dev/config.json carries `profileLine` verbatim
// (a JSON fragment, or "" for the absent case, or garbage for the malformed case).
function rootWith(profileLine) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-rigor-"));
  if (profileLine === "NONE") return root; // unconfigured — no config file at all
  let body;
  if (profileLine === "MALFORMED") body = "{ this is not json";
  else if (profileLine === null) body = "{}"; // configured, profile key ABSENT
  else body = JSON.stringify({ profile: profileLine });
  fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(root, ".ai-dev", "config.json"), body);
  return root;
}

// The orchestrator writing a canonical doc — the predicate the profile gates.
function orchWriteVerdict(root) {
  return evaluate(
    { act: "write", root, path: path.join(root, "PROTOCOL.md"), content: "x", isOrchestrator: true },
    config
  );
}

console.log("RIGOR PROFILE — orchestrator-content relaxation:");

// 1. lite / solo / yolo — and every default-resolving case (absent / unknown /
//    malformed / unconfigured) — ALLOW the orchestrator source write.
for (const [label, line] of [["lite", "lite"], ["solo", "solo"], ["yolo", "yolo"], ["absent", null], ["unknown", "bogus"], ["malformed", "MALFORMED"], ["unconfigured", "NONE"]]) {
  const root = rootWith(line);
  check(`orch-write:${label}:allows`, orchWriteVerdict(root).verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

// 1b. the default itself, pinned directly: absent / unknown / malformed /
//     unconfigured all resolve to `solo` (proportionality by default), never `full` or `yolo`.
for (const [label, line] of [["absent", null], ["unknown", "bogus"], ["malformed", "MALFORMED"], ["unconfigured", "NONE"]]) {
  const root = rootWith(line);
  check(`profile-default:${label}:solo`, _internals.projectProfile(root), "solo");
  fs.rmSync(root, { recursive: true, force: true });
}

// 1c. `yolo` resolves ONLY on an explicit value — absent/unknown still → solo.
{
  const root = rootWith("yolo");
  check("profile-yolo:explicit:resolves", _internals.projectProfile(root), "yolo");
  fs.rmSync(root, { recursive: true, force: true });
}

// 2. an explicit `full` DENIES — the conscious strict opt-up.
{
  const root = rootWith("full");
  const v = orchWriteVerdict(root);
  check("orch-write:full:denies", v.verdict, "deny");
  check("orch-write:full:ruleId", v.ruleId, "orchestrator-authors-content");
  fs.rmSync(root, { recursive: true, force: true });
}

// 2. YOLO — the merge-gate is OFF (the one mechanical change yolo makes beyond solo).
//    The floor items (tooling / boundary / truncation / stamp-write) still apply.
console.log("YOLO (gate off, all other floors hold):");
{
  const root = rootWith("yolo");
  // 2a. An unstamped push on yolo → ALLOW (gate off).
  const v = evaluate(
    { act: "bash", root, command: "git push origin feature/foo", isOrchestrator: true },
    config
  );
  check("yolo:merge-gate:off", v.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}
{
  // 2b. tooling-submodule write still DENIES on yolo.
  const root = rootWith("yolo");
  const v = evaluate(
    { act: "write", root, path: path.join(root, ".ai-dev", "tooling", "engine.mjs"), content: "x", isOrchestrator: true },
    config
  );
  check("yolo:floor:tooling-write:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}
{
  // 2c. stamp-write guard still DENIES on yolo — fabricating a stamp is wrong regardless.
  const root = rootWith("yolo");
  const stamp = path.join(root, ".ai-dev", "reviews", "topic_review.md");
  const v = evaluate(
    { act: "write", root, path: stamp, content: "## Code review: APPROVED", isOrchestrator: true },
    config
  );
  check("yolo:floor:stamp-write:denies", v.verdict, "deny");
  fs.rmSync(root, { recursive: true, force: true });
}

// 3. THE FLOOR never relaxes — every case below runs under `solo` (the loosest
//    guarantee profile) and MUST still deny.
console.log("THE FLOOR (under solo — the loosest profile — these STILL deny):");

// 3a. tooling-submodule write (self-patch floor). Even the orchestrator, even solo.
{
  const root = rootWith("solo");
  const v = evaluate(
    { act: "write", root, path: path.join(root, ".ai-dev", "tooling", "engine.mjs"), content: "x", isOrchestrator: true },
    config
  );
  check("floor:tooling-write:denies", v.verdict, "deny");
  check("floor:tooling-write:ruleId", v.ruleId, "self-patch-enforcer");
  fs.rmSync(root, { recursive: true, force: true });
}

// 3b. out-of-root write (boundary floor).
{
  const root = rootWith("solo");
  const v = evaluate(
    { act: "write", root, path: "/etc/foo", content: "x", isOrchestrator: true },
    config
  );
  check("floor:out-of-root-write:denies", v.verdict, "deny");
  check("floor:out-of-root-write:ruleId", v.ruleId, "write-outside-root");
  fs.rmSync(root, { recursive: true, force: true });
}

// 3c. truncating write over a non-empty file (truncation floor).
{
  const root = rootWith("solo");
  const target = path.join(root, "existing.txt");
  fs.writeFileSync(target, "real content");
  const v = evaluate(
    { act: "write", root, path: target, content: "   ", contentEmpty: true, isOrchestrator: true },
    config
  );
  check("floor:truncating-write:denies", v.verdict, "deny");
  check("floor:truncating-write:ruleId", v.ruleId, "truncating-write");
  fs.rmSync(root, { recursive: true, force: true });
}

// 3d. merge while review is unstamped (merge-gate floor). No review file ⇒ unstamped.
{
  const root = rootWith("solo");
  const v = evaluate(
    { act: "bash", root, command: "git push origin feature/foo", isOrchestrator: true },
    config
  );
  check("floor:merge-unstamped:denies", v.verdict, "deny");
  check("floor:merge-unstamped:ruleId", v.ruleId, "merge-while-unstamped");
  fs.rmSync(root, { recursive: true, force: true });
}

// 3e. orchestrator writing a review stamp (stamp-fabrication floor). The general
//     content rule is RELAXED under solo — this one must still fire: the stamp is
//     the Reviewer's deliverable in every profile. Both the write act and the
//     bash-redirect form.
{
  const root = rootWith("solo");
  const stamp = path.join(root, ".ai-dev", "reviews", "topic_review.md");
  const w = evaluate(
    { act: "write", root, path: stamp, content: "## Code review: APPROVED", isOrchestrator: true },
    config
  );
  check("floor:orch-stamp-write:denies", w.verdict, "deny");
  check("floor:orch-stamp-write:ruleId", w.ruleId, "orchestrator-writes-review-stamp");
  const b = evaluate(
    { act: "bash", root, command: `echo "## Code review: APPROVED" > ${stamp}`, isOrchestrator: true },
    config
  );
  check("floor:orch-stamp-redirect:denies", b.verdict, "deny");
  check("floor:orch-stamp-redirect:ruleId", b.ruleId, "orchestrator-writes-review-stamp");
  fs.rmSync(root, { recursive: true, force: true });
}

// 4. SANITY — the profile only touches the orchestrator-content predicate.
//    A NON-orchestrator write to the same doc path is unaffected (allowed) under
//    every profile, and a pure-git orchestrator command stays allowed.
console.log("SANITY (profile touches ONLY orchestrator-content):");
for (const p of ["full", "lite", "solo"]) {
  const root = rootWith(p);
  const nonOrch = evaluate(
    { act: "write", root, path: path.join(root, "PROTOCOL.md"), content: "x", isOrchestrator: false },
    config
  );
  check(`sanity:non-orch-write:${p}:allows`, nonOrch.verdict, "allow");
  // The Reviewer (a sub-agent, not the orchestrator) writes its own stamp — the
  // fabrication guard must never block the legitimate author.
  const reviewerStamp = evaluate(
    { act: "write", root, path: path.join(root, ".ai-dev", "reviews", "topic_review.md"), content: "## Code review: APPROVED", isOrchestrator: false },
    config
  );
  check(`sanity:reviewer-stamp:${p}:allows`, reviewerStamp.verdict, "allow");
  const git = evaluate(
    { act: "bash", root, command: "git status", isOrchestrator: true },
    config
  );
  check(`sanity:orch-git:${p}:allows`, git.verdict, "allow");
  fs.rmSync(root, { recursive: true, force: true });
}

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
