// The whole-set quality runner — the one command that runs EVERY tool for a
// beat, so "run the build-beat checks" is a single reliable act instead of an
// agent eyeballing tools.json and invoking rows by hand (PROTOCOL.md
// `## Quality tools`). It is project-AGNOSTIC: it carries no tool name itself,
// only the mechanism — it reads whatever rows the project's tools.json defines.
//
// Honesty: this makes "run the whole set" reliable; it does NOT make "the agent
// invoked it" mechanical. That invocation stays a persona act — a runner cannot
// force a positive act (protocol-reference.md `## Enforcement`).
//
// Usage: node src/quality/run.mjs <build|review|ship> [--touched [<base>]]
//
// Scope mode (--touched): a row with a `covers` array (path globs
// project-root-relative) runs only if a touched file matches at least one glob;
// a row WITHOUT `covers` always runs (fail-safe — an undeclared-scope tool is
// never silently dropped). No --touched flag = current full-run behaviour
// (this stays the ship-gate invocation). The touched set is the UNION of the
// committed diff since the merge-base of <base> and HEAD, and the working tree
// (staged + unstaged + untracked) — see computeTouchedFiles(); a Builder's
// uncommitted handback is exactly what the pre-handback invocation has to look
// at. The CLI prints the base it resolved.
//
// Exit codes: 0 = every matched row passed, was PARTIAL or SKIPPED (or no rows
//                 matched, or no registry);
//             non-zero = a row failed, a bad beat arg, or a malformed registry.
//
// Row verdicts: a row's exit 0 is PASS; exit SKIP_EXIT (77, the automake
// convention) is SKIP — the row could not run in this environment (its tool is
// not installed) and says so on its own output line; any other status is FAIL.
// A skip is printed `SKIP`, counted apart from the passes and named in the
// summary, and does not turn the beat red — but it is never reported as a pass
// (docs/agent-rules/quality-gate-rigor.md: "a skip is reported as a skip").
// Before this, a row's only way to skip was exit 0, which printed PASS (audit
// 2026-09-24 L1). Which rows skip where: .ai-dev/notes/quality-gate-environment.md.
//
// PARTIAL — a row that ran but skipped PART of its checks here. Every row gets a
// fresh, empty file named by QUALITY_SKIP_LOG in its environment; a harness that
// skips a section appends one line per skipped check (shell harnesses source
// .ai-dev/quality/checks/lib_skip.sh). Exit 0 with a non-empty log is PARTIAL:
// printed with what was skipped, counted apart from the passes, named in the
// summary, beat green (1.0.6.62 review A3). Exit codes stay as they are — a
// gate's status is also read directly (comment-mutation-proof), where a new
// non-zero "green" would read as RED. A harness that has not opted in still
// reads PASS on a partial run; which ones have: the note above. A row that
// itself runs other gates unsets QUALITY_SKIP_LOG for them, or their skips
// would land in its own log.
//
// Strict mode — QUALITY_STRICT set to anything but ""/0/false/no/off (see
// isStrict()): a SKIP and a PARTIAL are FAILs. CI sets it (web-quality.yml),
// because there every row's tool is installed on purpose and a skip means a
// broken install step; locally it is absent and a skip stays a skip.
//
// Security: the executed commands come ONLY from the tracked, reviewed
// tools.json (first-party, changes through git). The <beat> arg is used SOLELY
// as an equality filter (row.beat === beat) and is NEVER interpolated into a
// command — a hostile arg cannot reach a shell.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync, execSync } from "node:child_process";

const BEATS = ["build", "review", "ship"];

// The exit status a row returns for a documented environment skip (above).
export const SKIP_EXIT = 77;

// Strict mode from an environment object (header). An unrecognised non-empty
// value is strict: a typo in CI must not quietly turn the floor off.
export function isStrict(env) {
  const v = String((env && env.QUALITY_STRICT) ?? "").trim().toLowerCase();
  return v !== "" && !["0", "false", "no", "off"].includes(v);
}

// Resolve the tools.json path: prefer `<root>/src/quality/tools.json` (the
// project's real tools), fall back to the co-located file beside this runner
// (the template the installer ships). Exported for the self-test to verify
// the resolution chain without executing commands.
export function resolveRegistry(root, registryPath) {
  if (registryPath) return registryPath;
  const beside = path.join(path.dirname(fileURLToPath(import.meta.url)), "tools.json");
  const projectTools = path.join(root, "src", "quality", "tools.json");
  return fs.existsSync(projectTools) ? projectTools : beside;
}

// Convert a project-root-relative `covers` pattern to a regex for matching.
// tools.json documents the field as "path prefixes/globs", so both forms are
// supported: globs with ** (any depth) and * (within one path segment), and
// bare path prefixes. A malformed pattern returns null — the caller fails safe
// to always-run.
//
// A pattern carrying no glob metacharacter is a PREFIX, not an exact path. It
// used to compile literally, so `"etc/"` became /^etc\/$/ — a regex matching
// the directory string itself and no file under it. Every bare-prefix row (18
// entries across the registry, e.g. iface-naming-contract and
// kernel-policy-contract on `"etc/"`) therefore skipped under `--touched` no
// matter what the diff touched: a false green, since a skipped row still
// prints PASS. The boundary is a `/`, so `"install.sh"` still matches only
// itself and never `install.sh.bak`, and an exact file path keeps working.
// Direction of the change is fail-safe: it can only make MORE rows run.
export function coversToRegex(pattern) {
  try {
    if (!pattern.includes("*") && !pattern.includes("?")) {
      const trimmed = pattern.replace(/\/+$/, "");
      const literal = trimmed.replace(/[\\.[\]{}()+^$|]/g, "\\$&");
      return new RegExp("^" + literal + "(/.*)?$");
    }
    let escaped = "";
    let i = 0;
    while (i < pattern.length) {
      if (pattern[i] === "*" && pattern[i + 1] === "*") {
        // ** : match any characters including /
        if (pattern[i + 2] === "/") {
          escaped += "(.*/)?";
          i += 3;
        } else if (i + 2 === pattern.length) {
          escaped += ".*";
          i += 2;
        } else {
          // ** followed by non-/ — treat as **, then the next char normally
          escaped += ".*";
          i += 2;
        }
      } else if (pattern[i] === "*") {
        escaped += "[^/]*";
        i++;
      } else if (pattern[i] === "?") {
        escaped += "[^/]";
        i++;
      } else {
        // Escape regex-special characters
        const ch = pattern[i];
        if ("\\.[]{}()+^$|".includes(ch)) {
          escaped += "\\" + ch;
        } else {
          escaped += ch;
        }
        i++;
      }
    }
    return new RegExp("^" + escaped + "$");
  } catch {
    return null;
  }
}

// True when at least one pattern in `covers` matches any file in `files`
// (project-root-relative). `files` may be a single string or an array of strings.
export function fileMatchesCovers(files, covers) {
  const paths = Array.isArray(files) ? files : [files];
  for (const pattern of covers) {
    if (typeof pattern !== "string" || !pattern) continue;
    const re = coversToRegex(pattern);
    if (!re) continue; // malformed pattern → skip, tried by next pattern
    if (paths.some((f) => re.test(f))) return true;
  }
  return false;
}

function gitOut(root, args) {
  return execFileSync("git", args, { cwd: root, stdio: "pipe", encoding: "utf8" });
}

// The committed half of the touched set: `$(git merge-base <ref> HEAD)..HEAD` —
// what THIS branch committed since it left <ref>, i.e. git's `<ref>...HEAD`.
// Candidates, first with a non-empty diff wins: the explicit base, then
// origin/main, then main. It used to diff two-dot `<ref>..HEAD`, which compares
// the two trees: once <ref> moved on, everything <ref> changed since the fork
// counted as touched (81 files / 68 rows against the branch's true 6 files —
// 1.0.6.62 review A9). The old merge-base fallback also went through a shell
// with `2>/dev/null ||`, which cmd.exe cannot run, so on Windows it never
// resolved; git is called argv-style here, no shell.
//
// `-z` for the same reason workingTreeFiles() carries it (below): without it
// git QUOTES any path holding a non-ASCII byte or a control character
// (`"www/\320\260.js"`), and a quoted path matches no `covers` glob — the row
// guarding that file is skipped while the run still prints PASS. The fix was
// applied to the working-tree half only when the union landed; this is the
// committed half of it (review Q3, 1.0.6.24). Latent in this repo today (no
// tracked path carries such a byte) and fail-OPEN when it bites, which is
// exactly why it is not left to be found later.
function diffNamesZ(root, range) {
  return gitOut(root, ["diff", "--name-only", "-z", range]).split("\0").filter(Boolean);
}

function mergeBaseOf(root, ref) {
  try {
    return gitOut(root, ["merge-base", ref, "HEAD"]).trim() || null;
  } catch {
    return null; // ref missing, or no common history
  }
}

// Returns { files, ref, mergeBase }: the files, and the candidate ref + its
// merge-base that produced them. When no candidate yields a file, ref/mergeBase
// name the first one that RESOLVED (null when none did), so the caller can say
// which base came back empty.
export function committedTouchedDiff(root, base) {
  // A ref starting with "-" would be read by git as an option.
  const candidates = [...new Set([base, "origin/main", "main"])].filter((r) => r && !r.startsWith("-"));
  let first = null;
  for (const ref of candidates) {
    const mergeBase = mergeBaseOf(root, ref);
    if (!mergeBase) continue;
    if (!first) first = { ref, mergeBase };
    let files;
    try {
      files = diffNamesZ(root, `${mergeBase}..HEAD`);
    } catch {
      continue; // diff failed — try the next candidate
    }
    if (files.length) return { files, ref, mergeBase };
  }
  return { files: [], ref: first ? first.ref : null, mergeBase: first ? first.mergeBase : null };
}

export function committedTouchedFiles(root, base) {
  return committedTouchedDiff(root, base).files;
}

// The working-tree half: staged + unstaged + untracked, from
// `git status --porcelain -z -uall`.
//
// `-z` (NUL-separated, never quoted) rather than `--short`: the human format
// quotes any path holding a space, a quote or a non-ASCII byte
// (`"www/\320\260.js"`), so a naive slice(3) hands the runner a path that
// matches no `covers` glob — a silent skip, the exact failure mode this
// function exists to prevent. A rename/copy entry carries TWO NUL fields
// ("XY new\0old\0"); both are recorded, since a rename touches the coverage
// of the old path as well as the new.
export function workingTreeFiles(root) {
  let out;
  try {
    out = execSync("git status --porcelain -z -uall", { cwd: root, stdio: "pipe", encoding: "utf8" });
  } catch {
    return [];
  }

  const fields = out.split("\0");
  const files = [];
  for (let i = 0; i < fields.length; i++) {
    const entry = fields[i];
    if (entry.length < 4) continue; // "XY p" is the shortest valid entry
    const status = entry.slice(0, 2);
    const p = entry.slice(3);
    if (p) files.push(p);
    // Rename/copy: the ORIGINAL path is the next NUL-separated field, not part
    // of this entry. Consume it here or the loop would read it as an entry and
    // strip its first three characters.
    if (status.includes("R") || status.includes("C")) {
      const orig = fields[++i];
      if (orig) files.push(orig);
    }
  }
  return files;
}

// Compute the touched-file set from git: the UNION of the committed diff
// (since the merge-base — committedTouchedDiff()) and the working tree (staged +
// unstaged + untracked). Returns an array of project-root-relative paths, or
// `null` when no git / no file resolves — caller degrades to full run. An
// optional `meta` object receives { ref, mergeBase, committedCount } of the
// committed half.
//
// The union is the point. The working tree used to be a LAST RESORT, reached
// only when the committed diff came back empty; on any branch carrying a
// commit, a Builder's own uncommitted handback therefore selected no rows and
// `run.mjs build --touched` — the documented pre-handback command — reported
// green having never looked at the new work (found 2026-08-28, hollow-ratchet
// audit). Both halves are real edits a beat must check, so both are read every
// time; the direction is fail-safe (it can only make MORE rows run).
export function computeTouchedFiles(root, base, meta) {
  try {
    const topLevel = execSync("git rev-parse --show-toplevel", { cwd: root, stdio: "pipe", encoding: "utf8" }).trim();
    if (!topLevel) return null;
  } catch {
    return null; // no git at all
  }

  const committed = committedTouchedDiff(root, base);
  if (meta && typeof meta === "object") {
    meta.ref = committed.ref;
    meta.mergeBase = committed.mergeBase;
    meta.committedCount = committed.files.length;
  }
  const files = new Set();
  for (const f of committed.files) files.add(f);
  for (const f of workingTreeFiles(root)) files.add(f);

  if (files.size === 0) return null; // nothing resolved — degrade to full
  return [...files];
}

// Filter `rows` to only those whose `covers` field matches the touched set.
// A row WITHOUT `covers` always passes the filter (fail-safe to MORE rigor).
// Exported for testing scope filtering in isolation.
export function filterByScope(rows, touchedFiles) {
  if (!touchedFiles || touchedFiles.length === 0) return rows; // no touched set → full run

  return rows.filter((row) => {
    // Row with no covers, or covers is not a non-empty array → always run (fail-safe).
    if (!row.covers || !Array.isArray(row.covers) || row.covers.length === 0) {
      return true;
    }
    // Row with covers → runs only if at least one touched file matches a covers glob.
    try {
      return fileMatchesCovers(touchedFiles, row.covers);
    } catch {
      // A malformed glob throws? Fail safe: run the tool rather than skip it.
      return true;
    }
  });
}

// Run every tools.json row whose beat === `beat`, from `root` (the cwd the
// commands execute in). Returns an exit code (0 = all matched rows passed or
// skipped / none matched / no registry; non-zero = failure or malformed
// registry).
// `registryPath` overrides the default resolution (see resolveRegistry).
// `touchedFiles` (optional array of project-root-relative paths) enables
// scope mode — rows with a `covers` field run only if a touched file matches
// one of its globs; rows without `covers` always run (fail-safe).
// `opts.strict` (boolean) overrides the QUALITY_STRICT environment reading.
export function run(beat, root, registryPath, touchedFiles, opts) {
  if (!BEATS.includes(beat)) {
    console.error(`run.mjs: unknown beat "${beat}" — expected one of ${BEATS.join(" | ")}`);
    return 2;
  }

  const file = resolveRegistry(root, registryPath);

  // Absent registry ⇒ no-op success: a downstream may legitimately define no checks.
  if (!fs.existsSync(file)) {
    console.log(`run.mjs: no tools.json — no ${beat}-beat tools`);
    return 0;
  }

  // Present-but-malformed ⇒ fail closed: a broken registry must not pass as green.
  let registry;
  try {
    registry = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (e) {
    console.error(`run.mjs: malformed tools.json (${file}): ${e.message}`);
    return 1;
  }
  if (!registry || !Array.isArray(registry.tools)) {
    console.error(`run.mjs: malformed tools.json (${file}): missing "tools" array`);
    return 1;
  }

  let rows = registry.tools.filter((r) => r && r.beat === beat);
  if (rows.length === 0) {
    console.log(`run.mjs: no ${beat}-beat tools`);
    return 0;
  }

  // Scope filtering — runs before execution, so the PASS/FAIL summary is honest.
  const totalBefore = rows.length;
  rows = filterByScope(rows, touchedFiles);
  if (touchedFiles && touchedFiles.length > 0) {
    const notSelected = totalBefore - rows.length;
    if (notSelected > 0) console.log(`run.mjs: ${notSelected} tool(s) not selected (scope — not touched; not an environment SKIP)`);
  }

  const strict = opts && typeof opts.strict === "boolean" ? opts.strict : isStrict(process.env);
  if (strict) console.log("run.mjs: QUALITY_STRICT — a SKIP or a PARTIAL is a FAIL in this run");

  let failed = 0;
  const skipped = [];
  const partial = [];
  // One fresh skip log per row (header, PARTIAL). Forward slashes: the path is
  // handed to git-bash as well as read back here.
  const skipDir = fs.mkdtempSync(path.join(os.tmpdir(), "quality-skip-"));
  try {
    rows.forEach((row, i) => {
      if (typeof row.run !== "string" || !row.run) {
        console.error(`FAIL  ${row.id ?? "<no id>"} — row has no runnable "run" command`);
        failed++;
        return;
      }
      const skipLog = path.join(skipDir, `${i}.log`).replace(/\\/g, "/");
      let status = 0;
      try {
        execSync(row.run, { cwd: root, stdio: "inherit", env: { ...process.env, QUALITY_SKIP_LOG: skipLog } });
      } catch (e) {
        status = e && typeof e.status === "number" ? e.status : null; // null = killed by a signal
      }
      const skippedChecks = readSkipLog(skipLog);
      if (status === 0 && skippedChecks.length === 0) {
        console.log(`PASS  ${row.id}`);
      } else if (status === 0) {
        const what = summariseSkips(skippedChecks);
        if (strict) {
          console.log(`FAIL  ${row.id} — ${skippedChecks.length} check(s) skipped under QUALITY_STRICT: ${what}`);
          failed++;
        } else {
          console.log(`PARTIAL  ${row.id} — ${skippedChecks.length} check(s) skipped here, not verified: ${what}`);
          partial.push(`${row.id}: ${skippedChecks.length}`);
        }
      } else if (status === SKIP_EXIT) {
        if (strict) {
          console.log(`FAIL  ${row.id} — SKIP under QUALITY_STRICT (this environment is supposed to run it)`);
          failed++;
        } else {
          console.log(`SKIP  ${row.id}`);
          skipped.push(row.id);
        }
      } else {
        console.log(`FAIL  ${row.id}`);
        failed++;
      }
    });
  } finally {
    fs.rmSync(skipDir, { recursive: true, force: true });
  }

  const passed = rows.length - failed - skipped.length - partial.length;
  const partialNote = partial.length
    ? ` — ${partial.length} PARTIAL, passed with check(s) skipped here (${partial.join(", ")})`
    : "";
  const skipNote = skipped.length
    ? ` — ${skipped.length} SKIPPED, not verified here (${skipped.join(", ")})`
    : "";
  console.log(`\n${beat}: ${passed}/${rows.length} passed${partialNote}${skipNote}${failed ? ` — ${failed} FAILED` : ""}`);
  return failed ? 1 : 0;
}

// The non-empty lines a row appended to its QUALITY_SKIP_LOG ([] when none).
function readSkipLog(file) {
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return []; // the row never wrote it
  }
  return text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
}

function summariseSkips(lines) {
  const shown = lines.slice(0, 5).join("; ");
  return lines.length > 5 ? `${shown}; … (+${lines.length - 5} more)` : shown;
}

// Self-locate the project root from this script's path: src/quality/run.mjs ⇒
// root is two levels up (mirrors neutral-prose.test.mjs).
function projectRoot() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  return path.resolve(here, "..", "..");
}

// CLI entry — only when invoked directly, not when imported by the self-test.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const beat = process.argv[2];
  if (!beat) {
    console.error(`Usage: node src/quality/run.mjs <${BEATS.join("|")}> [--touched [<base>]]`);
    process.exit(2);
  }

  // Parse --touched flag and optional base.
  const touchedIdx = process.argv.indexOf("--touched");
  let touchedFiles = null;
  if (touchedIdx >= 0) {
    const base = process.argv[touchedIdx + 1] && !process.argv[touchedIdx + 1].startsWith("-")
      ? process.argv[touchedIdx + 1]
      : null;
    const meta = {};
    touchedFiles = computeTouchedFiles(projectRoot(), base, meta);
    if (touchedFiles === null) {
      console.log("run.mjs: --touched: no git / no base resolved — degrading to full run");
    } else if (meta.mergeBase) {
      console.log(`run.mjs: --touched: committed half = ${meta.mergeBase.slice(0, 12)}..HEAD (merge-base of ${meta.ref} and HEAD; ${meta.committedCount} file(s)) + working tree`);
    } else {
      console.log(`run.mjs: --touched: no base resolved (${base ? base + ", " : ""}origin/main, main) — working tree only`);
    }
  }

  process.exit(run(beat, projectRoot(), null, touchedFiles));
}
