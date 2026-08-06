#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   quality-runner-self-test — standalone Node test for run.mjs's `--touched`
   row selection. Two regressions, both of which made `--touched` report a
   FALSE GREEN (a skipped row still prints PASS):

     A. computeTouchedFiles()'s last-resort fallback (the unstaged `git status
        --short` branch, used right after branching with no new commits yet —
        no origin/main, no merge-base diff).
     B. coversToRegex()'s handling of a bare path PREFIX.
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

   No framework — mirrors scripts/dev/test-clear-session-cookie.mjs's
   stdlib-only posture. Spins up a real temp git repo so the test exercises
   the actual fallback path (no origin remote, branch even with main, one
   unstaged modification) rather than a copy of the parsing logic.
   ═══════════════════════════════════════════════════════════════════════════ */
import { execSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { computeTouchedFiles, coversToRegex, fileMatchesCovers } from './run.mjs';

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
