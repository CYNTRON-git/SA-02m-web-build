// The shared enforcement engine. ONE copy of every check, read by every
// platform shim. A shim normalises its harness payload to the neutral `input`
// shape and calls `evaluate`; the engine returns { verdict, ruleId, reason }.
// The engine holds the PREDICATES (the logic dispatch) + evaluate; deny-rules.json
// holds the rules and their data. The helper families the predicates lean on live
// in sibling modules (one home each), re-exported via `_internals` for the tests:
//   • engine-paths.mjs      — path/root resolution
//   • engine-bash.mjs       — bash-command parsing (write/read target extraction)
//   • engine-git.mjs        — merge-topic resolution + review-stamp reads
//   • engine-config.mjs     — config + filesystem-state reads
//   • engine-components.mjs — multi-repo boundary + orchestrator-writable
// No platform names appear here — that is the whole point.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { isInsideRoot, isInsideSanctioned, resolveTarget } from "./engine-paths.mjs";
import {
  findAbsolutePathArg,
  maskQuotedSpans,
  stripHeredocBodies,
  isStreamTarget,
  bashWriteTargets,
  bashReadTargets,
  isPureGitCommand,
} from "./engine-bash.mjs";
import {
  isTagPush,
  normalizeGitInvocation,
  pushExplicitTrunkRef,
  resolveMergeTopic,
  reviewStampSatisfied,
  reviewStampDiagnosis,
  STAMP_SEPARATORS,
  STAMP_LABELS,
  isTrunkFastForward,
} from "./engine-git.mjs";
import {
  fileNonEmpty,
  headBranch,
  repoHasCommits,
  TRUNK_BRANCHES,
  projectConfigured,
  productBriefFilled,
  projectProfile,
  projectHookMode,
  disabledSafeguards,
  safeguardRegistry,
} from "./engine-config.mjs";
import {
  relMatches,
  isOrchestratorAuthorable,
  writesIntoAnyNever,
  componentRoots,
  isInsideAnyComponent,
} from "./engine-components.mjs";

// ── neutral input shape ──────────────────────────────────────────────────────
// { act:'read'|'write'|'bash'|'spawn'|'prompt', root, path?, command?,
//   content?, contentEmpty?, spawnTarget?, isOrchestrator?, prompt?,
//   sanctionedScratch?, sanctionedScratchWritable?, home? }
// `sanctionedScratch`: adapter-derived canonical roots of the agent's OWN out-of-root
//   scratch (the tool-result overflow store + per-session temp), consulted by the READ
//   boundary predicates — a widening so a fetched/overflow artifact the harness itself
//   placed is not false-blocked. `sanctionedScratchWritable`: the narrower subset (the
//   per-session temp root only — never the overflow store), consulted by the WRITE
//   boundary predicate. `home`: $HOME for `~`→path expansion in bash-read. All threaded
//   by the platform shim; absent ⇒ today's strict behaviour.

// Compile a config-sourced pattern and test it, returning FALSE on a compile error.
// SCOPED TO INJECT-CLASS PREDICATES ONLY (promptMatchesChangeVerb / promptNeedsSetup /
// promptNeedsProductBrief): for an inject, false = "no nudge fires", the SAFE direction
// (a missing nudge is a lost reminder, never a missing deny). A DENY-class predicate
// must NOT reuse this — a deny needs fail-toward-deny (treat a bad pattern as a MATCH,
// not a miss), so `return false` there would fail OPEN. No deny predicate compiles a
// config pattern today (the floor predicates use literal inline regexes); this guard
// prevents a later footgun. `pat` is trusted internal data (deny-rules.json), not
// external injection — the try/catch handles a broken-install/malformed registry, not
// an attack.
function safeTest(pat, str) {
  if (!pat) return false;
  try { return new RegExp(pat, "i").test(str); }
  catch { return false; } // malformed pattern ⇒ no inject (safe for inject class only)
}

// ── predicates: (input, config) => boolean ───────────────────────────────────
// A predicate inspects only the neutral input + the rule data in config. The
// `actor` signal (isOrchestrator) is supplied by the shim where the platform
// can resolve it; where it cannot, the actor-dependent rules (orchestrator-content,
// stamp-write) fall back to persona (isOrchestrator undefined ⇒ predicate is
// false ⇒ allow, never a false deny).
function writeTargetsOf(input) {
  if (input.act === "write" && input.path) return [input.path];
  if (input.command) return bashWriteTargets(input.command);
  return [];
}
const PREDICATES = {
  // Read/find: a target is outside the boundary if it is in NO declared root, OR it
  // is inside ANY root's `.ai-dev/tooling/` carve-out (no dedicated tooling rule
  // covers reads, so the per-root carve-out is applied here — tooling deny outranks
  // the component-set allow, so a declared sibling's tooling is denied on read/find).
  pathOutsideRoot(input, config) {
    const r = resolveTarget(input.root, input.path);
    if (!r) return false;
    if (writesIntoAnyNever(input.root, r, config?.orchestrator_writable)) return true;
    return !(isInsideAnyComponent(input.root, r) || isInsideSanctioned(input.sanctionedScratch, r));
  },
  findTargetOutsideRoot(input, config) {
    const p = findAbsolutePathArg(input.command);
    if (!p) return false;
    const r = path.resolve(p);
    if (writesIntoAnyNever(input.root, r, config?.orchestrator_writable)) return true;
    return !(isInsideAnyComponent(input.root, r) || isInsideSanctioned(input.sanctionedScratch, r));
  },
  // Bash READ boundary — best-effort, the read twin of findTargetOutsideRoot.
  // For each extracted read target: a leading `~` is treated as outside-root (it
  // is $HOME — path.resolve would forge a fake in-root path; this also covers
  // `find ~/…` conceptually), then the same tooling carve-out (writesIntoAnyNever,
  // per-root) and component-set allow (isInsideAnyComponent) the other two
  // boundary read/find predicates use. A recognised+resolved out-of-root target
  // ⇒ deny (fail-closed); an unparseable command or a statically unresolvable
  // token ($VAR/$(…)/interpreter/unlisted) yields no target ⇒ allow (fail-open).
  bashReadTargetOutsideRoot(input, config) {
    for (const raw of bashReadTargets(input.command)) {
      // Expand a leading `~` to $HOME (input.home, threaded by the shim) so a sanctioned
      // scratch path referenced as `~/…` reaches the sanctioned check. Without a home a
      // `~`-target is unresolvable ⇒ deny (the original fail-closed behaviour).
      const t = (raw.startsWith("~") && input.home) ? raw.replace(/^~/, input.home) : raw;
      if (t.startsWith("~")) return true;
      const r = resolveTarget(input.root, t);
      if (!r) continue;
      if (writesIntoAnyNever(input.root, r, config?.orchestrator_writable)) return true;
      if (!(isInsideAnyComponent(input.root, r) || isInsideSanctioned(input.sanctionedScratch, r))) return true;
    }
    return false;
  },
  // Write: tooling writes are owned by the dedicated self-patch deny (writesIntoTooling,
  // per-root), which reports `self-patch-enforcer` — so this boundary predicate stays a
  // pure in-set membership test; a sibling's tooling write falls through here (sibling IS
  // in the set) and is caught by writesIntoTooling with the meaningful ruleId.
  // The sole write-side carve-out: input.sanctionedScratchWritable (the agent's OWN
  // per-session temp root ONLY — never the tool-result overflow store, which stays
  // write-denied via this same predicate). Fail-closed: absent/empty ⇒ this check never
  // admits anything ⇒ byte-identical to before the carve-out.
  writeTargetOutsideRoot(input) {
    return writeTargetsOf(input).some((t) => {
      const r = resolveTarget(input.root, t);
      return !!r && !isInsideAnyComponent(input.root, r) && !isInsideSanctioned(input.sanctionedScratchWritable, r);
    });
  },
  emptyWriteOverNonEmpty(input) {
    if (input.act !== "write" || !input.contentEmpty) return false;
    const r = resolveTarget(input.root, input.path);
    return !!r && fileNonEmpty(r);
  },
  orchestratorWritingContent(input, config) {
    if (!input.isOrchestrator) return false;
    if (input.act === "bash" && isPureGitCommand(input.command)) return false;
    // Configurable rigor: a lite/solo profile lets the orchestrator build directly,
    // so it MAY author source/doc paths — relax THIS predicate only. The tooling
    // (self-patch), boundary, truncation, merge-gate, and stamp-write denies are
    // SEPARATE predicates and untouched, so the floor never relaxes (`## Project config`).
    const profile = projectProfile(input.root);
    if (profile === "lite" || profile === "solo" || profile === "yolo") return false;
    const ow = config.orchestrator_writable;
    return writeTargetsOf(input).some((t) => {
      const r = resolveTarget(input.root, t);
      return !!r && isInsideRoot(input.root, r) && !isOrchestratorAuthorable(input.root, r, ow);
    });
  },
  // The stamp-fabrication guard: a review stamp is the Reviewer's deliverable —
  // the orchestrator never authors one, in ANY profile (the Reviewer seat never
  // collapses into the orchestrator, so this floor ignores the rigor relaxation).
  // Actor-resolved platforms deny; an undefined actor (Claude) fails open and
  // the guard is persona there. Pure git stays allowed (restore, not authorship).
  orchestratorWritesReviewStamp(input, config) {
    if (!input.isOrchestrator) return false;
    if (input.act === "bash" && isPureGitCommand(input.command)) return false;
    const prefix = config.review_stamps?.prefix;
    if (!prefix) return false;
    return writeTargetsOf(input).some((t) => {
      const r = resolveTarget(input.root, t);
      if (!r || !isInsideRoot(input.root, r)) return false;
      return relMatches(path.relative(path.resolve(input.root), r), prefix);
    });
  },
  // Self-patch deny — the `.ai-dev/tooling/` carve-out, PER-ROOT and unconditional:
  // a write into ANY validated component root's tooling dir is denied, not just the
  // session root's, so a manifest can never widen into a sibling's enforcer source
  // (invariant 2). The boundary deny (writeTargetOutsideRoot) already catches a
  // sibling-tooling write via the isInsideAnyComponent carve-out; this keeps the
  // dedicated self-patch deny consistent with it for defense in depth.
  writesIntoTooling(input, config) {
    const ow = config.orchestrator_writable;
    return writeTargetsOf(input).some((t) => {
      const r = resolveTarget(input.root, t);
      return !!r && writesIntoAnyNever(input.root, r, ow);
    });
  },
  mergeWithUnstampedReview(input) {
    // Scope to the SESSION repo (fail-CLOSED): an `add -A`/`push main` in a positively-
    // different nested repo (targetsSessionRepo===false) targets that repo's own db +
    // origin and can never move the protocol's main, so it is exempt; undefined (no
    // signal) or true ⇒ the merge-gate applies (the strict, unchanged behaviour).
    if (input.targetsSessionRepo === false) return false;
    // `merge(?![-\w])` lets read-only `git merge-base`/`merge-tree`/`merge-file`/`mergetool`
    // fall through (a hyphen/word-char after `merge` is plumbing, not a merge) while a real
    // `git merge <topic>` (whitespace/EOL after `merge`) still matches. `push\b` unchanged.
    // Normalize first so a `git -C <path> (push|merge)` global-flag span doesn't hide the
    // subcommand from this adjacency test — the helpers (resolveMergeTopic etc.) normalize
    // idempotently on their own, but they never run if this guard bails on the raw command.
    const cmd = normalizeGitInvocation(input.command || "");
    if (!/git\s+(merge(?![-\w])|push\b)/.test(cmd)) return false;
    if (projectProfile(input.root) === "yolo") return false; // gate explicitly off — Operator's merge word is the only remaining check
    if (isTagPush(input.command)) return false; // tags never need a review stamp
    if (isTrunkFastForward(input.command, input.root)) return false; // a trunk --ff-only sync to its upstream (the post-squash-merge `git pull`) is not a feature merge — see engine-git
    // Force-push operations are handled by the force-push guard, not the merge-gate.
    // This allows the force-push guard to apply its scoped logic (lease+non-trunk allowed,
    // bare force asks) without the merge-gate denying first.
    const flagMatch = /git\s+push(?:\s+[^\s]+)*\s+(--force-with-lease|--force|-f)(?:[ =]|$)/.exec(cmd);
    if (flagMatch) return false; // force-push guard handles it
    // F1: an EXPLICIT unstamped trunk push (`git push origin main`/`master`) DENIES on
    // both platforms — the bare `main` ref is unresolvable as a topic, so without this
    // it fell through to the ask rule, which a no-ask-return platform (OpenCode) silently
    // passed. Deny here (deny holds on both platforms; ask does not). The trunk ref IS
    // the stamp topic, so a reviewed `main`-named change (carrying main_review.md) still
    // ships; only the unstamped trunk push denies. Sits before the resolveMergeTopic null
    // check so it is never shadowed by the unresolvable-topic ask.
    const trunk = pushExplicitTrunkRef(input.command);
    if (trunk) return !reviewStampSatisfied(input.root, trunk);
    const topic = resolveMergeTopic(input.command, input.root);
    if (!topic) return false; // unresolved topic ⇒ the sibling ask rule (mergeTopicUnresolvable), never a silent pass
    return !reviewStampSatisfied(input.root, topic);
  },
  // The merge-gate's no-silent-pass companion: a merge/push whose topic cannot be
  // resolved (detached HEAD and no branch ref in the command) leaves the stamp
  // uncheckable — escalate to the Operator instead of passing.
  mergeTopicUnresolvable(input) {
    // Session-repo scope, consistent with its merge-gate sibling: a push/merge in a
    // positively-different nested repo is not the protocol's to gate, so it raises no
    // Operator ask; undefined/true ⇒ unchanged. (Fail-CLOSED — see session-root.mjs.)
    if (input.targetsSessionRepo === false) return false;
    // Same `merge(?![-\w])` tightening as mergeWithUnstampedReview — a `merge-*` plumbing
    // command must not be routed to the unresolvable-topic ask either.
    const cmd = normalizeGitInvocation(input.command || ""); // strip `git -C <path>` global span — see mergeWithUnstampedReview
    if (!/git\s+(merge(?![-\w])|push\b)/.test(cmd)) return false;
    if (isTagPush(input.command)) return false; // tags are fine — no topic, no ask
    if (isTrunkFastForward(input.command, input.root)) return false; // a trunk --ff-only sync is not a feature merge — no ask
    // Force-push operations are handled by the force-push guard, not the merge-gate.
    const flagMatch = /git\s+push(?:\s+[^\s]+)*\s+(--force-with-lease|--force|-f)(?:[ =]|$)/.exec(cmd);
    if (flagMatch) return false; // force-push guard handles it
    // An explicit trunk push is handled by the DENY rule (mergeWithUnstampedReview),
    // never routed to ask — deny outranks ask regardless, this keeps the intent clean.
    if (pushExplicitTrunkRef(input.command)) return false;
    return resolveMergeTopic(input.command, input.root) === null;
  },
  spawnTargetInDenySet(input, config) {
    const set = [
      ...(config.role_deny_set?.role_duplicators || []),
      ...(config.role_deny_set?.generic_builtins || []),
    ];
    return typeof input.spawnTarget === "string" && set.includes(input.spawnTarget);
  },
  sshContentEdit(input) {
    const c = input.command || "";
    if (!/(^|[\s;&|`(])ssh(\s|$)/.test(c)) return false;
    // An in-place editor / tee always intends a real-file edit → deny.
    if (/(sed[\s"'`]+-i|[\s"'`]vi[\s"'`]|[\s"'`]vim[\s"'`]|[\s"'`]nano[\s"'`]|[\s"'`]tee[\s"'`])/.test(c)) return true;
    // A `>` / `>>` redirect denies ONLY when its target is a real file. A stream
    // redirect (`2>/dev/null`, `> /dev/null`, `2>&1`) is read-only and must ALLOW —
    // it is a diagnostic, not a remote edit. Scan every redirect target and fire on
    // the first non-stream one (isStreamTarget is the single home for "what is a
    // stream"; an `&N` fd-dup never matches the target group, so it never trips).
    const redir = /\d?>>?\s*("[^"]*"|'[^']*'|[^\s&|;<>()]+)/g;
    let m;
    while ((m = redir.exec(c)) !== null) {
      const tok = m[1].replace(/^["']|["']$/g, "");
      if (tok && !isStreamTarget(tok)) return true;
    }
    return false;
  },
  sshMutatingAction(input) {
    const c = input.command || "";
    return /(^|[\s;&|`(])ssh(\s|$)/.test(c) &&
      /([\s"'`]systemctl[\s"'`]+(restart|reload|stop|start|enable|disable)|[\s"'`]docker[\s"'`]+(exec|compose[\s"'`]+(up|down|run|restart|exec))|[\s"'`]apt(-get)?[\s"'`]+(install|upgrade|remove|purge|autoremove)|[\s"'`]npm[\s"'`]+(install|update|uninstall)|[\s"'`]kubectl[\s"'`]+(edit|apply|patch|delete|create|replace)|[\s"'`]rm[\s"'`]|[\s"'`]cp[\s"'`]|[\s"'`]mv[\s"'`]|[\s"'`]mkdir[\s"'`]|[\s"'`]touch[\s"'`])/.test(c);
  },
  gitForcePush(input) {
    const cmd = input.command || "";
    // Extract the force flag from the FIRST push invocation (compound commands: multiple pushes possible)
    // Use the same pattern as pushExplicitTrunkRef: match up to the next shell separator
    const pushInvMatch = /\bgit\s+push\b([^;&|\n]*)/.exec(cmd);
    if (!pushInvMatch) return false;

    const firstPushCmd = "git push " + pushInvMatch[1];
    // Extract the force flag from the first push invocation
    const flagMatch = /git\s+push(?:\s+[^\s]+)*\s+(--force-with-lease|--force|-f)(?:[ =]|$)/.exec(firstPushCmd);
    const flag = flagMatch ? flagMatch[1] : null;

    if (flag === "--force-with-lease") {
      // Lease protection is active — check if target is trunk
      const trunk = pushExplicitTrunkRef(firstPushCmd);
      return !!trunk; // Ask only for trunk; allow non-trunk lease pushes
    }

    // Bare --force/-f has no protection — always ask
    return flag === "--force" || flag === "-f";
  },
  gitCommitNoVerify(input) {
    return /git\s+commit(\s+[^\s]+)*\s+(--no-verify|--no-gpg-sign)([ =]|$)/.test(input.command || "");
  },
  // F4a: `git add -A` / `git add .` / `git add --all` / `git add *` — a blind bulk-stage.
  // The orchestrator rule (`## Your seat`: "Stage named paths only — never git add -A/.")
  // makes this never legitimate: the tree holds untracked transients (plans, stamps) by
  // design and a blind stage leaks them into durable history. DENY on both platforms
  // (Operator decision — deny holds where ask degrades to persona on OpenCode; the
  // day-zero bootstrap stages NAMED paths instead). Runs on the quote-masked command so a
  // commit-message mention never trips it. Whole-token matching: `git add .gitignore` (a
  // file literally named `.gitignore`) and `git add -p` (interactive patch) are NOT bulk
  // stages and fall through.
  gitAddAll(input) {
    // Scope to the SESSION repo: a `git add -A` in a POSITIVELY-different nested repo
    // (targetsSessionRepo===false) stages only that repo's own tree into its own git db,
    // never the protocol's transients — so it is exempt. Undefined (no signal) or true ⇒
    // the deny applies (fail-CLOSED — session-root.mjs computes the signal).
    if (input.targetsSessionRepo === false) return false;
    const masked = maskQuotedSpans(input.command || "");
    // Each `git add` invocation's own argument span (to the next shell separator).
    const inv = /\bgit\s+add\b([^;&|\n]*)/g;
    let m;
    while ((m = inv.exec(masked)) !== null) {
      const toks = m[1].split(/\s+/).filter((t) => t.length > 0);
      for (const tok of toks) {
        if (tok === "-A" || tok === "--all" || tok === "." || tok === "*" || tok === "-all") return true;
        // A combined short-flag bundle containing A (e.g. `-Av`) is also a bulk stage.
        if (/^-[A-Za-z]*A[A-Za-z]*$/.test(tok)) return true;
      }
    }
    return false;
  },
  // F4b: a `git commit` whose checkout HEAD is `main`/`master` and which carries no
  // satisfied trunk stamp. "Never commit to main" is absolute (PROTOCOL.md `## Git flow`):
  // main moves via PR squash-merge, never a direct commit — so this DENIES on both
  // platforms (Operator decision; deny holds where ask degrades on OpenCode). Two
  // carve-outs preserve the only legitimate cases:
  //   • a STAMPED trunk change (main_review.md present) still commits (symmetric with the
  //     trunk-push allow) — a reviewed change branched/named main is honoured;
  //   • the day-zero bootstrap: an UNCONFIGURED project (no .ai-dev/config.json) OR a
  //     fresh-init repo with no commit history yet (Setup step 0: `git init` + the
  //     initial commit). A configured project with history committing to main is a
  //     violation and denies.
  // yolo turns the gate off (consistency with the merge-gate). Runs on the masked command.
  commitOnUnstampedMain(input) {
    // Scope to the SESSION repo (fail-CLOSED, like gitAddAll): a commit in a positively-
    // different nested repo lands in that repo's own db on its own main — it cannot move
    // the protocol's main — so it is exempt; undefined/true ⇒ the deny applies.
    if (input.targetsSessionRepo === false) return false;
    if (!/\bgit\s+commit\b/.test(maskQuotedSpans(input.command || ""))) return false;
    if (projectProfile(input.root) === "yolo") return false; // gate explicitly off
    const branch = headBranch(input.root);
    if (!branch || !TRUNK_BRANCHES.has(branch)) return false; // not on a trunk checkout
    // Bootstrap carve-out: an unconfigured project, or a fresh-init repo with no commits.
    if (!projectConfigured(input.root)) return false;
    if (!repoHasCommits(input.root)) return false;
    // A reviewed trunk-named change still commits (the stamp carve-out).
    return !reviewStampSatisfied(input.root, branch);
  },
  promptMatchesChangeVerb(input, config) {
    return safeTest(config.change_verbs?.pattern, input.prompt || "");
  },
  // The always-on language-mirror nudge: fires on EVERY submitted prompt (the act
  // filter in evaluate already gates this to prompt acts, so an unconditional true
  // is correct). Reinforces invariant 5 per turn — the constitution/code/PR context
  // in the turn is English, which without a per-turn prop drags the reply into
  // English. Compiles NO config pattern (it names no language), so it never touches
  // safeTest and carries no backtracking/injection surface. Aggregated with any
  // conditional inject that co-fires (see evaluate's collect-and-join).
  promptMirrorLanguage() {
    return true;
  },
  // Lazy-setup nudge: a work-request prompt (same change_verbs list — no second
  // verb list) to a project with NO .ai-dev/config.json. Reinforces the persona
  // act, never forces it. False once the config is present (a configured project
  // gets the change-route-reminder instead).
  promptNeedsSetup(input, config) {
    if (!safeTest(config.change_verbs?.pattern, input.prompt || "")) return false;
    return !projectConfigured(input.root);
  },
  // Lazy product-discovery nudge: a work-request prompt (same change_verbs list)
  // to a CONFIGURED project whose docs/product.md is absent OR still the unfilled
  // install template (productBriefFilled). Ordered after promptNeedsSetup (an
  // UNconfigured project gets the setup nudge first) and before
  // change-route-reminder (a configured project WITH a filled brief gets the
  // route reminder). Reinforces the persona act, never forces it.
  promptNeedsProductBrief(input, config) {
    if (!safeTest(config.change_verbs?.pattern, input.prompt || "")) return false;
    return projectConfigured(input.root) && !productBriefFilled(input.root);
  },
};

// ── diagnostics map — keyed by predicate name ────────────────────────────────
// When a deny or ask rule hits and a diagnostic function exists for its predicate,
// the diagnostic is appended to rule.intent to give the actor a remediation path.
const DIAGNOSTICS = {
  mergeWithUnstampedReview(input) {
    // Merge-gate diagnostic: name the topic, the expected stamp file, what's missing,
    // and the remediation (re-spawn the Reviewer).
    const topic = resolveMergeTopic(input.command, input.root) ||
                  pushExplicitTrunkRef(input.command);
    if (!topic) return "";
    const diagnosis = reviewStampDiagnosis(input.root, topic);
    const parts = [];
    parts.push(`Expected stamp: ${diagnosis.expectedFile}`);
    if (!diagnosis.fileExists) {
      parts.push(`Stamp file absent. Stamps in .ai-dev/reviews/: ${diagnosis.siblingStamps.join(", ") || "(none)"}`);
    } else {
      if (!diagnosis.anchors.verdict?.ok) {
        parts.push(`Code review/Doc review: ${diagnosis.anchors.verdict?.reason || "unknown issue"}`);
      }
      if (!diagnosis.anchors.contracts?.ok) {
        parts.push(`Contracts: ${diagnosis.anchors.contracts?.reason || "unknown issue"}`);
      }
    }
    // Build "Required form" by interpolating actual label and separator constants (single home, never hand-typed second copy)
    const verdictLabels = STAMP_LABELS.verdict;  // both possible verdict labels
    const contractsLabel = STAMP_LABELS.contracts;
    const canonicalSep = STAMP_SEPARATORS[0];  // canonical separator: first in list
    // Build examples with all verdict labels and all separators mentioned
    let requiredFormMsg = `Required form: ## ${verdictLabels[0]}${canonicalSep} APPROVED (or ## ${verdictLabels[1]}${canonicalSep} APPROVED); ## ${contractsLabel}${canonicalSep} <value>`;
    if (STAMP_SEPARATORS.length > 1) {
      requiredFormMsg += ` (separators accepted: ${STAMP_SEPARATORS.map(s => JSON.stringify(s)).join(", ")}; no separator accepted when value starts with uppercase token or literal "none")`;
    }
    parts.push(requiredFormMsg);
    parts.push(`Remediation: Re-spawn the Reviewer to regenerate the stamp. Do NOT author or edit the stamp yourself.`);
    return "\n" + parts.join("\n");
  },
  mergeTopicUnresolvable() {
    // Unresolvable-topic diagnostic: explain the issue and the path to resolve it.
    return "\nUnable to determine which branch is being pushed (detached HEAD and no branch ref in command). " +
           "Name the branch explicitly in the command (e.g. `git push origin feature/topic`) so the gate can check the review stamp.";
  }
};

// ── config + evaluate ────────────────────────────────────────────────────────
export function loadConfig(dir) {
  const base = dir || path.dirname(fileURLToPath(import.meta.url));
  return JSON.parse(fs.readFileSync(path.join(base, "deny-rules.json"), "utf8"));
}

// Evaluate one neutral input against the registry. Returns the first DENY hit
// (deny outranks ask), else — for a prompt — every matching INJECT's reason joined
// into one note, else the first ASK hit, else allow. `ruleId`/`reason` identify
// what fired. Inject is COLLECT-AND-JOIN (not return-first): an always-on nudge
// (language-mirror) must not suppress, nor be suppressed by, a conditional one
// (setup / discovery / route) on the turns those co-fire — they aggregate instead.
// Inject and ask never co-occur (inject rules are act:"prompt", ask rules act:"bash"),
// so the effective precedence is unchanged: deny > inject > ask > allow.
export function evaluate(input, config) {
  // Prepare the command string ONCE for every rule: non-shell heredoc bodies are
  // data, not commands — stripped here so no predicate pattern-matches prose.
  if (input.act === "bash" && typeof input.command === "string") {
    const prepared = stripHeredocBodies(input.command);
    if (prepared !== input.command) input = { ...input, command: prepared };
  }
  // Consciously-disabled guards (.ai-dev/config.json `safeguards`), read ONCE.
  // The skip below is gated on `rule.toggleable === true` too, so a deny/merge-gate
  // rule (no such flag) is never skipped — the mechanical floor holds regardless.
  const disabled = disabledSafeguards(input.root);
  // hookMode: "strict" (default, fail-safe) = ask-class rules ask the Operator;
  // "light" = ask-class rules become deny + an informative reason (no interruption).
  const hookMode = projectHookMode(input.root);
  const lightMode = hookMode === "light";
  let ask = null;
  let injectId = null; // the FIRST matched inject (registry order) — the leading ruleId
  const injectReasons = [];
  for (const rule of config.rules) {
    if (!rule.act.split("|").includes(input.act)) continue;
    if (rule.toggleable === true && disabled.has(rule.id)) continue; // opted-out guard
    const pred = PREDICATES[rule.predicate];
    if (!pred || !pred(input, config)) continue;
    if (rule.class === "deny") {
      let reason = rule.intent;
      // Append diagnostic if one exists for this predicate.
      const diag = DIAGNOSTICS[rule.predicate];
      if (diag) {
        try {
          const diagnostic = diag(input, config);
          if (diagnostic) reason = reason + diagnostic;
        } catch {
          // Diagnostic error; fall back to intent alone
        }
      }
      return { verdict: "deny", ruleId: rule.id, reason };
    }
    if (rule.class === "ask") {
      // In light mode, an ask-class match returns deny immediately (same precedence
      // as deny) with an informative reason naming what's denied + the safe path.
      if (lightMode) {
        let reason = rule.intent;
        // Append diagnostic for ask-class rules too.
        const diag = DIAGNOSTICS[rule.predicate];
        if (diag) {
          try {
            const diagnostic = diag(input, config);
            if (diagnostic) reason = reason + diagnostic;
          } catch {
            // Diagnostic error; fall back to intent alone
          }
        }
        const alt = rule.lightAlternative || "";
        const msg = alt
          ? `[hookMode: light] ${reason} Safe path: ${alt}`
          : `[hookMode: light] ${reason}`;
        return { verdict: "deny", ruleId: rule.id, reason: msg };
      }
      if (!ask) {
        let reason = rule.intent;
        // Append diagnostic for ask-class rules.
        const diag = DIAGNOSTICS[rule.predicate];
        if (diag) {
          try {
            const diagnostic = diag(input, config);
            if (diagnostic) reason = reason + diagnostic;
          } catch {
            // Diagnostic error; fall back to intent alone
          }
        }
        ask = { verdict: "ask", ruleId: rule.id, reason };
      }
    }
    if (rule.class === "inject") {
      if (injectId === null) injectId = rule.id; // leading inject keeps its identity
      injectReasons.push(rule.intent);
    }
  }
  // Inject (collected) outranks ask, exactly as the prior return-first did — every
  // matching inject's reason joined into one note (blank-line separated; the Claude
  // shim renders it via additionalContext, OpenCode is persona).
  if (injectReasons.length) return { verdict: "inject", ruleId: injectId, reason: injectReasons.join("\n\n") };
  return ask || { verdict: "allow", ruleId: null, reason: "" };
}

export const _internals = { bashWriteTargets, bashReadTargets, isOrchestratorAuthorable, resolveMergeTopic, reviewStampSatisfied, reviewStampDiagnosis, STAMP_SEPARATORS, STAMP_LABELS, stripHeredocBodies, projectProfile, projectHookMode, disabledSafeguards, safeguardRegistry, componentRoots, pushExplicitTrunkRef, PREDICATES };
