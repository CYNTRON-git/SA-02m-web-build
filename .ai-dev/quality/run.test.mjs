#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   quality-runner-self-test — standalone Node test for run.mjs's
   computeTouchedFiles() last-resort fallback (the unstaged `git status
   --short` branch, used right after branching with no new commits yet — no
   origin/main, no merge-base diff).
   ───────────────────────────────────────────────────────────────────────────
   Regression covered: the fallback used to `.trim()` the WHOLE multi-line
   `git status --short` output before splitting into lines. Trimming the
   whole blob eats the leading space of the FIRST modified-file line (a
   status line is "XY path" — a 2-char code + a space, e.g. " M path"), which
   desyncs that one entry's `slice(3)` by one character and over-trims its
   path. Effect: `node .ai-dev/quality/run.mjs build --touched` silently
   reported 0/0 passed (every row skipped as "not touched") on a freshly
   branched repo instead of running the real subset — a false green, found
   during the 1.0.5.47 build session on 2026-07-28.

   No framework — mirrors scripts/dev/test-clear-session-cookie.mjs's
   stdlib-only posture. Spins up a real temp git repo so the test exercises
   the actual fallback path (no origin remote, branch even with main, one
   unstaged modification) rather than a copy of the parsing logic.
   ═══════════════════════════════════════════════════════════════════════════ */
import { execSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { computeTouchedFiles } from './run.mjs';

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

if (failures) {
  console.error('quality-runner-self-test: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('quality-runner-self-test: computeTouchedFiles() last-resort fallback ok');
process.exit(0);
