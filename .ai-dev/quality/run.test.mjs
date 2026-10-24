#!/usr/bin/env node
// comment-mutation-proof-exempt: self-test of the runner's own touched-set and covers matching - it drives run.mjs against purpose-built temp git repos and asserts the returned file lists; it pins no source line of any shipped file.
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
     D. run() printing PASS for a row that could not run here (no skip
        verdict existed; audit 2026-09-24 L1) — now exit 77 = SKIP.
     E. the whole-row skippers returning 0 instead of 77 when their tool is
        forced absent.
     F. a registry-run script exiting with a failure COUNT (77 failures read
        as SKIP, 256 as PASS — review F1, 1.0.6.58).
     G. a row that skipped PART of its run reading PASS (1.0.6.62 review A3) —
        now PARTIAL, through the QUALITY_SKIP_LOG side channel + lib_skip.sh.
     H. `--touched <ref>` diffing two-dot `<ref>..HEAD`, so a base that moved on
        pulled its own changes into the branch's scope (1.0.6.62 review A9) —
        now `$(git merge-base <ref> HEAD)..HEAD`.
     I. QUALITY_STRICT (CI): a SKIP or a PARTIAL is a FAIL where the
        environment is supposed to run the row (1.0.6.58 follow-up (1)).
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
   Second half of the same class: BOTH halves must read git's `-z` output.
   The union landed with `-z` on the working-tree side only, so a COMMITTED
   path holding a non-ASCII byte came back quoted (`"\320\260\320\261.js"`)
   and matched no `covers` glob — the same silent skip, on the other half
   (review Q3, 1.0.6.24). Both are pinned below with a real quoted path.

   No framework — mirrors scripts/dev/test-clear-session-cookie.mjs's
   stdlib-only posture. Spins up a real temp git repo so the test exercises
   the actual fallback path (no origin remote, branch even with main, one
   unstaged modification) rather than a copy of the parsing logic.
   ═══════════════════════════════════════════════════════════════════════════ */
import { execSync, spawnSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as runner from './run.mjs';
import { computeTouchedFiles, coversToRegex, fileMatchesCovers, run, workingTreeFiles } from './run.mjs';
// Read off the namespace so the pre-fix runner (no such export) yields
// undefined and section D goes RED on assertions instead of a link error.
const SKIP_EXIT = runner.SKIP_EXIT;

// This file is itself a runner row: the runner that launched it hands it a
// QUALITY_SKIP_LOG and, in CI, QUALITY_STRICT. Neither may reach the fixtures
// below — the nested run() calls pick their own, and a harness spawned here must
// not write into the outer row's log. Sections G and I set them explicitly.
delete process.env.QUALITY_SKIP_LOG;
delete process.env.QUALITY_STRICT;

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
  // The COMMITTED half's own hostile shapes: git's human format quotes a path
  // holding a space or a non-ASCII byte ("www/\320\260.js"), so a naive
  // newline split hands the runner a quoted path that matches no `covers` glob
  // — the same silent skip the working-tree half was fixed for (Q3, 1.0.6.24).
  writeFileSync(join(dir2, 'абв.js'), 'a\n');
  writeFileSync(join(dir2, 'committed name with space.js'), 'a\n');
  git2('add -A');
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
  check(has('абв.js'),
    'union: a COMMITTED non-ASCII path comes back verbatim, unquoted (абв.js) — got ' + JSON.stringify(touched));
  check(has('committed name with space.js'),
    'union: a COMMITTED path with a space comes back verbatim, unquoted');
  // The consequence, not just the string: a quoted path matches no `covers`
  // pattern, so the row that guards it is silently skipped. The pattern here
  // can be satisfied by NOTHING else in the touched set.
  check(Array.isArray(touched) && fileMatchesCovers(touched, ['абв.js']),
    'union: the committed non-ASCII path MATCHES its own covers pattern — the silent skip is closed');
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

// ── B2. every `covers` entry of every registry row selects >=1 real file ──
// A wildcard-free entry is a PREFIX (B above), so `"etc/sa02m-update"` compiles
// to /^etc\/sa02m-update(\/.*)?$/ and matches NOTHING — the row never ran under
// --touched when the files it names were edited (status review F1, 1.0.7.5:
// three dead entries across port-coordinator / update-control-boundaries; the
// author meant `etc/sa02m-update*`). The registry itself is the fixture: every
// entry must select at least one file git knows (tracked, or untracked and not
// ignored — a row may name a script added in the same change). Non-vacuous:
// the enumeration must see >=100 entries over >=1000 files.
{
  const regRoot = dirname(dirname(dirname(fileURLToPath(import.meta.url))));
  const known = execSync('git ls-files -z --cached --others --exclude-standard', { cwd: regRoot, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 })
    .split('\0').filter(Boolean);
  const reg = JSON.parse(readFileSync(runner.resolveRegistry(regRoot), 'utf8'));
  let entries = 0;
  const dead = [];
  for (const row of reg.tools || []) {
    for (const pattern of row.covers || []) {
      entries++;
      if (!fileMatchesCovers(known, [pattern])) dead.push(row.id + ': ' + JSON.stringify(pattern));
    }
  }
  check(known.length >= 1000 && entries >= 100,
    'registry covers: the enumeration saw ' + entries + ' entries over ' + known.length + ' known files (floors 100 / 1000)');
  check(dead.length === 0,
    'registry covers: every entry selects >=1 known file' + (dead.length ? ' — dead entries (a bare prefix that is really a glob?): ' + dead.join(', ') : ''));
}

// ── D. a documented environment skip is reported as SKIP, never as PASS ───
// A row that cannot run here (its tool is not installed) used to exit 0, and
// the runner printed `PASS` for it — five rows did exactly that on a box
// without their tool (audit 2026-09-24 L1). The runner now reads SKIP_EXIT
// (77) as a skip: printed SKIP, counted apart from the passes, named in the
// summary, and NOT a red beat. Every other non-zero code stays a FAIL.
function runCaptured(rows, beat = 'build', opts) {
  const d = mkdtempSync(join(tmpdir(), 'sa02m-run-mjs-test-skip-'));
  const reg = join(d, 'tools.json');
  writeFileSync(reg, JSON.stringify({ tools: rows }));
  const lines = [];
  const origLog = console.log;
  const origErr = console.error;
  const origWrite = process.stdout.write;
  console.log = (...a) => { lines.push(a.join(' ')); };
  console.error = (...a) => { lines.push(a.join(' ')); };
  process.stdout.write = (text) => { lines.push(String(text).trimEnd()); return true; };
  let rc;
  try {
    rc = run(beat, d, reg, null, opts);
  } finally {
    console.log = origLog;
    console.error = origErr;
    process.stdout.write = origWrite;
    rmSync(d, { recursive: true, force: true });
  }
  return { rc, out: lines.join('\n') };
}
const exitRow = (id, code) => ({ id, beat: 'build', run: 'node -e "process.exit(' + code + ')"' });

check(SKIP_EXIT === 77, 'skip: SKIP_EXIT is 77 (the automake convention every converted row returns)');
{
  const { rc, out } = runCaptured([exitRow('d-pass', 0), exitRow('d-skip', SKIP_EXIT)]);
  check(rc === 0, 'skip: a pass + a documented skip is a GREEN beat (rc 0) — got ' + rc);
  check(/^SKIP  d-skip$/m.test(out), 'skip: the skipped row prints `SKIP  d-skip`');
  check(!/^PASS  d-skip$/m.test(out), 'skip: the skipped row does NOT print `PASS  d-skip` — the L1 defect');
  check(/^PASS  d-pass$/m.test(out), 'skip: the passing row still prints PASS');
  check(/build: 1\/2 passed/.test(out) && !/2\/2 passed/.test(out),
    'skip: the summary counts the skip apart from the passes (1/2 passed, never 2/2) — got ' + JSON.stringify(out.split('\n').pop()));
  check(/1 SKIPPED/.test(out) && /d-skip/.test(out.split('\n').pop()),
    'skip: the summary names the skip count and the skipped row id');
}
{
  const { rc, out } = runCaptured([exitRow('d-pass', 0), exitRow('d-skip', SKIP_EXIT), exitRow('d-fail', 1)]);
  check(rc === 1, 'skip: a skip does not mask a real failure (rc 1) — got ' + rc);
  check(/^FAIL  d-fail$/m.test(out) && /1 FAILED/.test(out), 'skip: the failing row prints FAIL and is counted');
}
{
  // Non-vacuity of the mapping: only 77 is a skip. The neighbouring codes a
  // row really uses (1 = check failed, 2 = usage/infra, 3 = ui-layout INFRA
  // ERROR) must stay FAILs, or "skip" would swallow real breakage.
  for (const code of [1, 2, 3, 76, 78]) {
    const { rc, out } = runCaptured([exitRow('d-code' + code, code)]);
    check(rc === 1 && /^FAIL  d-code/m.test(out) && !/SKIP/.test(out),
      'skip: exit ' + code + ' is a FAIL, not a SKIP');
  }
}
{
  const { rc, out } = runCaptured([exitRow('d-only-skip', SKIP_EXIT)]);
  check(rc === 0 && /build: 0\/1 passed/.test(out),
    'skip: a beat whose only row skipped reads 0/1 passed, green but not "passed" — got ' + JSON.stringify(out));
}

// ── E. the whole-row skippers return SKIP_EXIT, not 0 ─────────────────────
// Each row below skips its whole run when its tool is absent. Forced absence:
// an empty PATH for a binary probe (the scripts use only builtins before the
// probe), a PYTHONPATH shadow module that refuses to import for a python dep,
// and a copy of the driver outside the repo (no scripts/dev/node_modules) for
// playwright. Each must exit 77 AND print its skip line — the line alone was
// already there when the row read PASS.
const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
function sh(cmd, env) {
  const r = spawnSync('bash', ['-c', cmd], { cwd: REPO, env: { ...process.env, ...env }, encoding: 'utf8' });
  return { rc: r.status, out: (r.stdout || '') + (r.stderr || '') };
}
{
  const r = sh('PATH=/nonexistent-sa02m-skip-probe; exec "$BASH" .ai-dev/quality/checks/shellcheck.sh');
  check(r.rc === SKIP_EXIT && /shellcheck: skipped/.test(r.out),
    'skipper: shellcheck.sh without shellcheck exits 77 with its skip line — got rc ' + r.rc + ' ' + JSON.stringify(r.out.trim()));
}
{
  const r = sh('PATH=/nonexistent-sa02m-skip-probe; exec "$BASH" .ai-dev/quality/checks/sudoers-visudo.sh');
  check(r.rc === SKIP_EXIT && /sudoers-visudo: SKIP/.test(r.out),
    'skipper: sudoers-visudo.sh without visudo exits 77 with its skip line — got rc ' + r.rc + ' ' + JSON.stringify(r.out.trim()));
}
{
  const shadow = mkdtempSync(join(tmpdir(), 'sa02m-run-mjs-test-pyshadow-'));
  try {
    for (const m of ['pytest', 'jsonschema', 'cryptography']) {
      writeFileSync(join(shadow, m + '.py'), 'raise ImportError("shadowed by run.test.mjs section E")\n');
    }
    const env = { PYTHONPATH: shadow };
    let r = sh('bash .ai-dev/quality/checks/pytest-suite.sh e-row opt/sa02m-update', env);
    check(r.rc === SKIP_EXIT && /e-row: pytest not installed/.test(r.out),
      'skipper: pytest-suite.sh without pytest exits 77 — got rc ' + r.rc + ' ' + JSON.stringify(r.out.trim()));
    // The per-dep branch: pytest importable, a listed runtime dep not.
    writeFileSync(join(shadow, 'pytest.py'), '# importable stand-in\n');
    r = sh('bash .ai-dev/quality/checks/pytest-suite.sh e-row opt/sa02m-update cryptography', env);
    check(r.rc === SKIP_EXIT && /e-row: runtime dep 'cryptography' missing/.test(r.out),
      'skipper: pytest-suite.sh without a listed dep exits 77 — got rc ' + r.rc + ' ' + JSON.stringify(r.out.trim()));
    r = sh('bash .ai-dev/quality/checks/sh-model-schema.sh', env);
    check(r.rc === SKIP_EXIT && /jsonschema' not installed - skipped/.test(r.out),
      'skipper: sh-model-schema.sh without jsonschema exits 77 — got rc ' + r.rc + ' ' + JSON.stringify(r.out.trim()));
  } finally {
    rmSync(shadow, { recursive: true, force: true });
  }
}
// The runner harnesses under scripts/dev/ that guard on python3 skip the whole
// row without it (found by the L1 sweep beyond the entry's list).
for (const [script, line] of [
  ['scripts/dev/test-update-conditional-restart.sh', 'SKIP  python3 unavailable'],
  ['scripts/dev/test-update-deploy-skip.sh', 'SKIP  python3 unavailable'],
  ['scripts/dev/test-update-recover-boot.sh', 'SKIP  python3 unavailable'],
  ['scripts/dev/test-web-update-launcher-guard.sh', 'SKIP  python3 unavailable'],
]) {
  const r = sh('PATH=/nonexistent-sa02m-skip-probe; exec "$BASH" ' + script);
  check(r.rc === SKIP_EXIT && r.out.includes(line),
    'skipper: ' + script + ' without python3 exits 77 — got rc ' + r.rc + ' ' + JSON.stringify(r.out.trim().slice(-160)));
}
{
  // cache-bust-r with no reference state reachable: a throwaway repo carrying
  // only the check and a VERSION, no origin refs, fetching disabled.
  const d = mkdtempSync(join(tmpdir(), 'sa02m-run-mjs-test-cachebust-'));
  try {
    mkdirSync(join(d, '.ai-dev', 'quality', 'checks'), { recursive: true });
    mkdirSync(join(d, 'www', 'network_config'), { recursive: true });
    copyFileSync(join(REPO, '.ai-dev', 'quality', 'checks', 'cache-bust-r.sh'), join(d, '.ai-dev', 'quality', 'checks', 'cache-bust-r.sh'));
    writeFileSync(join(d, 'www', 'network_config', 'VERSION'), '1.0.6.57\n');
    const g = (c) => execSync('git ' + c, { cwd: d, stdio: 'pipe', env });
    g('init -q -b main'); g('add -A'); g('commit -q -m base');
    const r = spawnSync('bash', ['.ai-dev/quality/checks/cache-bust-r.sh'],
      { cwd: d, env: { ...process.env, CACHE_BUST_R_NO_FETCH: '1' }, encoding: 'utf8' });
    const out = (r.stdout || '') + (r.stderr || '');
    check(r.status === SKIP_EXIT && /cache-bust-r: skip  no reference state reachable/.test(out),
      'skipper: cache-bust-r.sh with no reference state exits 77 — got rc ' + r.status + ' ' + JSON.stringify(out.trim()));
  } finally {
    rmSync(d, { recursive: true, force: true });
  }
}
{
  const d = mkdtempSync(join(tmpdir(), 'sa02m-run-mjs-test-uilayout-'));
  try {
    const checksDir = join(d, '.ai-dev', 'quality', 'checks');
    mkdirSync(checksDir, { recursive: true });
    copyFileSync(join(REPO, '.ai-dev', 'quality', 'checks', 'ui-layout.mjs'), join(checksDir, 'ui-layout.mjs'));
    const r = spawnSync(process.execPath, [join(checksDir, 'ui-layout.mjs')], { cwd: d, encoding: 'utf8' });
    const out = (r.stdout || '') + (r.stderr || '');
    check(r.status === SKIP_EXIT && /ui-layout: skipped — playwright not installed/.test(out),
      'skipper: ui-layout.mjs without playwright exits 77 — got rc ' + r.status + ' ' + JSON.stringify(out.trim().slice(0, 200)));
  } finally {
    rmSync(d, { recursive: true, force: true });
  }
}

// ── F. no registry-run script exits with a COUNT ───────────────────────────
// SKIP_EXIT (77) is only honest if no gate can reach 77 by accident. A gate
// ending in `exit "$fails"` reports 77 failures as an environment SKIP (beat
// green) and 256 failures as exit 0 (PASS) — review F1, 1.0.6.58 (the measured
// RED lives in the quality-runner-self-test row). The rule: a registry-run script's OWN exit status
// is a literal, a boolean `$(( … > 0 ))`, or carries an `exit-status:` marker
// naming why a variable is safe. Every row is enumerated (the same
// check-script resolution comment-mutation-proof uses), not a list of names.
// Scope, stated: heredoc bodies are skipped — they are shims standing in for
// external tools (a fake `mkfs.exfat` exiting "${SHIM_MKFS_RC}") or embedded
// python whose status the bash around it reads — and only the script the row
// names is read, not what it sources. Also NOT scanned: a script whose final
// command is a function ending `return "$var"`. `process.exitCode = …` in a
// node row IS scanned, like `process.exit(…)`.
const EXIT_MARK = /exit-status:\s*\S/;
// Shell lexer, just enough for this rule: yields each line's TOP-LEVEL code
// (quoted strings, `$(…)` / backtick substitutions — awk programs, messages,
// printf'd shims, a subshell's own exit — collapse to `Q`, comments drop,
// heredoc bodies are skipped), so an `exit` inside a string, an awk program or
// a command substitution is never mistaken for the script's own. Quoting is a
// STACK, not a flag: `"$(sed 's/"x"/y/')"` nests a single- and a double-quoted
// string inside a substitution inside a double-quoted string (review F5 — a
// flag lost sync there and hid ~100 lines of test-alice-reload-handshake.sh).
// Returns { lines, open }: `open` is the state left at EOF — anything but
// empty means the lexer lost sync, and the caller FAILS rather than trusting a
// scan that may have skipped code.
function shLex(text) {
  const lines = text.split(/\r?\n/);
  const out = [];
  const stack = [];      // frames: { k: "'" | '"' | '$' (ANSI-C) | '(' (subst, depth) | '`' }
  const pending = [];    // heredoc terminators opened on the current line
  let inHeredoc = null;
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (inHeredoc) {
      if ((inHeredoc.dash ? ln.replace(/^\t+/, '') : ln) === inHeredoc.tag) inHeredoc = pending.shift() || null;
      out.push('');
      continue;
    }
    let code = '';
    for (let j = 0; j < ln.length; j++) {
      const c = ln[j];
      const top = stack[stack.length - 1];
      if (top && top.k === "'") { if (c === "'") stack.pop(); continue; }
      if (top && top.k === '$') {
        if (c === '\\') { j++; continue; }
        if (c === "'") stack.pop();
        continue;
      }
      if (top && top.k === '`') {
        if (c === '\\') { j++; continue; }
        if (c === '`') stack.pop();
        continue;
      }
      if (top && top.k === '"') {
        if (c === '\\') { j++; continue; }
        if (c === '"') { stack.pop(); continue; }
        if (c === '$' && ln[j + 1] === '(') { stack.push({ k: '(', d: 1 }); j++; continue; }
        if (c === '`') { stack.push({ k: '`' }); continue; }
        continue;
      }
      // Top level, or inside a command substitution: both are shell code.
      const inSub = top && top.k === '(';
      if (c === '\\') { j++; continue; }
      if (c === "'") { stack.push({ k: "'" }); if (!inSub) code += 'Q'; continue; }
      if (c === '"') { stack.push({ k: '"' }); if (!inSub) code += 'Q'; continue; }
      if (c === '$' && ln[j + 1] === "'") { stack.push({ k: '$' }); j++; if (!inSub) code += 'Q'; continue; }
      if (c === '`') { stack.push({ k: '`' }); if (!inSub) code += 'Q'; continue; }
      // Arithmetic `$(( … ))` at top level stays verbatim: it is an exit
      // operand the rule reads (`exit $(( fails > 0 ))`), not a subshell.
      if (!inSub && ln.startsWith('$((', j)) {
        const end = ln.indexOf('))', j + 3);
        if (end > 0) { code += ln.slice(j, end + 2); j = end + 1; continue; }
      }
      if (c === '$' && ln[j + 1] === '(') { stack.push({ k: '(', d: 1 }); j++; if (!inSub) code += 'Q'; continue; }
      if (c === '#' && (j === 0 || /[\s;&|(]/.test(ln[j - 1]))) break;
      if (inSub) {
        if (c === '(') top.d++;
        else if (c === ')' && --top.d === 0) stack.pop();
        continue;
      }
      if (c === '<' && ln[j + 1] === '<' && ln[j + 2] !== '<') {
        const m = ln.slice(j).match(/^<<(-?)\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\2/);
        if (m) { pending.push({ tag: m[3], dash: m[1] === '-' }); j += m[0].length - 1; code += '<<H'; continue; }
      }
      code += c;
    }
    out.push(code);
    if (!stack.length && pending.length) inHeredoc = pending.shift();
  }
  const open = stack.map((f) => f.k).join('') + (inHeredoc ? '<<' + inHeredoc.tag : '') +
    pending.map((p) => '<<' + p.tag).join('');
  return { lines: out, open };
}

function exitViolations(text, lang) {
  const out = [];
  const lines = text.split(/\r?\n/);
  if (lang === 'sh') {
    const lex = shLex(text);
    if (lex.open) out.push('EOF: the lexer ends inside `' + lex.open + '` — it lost sync, so the scan of this file cannot be trusted');
    lex.lines.forEach((code, i) => {
      // `)` is a case arm: `bad) exit "$fails" ;;` is the script's own exit.
      const re = /(?:^|[;{)]|&&|\|\||\bthen|\belse|\bdo)\s*exit\s+(\$\(\([^)]*\)\)|[^\s;&|)}]+)/g;
      let m;
      while ((m = re.exec(code))) {
        const op = m[1];
        const ok = /^\d+$/.test(op) || /^\$\(\(.*(>|!=|==|<).*\)\)$/.test(op) || EXIT_MARK.test(lines[i]);
        if (!ok) out.push((i + 1) + ': ' + lines[i].trim());
      }
    });
  } else {
    lines.forEach((ln, i) => {
      if (/^\s*(#|\/\/)/.test(ln)) return;
      const re = lang === 'py' ? /(?:sys\.exit|SystemExit)\(([^)]*)\)/g : /process\.exit\(([^)]*)\)/g;
      let m;
      while ((m = re.exec(ln))) {
        const op = m[1].trim();
        const ok = op === '' || /^\d+$/.test(op) || /\?\s*\d+\s*:\s*\d+$/.test(op) ||
          /^f?['"]/.test(op) || EXIT_MARK.test(ln);
        if (!ok) out.push((i + 1) + ': ' + ln.trim());
      }
      // The other way node sets its status (review F5): the same rule.
      if (lang === 'mjs') {
        const ec = ln.match(/process\.exitCode\s*=\s*([^;]+)/);
        if (ec) {
          const op = ec[1].trim();
          if (!(/^\d+$/.test(op) || /\?\s*\d+\s*:\s*\d+$/.test(op) || EXIT_MARK.test(ln))) out.push((i + 1) + ': ' + ln.trim());
        }
      }
    });
  }
  return out;
}

{
  // Non-vacuity of the scanner itself: each shape it must see, and must not.
  check(exitViolations('fails=3\nexit "$fails"\n', 'sh').length === 1, 'exit-scan: `exit "$fails"` is flagged');
  check(exitViolations('[ "$f" = 0 ] || { echo x; exit "$f"; }\n', 'sh').length === 1, 'exit-scan: an exit inside `{ …; }` is flagged');
  check(exitViolations('exit $rc\n', 'sh').length === 1, 'exit-scan: an unquoted `exit $rc` is flagged');
  check(exitViolations('exit 1\nexit 77  # SKIP\n[ x ] || exit 1\n', 'sh').length === 0, 'exit-scan: literal exits pass');
  check(exitViolations('exit $(( fails > 0 ))\n', 'sh').length === 0, 'exit-scan: a boolean `$(( … > 0 ))` passes');
  check(exitViolations('cat > f <<\'SHIM\'\nexit "${X:-0}"\nSHIM\nexit 0\n', 'sh').length === 0, 'exit-scan: a heredoc shim body is not the script\'s own exit');
  check(exitViolations('cat > f <<\'SHIM\'\nexit 0\nSHIM\nexit "$fails"\n', 'sh').length === 1, 'exit-scan: the scan resumes after the heredoc terminator');
  check(exitViolations('bad "exit $RC, expected 0"\n', 'sh').length === 0, 'exit-scan: "exit $RC" inside a message string is not an exit');
  // Spliced so this file's own scan does not read the fixture as an exit.
  check(exitViolations('process.' + 'exit(failures);\n', 'mjs').length === 1, 'exit-scan: a count handed to process.exit is flagged');
  check(exitViolations('process.exit(failures ? 1 : 0);\nprocess.exit(77);\n', 'mjs').length === 0, 'exit-scan: literal and boolean-ternary exits pass');
  check(exitViolations('sys.exit(fails)\n', 'py').length === 1, 'exit-scan: sys.exit(<count>) is flagged');
  // Review F5 shapes. The nested-quote line is test-alice-reload-handshake.sh:95
  // verbatim; with a quote FLAG it swallowed the rest of that file.
  check(exitViolations('x="$(grep -E \'^A=\' f | sed -E \'s/.*"([^"]+)".*/\\1/\')"\nexit "$fails"\n', 'sh').length === 1,
    'exit-scan: `"$(… \'…"…"…\' …)"` nesting does not hide the lines after it');
  check(exitViolations('case "$r" in\n  ok) exit 0 ;;\n  bad) exit "$fails" ;;\nesac\n', 'sh').length === 1,
    'exit-scan: an exit in a case arm is flagged');
  check(exitViolations('n=$(printf \'%s\' "$x"; exit 3)\nexit 0\n', 'sh').length === 0,
    'exit-scan: an exit inside `$(…)` is the subshell\'s, not the script\'s');
  check(exitViolations('echo "unterminated\nexit 0\n', 'sh').some((v) => v.startsWith('EOF:')),
    'exit-scan: a file that ends inside an open quote FAILS (lexer desync is not a clean scan)');
  check(exitViolations('cat <<EOF\nno terminator\n', 'sh').some((v) => v.startsWith('EOF:')),
    'exit-scan: a file that ends inside an open heredoc FAILS');
  check(exitViolations('process.' + 'exitCode = failures;\n', 'mjs').length === 1,
    'exit-scan: a count handed to the node exit-code property is flagged');
  check(exitViolations('process.' + 'exitCode = failures ? 1 : 0;\n', 'mjs').length === 0,
    'exit-scan: a boolean node exit-code passes');
}
{
  const reg = JSON.parse(readFileSync(join(REPO, '.ai-dev', 'quality', 'tools.json'), 'utf8'));
  const scriptRe = /(?:\.ai-dev\/quality\/|scripts\/dev\/)[A-Za-z0-9_.\/-]+\.(?:sh|mjs|py)/g;
  const scripts = new Set();
  for (const t of reg.tools || []) for (const m of String(t.run || '').matchAll(scriptRe)) scripts.add(m[0]);
  check(scripts.size >= 60, 'exit-scan: the registry resolves >=60 check scripts (got ' + scripts.size + ') — a broken enumeration FAILS, it does not pass empty');
  const bad = [];
  for (const s of [...scripts].sort()) {
    let text;
    try { text = readFileSync(join(REPO, s), 'utf8'); } catch { bad.push(s + ': unreadable'); continue; }
    for (const v of exitViolations(text, s.split('.').pop())) bad.push(s + ':' + v);
  }
  check(bad.length === 0, 'exit-scan: no registry-run script exits with a count (77 would read SKIP, 256 would read PASS)' +
    (bad.length ? ' — ' + bad.length + ' site(s):\n      ' + bad.join('\n      ') : ''));
}

// ── G. a partial section skip is reported as PARTIAL, never as PASS ───────
// `web-update-launcher-guard` runs L1–L7 everywhere and L8–L11 only where
// flock(1) exists; on git-bash it printed «PASS (all checks; SKIPPED here:
// L8–L11)» and the runner counted a PASS (1.0.6.62 review A3). The runner now
// hands each row a fresh QUALITY_SKIP_LOG file; a line in it after exit 0 makes
// the row PARTIAL: printed apart, not counted as passed, named in the summary,
// beat still green. The exit code is untouched on purpose (lib_skip.sh header).
const partRow = (id, code, text = 'L8 no flock') => ({
  id, beat: 'build',
  run: 'node -e "const f = process.env.QUALITY_SKIP_LOG; if (f) require(\'fs\').appendFileSync(f, \'' + text +
    '\\n\'); process.exit(' + code + ')"',
});
{
  const { rc, out } = runCaptured([partRow('g-part', 0), exitRow('g-pass', 0)]);
  check(rc === 0, 'partial: a partial row beside a pass is a GREEN beat (rc 0) — got ' + rc);
  check(/^PARTIAL  g-part\b/m.test(out), 'partial: the row that skipped a section prints `PARTIAL  g-part` — got ' + JSON.stringify(out));
  check(!/^PASS  g-part$/m.test(out), 'partial: the row that skipped a section does NOT print `PASS  g-part` — the A3 defect');
  check(/L8 no flock/.test(out), 'partial: the runner prints WHAT was skipped (the row\'s own skip line)');
  check(/^PASS  g-pass$/m.test(out), 'partial: the log is per row — the clean row after it still prints PASS');
  const last = out.split('\n').pop();
  check(/build: 1\/2 passed/.test(last) && /1 PARTIAL/.test(last) && /g-part/.test(last),
    'partial: the summary counts it apart from the passes and names it (1/2 passed, 1 PARTIAL) — got ' + JSON.stringify(last));
}
{
  const { rc, out } = runCaptured([partRow('g-part-fail', 1)]);
  check(rc === 1 && /^FAIL  g-part-fail$/m.test(out) && !/PARTIAL/.test(out),
    'partial: a skip line never softens a failure — exit 1 stays FAIL');
}
{
  const { rc, out } = runCaptured([partRow('g-part-skip', SKIP_EXIT)]);
  check(rc === 0 && /^SKIP  g-part-skip$/m.test(out) && !/PARTIAL/.test(out),
    'partial: a whole-row skip (77) stays SKIP, whatever the log holds');
}
check(process.env.QUALITY_SKIP_LOG === undefined,
  'partial: run() does not leave QUALITY_SKIP_LOG set in its own process (each row gets its own, in the child env only)');
{
  // The shell helper every opted-in harness sources. Behavioural: source it,
  // call it, read the file it wrote.
  const d = mkdtempSync(join(tmpdir(), 'sa02m-run-mjs-test-libskip-'));
  try {
    const log = join(d, 'skip.log').replace(/\\/g, '/');
    const cmd = '. .ai-dev/quality/checks/lib_skip.sh || exit 9; quality_skip "5  modes (not Linux)"; quality_skip "6  (see 5)"; echo "rc=$?"';
    let r = spawnSync('bash', ['-c', cmd], { cwd: REPO, env: { ...process.env, QUALITY_SKIP_LOG: log }, encoding: 'utf8' });
    let logged = '';
    try { logged = readFileSync(log, 'utf8'); } catch { /* absent — asserted below */ }
    check(r.status === 0 && /^SKIP  5  modes \(not Linux\)$/m.test(r.stdout) && /rc=0/.test(r.stdout),
      'lib_skip: quality_skip prints the `SKIP  <text>` line and returns 0 — got rc ' + r.status + ' ' + JSON.stringify((r.stdout || '') + (r.stderr || '')));
    check(logged === '5  modes (not Linux)\n6  (see 5)\n',
      'lib_skip: with QUALITY_SKIP_LOG set, each skip is appended to it, one line each — got ' + JSON.stringify(logged));
    const env = { ...process.env };
    delete env.QUALITY_SKIP_LOG;
    r = spawnSync('bash', ['-c', 'set -eu; ' + cmd], { cwd: REPO, env, encoding: 'utf8' });
    check(r.status === 0 && /^SKIP  6  \(see 5\)$/m.test(r.stdout),
      'lib_skip: unset (a hand run, a gate run by comment-mutation-proof) it only prints, and survives `set -eu` — got rc ' + r.status);
  } finally {
    rmSync(d, { recursive: true, force: true });
  }
}

// ── H. `--touched <ref>` diffs from the merge-base, not two-dot ────────────
// `git diff <ref>..HEAD` compares the two TREES, so once <ref> moved past the
// branch point every file <ref> changed since then showed up as touched (81
// files / 68 rows against the branch's true 6 files; 1.0.6.62 review A9). The
// committed half is now `$(git merge-base <ref> HEAD)..HEAD` — for an explicit
// base and for the origin/main / main fallbacks alike.
// A missing side channel is optional; an unreadable one is lost evidence.
{
  const row = { id: 'g-unreadable', beat: 'build', run: 'node -e "require(\'fs\').mkdirSync(process.env.QUALITY_SKIP_LOG)"' };
  const { rc, out } = runCaptured([row]);
  check(rc === 1 && /^FAIL  g-unreadable/m.test(out), 'partial: unreadable skip log fails closed');
}
{
  const row = { id: 'g-unittest', beat: 'build', run: 'node -e "console.error(\'Ran 3 tests in 0.1s\'); console.error(\'OK (skipped=2)\')"' };
  const { rc, out } = runCaptured([row]);
  check(rc === 0 && /^PARTIAL  g-unittest/m.test(out) && /2 unittest/.test(out), 'partial: native unittest skips are collected');
  const strict = runCaptured([row], 'build', { strict: true });
  check(strict.rc === 1, 'partial: native unittest skips fail strict mode');
}

const dir3 = mkdtempSync(join(tmpdir(), 'sa02m-run-mjs-test-mergebase-'));
function git3(cmd) {
  return execSync('git ' + cmd, { cwd: dir3, stdio: 'pipe', env, encoding: 'utf8' }).trim();
}
try {
  git3('init -q -b main');
  writeFileSync(join(dir3, 'base.txt'), 'a\n');
  git3('add base.txt');
  git3('commit -q -m base');
  const forkPoint = git3('rev-parse HEAD');
  git3('checkout -q -b feature');
  writeFileSync(join(dir3, 'feature.js'), 'branch work\n');
  git3('add feature.js');
  git3('commit -q -m "branch work"');
  git3('checkout -q main');
  // main moves on after the fork. Distinct bytes from feature.js: identical
  // content lets git pair the two files as a rename, and --name-only then
  // prints only the new name — hiding the very path this section is about.
  writeFileSync(join(dir3, 'moved-on.sh'), 'main moved on\n');
  git3('add moved-on.sh');
  git3('commit -q -m "main moves on"');
  git3('update-ref refs/remotes/origin/main refs/heads/main');
  git3('checkout -q feature');

  const meta = {};
  const touched = computeTouchedFiles(dir3, 'main', meta);
  check(Array.isArray(touched) && touched.includes('feature.js'),
    'merge-base: the branch\'s own commit is touched (feature.js) — got ' + JSON.stringify(touched));
  check(Array.isArray(touched) && !touched.includes('moved-on.sh'),
    'merge-base: `--touched main` does NOT pull in what main changed after the fork (moved-on.sh) — the A9 defect; got ' + JSON.stringify(touched));
  check(meta.ref === 'main' && meta.mergeBase === forkPoint,
    'merge-base: the resolved base is reported — ref main, merge-base = the fork point — got ' + JSON.stringify(meta));

  const dflt = computeTouchedFiles(dir3, null);
  check(Array.isArray(dflt) && dflt.includes('feature.js') && !dflt.includes('moved-on.sh'),
    'merge-base: the default base (origin/main, moved on) resolves through the merge-base too — got ' + JSON.stringify(dflt));

  const metaBad = {};
  const bad = computeTouchedFiles(dir3, 'no-such-ref', metaBad);
  check(Array.isArray(bad) && bad.includes('feature.js') && !bad.includes('moved-on.sh') && metaBad.ref === 'origin/main',
    'merge-base: an unresolvable base falls back to origin/main, and says so — got ' + JSON.stringify({ bad, metaBad }));
} finally {
  rmSync(dir3, { recursive: true, force: true });
}

// ── I. QUALITY_STRICT: a skip is a FAIL where the tool is supposed to exist ─
// CI installs every tool a row needs (web-quality.yml), so there a SKIP means a
// broken install step, not a missing optional tool — and a green beat over it is
// the L1 false green again, one level up (1.0.6.58 follow-up (1)). Locally the
// variable is absent and a skip stays a skip (section D).
const isStrict = runner.isStrict;
check(typeof isStrict === 'function', 'strict: run.mjs exports isStrict()');
if (typeof isStrict === 'function') {
  for (const [envVal, want] of [[undefined, false], ['', false], ['0', false], ['false', false], ['no', false],
    ['1', true], ['true', true], ['yes', true], ['on', true]]) {
    const e = envVal === undefined ? {} : { QUALITY_STRICT: envVal };
    check(isStrict(e) === want, 'strict: QUALITY_STRICT=' + JSON.stringify(envVal) + ' => ' + want +
      ' (an unrecognised non-empty value is strict — fail safe to more rigor)');
  }
}
{
  const { rc, out } = runCaptured([exitRow('i-pass', 0), exitRow('i-skip', SKIP_EXIT)], 'build', { strict: true });
  check(rc === 1, 'strict: a SKIP turns the beat RED (rc 1) — got ' + rc);
  check(/^FAIL  i-skip\b/m.test(out) && !/^SKIP  i-skip$/m.test(out), 'strict: the skipped row prints FAIL, not SKIP — got ' + JSON.stringify(out));
  check(/^PASS  i-pass$/m.test(out), 'strict: a passing row is unaffected');
}
{
  const { rc, out } = runCaptured([partRow('i-part', 0)], 'build', { strict: true });
  check(rc === 1 && /^FAIL  i-part\b/m.test(out) && !/^PARTIAL/m.test(out),
    'strict: a PARTIAL turns the beat RED too — a skipped section is a skip — got ' + JSON.stringify(out));
}
{
  // The env wiring: run() with no explicit option reads QUALITY_STRICT itself —
  // that is how the CI step turns it on.
  process.env.QUALITY_STRICT = '1';
  let res;
  try { res = runCaptured([exitRow('i-env-skip', SKIP_EXIT)]); } finally { delete process.env.QUALITY_STRICT; }
  check(res.rc === 1 && /^FAIL  i-env-skip\b/m.test(res.out) && /QUALITY_STRICT/.test(res.out),
    'strict: QUALITY_STRICT=1 in the environment is honoured by run() and named in its output — got ' + JSON.stringify(res));
  const off = runCaptured([exitRow('i-off-skip', SKIP_EXIT)]);
  check(off.rc === 0 && /^SKIP  i-off-skip$/m.test(off.out), 'strict: without the variable a skip stays a SKIP (the local default)');
}

// ── P. CLI output survives a slow pipe ────────────────────────────────────
// CI (#216, 2026-10-21) lost every PASS/FAIL line and the summary: the runner
// spools a row's output and replays it with process.stdout.write; on a pipe
// that write completes asynchronously, the next rows' execSync block the
// event loop, and process.exit() at the end drops whatever was still queued —
// the log stopped mid-word ~64 KB into a multi-MB row. The CLI is exercised as
// CI runs it: a copy of run.mjs with a registry beside it, stdout into a pipe
// whose reader starts late. The truncation reproduces on Linux only (node on
// Windows already blocks on pipes); the kill case below pins delivery AT
// row end — a verdict buffered until the run returns is lost when CI kills it.
{
  const d = mkdtempSync(join(tmpdir(), 'sa02m-run-mjs-test-pipe-'));
  try {
    const q = join(d, '.ai-dev', 'quality');
    mkdirSync(q, { recursive: true });
    copyFileSync(join(REPO, '.ai-dev', 'quality', 'run.mjs'), join(q, 'run.mjs'));
    const big = "node -e \"for(let i=0;i<60000;i++)process.stderr.write('INFO [telemetry] line '+String(i).padStart(6,'0')+' '+'x'.repeat(56)+'\\n')\"";
    writeFileSync(join(q, 'tools.json'), JSON.stringify({ tools: [
      { id: 'p-big', beat: 'build', run: big },
      { id: 'p-after', beat: 'build', run: 'node -e "setTimeout(()=>console.log(\'after-ok\'),1500)"' },
      { id: 'p-red', beat: 'build', run: 'node -e "process.exit(1)"' },
    ] }));
    const node = process.execPath.replace(/\\/g, '/');
    const cmd = `"${node}" .ai-dev/quality/run.mjs build | (sleep 2; cat); exit \${PIPESTATUS[0]}`;
    const r = spawnSync('bash', ['-c', cmd], { cwd: d, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    const out = r.stdout || '';
    check(/line 059999 x{56}/.test(out), 'pipe: the last line of a 5 MB row reaches a late reader (' + out.length + ' bytes seen)');
    check(/^PASS  p-after$/m.test(out) && /^FAIL  p-red$/m.test(out), 'pipe: the rows after the big one are reported');
    check(/^build: 2\/3 passed — 1 FAILED$/m.test(out), 'pipe: the summary line is printed');
    check(r.status === 1, 'pipe: the CLI exit code is the run result, not lost (got ' + r.status + ')');

    // Reader closes early (`| head`): no false FAIL, run result unchanged.
    writeFileSync(join(q, 'tools.json'), JSON.stringify({ tools: [
      { id: 'h-big', beat: 'build', run: big },
      { id: 'h-small', beat: 'build', run: 'node -e "0"' },
    ] }));
    const hcmd = `"${node}" .ai-dev/quality/run.mjs build 2>/dev/null | head -c 100 >/dev/null; exit \${PIPESTATUS[0]}`;
    const h = spawnSync('bash', ['-c', hcmd], { cwd: d, encoding: 'utf8' });
    check(h.status === 0, 'pipe: a reader that closes early does not turn a green run red (got ' + h.status + ')');

    // Killed mid-run: the verdicts of rows that already ended must be out.
    // Linux only — where the queued-write loss happens; on Windows the killed
    // runner's children keep the temp dir busy and the pipe already blocks.
    if (process.platform === 'linux') {
    writeFileSync(join(q, 'tools.json'), JSON.stringify({ tools: [
      { id: 'k-big', beat: 'build', run: big },
      { id: 'k-small', beat: 'build', run: 'node -e "0"' },
      { id: 'k-hang', beat: 'build', run: 'node -e "setTimeout(()=>{},10000)"' },
    ] }));
    const kcmd = `timeout -s KILL 6 "${node}" .ai-dev/quality/run.mjs build | (sleep 2; cat)`;
    // TMPDIR inside d: the killed runner never reaches its own spool cleanup.
    const k = spawnSync('bash', ['-c', kcmd], { cwd: d, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, env: { ...process.env, TMPDIR: d } });
    const kout = k.stdout || '';
    check(/^PASS  k-big$/m.test(kout) && /^PASS  k-small$/m.test(kout),
      'pipe: verdicts of finished rows survive a kill of the run (' + kout.length + ' bytes seen)');
    } else {
      console.log('  skip - pipe: kill-mid-run case is Linux-only (' + process.platform + ')');
    }
  } finally {
    // The killed runner's k-hang child lives on for a few seconds and holds
    // the directory on Windows: retry instead of failing the self-test.
    rmSync(d, { recursive: true, force: true, maxRetries: 30, retryDelay: 500 });
  }
}

if (failures) {
  console.error('quality-runner-self-test: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('quality-runner-self-test: computeTouchedFiles() fallback + merge-base + covers prefix/glob matching + SKIP / PARTIAL / strict status ok');
process.exit(0);
