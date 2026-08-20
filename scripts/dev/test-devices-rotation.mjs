#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-devices — standalone Node test for the ДТВ per-sensor rotation index
   math and the chart CONTINUOUS wheel-zoom window math + debounce, in devices.js.
   ───────────────────────────────────────────────────────────────────────────
   The project has no JS unit runner (only js-syntax = `node --check`). This is
   stdlib-only — no framework, no npm dep — the same graceful posture the js-unit
   and ui-layout rows keep. Each function under test is extracted from the SHIPPED
   devices.js source by brace-matching and evaluated with the closure identifiers
   it needs injected (window bounds + zoom factors for the step math, fake timers
   for the debounce), so the test exercises the real source, not a copy.

   Under test:
   • dtvRotationIndex(len, tick) — the shared-tick modulo the card cycles KPIs by.
     Mutation proof: change `((k % n) + n) % n` to a bare `k % n` and the negative-
     tick case goes red.
   • clampWindowSec(sec) — pins the continuous span to [60 s, 30 d].
   • stepWindowSec(sec, deltaY) — one smooth wheel notch (factor in/out), a ≥1 s
     floor so small windows still move, clamped at both ends.
   • debounceTrailing(fn, ms) — a fast scroll fires ONE trailing refetch.
   • integration: a burst of wheel-in notches shrinks the window toward 60 s and
     schedules exactly one refetch.
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

// The continuous-zoom constants the source defines; asserted below so a source
// rename/retune is caught by this test rather than silently drifting.
const WINDOW_MIN_S = 60;
const WINDOW_MAX_S = 30 * 86400; // 2592000
const ZOOM_IN_FACTOR = 0.8;
const ZOOM_OUT_FACTOR = 1.25;

const dtvRotationIndex = new Function(
  extractFn(SRC, 'dtvRotationIndex') + '\nreturn dtvRotationIndex;'
)();
const clampWindowSec = new Function(
  'WINDOW_MIN_S', 'WINDOW_MAX_S',
  extractFn(SRC, 'clampWindowSec') + '\nreturn clampWindowSec;'
)(WINDOW_MIN_S, WINDOW_MAX_S);
const stepWindowSec = new Function(
  'clampWindowSec', 'ZOOM_IN_FACTOR', 'ZOOM_OUT_FACTOR',
  extractFn(SRC, 'stepWindowSec') + '\nreturn stepWindowSec;'
)(clampWindowSec, ZOOM_IN_FACTOR, ZOOM_OUT_FACTOR);

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

// rangeLabelKey() closes over `calendarMode` + `windowSec`; expose a setter that
// writes those (as Function-body globals the extracted source reads) so the
// x-axis label GRANULARITY tiers can be exercised against the real source.
const rangeLabelBridge = new Function(
  extractFn(SRC, 'rangeLabelKey') +
  '\nreturn { fn: rangeLabelKey, set: function (w, c) { windowSec = w; calendarMode = c; } };'
)();
function labelKey(windowSec, calendarMode) {
  rangeLabelBridge.set(windowSec, calendarMode || null);
  return rangeLabelBridge.fn();
}

let failures = 0;
function check(cond, msg) {
  if (cond) { console.log('  ok   - ' + msg); }
  else { failures++; console.error('  FAIL - ' + msg); }
}
function eq(a, b, msg) { check(a === b, msg + ' (got ' + JSON.stringify(a) + ')'); }

// Drift guard: the source still defines the bounds and factors we test against.
check(SRC.includes('const WINDOW_MIN_S = 60'), 'source WINDOW_MIN_S = 60');
check(SRC.includes('const WINDOW_MAX_S = 30 * 86400'), 'source WINDOW_MAX_S = 30d');
check(SRC.includes('const ZOOM_IN_FACTOR = 0.8'), 'source ZOOM_IN_FACTOR = 0.8');
check(SRC.includes('const ZOOM_OUT_FACTOR = 1.25'), 'source ZOOM_OUT_FACTOR = 1.25');
check(SRC.includes('return "sec"'), 'source defines the sub-minute seconds axis tier');

/* ── rangeLabelKey: x-axis label granularity tiers ────────────────────────── */
// The sub-minute «sec» tier (≤10 min → HH:MM:SS) is the fix: below it every
// minute-resolution tick collapses to the same value. Drop the tier and the
// first three asserts go red.
eq(labelKey(60), 'sec', '1-min window → seconds axis');
eq(labelKey(300), 'sec', '5-min window → seconds axis');
eq(labelKey(600), 'sec', 'at the 10-min boundary → seconds axis');
eq(labelKey(601), '1h', 'just over 10 min → HH:MM');
eq(labelKey(12 * 3600), '1h', 'at 12 h → HH:MM');
eq(labelKey(12 * 3600 + 1), '24h', 'over 12 h → dd.mm HH:MM');
eq(labelKey(7 * 86400), '24h', 'at 7 d → 24h tier');
eq(labelKey(7 * 86400 + 1), '30d', 'over 7 d → dd.mm');
eq(labelKey(300, 'mtd'), 'mtd', 'calendar mode short-circuits the window tiers');

/* ── dtvRotationIndex ─────────────────────────────────────────────────────── */
eq(dtvRotationIndex(3, 0), 0, 'tick 0, len 3 → 0');
eq(dtvRotationIndex(3, 1), 1, 'tick 1, len 3 → 1');
eq(dtvRotationIndex(3, 3), 0, 'tick 3, len 3 → 0 (wrap)');
eq(dtvRotationIndex(3, 4), 1, 'tick 4, len 3 → 1');
eq(dtvRotationIndex(1, 7), 0, 'single-sensor roster always index 0 (no motion)');
eq(dtvRotationIndex(0, 5), -1, 'empty roster → -1');
eq(dtvRotationIndex(3, -1), 2, 'negative tick wraps forward (−1 → 2)');
eq(dtvRotationIndex(5, 2), 2, 'tick 2, len 5 → 2');

/* ── clampWindowSec: both ends pinned ─────────────────────────────────────── */
eq(clampWindowSec(10), WINDOW_MIN_S, 'below floor → 60');
eq(clampWindowSec(WINDOW_MIN_S), WINDOW_MIN_S, 'at floor → 60');
eq(clampWindowSec(3600), 3600, 'in range passes through');
eq(clampWindowSec(9e9), WINDOW_MAX_S, 'above ceiling → 30 d');
eq(clampWindowSec(NaN), WINDOW_MIN_S, 'NaN → floor');
eq(clampWindowSec(3600.4), 3600, 'rounds to integer seconds');

/* ── stepWindowSec: smooth continuous notch ───────────────────────────────── */
// deltaY > 0 = wheel DOWN = zoom OUT (grow); deltaY < 0 = wheel UP = zoom IN.
eq(stepWindowSec(3600, 1), 4500, 'zoom-out from 1h → *1.25 = 4500 s');
eq(stepWindowSec(3600, -1), 2880, 'zoom-in from 1h → *0.8 = 2880 s');
eq(stepWindowSec(WINDOW_MIN_S, -1), WINDOW_MIN_S, 'zoom-in at floor stays 60');
eq(stepWindowSec(WINDOW_MIN_S, 1), 75, 'zoom-out from 60 → 75');
eq(stepWindowSec(WINDOW_MAX_S, 1), WINDOW_MAX_S, 'zoom-out at ceiling stays 30 d');
eq(stepWindowSec(WINDOW_MAX_S, -1), 2073600, 'zoom-in from 30 d → *0.8');

// Monotonic, no-stall: repeated zoom-in strictly shrinks until the 60 s floor.
{
  let w = 3600;
  let stalled = false;
  for (let i = 0; i < 200; i++) {
    const n = stepWindowSec(w, -1);
    if (w > WINDOW_MIN_S && n >= w) { stalled = true; break; } // never stall above floor
    w = n;
  }
  check(!stalled, 'zoom-in never stalls above the floor (≥1 s change each notch)');
  eq(w, WINDOW_MIN_S, 'repeated zoom-in converges to 60 s');
}
// Repeated zoom-out strictly grows until the 30 d ceiling.
{
  let w = 3600;
  let stalled = false;
  for (let i = 0; i < 200; i++) {
    const n = stepWindowSec(w, 1);
    if (w < WINDOW_MAX_S && n <= w) { stalled = true; break; }
    w = n;
  }
  check(!stalled, 'zoom-out never stalls below the ceiling');
  eq(w, WINDOW_MAX_S, 'repeated zoom-out converges to 30 d');
}

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

/* ── integration: wheel-in notches shrink the window + schedule one refetch ── */
{
  let w = 3600;
  let refetches = 0;
  const scheduleRefetch = debounceTrailing(() => { refetches++; }, 200);
  // Three fast zoom-in wheel notches (deltaY < 0).
  for (let i = 0; i < 3; i++) {
    const next = stepWindowSec(w, -10);
    if (next !== w) { w = next; scheduleRefetch(); }
  }
  check(w < 3600, 'three zoom-in notches shrank the window below 1h');
  eq(timers.length, 1, 'three wheel notches leave ONE pending refetch');
  flushTimers();
  eq(refetches, 1, 'debounced refetch fired once for the burst');
}

if (failures) {
  console.error('js-unit-devices: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('js-unit-devices: dtv rotation + continuous wheel-zoom ok');
process.exit(0);
