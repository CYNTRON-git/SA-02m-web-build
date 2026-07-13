// Adapter integrity tests — the three guards the data-adapter design calls for
// (src/adapter/README.md "No regex drift"):
//
//   1. PARITY      — one shared fixture, each case run through BOTH shims' full
//                    path (normalise → shared engine → verdict). Both must reach
//                    the identical engine verdict, except where a platform's
//                    capability legitimately diverges (recorded per-case, printed).
//   2. SINGLE-ENGINE — grep each shim for rule logic (patterns, role lists). A
//                    predicate re-implemented in a shim instead of called from the
//                    engine makes a forbidden token appear, and this fails.
//   3. NODE-SPIKE  — the one unproven assumption: a Claude PreToolUse hook running
//                    `node shim.mjs` with the payload on stdin actually emits the
//                    deny JSON. Run as a real subprocess; also measure latency.
//
// Run: node src/adapter/parity.test.mjs

import { loadConfig } from "./engine.mjs";
import { decide as claudeDecide } from "./claude/shim.mjs";
import { decide as ocDecide, decidePrompt as ocDecidePrompt } from "./opencode/normalise.mjs";
import { execFileSync } from "node:child_process";
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

// ── fixture root: a real tmp dir (paths are fs-checked by some predicates) ────
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-parity-"));
fs.writeFileSync(path.join(ROOT, "existing.txt"), "real content"); // for truncating-write
fs.writeFileSync(path.join(ROOT, "README.md"), "readme");          // for allow-read
const TOOLING = path.join(ROOT, ".ai-dev", "tooling", "engine.mjs"); // never-writable
const STAMP = path.join(ROOT, ".ai-dev", "reviews", "x_review.md");  // the Reviewer's deliverable

// A second root with an EXPLICIT `full` profile, for the orchestrator-content case
// only: the profile default is `solo` (protocol-reference.md `## Project config`), so on the
// unconfigured ROOT that deny relaxes — only an explicit `full` keeps it. The
// default-resolution itself is pinned in rigor-profile.test.mjs.
const FULL = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-parity-full-"));
fs.mkdirSync(path.join(FULL, ".ai-dev"), { recursive: true });
fs.writeFileSync(path.join(FULL, ".ai-dev", "config.json"), '{ "profile": "full" }');

// A third root with an EXPLICIT `yolo` profile — turns the merge-gate OFF.
const YOLO = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-parity-yolo-"));
fs.mkdirSync(path.join(YOLO, ".ai-dev"), { recursive: true });
fs.writeFileSync(path.join(YOLO, ".ai-dev", "config.json"), '{ "profile": "yolo" }');
const ARCH = path.join(FULL, "docs", "architecture.md");           // canonical doc (not orch-writable)

// A configured root checked out on `main` WITH commit history — for the F4
// commit-on-unstamped-main deny. A loose ref under refs/heads/main is the history marker
// repoHasCommits reads; .ai-dev/config.json makes it a configured (non-bootstrap) project.
const MAINCFG = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-parity-main-"));
fs.mkdirSync(path.join(MAINCFG, ".git", "refs", "heads"), { recursive: true });
fs.writeFileSync(path.join(MAINCFG, ".git", "HEAD"), "ref: refs/heads/main\n");
fs.writeFileSync(path.join(MAINCFG, ".git", "refs", "heads", "main"), "abc1234\n");
fs.mkdirSync(path.join(MAINCFG, ".ai-dev"), { recursive: true });
fs.writeFileSync(path.join(MAINCFG, ".ai-dev", "config.json"), "{}");

// Each case carries BOTH platforms' native payloads + the expected ENGINE verdict
// (pre platform-mapping). `claudeExpect` / `opencodeExpect` override `expect` only
// where a platform's capability legitimately diverges — those are the documented
// divergences, printed below, never hidden.
const FIXTURE = [
  { name: "read-outside-root", expect: "deny",
    claude: { tool_name: "Read", tool_input: { file_path: "/etc/passwd" } },
    opencode: { tool: "read", args: { filePath: "/etc/passwd" } } },

  { name: "find-outside-root", expect: "deny",
    claude: { tool_name: "Bash", tool_input: { command: "find / -name secret" } },
    opencode: { tool: "bash", args: { command: "find / -name secret" } } },

  // ── Bash-read boundary (read-bash-outside-root) — DENY: a recognised+resolved
  // out-of-root read. Conservative extractor (input redirect / pure-file / pattern-first).
  { name: "read-bash-cat-abs", expect: "deny",
    claude: { tool_name: "Bash", tool_input: { command: "cat /etc/passwd" } },
    opencode: { tool: "bash", args: { command: "cat /etc/passwd" } } },
  { name: "read-bash-grep-pattern-first", expect: "deny", // the live observed case
    claude: { tool_name: "Bash", tool_input: { command: "grep -c . /tmp/x/tasks/a.output" } },
    opencode: { tool: "bash", args: { command: "grep -c . /tmp/x/tasks/a.output" } } },
  { name: "read-bash-head-other-project", expect: "deny",
    claude: { tool_name: "Bash", tool_input: { command: "head /other-project/secret" } },
    opencode: { tool: "bash", args: { command: "head /other-project/secret" } } },
  { name: "read-bash-tilde", expect: "deny", // ~ is $HOME — outside the project root
    claude: { tool_name: "Bash", tool_input: { command: "cat ~/.ssh/id_rsa" } },
    opencode: { tool: "bash", args: { command: "cat ~/.ssh/id_rsa" } } },
  { name: "read-bash-input-redirect", expect: "deny",
    claude: { tool_name: "Bash", tool_input: { command: "tr a b < /etc/shadow" } },
    opencode: { tool: "bash", args: { command: "tr a b < /etc/shadow" } } },
  { name: "read-bash-relative-traversal", expect: "deny", // ../ resolves outside ROOT
    claude: { tool_name: "Bash", tool_input: { command: "tail ../../secret" } },
    opencode: { tool: "bash", args: { command: "tail ../../secret" } } },
  { name: "read-bash-tooling-carveout", expect: "deny", // a tooling read denies like find/write
    claude: { tool_name: "Bash", tool_input: { command: `cat ${TOOLING}` } },
    opencode: { tool: "bash", args: { command: `cat ${TOOLING}` } } },

  // ── Bash-read boundary — ALLOW (no false positive / fail-open on a parse miss).
  // Quoted-prose regressions (the live dogfood false-positive): a quoted argument
  // body that merely MENTIONS an out-of-root read is masked first ⇒ NO target ⇒ allow.
  { name: "read-bash-commit-msg-mentions-read", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: 'git commit -m "describes cat /etc/passwd and < /etc/shadow"' } },
    opencode: { tool: "bash", args: { command: 'git commit -m "describes cat /etc/passwd and < /etc/shadow"' } } },
  { name: "read-bash-echo-mentions-path", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: 'echo "see /etc/passwd"' } },
    opencode: { tool: "bash", args: { command: 'echo "see /etc/passwd"' } } },
  // A genuinely quoted-PATH read is the accepted fail-open consequence of masking.
  { name: "read-bash-quoted-path-fail-open", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: 'cat "/etc/passwd"' } },
    opencode: { tool: "bash", args: { command: 'cat "/etc/passwd"' } } },
  // A quoted sed script that mentions an out-of-root path is masked ⇒ allow.
  { name: "read-bash-sed-quoted-script", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: "sed -e 's#/etc/x#y#' README.md" } },
    opencode: { tool: "bash", args: { command: "sed -e 's#/etc/x#y#' README.md" } } },
  { name: "read-bash-grep-pattern-not-file", expect: "allow", // '/etc/passwd' is the PATTERN
    claude: { tool_name: "Bash", tool_input: { command: "grep '/etc/passwd' README.md" } },
    opencode: { tool: "bash", args: { command: "grep '/etc/passwd' README.md" } } },
  { name: "read-bash-relative-in-root", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: "cat subdir/file" } },
    opencode: { tool: "bash", args: { command: "cat subdir/file" } } },
  { name: "read-bash-stream-redirect", expect: "allow", // < /dev/null is a stream, not a file
    claude: { tool_name: "Bash", tool_input: { command: "cmd < /dev/null" } },
    opencode: { tool: "bash", args: { command: "cmd < /dev/null" } } },
  { name: "read-bash-dot-in-root", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: "cat ./README.md" } },
    opencode: { tool: "bash", args: { command: "cat ./README.md" } } },
  { name: "read-bash-var-fail-open", expect: "allow", // $VAR unresolvable ⇒ fail-open
    claude: { tool_name: "Bash", tool_input: { command: "head $SECRET" } },
    opencode: { tool: "bash", args: { command: "head $SECRET" } } },
  { name: "read-bash-interpreter-fail-open", expect: "allow", // unlisted/interpreter ⇒ fail-open
    claude: { tool_name: "Bash", tool_input: { command: "python3 -c \"open('/etc/passwd')\"" } },
    opencode: { tool: "bash", args: { command: "python3 -c \"open('/etc/passwd')\"" } } },

  { name: "write-outside-root", expect: "deny",
    claude: { tool_name: "Write", tool_input: { file_path: "/etc/foo", content: "x" } },
    opencode: { tool: "write", args: { filePath: "/etc/foo", content: "x" } } },

  { name: "truncating-write", expect: "deny",
    claude: { tool_name: "Write", tool_input: { file_path: path.join(ROOT, "existing.txt"), content: "   " } },
    opencode: { tool: "write", args: { filePath: path.join(ROOT, "existing.txt"), content: "  " } } },

  // THE documented actor divergence: the orchestrator-content rule is mechanical
  // on OpenCode (session lookup resolves the actor) but persona on Claude (a hook
  // payload carries no session-role signal → isOrchestrator undefined → allow).
  // Runs on the explicit-`full` root — under the `solo` default this deny relaxes.
  { name: "orchestrator-authors-content", claudeExpect: "allow", opencodeExpect: "deny", divergence: true, root: FULL,
    claude: { tool_name: "Write", tool_input: { file_path: ARCH, content: "x" } },
    opencode: { tool: "write", args: { filePath: ARCH, content: "x" }, isOrchestrator: true } },

  { name: "self-patch-enforcer", expect: "deny",
    claude: { tool_name: "Write", tool_input: { file_path: TOOLING, content: "x" } },
    opencode: { tool: "write", args: { filePath: TOOLING, content: "x" } } },

  // The stamp-fabrication guard shares the actor divergence: the orchestrator
  // writing a review stamp is denied where the actor resolves (OpenCode), persona
  // on Claude (no session-role signal in the hook payload).
  { name: "orchestrator-writes-review-stamp", claudeExpect: "allow", opencodeExpect: "deny", divergence: true,
    claude: { tool_name: "Write", tool_input: { file_path: STAMP, content: "## Code review: APPROVED" } },
    opencode: { tool: "write", args: { filePath: STAMP, content: "## Code review: APPROVED" }, isOrchestrator: true } },

  { name: "merge-while-unstamped", expect: "deny",
    claude: { tool_name: "Bash", tool_input: { command: "git push origin feature/foo" } },
    opencode: { tool: "bash", args: { command: "git push origin feature/foo" } } },

  // F1: an explicit unstamped trunk push (`git push origin main`) DENIES on BOTH
  // columns — the whole point of the fix. Before it, the bare `main` ref was
  // unresolvable → routed to the ask rule → SILENT PASS on OpenCode (no ask-return).
  // ROOT carries no main_review.md, so the gate denies; both shims must agree on deny.
  { name: "explicit-trunk-push-denies", expect: "deny",
    claude: { tool_name: "Bash", tool_input: { command: "git push origin main" } },
    opencode: { tool: "bash", args: { command: "git push origin main" } } },

  // The merge-gate's no-silent-pass companion: an UNRESOLVABLE topic (ROOT has no
  // .git HEAD ref, and a bare push names no branch ref) leaves the stamp
  // uncheckable — ask, never pass.
  { name: "merge-topic-unresolvable", expect: "ask",
    claude: { tool_name: "Bash", tool_input: { command: "git push" } },
    opencode: { tool: "bash", args: { command: "git push" } } },

  // A non-shell heredoc body is DATA: push prose inside it must not trip the
  // merge-gate verbs on either platform (the engine strips it before any rule).
  { name: "heredoc-body-is-data", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: "python3 <<'EOF'\nprint(\"git push origin main\")\nEOF" } },
    opencode: { tool: "bash", args: { command: "python3 <<'EOF'\nprint(\"git push origin main\")\nEOF" } } },

  { name: "role-generic-in-seat", expect: "deny",
    claude: { tool_name: "Task", tool_input: { subagent_type: "general" } },
    opencode: { tool: "task", args: { subagent_type: "general" } } },

  { name: "role-duplicator-in-seat", expect: "deny",
    claude: { tool_name: "Task", tool_input: { subagent_type: "wb-development:coder" } },
    opencode: { tool: "task", args: { subagent_type: "wb-development:coder" } } },

  { name: "ssh-content-edit", expect: "deny",
    claude: { tool_name: "Bash", tool_input: { command: 'ssh host "sed -i s/a/b/ /etc/f"' } },
    opencode: { tool: "bash", args: { command: 'ssh host "sed -i s/a/b/ /etc/f"' } } },

  // A remote READ that only uses a stream redirect is NOT a remote edit — both shims allow.
  { name: "ssh-stream-redirect-read", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: 'ssh host "grep err /var/log/syslog 2>/dev/null"' } },
    opencode: { tool: "bash", args: { command: 'ssh host "grep err /var/log/syslog 2>/dev/null"' } } },

  { name: "ssh-mutating-action", expect: "ask",
    claude: { tool_name: "Bash", tool_input: { command: 'ssh host "systemctl restart nginx"' } },
    opencode: { tool: "bash", args: { command: 'ssh host "systemctl restart nginx"' } } },

  { name: "force-push", expect: "ask",
    claude: { tool_name: "Bash", tool_input: { command: "git push --force origin topic" } },
    opencode: { tool: "bash", args: { command: "git push --force origin topic" } } },

  { name: "commit-no-verify", expect: "ask",
    claude: { tool_name: "Bash", tool_input: { command: "git commit --no-verify -m wip" } },
    opencode: { tool: "bash", args: { command: "git commit --no-verify -m wip" } } },

  // F4a: a blind bulk-stage denies on BOTH platforms (no actor resolution — the command
  // is visible to both shims). Runs on the shared ROOT (gitAddAll ignores branch/config).
  { name: "git-add-all-denies", expect: "deny",
    claude: { tool_name: "Bash", tool_input: { command: "git add -A" } },
    opencode: { tool: "bash", args: { command: "git add -A" } } },

  // F4a ALLOW: a named-path add is never a bulk stage — both shims allow.
  { name: "git-add-named-allows", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: "git add README.md" } },
    opencode: { tool: "bash", args: { command: "git add README.md" } } },

  // F4b: a git commit on a configured main checkout with history denies on BOTH platforms.
  { name: "commit-on-main-denies", expect: "deny", root: MAINCFG,
    claude: { tool_name: "Bash", tool_input: { command: "git commit -m x" } },
    opencode: { tool: "bash", args: { command: "git commit -m x" } } },

  // inject is prompt-act: the engine DECIDES inject for both platforms (asserted here).
  // Claude REALISES it via UserPromptSubmit; OpenCode no longer realises it at all — the
  // former chat.message hook crashed the host (plugin-entry.mjs), so inject is
  // persona-only on OpenCode. A change-verb prompt on the shared (UNconfigured) ROOT
  // yields an inject DECISION; WHICH inject (setup vs route) is asserted by ruleId in
  // block 1b below.
  { name: "change-verb-prompt-injects", expect: "inject",
    claude: { hook_event_name: "UserPromptSubmit", prompt: "please implement the new export feature" },
    opencodePrompt: "please implement the new export feature" },

  { name: "allow-read-inside-root", expect: "allow",
    claude: { tool_name: "Read", tool_input: { file_path: path.join(ROOT, "README.md") } },
    opencode: { tool: "read", args: { filePath: path.join(ROOT, "README.md") } } },

  { name: "allow-spawn-dev-role", expect: "allow",
    claude: { tool_name: "Task", tool_input: { subagent_type: "dev-builder" } },
    opencode: { tool: "task", args: { subagent_type: "dev-builder" } } },

  { name: "allow-spawn-dev-planner", expect: "allow",
    claude: { tool_name: "Task", tool_input: { subagent_type: "dev-planner" } },
    opencode: { tool: "task", args: { subagent_type: "dev-planner" } } },

  // fork subagent_type is NOT in the deny set (role_deny_set generic_builtins / role_duplicators)
  // — the Orchestrator MAY use it for its own direct plan/build work (tool-map fork-for-direct-work).
  // This case pins that it is never denied by the engine, so the prose-only guard (never fork for
  // the Reviewer or full-profile Planner) is the only thing that holds — confirming the decision
  // doc's analysis (docs/decisions/fork-primitive-adoption.md ## 2).
  { name: "allow-spawn-fork", expect: "allow",
    claude: { tool_name: "Task", tool_input: { subagent_type: "fork" } },
    opencode: { tool: "task", args: { subagent_type: "fork" } } },

  // yolo profile turns the merge-gate OFF — an unstamped push on a yolo project allows.
  // Uses a dedicated yolo root so only this case sees that profile.
  { name: "yolo-merge-gate-off", expect: "allow", root: YOLO,
    claude: { tool_name: "Bash", tool_input: { command: "git push origin feature/foo" } },
    opencode: { tool: "bash", args: { command: "git push origin feature/foo" } } },

  // Tag pushes never need a review stamp — vN.N.N token, refs/tags/ prefix, --tags flag.
  { name: "tag-push-version-allow", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: "git push origin v5.3.1" } },
    opencode: { tool: "bash", args: { command: "git push origin v5.3.1" } } },

  { name: "tag-push-refs-tags-allow", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: "git push origin refs/tags/v5.4.0" } },
    opencode: { tool: "bash", args: { command: "git push origin refs/tags/v5.4.0" } } },

  { name: "tag-push-flag-allow", expect: "allow",
    claude: { tool_name: "Bash", tool_input: { command: "git push --tags origin" } },
    opencode: { tool: "bash", args: { command: "git push --tags origin" } } },
];

// ── 1. PARITY ────────────────────────────────────────────────────────────────
console.log("PARITY (each case through both shims → identical engine verdict):");
const divergences = [];
for (const c of FIXTURE) {
  const root = c.root ?? ROOT; // a case may pin its own root (the explicit-`full` one)
  let cv, ov;
  if (c.claude) {
    cv = claudeDecide(c.claude, root, config).verdict;
    check(`claude:${c.name}`, cv, c.claudeExpect ?? c.expect);
  }
  if (c.opencode) {
    ov = ocDecide(c.opencode.tool, c.opencode.args, root, c.opencode.isOrchestrator ?? false, config).verdict;
    check(`opencode:${c.name}`, ov, c.opencodeExpect ?? c.expect);
  }
  // Prompt-act cases drive the OpenCode chat.message path (decidePrompt) rather
  // than the tool path — same shared engine, asserting OpenCode reaches the same
  // verdict as Claude on a prompt.
  if (c.opencodePrompt) {
    ov = ocDecidePrompt(c.opencodePrompt, root, false, config).verdict;
    check(`opencode:${c.name}`, ov, c.opencodeExpect ?? c.expect);
  }
  // Cross-check: where both platforms run and NO divergence is declared, the two
  // shims must agree byte-for-byte on the verdict — the real anti-drift assertion.
  if (c.claude && (c.opencode || c.opencodePrompt) && !c.divergence) check(`parity:${c.name}`, cv, ov);
  if (c.divergence) divergences.push(`${c.name}: claude=${cv} opencode=${ov}`);
}
if (divergences.length) {
  console.log("  documented capability divergences (NOT drift — recorded per-case):");
  for (const d of divergences) console.log(`    • ${d}`);
}

// ── 1b. CONFIG-SENSITIVE INJECT: which inject fires depends on project state ───
// Three inject rules match a change-verb prompt; the engine returns the FIRST, in
// registry order: no-config-run-setup → no-product-brief-discover → change-route-
// reminder. So the prompt walks a three-stage ladder as the project fills in:
//   no config           ⇒ setup nudge,
//   configured, no brief ⇒ product-discovery nudge,
//   configured + brief   ⇒ route reminder.
// Asserted by ruleId (verdict is `inject` at every stage) — the only place the
// three injects are told apart. Engine-level and platform-independent (both
// Claude's UserPromptSubmit and OpenCode's chat.message reach the same engine),
// so asserted once via the Claude prompt path.
console.log("CONFIG-SENSITIVE INJECT (no config ⇒ setup; configured no-brief ⇒ discovery; configured+brief ⇒ route):");
const changePrompt = { hook_event_name: "UserPromptSubmit", prompt: "please implement the new export feature" };

// Stage 1 — unconfigured root: a fresh tmp dir with NO .ai-dev/config.json.
const NOCFG = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-nocfg-"));
check("no-config-run-setup:fires", claudeDecide(changePrompt, NOCFG, config).ruleId, "no-config-run-setup");
fs.rmSync(NOCFG, { recursive: true, force: true });

// Stage 2 — configured but NO docs/product.md ⇒ promptNeedsSetup is false but
// promptNeedsProductBrief is true ⇒ the discovery nudge fires.
const CFG = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-cfg-"));
fs.mkdirSync(path.join(CFG, ".ai-dev"), { recursive: true });
fs.writeFileSync(path.join(CFG, ".ai-dev", "config.json"), "{}");
check("no-product-brief-discover:fires-when-configured-no-brief", claudeDecide(changePrompt, CFG, config).ruleId, "no-product-brief-discover");

// Stage 2b — configured AND docs/product.md present but STILL THE TEMPLATE ⇒ the
// discovery nudge still fires (the 4.18.0 fix: install.mjs lands the template
// verbatim, so presence alone proved nothing — on every real install the nudge
// was dead code). Three sub-cases, one per detection layer:
//   the real template file (the exact install-landed content, end to end),
//   the sentinel alone (the forward-looking layer),
//   the legacy §0 placeholder alone (a pre-sentinel template, no first line).
fs.mkdirSync(path.join(CFG, "docs"));
fs.writeFileSync(path.join(CFG, "docs", "product.md"), fs.readFileSync(path.join(HERE, "..", "templates", "product.md")));
check("no-product-brief-discover:fires-on-installed-template", claudeDecide(changePrompt, CFG, config).ruleId, "no-product-brief-discover");
fs.writeFileSync(path.join(CFG, "docs", "product.md"), "<!-- ai-dev:template -->\n# Product brief\n");
check("no-product-brief-discover:fires-on-sentinel-marker", claudeDecide(changePrompt, CFG, config).ruleId, "no-product-brief-discover");
fs.writeFileSync(path.join(CFG, "docs", "product.md"), "# Product brief\n\n`<one plain sentence: what this product is and what it does. …>`\n");
check("no-product-brief-discover:fires-on-legacy-placeholder", claudeDecide(changePrompt, CFG, config).ruleId, "no-product-brief-discover");

// Stage 3 — configured AND docs/product.md present and FILLED (no template
// marker) ⇒ both setup and brief predicates are false ⇒ change-route-reminder fires
// (as the LEADING inject), now joined with the always-on language-mirror reminder.
fs.writeFileSync(path.join(CFG, "docs", "product.md"), "brief");
check("change-route-reminder:fires-when-configured-with-brief", claudeDecide(changePrompt, CFG, config).ruleId, "change-route-reminder");

// ── 1d. LANGUAGE-MIRROR: an always-on inject, aggregated with any conditional one ──
// The intent strings are read from the registry so the assertions track the rule
// wording (one home — invariant 6), never a hand-copied paraphrase.
const mirrorIntent = config.rules.find((r) => r.id === "language-mirror").intent;
const routeIntent = config.rules.find((r) => r.id === "change-route-reminder").intent;
const plainPrompt = { hook_event_name: "UserPromptSubmit", prompt: "good morning" };

// fires-alone: a plain (non-change) prompt on a configured+brief root now INJECTS
// via language-mirror alone — ruleId is language-mirror, reason is just its intent.
{
  const r = claudeDecide(plainPrompt, CFG, config);
  check("language-mirror:fires-alone-verdict", r.verdict, "inject");
  check("language-mirror:fires-alone-ruleId", r.ruleId, "language-mirror");
  check("language-mirror:fires-alone-reason", r.reason, mirrorIntent);
}

// co-fires-joined: a change-verb prompt aggregates change-route-reminder (leading)
// WITH language-mirror — ruleId is the leading conditional, reason carries BOTH
// intents, change-route BEFORE language-mirror (registry/join order).
{
  const r = claudeDecide(changePrompt, CFG, config);
  check("language-mirror:co-fires-verdict", r.verdict, "inject");
  check("language-mirror:co-fires-ruleId-leads-with-conditional", r.ruleId, "change-route-reminder");
  check("language-mirror:co-fires-reason-has-route", r.reason.includes(routeIntent), true);
  check("language-mirror:co-fires-reason-has-mirror", r.reason.includes(mirrorIntent), true);
  check("language-mirror:co-fires-join-order", r.reason.indexOf(routeIntent) < r.reason.indexOf(mirrorIntent), true);
  // parity: both shims reach the IDENTICAL aggregated engine result (verdict + reason).
  const oc = ocDecidePrompt(changePrompt.prompt, CFG, false, config);
  check("language-mirror:co-fires-parity-verdict", r.verdict, oc.verdict);
  check("language-mirror:co-fires-parity-reason", r.reason, oc.reason);
}
fs.rmSync(CFG, { recursive: true, force: true });

// toggle-off: a configured+brief root with safeguards.language-mirror:"off" — the
// always-on reminder is suppressed. A plain prompt ⇒ allow (nothing else fires); a
// change-verb prompt ⇒ change-route-reminder alone, its reason WITHOUT the mirror intent.
{
  const OFF = fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-mirror-off-"));
  fs.mkdirSync(path.join(OFF, ".ai-dev"), { recursive: true });
  fs.writeFileSync(path.join(OFF, ".ai-dev", "config.json"), '{ "safeguards": { "language-mirror": "off" } }');
  fs.mkdirSync(path.join(OFF, "docs"));
  fs.writeFileSync(path.join(OFF, "docs", "product.md"), "brief");
  check("language-mirror:toggle-off-plain-allows", claudeDecide(plainPrompt, OFF, config).verdict, "allow");
  const r = claudeDecide(changePrompt, OFF, config);
  check("language-mirror:toggle-off-change-still-routes", r.ruleId, "change-route-reminder");
  check("language-mirror:toggle-off-no-mirror-in-reason", r.reason.includes(mirrorIntent), false);
  fs.rmSync(OFF, { recursive: true, force: true });
}

// ── 1c. MULTI-REPO COMPONENTS: the boundary widens to the declared set ────────
// The full Step-2 test matrix (`.ai-dev/plans/multi-repo-components.md` `## Test
// matrix`), asserted through BOTH shims at the engine-verdict level. A valid
// `.ai-dev/components.json` at the session root widens the project-boundary denies
// to any declared sibling; everything else (no manifest / non-declared / malformed /
// overbroad / nonexistent / symlink-escape) stays denied — fail closed. The tooling
// deny stays unconditional even when a declared sibling contains a tooling dir.
//
// Each case builds a real workspace: a parent dir holding the session root `host/`
// plus real sibling dirs and a real components.json — the boundary predicates
// fs-check + realpath the declared roots, so real dirs are required.
console.log("COMPONENTS MATRIX (boundary widens to the declared set; both shims, fail closed):");

// realpath the parent so expected canonical paths match what realpathSync resolves.
function compWorkspace(siblings = []) {
  const parent = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ai-dev-cmatrix-")));
  const host = path.join(parent, "host");
  fs.mkdirSync(path.join(host, ".ai-dev"), { recursive: true });
  for (const s of siblings) fs.mkdirSync(path.join(parent, s), { recursive: true });
  return { parent, host };
}
function writeComponents(host, body) {
  fs.writeFileSync(path.join(host, ".ai-dev", "components.json"), body);
}
function cleanupComp(parent) { fs.rmSync(parent, { recursive: true, force: true }); }
// Run one read/write through BOTH shims against a pinned root, assert the engine
// verdict on each, and (no divergence here — boundary is mechanical on both) assert
// the two shims agree.
function bothExpect(name, root, claudePayload, ocTool, ocArgs, want) {
  const cv = claudeDecide(claudePayload, root, config).verdict;
  const ov = ocDecide(ocTool, ocArgs, root, false, config).verdict;
  check(`claude:${name}`, cv, want);
  check(`opencode:${name}`, ov, want);
  check(`parity:${name}`, cv, ov);
}

// declared-sibling-read / declared-sibling-write ⇒ ALLOW (the widening).
{
  const { parent, host } = compWorkspace(["frontend"]);
  writeComponents(host, JSON.stringify([{ root: "../frontend", role: "frontend", stack: "react" }]));
  const target = path.join(parent, "frontend", "src.js");
  bothExpect("declared-sibling-read", host,
    { tool_name: "Read", tool_input: { file_path: target } }, "read", { filePath: target }, "allow");
  bothExpect("declared-sibling-write", host,
    { tool_name: "Write", tool_input: { file_path: target, content: "x" } }, "write", { filePath: target, content: "x" }, "allow");
  // A Bash READ of a declared sibling ⇒ ALLOW (read-bash-outside-root inherits the
  // component-set allow via isInsideAnyComponent, like the Read-tool case above).
  bothExpect("declared-sibling-bash-read", host,
    { tool_name: "Bash", tool_input: { command: `cat ${target}` } }, "bash", { command: `cat ${target}` }, "allow");
  // non-declared sibling (backend exists but is NOT in the manifest) ⇒ DENY.
  fs.mkdirSync(path.join(parent, "backend"));
  const undeclared = path.join(parent, "backend", "src.py");
  bothExpect("non-declared-sibling", host,
    { tool_name: "Read", tool_input: { file_path: undeclared } }, "read", { filePath: undeclared }, "deny");
  // session-root-still-works ⇒ ALLOW (root always in the set).
  fs.writeFileSync(path.join(host, "README.md"), "r");
  const inRoot = path.join(host, "README.md");
  bothExpect("session-root-still-works", host,
    { tool_name: "Read", tool_input: { file_path: inRoot } }, "read", { filePath: inRoot }, "allow");
  cleanupComp(parent);
}

// no-manifest-single-root ⇒ DENY (unchanged from today): the SAME sibling target,
// but with no components.json, is outside the boundary.
{
  const { parent, host } = compWorkspace(["frontend"]); // no manifest written
  const target = path.join(parent, "frontend", "src.js");
  bothExpect("no-manifest-single-root", host,
    { tool_name: "Read", tool_input: { file_path: target } }, "read", { filePath: target }, "deny");
  cleanupComp(parent);
}

// malformed-json ⇒ DENY (fail closed): a declared sibling is denied when the
// manifest cannot parse.
{
  const { parent, host } = compWorkspace(["frontend"]);
  writeComponents(host, "{ not valid json ][");
  const target = path.join(parent, "frontend", "src.js");
  bothExpect("malformed-json", host,
    { tool_name: "Read", tool_input: { file_path: target } }, "read", { filePath: target }, "deny");
  cleanupComp(parent);
}

// overbroad-root ⇒ DENY (whole manifest rejected): one entry resolves to "/", so
// the WHOLE manifest is rejected and even the otherwise-valid sibling is denied.
{
  const { parent, host } = compWorkspace(["frontend"]);
  writeComponents(host, JSON.stringify([
    { root: "../frontend", role: "frontend", stack: "x" },
    { root: "/", role: "evil", stack: "y" },
  ]));
  const target = path.join(parent, "frontend", "src.js");
  bothExpect("overbroad-root", host,
    { tool_name: "Read", tool_input: { file_path: target } }, "read", { filePath: target }, "deny");
  cleanupComp(parent);
}

// nonexistent-root ⇒ DENY (whole manifest rejected): a declared `../ghost` does not
// exist, so the manifest is rejected and the real sibling is denied.
{
  const { parent, host } = compWorkspace(["frontend"]); // no `ghost`
  writeComponents(host, JSON.stringify([
    { root: "../frontend", role: "frontend", stack: "x" },
    { root: "../ghost", role: "missing", stack: "y" },
  ]));
  const target = path.join(parent, "frontend", "src.js");
  bothExpect("nonexistent-root", host,
    { tool_name: "Read", tool_input: { file_path: target } }, "read", { filePath: target }, "deny");
  cleanupComp(parent);
}

// empty-shape ⇒ DENY (fail closed): a valid-JSON but empty array is not a non-empty
// array of root objects, so the manifest is rejected.
{
  const { parent, host } = compWorkspace(["frontend"]);
  writeComponents(host, "[]");
  const target = path.join(parent, "frontend", "src.js");
  bothExpect("empty-shape", host,
    { tool_name: "Read", tool_input: { file_path: target } }, "read", { filePath: target }, "deny");
  cleanupComp(parent);
}

// symlink-escape ⇒ DENY: a declared root that is a symlink to a filesystem root
// resolves (via realpath) to "/" and is rejected as overbroad — so a read in /etc
// stays denied. Skipped where the sandbox forbids symlink creation.
{
  const { parent, host } = compWorkspace([]);
  let symlinked = false;
  try { fs.symlinkSync("/", path.join(parent, "escape")); symlinked = true; }
  catch (e) { if (e.code !== "EPERM" && e.code !== "EACCES") throw e; }
  if (symlinked) {
    writeComponents(host, JSON.stringify([{ root: "../escape", role: "x", stack: "y" }]));
    bothExpect("symlink-escape", host,
      { tool_name: "Read", tool_input: { file_path: "/etc/passwd" } }, "read", { filePath: "/etc/passwd" }, "deny");
  } else { console.log("  · symlink-escape: skipped (symlink not permitted here)"); }
  cleanupComp(parent);
}

// sibling-tooling ⇒ DENY (unconditional, PER-ROOT): a DECLARED sibling whose own
// subtree contains a `.ai-dev/tooling/` dir. The tooling carve-out (invariant 2)
// must hold under EVERY declared root, not only the session's — so a read AND a
// write of the SIBLING's tooling both stay denied even though the sibling root IS
// in the validated set (the manifest must never widen into a sibling's enforcer
// source). This is the threat F1 named: before the per-root fix the sibling was in
// the set and its tooling was reachable. We build a REAL `frontend/.ai-dev/tooling/`
// dir so realpath resolves, and assert deny on both read and write, both shims.
{
  const { parent, host } = compWorkspace(["frontend"]);
  fs.mkdirSync(path.join(parent, "frontend", ".ai-dev", "tooling"), { recursive: true });
  writeComponents(host, JSON.stringify([{ root: "../frontend", role: "frontend", stack: "x" }]));
  const siblingTooling = path.join(parent, "frontend", ".ai-dev", "tooling", "engine.mjs");
  bothExpect("sibling-tooling-write", host,
    { tool_name: "Write", tool_input: { file_path: siblingTooling, content: "x" } },
    "write", { filePath: siblingTooling, content: "x" }, "deny");
  bothExpect("sibling-tooling-read", host,
    { tool_name: "Read", tool_input: { file_path: siblingTooling } },
    "read", { filePath: siblingTooling }, "deny");
  // Sanity: a NON-tooling file in the SAME declared sibling stays ALLOWed — the
  // carve-out denies only the tooling subtree, not the whole sibling.
  const siblingNormal = path.join(parent, "frontend", "src.js");
  bothExpect("sibling-nontooling-allow", host,
    { tool_name: "Read", tool_input: { file_path: siblingNormal } },
    "read", { filePath: siblingNormal }, "allow");
  // And the SESSION root's own tooling stays denied exactly as before (unchanged).
  const sessionTooling = path.join(host, ".ai-dev", "tooling", "engine.mjs");
  bothExpect("session-tooling-write", host,
    { tool_name: "Write", tool_input: { file_path: sessionTooling, content: "x" } },
    "write", { filePath: sessionTooling, content: "x" }, "deny");
  cleanupComp(parent);
}

// inside-root-unchanged ⇒ a normal in-root write reaches the IDENTICAL verdict with
// and without a valid manifest present (the regression guard: the widening never
// changes an in-root verdict). A truncating write over a non-empty in-root file is
// denied in both states; a fresh in-root write is allowed in both.
{
  const { parent, host } = compWorkspace(["frontend"]);
  fs.writeFileSync(path.join(host, "existing.txt"), "real content");
  const fresh = path.join(host, "new.txt");
  // without manifest
  const noManFresh = claudeDecide({ tool_name: "Write", tool_input: { file_path: fresh, content: "x" } }, host, config).verdict;
  // with valid manifest
  writeComponents(host, JSON.stringify([{ root: "../frontend", role: "frontend", stack: "x" }]));
  const manFresh = claudeDecide({ tool_name: "Write", tool_input: { file_path: fresh, content: "x" } }, host, config).verdict;
  check("inside-root-unchanged:fresh-write", noManFresh, manFresh);
  check("inside-root-unchanged:fresh-write-allow", manFresh, "allow");
  cleanupComp(parent);
}

// Regression guard: the EXISTING boundary cases (read/find/write-outside-root) still
// deny on a no-manifest root — re-run them on a fresh manifest-less root to prove the
// default is byte-unchanged (they already run on ROOT above; this pins it explicitly
// against a root that never had a components.json).
{
  const { parent, host } = compWorkspace([]); // no manifest
  bothExpect("boundary-default:read-outside", host,
    { tool_name: "Read", tool_input: { file_path: "/etc/passwd" } }, "read", { filePath: "/etc/passwd" }, "deny");
  bothExpect("boundary-default:write-outside", host,
    { tool_name: "Write", tool_input: { file_path: "/etc/foo", content: "x" } }, "write", { filePath: "/etc/foo", content: "x" }, "deny");
  const cf = claudeDecide({ tool_name: "Bash", tool_input: { command: "find / -name secret" } }, host, config).verdict;
  const of = ocDecide("bash", { command: "find / -name secret" }, host, false, config).verdict;
  check("claude:boundary-default:find-outside", cf, "deny");
  check("opencode:boundary-default:find-outside", of, "deny");
  cleanupComp(parent);
}

// ── 2. SINGLE-ENGINE: no rule logic leaked into a shim ───────────────────────
// Tokens that appear ONLY in rule data/predicates (deny-rules.json / engine.mjs),
// never in legitimate normalise/verdict-map glue. If one shows up in a shim, a
// rule was re-implemented there and parity could silently drift.
console.log("SINGLE-ENGINE (shims carry no rule logic):");
const FORBIDDEN = ["wb-development", "systemctl", "force-with-lease", "no-verify", "реализ", "maskQuotedSpans"];
const SHIMS = ["claude/shim.mjs", "opencode/normalise.mjs", "opencode/plugin-entry.mjs"];
for (const rel of SHIMS) {
  const text = fs.readFileSync(path.join(HERE, rel), "utf8");
  const hit = FORBIDDEN.find((tok) => text.includes(tok));
  check(`no-rule-logic:${rel}`, hit ? `leaked:${hit}` : "clean", "clean");
}

// ── 3. NODE-SPIKE: a Claude hook running `node shim.mjs` denies via stdin ─────
console.log("NODE-SPIKE (Claude hook → node shim.mjs → deny JSON on stdin):");
const SHIM = path.join(HERE, "claude", "shim.mjs");
function runShim(payload) {
  return execFileSync("node", [SHIM], { input: JSON.stringify(payload), encoding: "utf8" });
}
// deny: a read outside root. cwd is a non-git tmp dir → root falls back to it.
const denyOut = runShim({ tool_name: "Read", tool_input: { file_path: "/etc/passwd" }, cwd: ROOT });
let denyOK = false;
try { denyOK = JSON.parse(denyOut).hookSpecificOutput.permissionDecision === "deny"; } catch { /* non-JSON output ⇒ denyOK stays false */ }
check("spike:deny-json", denyOK, true);
// allow: a read inside root prints nothing.
const allowOut = runShim({ tool_name: "Read", tool_input: { file_path: path.join(ROOT, "README.md") }, cwd: ROOT });
check("spike:allow-empty", allowOut.trim(), "");
// latency: the cost of node-per-call (the assumption being validated).
const N = 5; const t0 = process.hrtime.bigint();
for (let i = 0; i < N; i++) runShim({ tool_name: "Read", tool_input: { file_path: "/etc/passwd" }, cwd: ROOT });
const msPerCall = Number(process.hrtime.bigint() - t0) / 1e6 / N;
console.log(`  node-per-call latency ≈ ${msPerCall.toFixed(0)} ms (cold spawn; inline shell+jq is the fallback if a hot path needs it)`);

// ── cleanup + report ─────────────────────────────────────────────────────────
fs.rmSync(ROOT, { recursive: true, force: true });
fs.rmSync(FULL, { recursive: true, force: true });
fs.rmSync(YOLO, { recursive: true, force: true });
fs.rmSync(MAINCFG, { recursive: true, force: true });
console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
