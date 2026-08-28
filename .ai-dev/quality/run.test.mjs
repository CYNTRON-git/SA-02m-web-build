#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   quality-runner-self-test — standalone Node test for run.mjs's `--touched`
   row selection. Two regressions, both of which made `--touched` report a
   FALSE GREEN (a skipped row still prints PASS):

     A. computeTouchedFiles()'s last-resort fallback (the unstaged `git status
        --short` branch, used right after branching with no new commits yet —
        no origin/main, no merge-base diff).
     B. coversToRegex()'s handling of a bare path PREFIX.
     C. computeTouchedFiles() ignoring the working tree once the branch has a
        commit — the Builder's own handback invisible to `--touched`.
   ───────────────────────────────────────────────────────────────────────────
   Regression A: the fallback used to `.trim()` the WHOLE multi-line
   `git status --short` output before splitting into lines. Trimming the
   whole blob eats the leading space of the FIRST modified-file line (a
   status line is "XY path" — a 2-char code + a space, e.g. " M path"), which
   desyncs that one entry's `slice(3)` by one character and over-trims its
   path. Effect: `node .ai-dev/quality/run.mjs build --touched` silently
   reported 0/0 passed (every row skipped as "not touched") on a freshly
   branched repo instead of running the real subset — a false green, found
   during the 1.0.5.47 build session on 2026-07-28.

   Regression B: `covers` is documented in tools.json as "path prefixes/globs",
   but a pattern with no glob metacharacter compiled literally — `"etc/"`
   became /^etc\/$/, a regex matching the directory string and no file beneath
   it. All 18 bare-prefix entries in the registry were therefore dead under
   `--touched`; on the branch that fixed it, `iface-naming-contract` and
   `kernel-policy-contract` (both covering `"etc/"`) skipped while the diff
   changed `etc/sa02m-web-service-ctl.sh`. Found 2026-07-22, re-found by the
   2026-08-05 audit, fixed in the 2026-08-06 backlog sweep. Both forms are
   pinned below, plus the `/` boundary that keeps a prefix from matching a
   sibling whose name merely starts the same way.

   Regression C: the working tree was a LAST RESORT — read only when the
   committed `<base>..HEAD` diff came back empty. On any branch carrying a
   commit (i.e. every real feature branch past its first commit), a Builder's
   uncommitted edits therefore selected NO rows: `run.mjs build --touched`,
   the documented pre-handback command, printed a green summary having never
   run a single check over the new work. Live instance, 1.0.6.24: an edit to
   `www/network_config/cgi-bin/status.cgi` left `bash-cgi-syntax` and every
   other `cgi-bin/` row unselected. The touched set is now the UNION of the
   committed diff and the working tree (staged + unstaged + untracked).

   No framework — mirrors scripts/dev/test-clear-session-cookie.mjs's
   stdlib-only posture. Spins up a real temp git repo so the test exercises
   the actual fallback path (no origin remote, branch even with main, one
   unstaged modification) rather than a copy of the parsing logic.
   ═══════════════════════════════════════════════════════════════════════════ */
import { execSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { computeTouchedFiles, coversToRegex, fileMatchesCovers, workingTreeFiles } from './run.mjs';

let failures = 0;
function check(cond, msg) {
  if (cond) { console.log('  ok   - ' + msg); }
  else { failures++; console.error('  FAIL - ' + msg); }
}

const dir = mkdtempSync(join(tmpdir(), 'sa02m-run-mjs-test-'));
const env = {
  ...process.env,
  GIT_AUTHOR_NAME: 'test', GIT_AUTHOR_EMAIL: 'test@test',
  GIT_COMMITTER_NAME: 'test', GIT_COMMITTER_EMAIL: 'test@test',
};
function git(cmd) {
  execSync('git ' + cmd, { cwd: dir, stdio: 'pipe', env });
}

try {
  git('init -q -b main');
  writeFileSync(join(dir, 'widget.js'), 'a\n');
  git('add widget.js');
  git('commit -q -m initial');
  git('checkout -q -b feature'); // even with main — no new commits, no origin remote

  // Unstaged modification → `git status --short` first line is " M widget.js".
  writeFileSync(join(dir, 'widget.js'), 'a\nb\n');

  const touched = computeTouchedFiles(dir, null);
  check(Array.isArray(touched), 'computeTouchedFiles() falls through to the git-status last resort (returns an array, not null)');
  if (Array.isArray(touched)) {
    check(touched.length === 1, 'exactly one touched file (got ' + JSON.stringify(touched) + ')');
    check(touched[0] === 'widget.js', 'first entry is the untruncated path "widget.js" (got ' + JSON.stringify(touched[0]) + ')');
  }
} finally {
  rmSync(dir, { recursive: true, force: true });
}

// ── C. the Builder's own handback: committed diff UNION working tree ───────
// The scenario that shipped hollow: a branch WITH a commit (so the committed
// diff is non-empty and the old last-resort fallback never fired) plus
// uncommitted work of every shape a handback carries — unstaged, staged,
// untracked, renamed, and a path git's human format would have QUOTED.
const dir2 = mkdtempSync(join(tmpdir(), 'sa02m-run-mjs-test-union-'));
function git2(cmd) {
  execSync('git ' + cmd, { cwd: dir2, stdio: 'pipe', env });
}
try {
  git2('init -q -b main');
  writeFileSync(join(dir2, 'base.txt'), 'a\n');
  writeFileSync(join(dir2, 'renamed-from.sh'), 'a\n');
  writeFileSync(join(dir2, 'status.cgi'), 'a\n');
  git2('add base.txt renamed-from.sh status.cgi');
  git2('commit -q -m initial');
  // A real feature branch: cut from origin/main, then a commit of its own.
  git2('update-ref refs/remotes/origin/main refs/heads/main');
  git2('checkout -q -b feature');
  writeFileSync(join(dir2, 'committed.js'), 'a\n');
  git2('add committed.js');
  git2('commit -q -m "first commit on the branch"');

  // The handback, uncommitted.
  writeFileSync(join(dir2, 'status.cgi'), 'a\nb\n');            // unstaged edit
  writeFileSync(join(dir2, 'staged.py'), 'a\n');                 // staged add
  git2('add staged.py');
  writeFileSync(join(dir2, 'untracked.sh'), 'a\n');              // untracked
  writeFileSync(join(dir2, 'name with space.js'), 'a\n');        // untracked, quoted by --short
  git2('mv renamed-from.sh renamed-to.sh');                      // staged rename

  const touched = computeTouchedFiles(dir2, null);
  check(Array.isArray(touched), 'union: computeTouchedFiles() returns an array on a branch with a commit');
  const has = (p) => Array.isArray(touched) && touched.includes(p);
  check(has('committed.js'), 'union: the committed half survives (committed.js)');
  check(has('status.cgi'),
    'union: an UNSTAGED edit is seen even though the committed diff is non-empty — the regression (status.cgi)');
  check(has('staged.py'), 'union: a STAGED add is seen (staged.py)');
  check(has('untracked.sh'), 'union: an UNTRACKED file is seen (untracked.sh)');
  check(has('name with space.js'),
    'union: a path git would quote in --short comes back verbatim, unquoted (name with space.js)');
  check(has('renamed-to.sh') && has('renamed-from.sh'),
    'union: a rename yields BOTH paths — the new one and the old one whose covers also mattered');
  check(Array.isArray(touched) && new Set(touched).size === touched.length,
    'union: no duplicate entries (got ' + JSON.stringify(touched) + ')');

  // Non-vacuity for the working-tree half: it must actually read git, not
  // return a constant. A clean tree yields nothing.
  git2('reset -q --hard HEAD');
  git2('clean -qfd');
  check(workingTreeFiles(dir2).length === 0,
    'union: workingTreeFiles() returns nothing on a clean tree (not a constant) — got ' +
    JSON.stringify(workingTreeFiles(dir2)));
  const clean = computeTouchedFiles(dir2, null);
  check(Array.isArray(clean) && clean.includes('committed.js') && !clean.includes('status.cgi'),
    'union: with a clean tree only the committed half remains');
} finally {
  rmSync(dir2, { recursive: true, force: true });
}

// ── B. covers: bare path prefix AND glob ──────────────────────────────────
// Table-driven so a new form is one row. Each case is [pattern, path, expected].
const coversCases = [
  // The prefix form — the regression. Every one of these was `false`.
  ['etc/', 'etc/systemd/sa02m-watchdog.conf', true, 'bare prefix matches a file beneath it'],
  ['etc/', 'etc/sa02m-web-service-ctl.sh', true, 'bare prefix matches a direct child'],
  ['tools/imaging/', 'tools/imaging/make-image.sh', true, 'nested bare prefix matches'],
  ['www/network_config/static/js/', 'www/network_config/static/js/app/status.js', true,
   'bare prefix matches at any depth beneath it'],
  // A prefix must stop at a path boundary, or `etc/` would sweep in `etcetera/`.
  ['etc/', 'etcetera/x.conf', false, 'prefix stops at the / boundary, not mid-segment'],
  ['opt/', 'www/network_config/x.py', false, 'an unrelated path does not match'],
  // An exact file path keeps exact semantics — the prefix rule must not make
  // a covered file also cover its own backups.
  ['install.sh', 'install.sh', true, 'exact file path still matches itself'],
  ['install.sh', 'install.sh.bak', false, 'exact file path does not match a sibling that extends it'],
  ['etc/sa02m-web-service-ctl.sh', 'etc/sa02m-web-service-ctl.sh.orig', false,
   'exact file path does not match its own .orig'],
  // The glob form — unchanged by the fix, pinned so it stays that way.
  ['tools/imaging/**', 'tools/imaging/firstboot-overlay/etc/x.conf', true, '** matches at any depth'],
  ['tools/imaging/**', 'tools/system-hardening/install.sh', false, '** stays inside its own root'],
  ['www/network_config/static/js/*.js', 'www/network_config/static/js/app.js', true,
   'single * matches within one segment'],
  ['www/network_config/static/js/*.js', 'www/network_config/static/js/app/status.js', false,
   'single * does not cross a / boundary'],
];
for (const [pattern, file, want, msg] of coversCases) {
  const got = fileMatchesCovers([file], [pattern]);
  check(got === want,
    'covers ' + JSON.stringify(pattern) + ' vs ' + JSON.stringify(file) + ' => ' + want + ' — ' + msg);
}

// Non-vacuity: the helpers must actually be wired up. A coversToRegex() that
// returned null for everything would make every case above pass by way of
// fileMatchesCovers' malformed-pattern skip returning false.
check(coversToRegex('etc/') instanceof RegExp, 'coversToRegex() returns a RegExp for a bare prefix (not null)');
check(fileMatchesCovers(['etc/x'], ['nope/', 'etc/']) === true,
  'fileMatchesCovers() ORs across patterns — a later pattern still matches');

if (failures) {
  console.error('quality-runner-self-test: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('quality-runner-self-test: computeTouchedFiles() fallback + covers prefix/glob matching ok');
process.exit(0);
