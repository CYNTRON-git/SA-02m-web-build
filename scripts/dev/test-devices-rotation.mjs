#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-devices — standalone Node test for the ДТВ per-sensor rotation index
   math and the chart wheel-zoom ladder + debounce, in devices.js.
   ───────────────────────────────────────────────────────────────────────────
   The project has no JS unit runner (only js-syntax = `node --check`). This is
   stdlib-only — no framework, no npm dep — the same graceful posture the js-unit
   and ui-layout rows keep. Each function under test is extracted from the SHIPPED
   devices.js source by brace-matching and evaluated with the closure identifiers
   it needs injected (ZOOM_LADDER for nextZoomRange, fake timers for the debounce),
   so the test exercises the real source, not a copy.

   Under test:
   • dtvRotationIndex(len, tick) — the shared-tick modulo the card cycles KPIs by.
     Mutation proof: change `((k % n) + n) % n` to a bare `k % n` and the negative-
     tick case goes red.
   • nextZoomRange(range, deltaY) — one wheel step along the 1ч…30д ladder.
   • debounceTrailing(fn, ms) — a fast scroll fires ONE trailing refetch.
   • integration: several rapid wheel steps move the range the right way and
     schedule exactly one refetch.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEVICES_JS = join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'devices.js');
const SRC = readFileSync(DEVICES_JS, 'utf8');

function extractFn(src, name) {
  const start = src.indexOf('function ' + name);
  if (start < 0) throw new Error('function ' + name + '() not found in devices.js');
  const open = src.indexOf('{', start);
  if (open < 0) throw new Error('no body brace for ' + name + '()');
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name + '()');
}

// The ladder the source defines; asserted below so a source rename is caught.
const ZOOM_LADDER = ['1h', '6h', '24h', '7d', '30d'];

const dtvRotationIndex = new Function(
  extractFn(SRC, 'dtvRotationIndex') + '\nreturn dtvRotationIndex;'
)();
const nextZoomRange = new Function(
  'ZOOM_LADDER',
  extractFn(SRC, 'nextZoomRange') + '\nreturn nextZoomRange;'
)(ZOOM_LADDER);

// Fake timers so the debounce is deterministic (no wall-clock wait).
let timers = [];
let nextId = 1;
const fakeSetTimeout = (fn) => { const id = nextId++; timers.push({ id, fn }); return id; };
const fakeClearTimeout = (id) => { timers = timers.filter((t) => t.id !== id); };
const flushTimers = () => { const t = timers; timers = []; t.forEach((x) => x.fn()); };
const debounceTrailing = new Function(
  'setTimeout', 'clearTimeout',
  extractFn(SRC, 'debounceTrailing') + '\nreturn debounceTrailing;'
)(fakeSetTimeout, fakeClearTimeout);

let failures = 0;
function check(cond, msg) {
  if (cond) { console.log('  ok   - ' + msg); }
  else { failures++; console.error('  FAIL - ' + msg); }
}
function eq(a, b, msg) { check(a === b, msg + ' (got ' + JSON.stringify(a) + ')'); }

// Drift guard: the source still defines the ladder we test against.
check(SRC.includes('"1h", "6h", "24h", "7d", "30d"'), 'source ZOOM_LADDER matches test');

/* ── dtvRotationIndex ─────────────────────────────────────────────────────── */
eq(dtvRotationIndex(3, 0), 0, 'tick 0, len 3 → 0');
eq(dtvRotationIndex(3, 1), 1, 'tick 1, len 3 → 1');
eq(dtvRotationIndex(3, 3), 0, 'tick 3, len 3 → 0 (wrap)');
eq(dtvRotationIndex(3, 4), 1, 'tick 4, len 3 → 1');
eq(dtvRotationIndex(1, 7), 0, 'single-sensor roster always index 0 (no motion)');
eq(dtvRotationIndex(0, 5), -1, 'empty roster → -1');
eq(dtvRotationIndex(3, -1), 2, 'negative tick wraps forward (−1 → 2)');
eq(dtvRotationIndex(5, 2), 2, 'tick 2, len 5 → 2');

/* ── nextZoomRange ────────────────────────────────────────────────────────── */
eq(nextZoomRange('1h', -1), '1h', 'zoom-in at floor stays 1h');
eq(nextZoomRange('1h', 1), '6h', 'zoom-out from 1h → 6h');
eq(nextZoomRange('6h', -1), '1h', 'zoom-in from 6h → 1h');
eq(nextZoomRange('24h', 1), '7d', 'zoom-out from 24h → 7d');
eq(nextZoomRange('30d', 1), '30d', 'zoom-out at ceiling stays 30d');
eq(nextZoomRange('30d', -1), '7d', 'zoom-in from 30d → 7d');
// CE calendar mode is off-ladder → a zoom-in enters at the wide end (30d).
eq(nextZoomRange('mtd', -1), '30d', 'off-ladder mtd, zoom-in → 30d');
eq(nextZoomRange('month', 1), '30d', 'off-ladder month, zoom-out clamps 30d');

/* ── debounceTrailing: rapid calls → one trailing fire ─────────────────────── */
{
  let calls = 0;
  const debounced = debounceTrailing(() => { calls++; }, 200);
  for (let i = 0; i < 5; i++) debounced();
  eq(timers.length, 1, 'five rapid calls leave ONE pending timer');
  eq(calls, 0, 'nothing fires before the timer elapses');
  flushTimers();
  eq(calls, 1, 'debounced refetch fires exactly once');
}

/* ── integration: wheel steps move the range + schedule one refetch ───────── */
{
  let range = '1h';
  let refetches = 0;
  const scheduleRefetch = debounceTrailing(() => { refetches++; }, 200);
  // Simulate three fast zoom-out wheel ticks (deltaY > 0).
  for (let i = 0; i < 3; i++) {
    const next = nextZoomRange(range, 10);
    if (next !== range) { range = next; scheduleRefetch(); }
  }
  eq(range, '7d', '1h → 6h → 24h → 7d after three zoom-out steps');
  eq(timers.length, 1, 'three wheel steps leave ONE pending refetch');
  flushTimers();
  eq(refetches, 1, 'debounced refetch fired once for the burst');
}

if (failures) {
  console.error('js-unit-devices: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('js-unit-devices: dtv rotation + wheel-zoom ok');
process.exit(0);
