#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it drives the shipped chart helpers and asserts their output, pinning no source line by text; a commented-out line changes the result it measures.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-devices-charts — standalone Node test for the «Устройства» history
   chart helpers in devices.js: legend show/hide state and the СЭ bar charts.
   ───────────────────────────────────────────────────────────────────────────
   Same posture as js-unit-devices (test-devices-rotation.mjs): stdlib only, each
   function under test is brace-extracted from the SHIPPED devices.js and
   evaluated with the closure identifiers it needs injected, so the test runs the
   real source, not a copy. TZ is pinned to the board's zone (Europe/Moscow) so
   the local-clock bucket alignment is deterministic on any host.

   Under test:
   • seriesKey / toggleHiddenKey / visibleSeriesOf — the legend toggle: a click
     hides or shows one series, the LAST visible series can never be hidden, a
     stale hidden set that would hide everything falls back to all visible, and a
     visible series keeps its ORIGINAL colour index (_ci).
   • barModeFor — only СЭ «Мощность» (avg) and «Энергия» (delta) draw bars, and
     only in the single-metric mode; every other chart stays a line.
   • barBucketSec — the bucket table (1 ч→5 мин, 6 ч→15 мин, 24 ч→1 ч, 7 д/30 д/
     calendar→1 сут) and the zoom tiers between presets.
   • barBucketStart / barBucketEnd — local-clock alignment; day buckets start at
     local midnight.
   • bucketAvgPoints — average power per bucket (Pa/Pb/Pc/PΣ each).
   • bucketEnergyDeltaPoints — ΔE per bucket from the counter: monotonic sums, a
     counter reset rebases (no negative bar), a one-sample dip is a glitch (no
     spike on recovery), a gap leaves an empty bucket and its spanning increment
     undrawn.
   • barGroupSlots — the side-by-side grouping of the visible series inside one
     bucket (ordered, non-overlapping, inside the bucket).
   • barYDomain — bars rise from zero, grid lines on round values.
   • xTickLabelFits — dated x labels never collide at 500 px.
   • fmtBucketRange — the hover label «HH:MM–HH:MM» (day bucket ends at 24:00).
   • barStepLabel — every status-line note has an i18n DICT entry.
   ═══════════════════════════════════════════════════════════════════════════ */
process.env.TZ = 'Europe/Moscow';

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEVICES_JS = join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'devices.js');
const SRC = readFileSync(DEVICES_JS, 'utf8');

function extractFn(src, name) {
  const re = new RegExp('function ' + name + '\\s*\\(');
  const m = re.exec(src);
  if (!m) throw new Error('function ' + name + '() not found in devices.js');
  const start = m.index;
  const open = src.indexOf('{', src.indexOf(')', start));
  if (open < 0) throw new Error('no body brace for ' + name + '()');
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name + '()');
}

function load(name, deps) {
  const names = Object.keys(deps || {});
  const vals = names.map((k) => deps[k]);
  return new Function(...names, extractFn(SRC, name) + '\nreturn ' + name + ';')(...vals);
}

let failures = 0;
function check(cond, msg) {
  if (cond) console.log('  ok   - ' + msg);
  else { failures++; console.error('  FAIL - ' + msg); }
}
function eq(a, b, msg) { check(a === b, msg + ' (got ' + JSON.stringify(a) + ')'); }
function close(a, b, msg) {
  check(typeof a === 'number' && Math.abs(a - b) < 1e-9, msg + ' (got ' + JSON.stringify(a) + ', want ' + b + ')');
}

let seriesKey, toggleHiddenKey, visibleSeriesOf, barModeFor, barBucketSec;
let barBucketStart, barBucketEnd, bucketAvgPoints, bucketEnergyDeltaPoints;
let barGroupSlots, fmtBucketRange;
try {
  seriesKey = load('seriesKey');
  toggleHiddenKey = load('toggleHiddenKey');
  visibleSeriesOf = load('visibleSeriesOf', { seriesKey });
  barModeFor = load('barModeFor');
  barBucketSec = load('barBucketSec');
  barBucketStart = load('barBucketStart');
  barBucketEnd = load('barBucketEnd');
  bucketAvgPoints = load('bucketAvgPoints', { barBucketStart, barBucketEnd });
  bucketEnergyDeltaPoints = load('bucketEnergyDeltaPoints', { barBucketStart, barBucketEnd });
  barGroupSlots = load('barGroupSlots');
  fmtBucketRange = load('fmtBucketRange', { fmtTime: load('fmtTime') });
} catch (e) {
  console.error('  FAIL - ' + e.message);
  console.error('js-unit-devices-charts: helpers missing from devices.js');
  process.exit(1);
}

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
// 2026-09-30 12:00 local (Moscow, UTC+3) — a 5-min / 15-min / 1-h aligned instant.
const T = Date.UTC(2026, 8, 30, 9, 0, 0);

console.log('# legend toggle');
{
  const s = [{ field: 'power_w_a' }, { field: 'power_w_b' }, { field: 'power_w_c' }, { field: 'power_w_total' }];
  const keys = s.map((x, i) => seriesKey(x, i));
  eq(new Set(keys).size, 4, 'four distinct series keys');
  eq(seriesKey({ metric: 'voltage', field: 'voltage_a' }, 0), 'voltage:voltage_a', 'overview key carries the metric');
  eq(seriesKey({ label: 'E' }, 3), 'E', 'label fallback');
  eq(seriesKey({}, 3), '3', 'index fallback');

  let hidden = [];
  hidden = toggleHiddenKey(hidden, keys[0], keys);
  eq(JSON.stringify(hidden), JSON.stringify([keys[0]]), 'click hides Pa');
  hidden = toggleHiddenKey(hidden, keys[0], keys);
  eq(hidden.length, 0, 'second click shows Pa again');
  hidden = toggleHiddenKey(toggleHiddenKey(toggleHiddenKey([], keys[0], keys), keys[1], keys), keys[2], keys);
  eq(hidden.length, 3, 'three of four hidden');
  const last = toggleHiddenKey(hidden, keys[3], keys);
  eq(last.length, 3, 'the last visible series cannot be hidden');
  check(last.indexOf(keys[3]) < 0, 'PΣ stays visible');
  const one = toggleHiddenKey([], 'E', ['E']);
  eq(one.length, 0, 'a single-series chart cannot hide its only series');

  const vis = visibleSeriesOf(s, [keys[1]]);
  eq(vis.length, 3, 'hidden Pb is filtered out');
  eq(vis.map((v) => v._ci).join(','), '0,2,3', 'visible series keep their original colour index');
  check(vis[0] !== s[0] && vis[0].field === 'power_w_a', 'visible series are copies (source untouched)');
  eq(visibleSeriesOf(s, keys).length, 4, 'a stale all-hidden set falls back to all visible');
  eq(visibleSeriesOf(s, ['gone:key']).length, 4, 'an unknown hidden key hides nothing');
}

console.log('# bar mode');
{
  eq(barModeFor('ce', 'metric', 'power'), 'avg', 'СЭ power → avg bars');
  eq(barModeFor('ce', 'metric', 'energy_kwh_import'), 'delta', 'СЭ energy → ΔE bars');
  eq(barModeFor('ce', 'metric', 'voltage'), '', 'voltage stays a line');
  eq(barModeFor('ce', 'metric', 'current'), '', 'current stays a line');
  eq(barModeFor('ce', 'metric', 'frequency_hz'), '', 'frequency stays a line');
  eq(barModeFor('ce', 'overview', 'power'), '', '«Общее» stays lines');
  eq(barModeFor('dtv', 'metric', 'power'), '', 'a non-СЭ kind never draws bars');
  eq(barModeFor('mr', 'metric', 'ai_1'), '', 'MR stays a line');
  eq(barModeFor('carel', 'metric', 'supply_temp'), '', 'Carel stays a line');
}

console.log('# bucket table');
{
  eq(barBucketSec(3600, ''), 300, '1 ч → 5 мин');
  eq(barBucketSec(6 * 3600, ''), 900, '6 ч → 15 мин');
  eq(barBucketSec(24 * 3600, ''), 3600, '24 ч → 1 ч');
  eq(barBucketSec(7 * 86400, ''), 86400, '7 д → 1 сут');
  eq(barBucketSec(30 * 86400, ''), 86400, '30 д → 1 сут');
  eq(barBucketSec(3600, 'mtd'), 86400, 'с нач. мес. → 1 сут');
  eq(barBucketSec(3600, 'month'), 86400, 'за месяц → 1 сут');
  eq(barBucketSec(10 * 60, ''), 60, 'zoom < 1 ч → 1 мин');
  eq(barBucketSec(2 * 3600, ''), 300, 'zoom 2 ч → 5 мин (1 ч tier)');
  eq(barBucketSec(12 * 3600, ''), 900, 'zoom 12 ч → 15 мин (6 ч tier)');
  eq(barBucketSec(3 * 86400, ''), 3600, 'zoom 3 д → 1 ч (24 ч tier)');
}

console.log('# bucket alignment');
{
  eq(barBucketStart(T + 7 * MIN + 13000, 300), T + 5 * MIN, '5-min bucket start');
  eq(barBucketEnd(T + 5 * MIN, 300), T + 10 * MIN, '5-min bucket end');
  eq(barBucketStart(T + 59 * MIN, 3600), T, '1-h bucket starts on the local hour');
  const midnight = new Date(2026, 8, 30, 0, 0, 0, 0).getTime();
  eq(barBucketStart(T, 86400), midnight, 'day bucket starts at local midnight');
  eq(barBucketStart(midnight - 1, 86400), new Date(2026, 8, 29, 0, 0, 0, 0).getTime(), 'one ms before midnight is the previous day');
  eq(barBucketEnd(midnight, 86400), new Date(2026, 9, 1, 0, 0, 0, 0).getTime(), 'day bucket ends at the next local midnight');
}

console.log('# power: average per bucket');
{
  const pts = [[T, 10], [T + MIN, 20], [T + 2 * MIN, 30], [T + 5 * MIN, 40], [T + 6 * MIN, NaN], [T + 7 * MIN, 60]];
  const out = bucketAvgPoints(pts, 300);
  eq(out.length, 2, 'two buckets');
  eq(out[0][0], T, 'bucket 1 start');
  close(out[0][1], 20, 'bucket 1 avg (10,20,30)');
  eq(out[0][2], T + 5 * MIN, 'bucket 1 end');
  close(out[1][1], 50, 'bucket 2 avg skips a non-finite sample (40,60)');
  eq(bucketAvgPoints([], 300).length, 0, 'no points → no bars');
  const gap = bucketAvgPoints([[T, 5], [T + 20 * MIN, 7]], 300);
  eq(gap.length, 2, 'a gap leaves the empty buckets out');
}

console.log('# energy: ΔE per bucket from the counter');
{
  const mono = bucketEnergyDeltaPoints(
    [[T, 100.0], [T + MIN, 100.1], [T + 2 * MIN, 100.3], [T + 5 * MIN, 100.4], [T + 6 * MIN, 100.6]], 300);
  eq(mono.length, 2, 'monotonic: two buckets');
  close(Math.round(mono[0][1] * 1e9) / 1e9, 0.3, 'bucket 1 = 0.1 + 0.2 (first sample has no predecessor)');
  close(Math.round(mono[1][1] * 1e9) / 1e9, 0.3, 'bucket 2 = 0.1 (from the adjacent bucket) + 0.2');
  eq(mono[1][2], T + 10 * MIN, 'bucket end carried');

  const reset = bucketEnergyDeltaPoints(
    [[T, 100.0], [T + MIN, 100.2], [T + 2 * MIN, 0.1], [T + 3 * MIN, 0.3], [T + 4 * MIN, 0.5]], 300);
  eq(reset.length, 1, 'reset: one bucket');
  close(Math.round(reset[0][1] * 1e9) / 1e9, 0.6, 'reset rebases: 0.2 before + 0.2 + 0.2 after, the drop counts 0');
  check(reset.every((p) => p[1] >= 0), 'reset: no negative bar');

  const glitch = bucketEnergyDeltaPoints(
    [[T, 100.0], [T + MIN, 100.2], [T + 2 * MIN, 5.0], [T + 3 * MIN, 100.3]], 300);
  close(Math.round(glitch[0][1] * 1e9) / 1e9, 0.3, 'one-sample dip is a glitch: recovery counts only the rise above 100.2');

  const dropDeep = bucketEnergyDeltaPoints(
    [[T, 50.0], [T + MIN, 3.0], [T + 2 * MIN, 1.0], [T + 3 * MIN, 1.5]], 300);
  close(Math.round(dropDeep[0][1] * 1e9) / 1e9, 0.5, 'a reset that keeps falling counts 0, then counts from the new base');
  check(dropDeep.every((p) => p[1] >= 0), 'falling reset: no negative bar');

  const gap = bucketEnergyDeltaPoints(
    [[T, 10.0], [T + MIN, 10.1], [T + 15 * MIN, 12.0], [T + 16 * MIN, 12.2], [T + 25 * MIN, 13.0]], 300);
  eq(gap.map((p) => (p[0] - T) / MIN).join(','), '0,15', 'gap: empty buckets 5 and 10 absent; post-gap bucket 25 has no counted increment');
  close(Math.round(gap[0][1] * 1e9) / 1e9, 0.1, 'gap: bucket before the gap');
  close(Math.round(gap[1][1] * 1e9) / 1e9, 0.2, 'gap: the increment spanning the gap (10.1→12.0) is not drawn');

  const nan = bucketEnergyDeltaPoints([[T, 1], [T + MIN, NaN], [T + 2 * MIN, 1.5]], 300);
  close(nan[0][1], 0.5, 'a non-finite sample is skipped');
  eq(bucketEnergyDeltaPoints([[T, 1]], 300).length, 0, 'a single sample → no bar');

  const midnight = new Date(2026, 8, 30, 0, 0, 0, 0).getTime();
  const day = bucketEnergyDeltaPoints(
    [[midnight - 2 * HOUR, 200], [midnight - HOUR, 201], [midnight + HOUR, 203.5], [midnight + 2 * HOUR, 204]], 86400);
  eq(day.length, 2, 'day buckets: two days');
  close(day[0][1], 1, 'day 1 = 1 kWh');
  close(day[1][1], 3, 'day 2 = 2.5 (across midnight, adjacent day) + 0.5');
  eq(day[1][0], midnight, 'day 2 starts at local midnight');
}

console.log('# line series → bar series');
{
  const toBarSeries = load('toBarSeries', { bucketEnergyDeltaPoints, bucketAvgPoints });
  const counter = [{ field: 'energy_kwh_import', label: 'E', points: [[T, 10], [T + MIN, 10.5], [T + 2 * MIN, 11]] }];
  const e = toBarSeries(counter, 'delta', 300);
  eq(e[0].label, 'ΔE', 'energy bars are labelled ΔE');
  eq(e[0].field, 'energy_kwh_import', 'energy bars keep the series key');
  close(e[0].points[0][1], 1, 'energy bar = ΔE from the counter (1 kWh), not an average of the counter');
  check(e[0]._bars === true && counter[0].points.length === 3, 'bar flag set, source series untouched');
  const preparedBarSeries = load('preparedBarSeries', { barBucketStart, barBucketEnd });
  const midnight = new Date(2026, 8, 30, 0, 0, 0, 0).getTime();
  const prepared = [{ field: 'energy_kwh_import', label: 'E', points: [[midnight, 3.5], [midnight + 86400000, 4.0]] }];
  const pb = preparedBarSeries(prepared, 'delta', 86400);
  eq(pb[0].points.length, 2, 'prepared ΔE keeps both daily buckets');
  close(pb[0].points[0][1], 3.5, 'prepared ΔE is plotted as returned, not differenced again');
  close(pb[0].points[1][1], 4.0, 'second prepared bucket stays 4.0, not 0.5');
  eq(pb[0].label, 'ΔE', 'prepared energy bars are labelled ΔE');
  eq(pb[0].points[0][2], midnight + 86400000, 'prepared day bar ends at the next midnight');
  const power = ['a', 'b', 'c', 'total'].map((ph, i) => ({
    field: 'power_w_' + ph, label: 'P' + ph, points: [[T, 10 * (i + 1)], [T + MIN, 30 * (i + 1)]],
  }));
  const p = toBarSeries(power, 'avg', 300);
  eq(p.length, 4, 'four power bar series (grouped Pa/Pb/Pc/PΣ)');
  eq(p.map((x) => x.points[0][1]).join(','), '20,40,60,80', 'each phase bar = its own bucket average');
  eq(p[0].label, 'Pa', 'power bars keep their phase label');
}

console.log('# power grouping inside a bucket');
{
  const slots = barGroupSlots(100, 200, 4);
  eq(slots.length, 4, 'four side-by-side bars (Pa/Pb/Pc/PΣ)');
  check(slots.every((s) => s.w >= 1), 'every bar at least 1 px wide');
  check(slots[0].x >= 100 && slots[3].x + slots[3].w <= 200, 'the group stays inside its bucket');
  let ordered = true;
  for (let i = 1; i < slots.length; i++) if (!(slots[i].x >= slots[i - 1].x + slots[i - 1].w)) ordered = false;
  check(ordered, 'bars are ordered and do not overlap');
  const three = barGroupSlots(100, 200, 3);
  check(three.length === 3 && three[0].w > slots[0].w, 'hiding one series widens the remaining bars');
  const one = barGroupSlots(100, 200, 1);
  check(Math.abs(one[0].x + one[0].w / 2 - 150) < 1e-9, 'a lone bar is centred in its bucket');
  const narrow = barGroupSlots(0, 2, 4);
  check(narrow.every((s) => s.w >= 1), 'a very narrow bucket still draws ≥1 px bars');
  eq(barGroupSlots(0, 100, 0).length, 0, 'no visible series → no bars');
}

console.log('# bar Y domain');
{
  const barYDomain = load('barYDomain', { niceCeil: load('niceCeil') });
  const d1 = barYDomain(0, 271);
  eq(d1.minY + '..' + d1.maxY, '0..400', 'power 271 W → 0..400 (grid 100 W)');
  const d2 = barYDomain(0, 0.1);
  eq(d2.minY + '..' + d2.maxY, '0..0.2', 'ΔE 0.1 kWh → 0..0.2 (grid 0.05)');
  const d3 = barYDomain(0, 0);
  eq(d3.minY + '..' + d3.maxY, '0..1', 'all-zero bars keep a usable axis');
  const d4 = barYDomain(-50, 200);
  eq(d4.minY + '..' + d4.maxY, '-80..400', 'negative (export) power gets its own nice floor below zero');
  check(barYDomain(20, 30).minY === 0, 'bars always rise from zero (no cropped baseline)');
}

console.log('# x-axis labels at 500 px');
{
  // Real 500 px geometry: pad.l 48, plotW 408, nTicks 5, dated label ≈ 62 px.
  // The first label (left-aligned at 48) spans 48..110; the second, centred on
  // 48 + 81.6, starts at ≈ 98.6 — they collided before this fix.
  const fits = load('xTickLabelFits');
  eq(fits(0, 5, 48, 62, -Infinity, 390), true, 'the first label always draws');
  eq(fits(5, 5, 390, 62, 300, 390), true, 'the last label always draws');
  eq(fits(1, 5, 98.6, 62, 110, 390), false, 'a middle label overlapping the first is skipped');
  eq(fits(2, 5, 180.2, 62, 110, 390), true, 'a middle label clear of both neighbours draws');
  eq(fits(4, 5, 343.4, 62, 242.2, 390), false, 'a middle label running into the last one is skipped');
}

console.log('# hover label');
{
  eq(fmtBucketRange(T, T + 5 * MIN, '1h'), '12:00–12:05', 'sub-day range on a 1 ч chart');
  eq(fmtBucketRange(T, T + HOUR, '24h'), '30.09 12:00–13:00', 'date prefix on a 24 ч chart');
  const midnight = new Date(2026, 8, 30, 0, 0, 0, 0).getTime();
  eq(fmtBucketRange(midnight, new Date(2026, 9, 1).getTime(), '30d'), '30.09 00:00–24:00', 'a day bucket ends at 24:00');
}

console.log('# status note i18n');
{
  // Every bar-step note the status line can show has an EN entry in the i18n DICT
  // (the note is a composite string, so the DOM observer cannot translate it).
  const I18N = readFileSync(join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'i18n.js'), 'utf8');
  const barStepLabel = load('barStepLabel');
  const tiers = [60, 300, 900, 3600, 86400];
  for (const s of [60, 600, 3600, 6 * 3600, 86400, 7 * 86400]) {
    check(tiers.indexOf(barBucketSec(s, '')) >= 0, 'window ' + s + ' s maps to a known tier');
  }
  let n = 0;
  for (const mode of ['avg', 'delta']) {
    for (const sec of tiers) {
      const lab = barStepLabel(mode, sec);
      n++;
      check(I18N.indexOf("'" + lab + "':") >= 0, 'DICT has «' + lab + '»');
    }
  }
  eq(n, 10, 'ten notes checked (non-vacuous)');
  eq(barStepLabel('delta', 300), 'расход за 5 мин', 'energy note wording');
  eq(barStepLabel('avg', 86400), 'среднее за сутки', 'power note wording');
  check(I18N.indexOf("'Скрыть с графика':") >= 0 && I18N.indexOf("'Показать на графике':") >= 0, 'DICT has both legend toggle hints');
}

if (failures) {
  console.error('js-unit-devices-charts: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('js-unit-devices-charts: legend toggle + bar buckets + ΔE + grouping ok');
process.exit(0);
