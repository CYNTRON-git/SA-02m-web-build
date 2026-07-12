// hookMode: strict | light — ask-class rule behaviour switch.
//
// strict (default, fail-safe): ask-class rules return verdict "ask".
// light: ask-class rules return verdict "deny" with an informative reason
//   (the model sees what's denied + why + the safe path, no Operator interruption).
// absent / unrecognised ⇒ strict (fail-safe).
// Overridable via .ai-dev/config.local.json (the personal/shared split).
//
// Run: node --test src/adapter/hook-mode.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { evaluate, loadConfig, _internals } from "./engine.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const config = loadConfig(HERE);
const { projectHookMode } = _internals;

// ── helpers ──────────────────────────────────────────────────────────────────

// A fresh temp root with optional shared config body and/or personal config body.
// "NONE" = no file, "MALFORMED" = unparseable JSON.
function rootWith(sharedBody, localBody) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-hookmode-"));
  if (sharedBody !== "NONE") {
    fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
    fs.writeFileSync(
      path.join(root, ".ai-dev", "config.json"),
      sharedBody === "MALFORMED" ? "{ not json" : sharedBody,
    );
  }
  if (localBody && localBody !== "NONE") {
    fs.mkdirSync(path.join(root, ".ai-dev"), { recursive: true });
    fs.writeFileSync(
      path.join(root, ".ai-dev", "config.local.json"),
      localBody === "MALFORMED" ? "{ not json" : localBody,
    );
  }
  return root;
}
function cleanup(root) { fs.rmSync(root, { recursive: true, force: true }); }
function cfg(obj) { return JSON.stringify(obj); }

// Force-push runs through the merge-gate first; to isolate the force-push ASK we give
// the root a resolvable feature topic WITH a satisfied review stamp, so the merge-gate
// (merge-while-unstamped / merge-topic-unresolvable) passes and force-push is what's left.
function stampedFeatureRoot(sharedBody, localBody) {
  const root = rootWith(sharedBody, localBody);
  fs.mkdirSync(path.join(root, ".git", "refs", "heads", "feature"), { recursive: true });
  fs.writeFileSync(path.join(root, ".git", "HEAD"), "ref: refs/heads/feature/topic\n");
  fs.mkdirSync(path.join(root, ".ai-dev", "reviews"), { recursive: true });
  fs.writeFileSync(path.join(root, ".ai-dev", "reviews", "topic_review.md"), "## Code review: APPROVED\n");
  return root;
}

// A detached-HEAD root — merge-topic-unresolvable asks here.
function rootDetached() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-hookmode-det-"));
  fs.mkdirSync(path.join(root, ".git"), { recursive: true });
  fs.writeFileSync(path.join(root, ".git", "HEAD"), "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678\n");
  return root;
}

// ── projectHookMode unit tests ────────────────────────────────────────────────

test("projectHookMode: absent config ⇒ strict", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-hm-absent-"));
  assert.equal(projectHookMode(root), "strict");
  fs.rmSync(root, { recursive: true, force: true });
});

test("projectHookMode: explicit strict ⇒ strict", () => {
  const root = rootWith(cfg({ hookMode: "strict" }));
  assert.equal(projectHookMode(root), "strict");
  cleanup(root);
});

test("projectHookMode: explicit light ⇒ light", () => {
  const root = rootWith(cfg({ hookMode: "light" }));
  assert.equal(projectHookMode(root), "light");
  cleanup(root);
});

test("projectHookMode: unrecognised value ⇒ strict", () => {
  const root = rootWith(cfg({ hookMode: "medium" }));
  assert.equal(projectHookMode(root), "strict");
  cleanup(root);
});

test("projectHookMode: malformed config ⇒ strict", () => {
  const root = rootWith("MALFORMED");
  assert.equal(projectHookMode(root), "strict");
  cleanup(root);
});

test("projectHookMode: local override wins over shared", () => {
  // shared = strict, local = light ⇒ light
  const root = rootWith(cfg({ hookMode: "strict" }), cfg({ hookMode: "light" }));
  assert.equal(projectHookMode(root), "light");
  cleanup(root);
});

test("projectHookMode: local override fills absent shared", () => {
  const root = rootWith(cfg({ profile: "solo" }), cfg({ hookMode: "light" }));
  assert.equal(projectHookMode(root), "light");
  cleanup(root);
});

test("projectHookMode: malformed local ⇒ shared wins", () => {
  // local is malformed ⇒ ignored, shared applies
  const root = rootWith(cfg({ hookMode: "light" }), "MALFORMED");
  assert.equal(projectHookMode(root), "light");
  cleanup(root);
});

test("projectHookMode: both absent ⇒ strict", () => {
  const root = rootWith(cfg({ profile: "solo" }), null);
  assert.equal(projectHookMode(root), "strict");
  cleanup(root);
});

// ── evaluate: strict mode (default) ───────────────────────────────────────────

test("strict (default): force-push ⇒ ask", () => {
  const fp = stampedFeatureRoot(cfg({ profile: "solo" }));
  const v = evaluate({ act: "bash", root: fp, command: "git push --force origin feature/topic" }, config);
  assert.equal(v.verdict, "ask", "strict: force-push returns ask");
  assert.equal(v.ruleId, "force-push");
  cleanup(fp);
});

test("strict (default): ssh-mutating-action ⇒ ask", () => {
  const root = rootWith(cfg({ profile: "solo" }));
  const v = evaluate({ act: "bash", root, command: 'ssh host "systemctl restart nginx"' }, config);
  assert.equal(v.verdict, "ask", "strict: ssh mutating returns ask");
  assert.equal(v.ruleId, "ssh-mutating-action");
  cleanup(root);
});

test("strict (default): git-commit-no-verify ⇒ ask", () => {
  const root = rootWith(cfg({ profile: "solo" }));
  const v = evaluate({ act: "bash", root, command: "git commit --no-verify -m fix" }, config);
  assert.equal(v.verdict, "ask", "strict: no-verify returns ask");
  assert.equal(v.ruleId, "git-commit-no-verify");
  cleanup(root);
});

test("strict (default): merge-topic-unresolvable ⇒ ask", () => {
  const root = rootDetached();
  const v = evaluate({ act: "bash", root, command: "git push" }, config);
  assert.equal(v.verdict, "ask", "strict: unresolvable topic returns ask");
  assert.equal(v.ruleId, "merge-topic-unresolvable");
  cleanup(root);
});

// ── evaluate: light mode ──────────────────────────────────────────────────────

test("light: force-push ⇒ deny with informative reason", () => {
  const fp = stampedFeatureRoot(cfg({ hookMode: "light" }));
  const v = evaluate({ act: "bash", root: fp, command: "git push --force origin feature/topic" }, config);
  assert.equal(v.verdict, "deny", "light: force-push returns deny");
  assert.equal(v.ruleId, "force-push");
  assert.ok(v.reason.includes("[hookMode: light]"), "reason includes hookMode tag");
  assert.ok(v.reason.includes("Safe path:"), "reason includes safe path");
  cleanup(fp);
});

test("light: ssh-mutating-action ⇒ deny with informative reason", () => {
  const root = rootWith(cfg({ hookMode: "light" }));
  const v = evaluate({ act: "bash", root, command: 'ssh host "systemctl restart nginx"' }, config);
  assert.equal(v.verdict, "deny", "light: ssh mutating returns deny");
  assert.equal(v.ruleId, "ssh-mutating-action");
  assert.ok(v.reason.includes("[hookMode: light]"), "reason includes hookMode tag");
  assert.ok(v.reason.includes("Safe path:"), "reason includes safe path");
  cleanup(root);
});

test("light: git-commit-no-verify ⇒ deny with informative reason", () => {
  const root = rootWith(cfg({ hookMode: "light" }));
  const v = evaluate({ act: "bash", root, command: "git commit --no-verify -m fix" }, config);
  assert.equal(v.verdict, "deny", "light: no-verify returns deny");
  assert.equal(v.ruleId, "git-commit-no-verify");
  assert.ok(v.reason.includes("[hookMode: light]"), "reason includes hookMode tag");
  assert.ok(v.reason.includes("Safe path:"), "reason includes safe path");
  cleanup(root);
});

test("light: merge-topic-unresolvable ⇒ deny with informative reason", () => {
  const root = rootWith(cfg({ hookMode: "light" }));
  fs.mkdirSync(path.join(root, ".git"), { recursive: true });
  fs.writeFileSync(path.join(root, ".git", "HEAD"), "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678\n");
  const v = evaluate({ act: "bash", root, command: "git push" }, config);
  assert.equal(v.verdict, "deny", "light: unresolvable topic returns deny");
  assert.equal(v.ruleId, "merge-topic-unresolvable");
  assert.ok(v.reason.includes("[hookMode: light]"), "reason includes hookMode tag");
  assert.ok(v.reason.includes("Safe path:"), "reason includes safe path");
  cleanup(root);
});

test("light: deny-class rules are UNAFFECTED", () => {
  // Deny-class rules must still fire as deny regardless of hookMode.
  const root = rootWith(cfg({ hookMode: "light" }));
  const v = evaluate({ act: "bash", root, command: "git add -A" }, config);
  assert.equal(v.verdict, "deny", "light: deny-class still denies");
  assert.equal(v.ruleId, "git-add-all");
  // The reason must NOT carry the hookMode tag — deny-class is untouched.
  assert.ok(!v.reason.includes("[hookMode: light]"), "deny-class reason has no hookMode tag");
  cleanup(root);
});

test("light: inject-class rules are UNAFFECTED", () => {
  // Inject-class rules must still fire as inject regardless of hookMode.
  const root = rootWith(cfg({ hookMode: "light" }));
  const v = evaluate({ act: "prompt", root, prompt: "implement feature" }, config);
  // The prompt fires language-mirror (always-on inject) — it is NOT converted to deny.
  assert.equal(v.verdict, "inject", "light: inject-class still injects");
  cleanup(root);
});

// ── evaluate: absent/invalid hookMode ⇒ strict ────────────────────────────────

test("no config file ⇒ strict (force-push asks)", () => {
  const fp = stampedFeatureRoot("NONE");
  const v = evaluate({ act: "bash", root: fp, command: "git push --force origin feature/topic" }, config);
  assert.equal(v.verdict, "ask", "no config ⇒ strict: force-push asks");
  cleanup(fp);
});

test("unrecognised hookMode ⇒ strict (force-push asks)", () => {
  const fp = stampedFeatureRoot(cfg({ hookMode: "medium" }));
  const v = evaluate({ act: "bash", root: fp, command: "git push --force origin feature/topic" }, config);
  assert.equal(v.verdict, "ask", "unrecognised ⇒ strict: force-push asks");
  cleanup(fp);
});

test("malformed config ⇒ strict (force-push asks)", () => {
  const fp = stampedFeatureRoot("MALFORMED");
  const v = evaluate({ act: "bash", root: fp, command: "git push --force origin feature/topic" }, config);
  assert.equal(v.verdict, "ask", "malformed ⇒ strict: force-push asks");
  cleanup(fp);
});

// ── config.local override flows to evaluate ──────────────────────────────────

test("shared strict + local light ⇒ light (force-push denies)", () => {
  const fp = stampedFeatureRoot(cfg({ hookMode: "strict" }), cfg({ hookMode: "light" }));
  const v = evaluate({ act: "bash", root: fp, command: "git push --force origin feature/topic" }, config);
  assert.equal(v.verdict, "deny", "local light overrides shared strict: force-push denies");
  assert.equal(v.ruleId, "force-push");
  cleanup(fp);
});

test("shared absent + local light ⇒ light (force-push denies)", () => {
  const fp = stampedFeatureRoot(cfg({ profile: "solo" }), cfg({ hookMode: "light" }));
  const v = evaluate({ act: "bash", root: fp, command: "git push --force origin feature/topic" }, config);
  assert.equal(v.verdict, "deny", "local light with no shared hookMode: force-push denies");
  cleanup(fp);
});

// ── specific safe-path content checks ────────────────────────────────────────

test("light: force-push reason names rebase alternative", () => {
  const fp = stampedFeatureRoot(cfg({ hookMode: "light" }));
  const v = evaluate({ act: "bash", root: fp, command: "git push --force origin feature/topic" }, config);
  assert.ok(v.reason.includes("rebase"), "force-push safe path mentions rebase");
  cleanup(fp);
});

test("light: no-verify reason names verification alternative", () => {
  const root = rootWith(cfg({ hookMode: "light" }));
  const v = evaluate({ act: "bash", root, command: "git commit --no-verify -m fix" }, config);
  assert.ok(v.reason.includes("WITH verification"), "no-verify safe path mentions verification");
  cleanup(root);
});

test("light: ssh-mutating reason names chat alternative", () => {
  const root = rootWith(cfg({ hookMode: "light" }));
  const v = evaluate({ act: "bash", root, command: 'ssh host "rm -rf /x"' }, config);
  assert.ok(v.reason.includes("chat"), "ssh-mutating safe path mentions chat");
  cleanup(root);
});

test("light: unresolvable-topic reason names explicit topic", () => {
  const root = rootWith(cfg({ hookMode: "light" }));
  fs.mkdirSync(path.join(root, ".git"), { recursive: true });
  fs.writeFileSync(path.join(root, ".git", "HEAD"), "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678\n");
  const v = evaluate({ act: "bash", root, command: "git push" }, config);
  assert.ok(v.reason.includes("explicitly"), "unresolvable-topic safe path mentions explicit");
  cleanup(root);
});
