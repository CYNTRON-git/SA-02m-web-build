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
// committed <base>..HEAD diff and the working tree (staged + unstaged +
// untracked) — see computeTouchedFiles(); a Builder's uncommitted handback is
// exactly what the pre-handback invocation has to look at.
//
// Exit codes: 0 = every matched row passed (or no rows matched, or no registry);
//             non-zero = a row failed, a bad beat arg, or a malformed registry.
//
// Security: the executed commands come ONLY from the tracked, reviewed
// tools.json (first-party, changes through git). The <beat> arg is used SOLELY
// as an equality filter (row.beat === beat) and is NEVER interpolated into a
// command — a hostile arg cannot reach a shell.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execSync } from "node:child_process";

const BEATS = ["build", "review", "ship"];

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

// The committed half of the touched set: `<base>..HEAD`, with the same base
// resolution the runner has always used (explicit base → origin/main →
// merge-base against origin/main or main). Returns [] when nothing resolves —
// the caller decides what an empty result means.
export function committedTouchedFiles(root, base) {
  const candidates = [];
  if (base) candidates.push(base);
  if (!base) candidates.push("origin/main");

  for (const ref of candidates) {
    try {
      const out = execSync(`git diff --name-only ${ref}..HEAD`, { cwd: root, stdio: "pipe", encoding: "utf8" }).trim();
      if (out) return out.split("\n").filter(Boolean);
    } catch {
      // ref missing or diff failed — try the next resolution step
    }
  }

  try {
    const mergeBase = execSync(
      `git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || echo ""`,
      { cwd: root, stdio: "pipe", encoding: "utf8", shell: true }
    ).trim();
    if (mergeBase) {
      const out = execSync(`git diff --name-only ${mergeBase}..HEAD`, { cwd: root, stdio: "pipe", encoding: "utf8" }).trim();
      if (out) return out.split("\n").filter(Boolean);
    }
  } catch {
    // no merge-base either
  }

  return [];
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
// (`<base>..HEAD`) and the working tree (staged + unstaged + untracked).
// Returns an array of project-root-relative paths, or `null` when no git / no
// file resolves — caller degrades to full run.
//
// The union is the point. The working tree used to be a LAST RESORT, reached
// only when the committed diff came back empty; on any branch carrying a
// commit, a Builder's own uncommitted handback therefore selected no rows and
// `run.mjs build --touched` — the documented pre-handback command — reported
// green having never looked at the new work (found 2026-08-28, hollow-ratchet
// audit). Both halves are real edits a beat must check, so both are read every
// time; the direction is fail-safe (it can only make MORE rows run).
export function computeTouchedFiles(root, base) {
  try {
    const topLevel = execSync("git rev-parse --show-toplevel", { cwd: root, stdio: "pipe", encoding: "utf8" }).trim();
    if (!topLevel) return null;
  } catch {
    return null; // no git at all
  }

  const files = new Set();
  for (const f of committedTouchedFiles(root, base)) files.add(f);
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
// commands execute in). Returns an exit code (0 = all matched rows passed /
// none matched / no registry; non-zero = failure or malformed registry).
// `registryPath` overrides the default resolution (see resolveRegistry).
// `touchedFiles` (optional array of project-root-relative paths) enables
// scope mode — rows with a `covers` field run only if a touched file matches
// one of its globs; rows without `covers` always run (fail-safe).
export function run(beat, root, registryPath, touchedFiles) {
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
    const skipped = totalBefore - rows.length;
    if (skipped > 0) console.log(`run.mjs: ${skipped} tool(s) skipped (scope — not touched)`);
  }

  let failed = 0;
  for (const row of rows) {
    if (typeof row.run !== "string" || !row.run) {
      console.error(`FAIL  ${row.id ?? "<no id>"} — row has no runnable "run" command`);
      failed++;
      continue;
    }
    try {
      execSync(row.run, { cwd: root, stdio: "inherit" });
      console.log(`PASS  ${row.id}`);
    } catch {
      console.log(`FAIL  ${row.id}`);
      failed++;
    }
  }

  console.log(`\n${beat}: ${rows.length - failed}/${rows.length} passed${failed ? ` — ${failed} FAILED` : ""}`);
  return failed ? 1 : 0;
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
    touchedFiles = computeTouchedFiles(projectRoot(), base);
    if (touchedFiles === null) {
      console.log("run.mjs: --touched: no git / no base resolved — degrading to full run");
    }
  }

  process.exit(run(beat, projectRoot(), null, touchedFiles));
}
