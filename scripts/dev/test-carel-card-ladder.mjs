#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-carel-card-ladder — the browser copies of the Carel fan ladder,
   setpoint limits and fan-percent floor equal their Python homes.
   ───────────────────────────────────────────────────────────────────────────
   WHY. The fan ladder has ONE home, sa02m_carel.carel_fan (`MODES`,
   `_STEP_RUNGS`, the c.pCOmini percent rungs = steps × FAN_PCT_MAX /
   UARIA_FAN_STEP_MAX from carel_ahu), and the writable setpoint limits live in
   sa02m_carel.controls.SETPOINT_RANGE / SETPOINT_PRECISION. The browser cannot
   import Python, so devices.js (`carelControlModel`, the «Устройства» card)
   and mqtt.js (`CAREL_FAN_PCT_MIN`, the MQTT tab write box) carry copies.
   Before this row a rung moved in Python would leave the card writing the old
   values with every gate green (carel-audit #10: silent drift card vs Alice).
   The values are deliberately NOT moved out of devices.js — they are pinned.

   HOW. Python side: the three modules are read as TEXT with `#` comments
   blanked, and each constant must match exactly one live assignment (a pin
   that matches nothing FAILS — non-vacuity; a commented-out home is "no
   match", never a pass). JS side: `carelControlModel` is extracted from the
   SHIPPED devices.js by brace matching and CALLED for both families, so the
   test compares what the card would really render; `CAREL_FAN_PCT_MIN` is read
   from comment-stripped mqtt.js. Every family/mode pair is compared: ids AND
   values, in order, plus setpoint min/max/step.

   RED/GREEN (2026-10-07, recorded in the commit body): a scratch copy of
   devices.js with the c.pCOmini «high» rung 70 → 75 FAILS naming
   crst/high; the shipped tree is GREEN. Comment-out cases are registered in
   comment-mutation-proof (carel_fan `_STEP_RUNGS`, controls uAria setpoint
   range, carel_ahu FAN_PCT_MIN, devices.js turbo rung, mqtt.js floor).

   Env overrides (scratch-copy RED runs only): CAREL_LADDER_DEVICES_JS,
   CAREL_LADDER_MQTT_JS.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const DEVICES_JS = process.env.CAREL_LADDER_DEVICES_JS
  || join(ROOT, 'www', 'network_config', 'static', 'js', 'devices.js');
const MQTT_JS = process.env.CAREL_LADDER_MQTT_JS
  || join(ROOT, 'www', 'network_config', 'static', 'js', 'mqtt.js');
const CAREL_PKG = join(ROOT, 'opt', 'sa02m-carel', 'sa02m_carel');

let failures = 0;
function check(cond, msg) {
  if (cond) console.log('  ok   - ' + msg);
  else { console.error('  FAIL - ' + msg); failures++; }
}
function eq(got, want, msg) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  check(g === w, g === w ? msg : `${msg} (got ${g}, want ${w})`);
}

/* ── Python homes, by text ──────────────────────────────────────────────── */

// Blank `#` comments (outside quotes is enough for these assignment lines:
// none of the pinned values contains a `#`).
function pyLive(path) {
  return readFileSync(path, 'utf8').split('\n').map((l) => l.replace(/#.*$/, '')).join('\n');
}
function oneMatch(src, re, what) {
  const all = [...src.matchAll(new RegExp(re.source, re.flags.includes('g') ? re.flags : re.flags + 'g'))];
  check(all.length === 1, `${what}: exactly one live assignment (found ${all.length})`);
  return all.length === 1 ? all[0] : null;
}
function pyTuple(text) {
  return text.split(',').map((s) => s.trim()).filter(Boolean)
    .map((s) => s.replace(/^["']|["']$/g, ''));
}

console.log('A. Python homes (sa02m_carel)');
const fanSrc = pyLive(join(CAREL_PKG, 'carel_fan.py'));
const ahuSrc = pyLive(join(CAREL_PKG, 'carel_ahu.py'));
const ctlSrc = pyLive(join(CAREL_PKG, 'controls.py'));

const mModes = oneMatch(fanSrc, /^MODES\s*:[^=]*=\s*\(([^)]*)\)/m, 'carel_fan.MODES');
const mRungs = oneMatch(fanSrc, /^_STEP_RUNGS\s*:[^=]*=\s*\(([^)]*)\)/m, 'carel_fan._STEP_RUNGS');
const num = (src, name) => {
  const m = oneMatch(src, new RegExp('^' + name + '\\s*=\\s*([0-9.]+)\\s*$', 'm'), 'carel_ahu.' + name);
  return m ? Number(m[1]) : NaN;
};
const FAN_PCT_MIN = num(ahuSrc, 'FAN_PCT_MIN');
const FAN_PCT_MAX = num(ahuSrc, 'FAN_PCT_MAX');
const STEP_MAX = num(ahuSrc, 'UARIA_FAN_STEP_MAX');
const mRange = oneMatch(ctlSrc, /^SETPOINT_RANGE\s*=\s*\{([^}]*)\}/m, 'controls.SETPOINT_RANGE');
const mPrec = oneMatch(ctlSrc, /^SETPOINT_PRECISION\s*=\s*([0-9.]+)\s*$/m, 'controls.SETPOINT_PRECISION');

const modes = mModes ? pyTuple(mModes[1]) : [];
const steps = mRungs ? pyTuple(mRungs[1]).map(Number) : [];
check(modes.length === 4 && steps.length === modes.length,
  `ladder parsed: ${modes.length} modes, ${steps.length} rungs (non-vacuity: 4 each)`);
check(Number.isFinite(FAN_PCT_MAX) && Number.isFinite(STEP_MAX) && STEP_MAX > 0,
  'carel_ahu maxima parsed');
const pctRungs = steps.map((s) => (s * FAN_PCT_MAX) / STEP_MAX);
const range = {};
if (mRange) {
  for (const m of mRange[1].matchAll(/FAMILY_(CRST|UARIA)\s*:\s*\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\)/g)) {
    range[m[1].toLowerCase()] = [Number(m[2]), Number(m[3])];
  }
}
check(Boolean(range.crst && range.uaria), 'SETPOINT_RANGE carries both families');
const precision = mPrec ? Number(mPrec[1]) : NaN;

/* ── devices.js carelControlModel, executed ──────────────────────────────── */

function extractFn(src, name) {
  const re = new RegExp('function ' + name + '\\(');
  const m = re.exec(src);
  if (!m) return null;
  let i = src.indexOf('{', m.index), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}' && --depth === 0) return src.slice(m.index, i + 1);
  }
  return null;
}

console.log('B. devices.js carelControlModel (the «Устройства» card)');
const devSrc = readFileSync(DEVICES_JS, 'utf8');
const fnSrc = extractFn(devSrc, 'carelControlModel');
check(Boolean(fnSrc), 'carelControlModel() found in devices.js');
let model = null;
try {
  model = fnSrc ? new Function(fnSrc + '\nreturn carelControlModel;')() : null;
} catch (e) {
  check(false, 'carelControlModel evaluates: ' + e.message);
}
const FAMS = { uaria: steps, crst: pctRungs };
for (const fam of ['uaria', 'crst']) {
  const m = model ? model(fam) : null;
  const ctl = (id) => (m && Array.isArray(m.controls) ? m.controls.find((c) => c.id === id) : null);
  const fan = ctl('fan');
  check(Boolean(fan && Array.isArray(fan.steps) && fan.steps.length), `${fam}: fan control has rungs`);
  eq(fan ? fan.mqtt : null, fam === 'uaria' ? 'fan_step' : 'fan_supply', `${fam}: fan writes its family's control`);
  eq(fan ? fan.steps.map((s) => s.id) : [], modes, `${fam}: rung ids == carel_fan.MODES`);
  const want = FAMS[fam];
  (fan ? fan.steps : []).forEach((s, i) => {
    eq(s.value, want[i], `${fam}/${s.id}: card rung == carel_fan`);
  });
  for (const sp of ['setpoint', 'setpoint_summer']) {
    const c = ctl(sp);
    eq(c ? [c.min, c.max] : null, range[fam] || null, `${fam}/${sp}: min/max == controls.SETPOINT_RANGE`);
    eq(c ? c.step : null, precision, `${fam}/${sp}: step == controls.SETPOINT_PRECISION`);
  }
}

/* ── mqtt.js write-box floor ─────────────────────────────────────────────── */

console.log('C. mqtt.js CAREL_FAN_PCT_MIN (the MQTT tab write box)');
const mqttLive = readFileSync(MQTT_JS, 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))
  .split('\n').map((l) => l.replace(/^\s*\/\/.*$/, '')).join('\n');
const mFloor = oneMatch(mqttLive, /^const CAREL_FAN_PCT_MIN\s*=\s*([0-9.]+)\s*;/m, 'mqtt.js CAREL_FAN_PCT_MIN');
eq(mFloor ? Number(mFloor[1]) : null, FAN_PCT_MIN, 'mqtt.js fan floor == carel_ahu.FAN_PCT_MIN');

if (failures) {
  console.error('js-unit-carel-card-ladder: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('js-unit-carel-card-ladder: ok');
process.exit(0);
