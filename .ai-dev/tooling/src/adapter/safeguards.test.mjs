// Configurable safeguards — the engine respects .ai-dev/config.json `safeguards`.
//
// The GENERAL evaluate-level skip: an explicit `safeguards.<id>: "off"` on a
// TOGGLEABLE rule (deny-rules.json `toggleable: true`) opts that one ask/inject guard
// out. A rule WITHOUT `toggleable: true` is NEVER skipped — so every deny-class rule
// and the merge-gate stay the permanent mechanical floor however config reads.
// Fail-CLOSED: only an exact-string "off" disables; everything else keeps the guard on.
//
// This test proves:
//   1. no `safeguards` field ⇒ every guard still fires (regression),
//   2. a toggleable guard listed "off" ⇒ that guard skipped (ALLOW),
//   3. a FLOOR id listed "off" is IGNORED (deny still denies, the merge-gate still
//      asks/denies) — the floor is never config-disablable,
//   4. unknown value / non-object / malformed config ⇒ all guards on (fail-safe),
//   5. disabledSafeguards returns only well-formed "off" ids; safeguardRegistry lists
//      exactly the seven toggleable with toggleable:true and the rest false,
//   6. a DENY (ssh-content-edit) still denies even with EVERY toggleable guard off.
//
// The safeguards are read at evaluate-time from input.root, so each case writes a
// .ai-dev/config.json into a temp root and asserts the engine verdict directly.
//
// Run: node --test src/adapter/safeguards.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { evaluate, loadConfig, _internals } from "./engine.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const config = loadConfig(HERE);

// The seven guards the contract makes toggleable — pinned here so a stray
// `toggleable` added elsewhere (a floor rule, say) is caught by the registry test.
const TOGGLEABLE = [
  "ssh-mutating-action",
  "force-push",
  "git-commit-no-verify",
  "no-config-run-setup",
  "no-product-brief-discover",
  "change-route-reminder",
  "language-mirror",
];

// A fresh temp root whose .ai-dev/config.json carries `body` verbatim (a JSON string,
// "NONE" for no config file, or "MALFORMED" for unparseable JSON).
function rootWith(body) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-safeguards-"));
  if (body === "NONE") return root;
  fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(root, ".ai-dev", "config.json"), body === "MALFORMED" ? "{ not json" : body);
  return root;
}
function cleanup(root) { fs.rmSync(root, { recursive: true, force: true }); }
function cfg(obj) { return JSON.stringify(obj); }

// Force-push runs through the merge-gate first; to isolate the force-push ASK we give
// the root a resolvable feature topic WITH a satisfied review stamp, so the merge-gate
// (merge-while-unstamped / merge-topic-unresolvable) passes and force-push is what's left.
function stampedFeatureRoot(body) {
  const root = rootWith(body);
  fs.mkdirSync(path.join(root, ".git", "refs", "heads", "feature"), { recursive: true });
  fs.writeFileSync(path.join(root, ".git", "HEAD"), "ref: refs/heads/feature/topic\n");
  fs.mkdirSync(path.join(root, ".ai-dev", "reviews"), { recursive: true });
  fs.writeFileSync(path.join(root, ".ai-dev", "reviews", "topic_review.md"), "## Code review: APPROVED\n");
  return root;
}

// Convenience verdict helpers for the two ask guards exercised below.
function sshRm(root) {
  return evaluate({ act: "bash", root, command: 'ssh host "rm -rf /x"' }, config);
}
// A force-push of the current (stamped) feature branch — the merge-gate passes,
// leaving force-push as the only ask in play.
function forcePush(root) {
  return evaluate({ act: "bash", root, command: "git push --force origin feature/topic" }, config);
}

test("1. no safeguards field ⇒ every guard still fires (regression)", () => {
  const root = rootWith(cfg({ profile: "solo" }));
  assert.equal(sshRm(root).verdict, "ask", "ssh rm asks by default");
  assert.equal(sshRm(root).ruleId, "ssh-mutating-action");
  cleanup(root);
  const fp = stampedFeatureRoot(cfg({ profile: "solo" }));
  assert.equal(forcePush(fp).verdict, "ask", "force-push asks by default");
  assert.equal(forcePush(fp).ruleId, "force-push");
  cleanup(fp);
});

test("2. a toggleable guard listed off ⇒ that guard is skipped (allow)", () => {
  const a = rootWith(cfg({ safeguards: { "ssh-mutating-action": "off" } }));
  assert.equal(sshRm(a).verdict, "allow", "ssh-mutating-action off ⇒ ssh rm allowed");
  cleanup(a);
  const b = stampedFeatureRoot(cfg({ safeguards: { "force-push": "off" } }));
  assert.equal(forcePush(b).verdict, "allow", "force-push off ⇒ force-push allowed");
  // the OTHER guard is unaffected — only the named one is disabled.
  assert.equal(sshRm(b).verdict, "ask", "ssh-mutating-action still asks when only force-push is off");
  cleanup(b);
});

test("3. a FLOOR id listed off is IGNORED — the floor is never config-disablable", () => {
  // 3a. merge-topic-unresolvable (an ask, but a merge-gate FLOOR — not toggleable):
  //     a bare `git push` on a root with no resolvable topic still ASKS.
  const root = rootWith(cfg({ safeguards: { "merge-topic-unresolvable": "off" } }));
  const unresolvable = evaluate({ act: "bash", root, command: "git push" }, config);
  assert.equal(unresolvable.verdict, "ask", "merge-topic-unresolvable cannot be toggled off");
  assert.equal(unresolvable.ruleId, "merge-topic-unresolvable");
  cleanup(root);

  // 3b. a deny id (git-add-all) listed "off" STILL denies.
  const addRoot = rootWith(cfg({ safeguards: { "git-add-all": "off" } }));
  const add = evaluate({ act: "bash", root: addRoot, command: "git add -A" }, config);
  assert.equal(add.verdict, "deny", "git-add-all (deny) cannot be toggled off");
  assert.equal(add.ruleId, "git-add-all");
  cleanup(addRoot);

  // 3c. the merge-gate deny (merge-while-unstamped) listed "off" STILL denies.
  const mergeRoot = rootWith(cfg({ safeguards: { "merge-while-unstamped": "off" } }));
  const push = evaluate({ act: "bash", root: mergeRoot, command: "git push origin feature/foo" }, config);
  assert.equal(push.verdict, "deny", "merge-while-unstamped (merge-gate floor) cannot be toggled off");
  assert.equal(push.ruleId, "merge-while-unstamped");
  cleanup(mergeRoot);

  // 3d. a boundary deny (write-outside-root) listed "off" STILL denies.
  const boundaryRoot = rootWith(cfg({ safeguards: { "write-outside-root": "off" } }));
  const w = evaluate({ act: "write", root: boundaryRoot, path: "/etc/foo", content: "x" }, config);
  assert.equal(w.verdict, "deny", "write-outside-root cannot be toggled off");
  cleanup(boundaryRoot);
});

test("4. unknown value / non-object / malformed ⇒ all guards on (fail-safe)", () => {
  // an unknown value (string, bool, number) keeps the guard ON.
  for (const val of ["yes", "ON", true, 1, null]) {
    const root = rootWith(cfg({ safeguards: { "ssh-mutating-action": val } }));
    assert.equal(sshRm(root).verdict, "ask", `value ${JSON.stringify(val)} ⇒ guard stays on`);
    cleanup(root);
  }
  // a non-object `safeguards` ⇒ empty set, guard on.
  const strSg = rootWith(cfg({ safeguards: "off" }));
  assert.equal(sshRm(strSg).verdict, "ask", "non-object safeguards ⇒ guard stays on");
  cleanup(strSg);
  const arrSg = rootWith(cfg({ safeguards: ["ssh-mutating-action"] }));
  assert.equal(sshRm(arrSg).verdict, "ask", "array safeguards ⇒ guard stays on");
  cleanup(arrSg);
  // malformed config / no config at all ⇒ guard on.
  const mal = rootWith("MALFORMED");
  assert.equal(sshRm(mal).verdict, "ask", "malformed config ⇒ guard stays on");
  cleanup(mal);
  const none = rootWith("NONE");
  assert.equal(sshRm(none).verdict, "ask", "no config file ⇒ guard stays on");
  cleanup(none);
});

test("5a. disabledSafeguards returns only well-formed \"off\" ids", () => {
  const root = rootWith(cfg({
    safeguards: {
      "ssh-mutating-action": "off",
      "force-push": "on",
      "git-commit-no-verify": "yes",
      "merge-while-unstamped": "off", // a floor id — collected here but never SKIPPED (toggleable gate)
    },
  }));
  const off = _internals.disabledSafeguards(root);
  assert.ok(off instanceof Set);
  assert.deepEqual([...off].sort(), ["merge-while-unstamped", "ssh-mutating-action"].sort());
  cleanup(root);
  // fail-safe to an empty set.
  const none = rootWith("NONE");
  assert.equal(_internals.disabledSafeguards(none).size, 0, "no config ⇒ empty set");
  cleanup(none);
  const mal = rootWith("MALFORMED");
  assert.equal(_internals.disabledSafeguards(mal).size, 0, "malformed ⇒ empty set");
  cleanup(mal);
  const nonObj = rootWith(cfg({ safeguards: 42 }));
  assert.equal(_internals.disabledSafeguards(nonObj).size, 0, "non-object ⇒ empty set");
  cleanup(nonObj);
});

test("5b. safeguardRegistry lists exactly the seven toggleable; the rest false", () => {
  const reg = _internals.safeguardRegistry(config);
  assert.equal(reg.length, config.rules.length, "every rule appears in the registry");
  const flagged = reg.filter((r) => r.toggleable).map((r) => r.id).sort();
  assert.deepEqual(flagged, [...TOGGLEABLE].sort(), "exactly the seven contract guards are toggleable");
  // every entry carries a non-empty label and a class.
  for (const r of reg) {
    assert.ok(typeof r.label === "string" && r.label.length > 0, `${r.id} has a label`);
    assert.ok(["deny", "ask", "inject"].includes(r.class), `${r.id} has a valid class`);
  }
  // the floor rules are NOT toggleable.
  for (const id of ["merge-while-unstamped", "commit-on-unstamped-main", "merge-topic-unresolvable", "write-outside-root", "self-patch-enforcer"]) {
    assert.equal(reg.find((r) => r.id === id).toggleable, false, `${id} is a permanent floor`);
  }
});

test("6. a DENY (ssh-content-edit) still denies with EVERY toggleable guard off", () => {
  const allOff = {};
  for (const id of TOGGLEABLE) allOff[id] = "off";
  const root = rootWith(cfg({ safeguards: allOff }));
  // the deny floor holds.
  const edit = evaluate({ act: "bash", root, command: 'ssh host "sed -i s/a/b/ /etc/f"' }, config);
  assert.equal(edit.verdict, "deny", "ssh-content-edit denies regardless of toggleable guards");
  assert.equal(edit.ruleId, "ssh-content-edit");
  // sanity: the toggleable ask guards ARE off (ssh rm now allowed).
  assert.equal(sshRm(root).verdict, "allow", "with all toggleables off, ssh rm is allowed");
  cleanup(root);
});
