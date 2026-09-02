#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it drives the shipped cookie-clearing helper and asserts its output, pinning no source line by text; a commented-out line changes the result it measures.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit — standalone Node test for clearSessionCookie() in app.js.
   ───────────────────────────────────────────────────────────────────────────
   The project has no JS unit runner (only js-syntax = `node --check`). This is
   a stdlib-only test — no framework, no npm dep — so the quality gate that runs
   it (`js-unit` row, review beat) needs nothing installed beyond Node itself,
   the same graceful posture the ui-layout row keeps for its missing browser.

   Under test: the 401 auto-logout cookie eviction. Through the cloud proxy the
   session cookie is re-scoped to Path=/devcfg/<id>; the device cannot know its
   own cloud <id>, so a fixed 'Path=/' delete misses the real cookie and the
   auto-logout loops. clearSessionCookie() clears session_token at EVERY prefix
   of the current path — one prefix is the real one, the rest are no-ops.

   Mutation proof: revert app.js:62 to the single `document.cookie =
   'session_token=; Path=/; ...'` write (drop the prefix loop) and test A goes
   red — it asserts the deeper Path=/devcfg and Path=/devcfg/<id> deletes that
   only the loop produces.

   The function source is extracted from app.js by brace-matching and evaluated
   with a recording `document.cookie` setter and a stub `window.location`, so
   the test exercises the SHIPPED source, not a copy. (Brace-matching is naive —
   it does not skip braces inside string literals; safe here because the
   function's string literals contain no braces.)
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_JS = join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'app.js');

function extractFn(src, name) {
  const start = src.indexOf('function ' + name);
  if (start < 0) throw new Error('function ' + name + '() not found in app.js');
  const open = src.indexOf('{', start);
  if (open < 0) throw new Error('no body brace for ' + name + '()');
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name + '()');
}

function runClear(pathname) {
  const writes = [];
  const documentStub = {
    set cookie(v) { writes.push(v); },
    get cookie() { return ''; },
  };
  const windowStub = { location: { pathname } };
  const src = extractFn(readFileSync(APP_JS, 'utf8'), 'clearSessionCookie');
  const fn = new Function('window', 'document', src + '\nreturn clearSessionCookie;')(
    windowStub, documentStub);
  fn();
  return writes;
}

/** The Path= value of every write that DELETES session_token (Max-Age=0). */
function deletedPaths(writes) {
  return writes
    .filter((w) => /^session_token=;/.test(w) && /Max-Age=0/.test(w))
    .map((w) => (w.match(/Path=([^;]*)/) || [])[1]);
}

let failures = 0;
function check(cond, msg) {
  if (cond) { console.log('  ok   - ' + msg); }
  else { failures++; console.error('  FAIL - ' + msg); }
}

// Test A — through the cloud, the cookie lives at Path=/devcfg/<id>.
const cloud = deletedPaths(runClear('/devcfg/sa02m-abc/index.html'));
check(cloud.includes('/'), 'cloud path: clears Path=/');
check(cloud.includes('/devcfg'), 'cloud path: clears Path=/devcfg');
check(cloud.includes('/devcfg/sa02m-abc'),
  'cloud path: clears Path=/devcfg/sa02m-abc (the real cookie)');

// Test B — on the LAN the cookie is at Path=/; the root delete must still fire.
const lan = deletedPaths(runClear('/index.html'));
check(lan.includes('/'), 'LAN path: clears Path=/');

if (failures) {
  console.error('js-unit: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('js-unit: clearSessionCookie ok');
process.exit(0);
