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

// ── D. a documented environment skip is reported as SKIP, never as PASS ───
// A row that cannot run here (its tool is not installed) used to exit 0, and
// the runner printed `PASS` for it — five rows did exactly that on a box
// without their tool (audit 2026-09-24 L1). The runner now reads SKIP_EXIT
// (77) as a skip: printed SKIP, counted apart from the passes, named in the
// summary, and NOT a red beat. Every other non-zero code stays a FAIL.
function runCaptured(rows, beat = 'build') {
  const d = mkdtempSync(join(tmpdir(), 'sa02m-run-mjs-test-skip-'));
  const reg = join(d, 'tools.json');
  writeFileSync(reg, JSON.stringify({ tools: rows }));
  const lines = [];
  const origLog = console.log;
  const origErr = console.error;
  console.log = (...a) => { lines.push(a.join(' ')); };
  console.error = (...a) => { lines.push(a.join(' ')); };
  let rc;
  try {
    rc = run(beat, d, reg, null);
  } finally {
    console.log = origLog;
    console.error = origErr;
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
// green) and 256 failures as exit 0 (PASS) — review F1, 1.0.6.58: fifteen such
// sites in fourteen scripts. The rule: a registry-run script's OWN exit status
// is a literal, a boolean `$(( … > 0 ))`, or carries an `exit-status:` marker
// naming why a variable is safe. Every row is enumerated (the same
// check-script resolution comment-mutation-proof uses), not a list of names.
// Scope, stated: heredoc bodies are skipped — they are shims standing in for
// external tools (a fake `mkfs.exfat` exiting "${SHIM_MKFS_RC}") or embedded
// python whose status the bash around it reads — and only the script the row
// names is read, not what it sources.
const EXIT_MARK = /exit-status:\s*\S/;
// Shell lexer, just enough for this rule: yields each line's UNQUOTED code
// (quoted strings — awk programs, messages, printf'd shims — collapse to `Q`,
// comments drop, heredoc bodies are skipped), so an `exit` inside a string or
// an awk program is never mistaken for the script's own.
function shCodeLines(text) {
  const lines = text.split(/\r?\n/);
  const out = [];
  let q = null;        // null | "'" | '"' — quote state carried across lines
  let heredocs = [];   // pending heredoc terminators opened on this line
  let inHeredoc = null;
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (inHeredoc) {
      if ((inHeredoc.dash ? ln.replace(/^\t+/, '') : ln) === inHeredoc.tag) {
        inHeredoc = heredocs.shift() || null;
      }
      out.push('');
      continue;
    }
    let code = '';
    for (let j = 0; j < ln.length; j++) {
      const c = ln[j];
      if (q === "'") { if (c === "'") q = null; continue; }
      if (q === '"') {
        if (c === '\\') { j++; continue; }
        if (c === '"') q = null;
        continue;
      }
      if (c === '\\') { j++; continue; }
      if (c === "'" || c === '"') { q = c; code += 'Q'; continue; }
      if (c === '#' && (j === 0 || /\s/.test(ln[j - 1]))) break;
      if (c === '<' && ln[j + 1] === '<' && ln[j + 2] !== '<') {
        const m = ln.slice(j).match(/^<<(-?)\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\2/);
        if (m) { heredocs.push({ tag: m[3], dash: m[1] === '-' }); j += m[0].length - 1; code += '<<H'; continue; }
      }
      code += c;
    }
    out.push(code);
    if (!q && heredocs.length) inHeredoc = heredocs.shift();
  }
  return out;
}

function exitViolations(text, lang) {
  const out = [];
  const lines = text.split(/\r?\n/);
  if (lang === 'sh') {
    shCodeLines(text).forEach((code, i) => {
      const re = /(?:^|[;{]|&&|\|\||\bthen|\belse|\bdo)\s*exit\s+(\$\(\([^)]*\)\)|[^\s;&|)}]+)/g;
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

if (failures) {
  console.error('quality-runner-self-test: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('quality-runner-self-test: computeTouchedFiles() fallback + covers prefix/glob matching + SKIP status ok');
process.exit(0);
