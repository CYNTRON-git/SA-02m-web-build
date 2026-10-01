#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it drives the shipped Carel control model and asserts its output, pinning no source line by text.
/* Carel widget control model: which commands the card exposes, chart closed. */
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

function load(name) {
  return new Function(extractFn(SRC, name) + '\nreturn ' + name + ';')();
}

let failures = 0;
function check(cond, msg) {
  if (cond) console.log('  ok   - ' + msg);
  else {
    failures++;
    console.error('  FAIL - ' + msg);
  }
}
function eq(a, b, msg) {
  check(a === b, msg + ' (got ' + JSON.stringify(a) + ')');
}

const carelControlModel = load('carelControlModel');
const nearestFanStep = load('nearestFanStep');

function byId(model, id) {
  return model.controls.filter((c) => c.id === id)[0];
}

console.log('# carel control model');
{
  const crst = carelControlModel('crst');
  const uaria = carelControlModel('uaria');
  const unknown = carelControlModel('');
  eq(crst.chartOpen, false, 'c.pCO chart is closed by default');
  eq(uaria.chartOpen, false, 'uAria chart is closed by default');
  eq(unknown.chartOpen, false, 'unknown family chart is closed by default');
  eq(crst.controls.map((c) => c.id).join(','), 'unit_on,setpoint,setpoint_summer,fan', 'c.pCO exposes on/off, both setpoints, fan');
  eq(byId(crst, 'unit_on').mqtt, 'unit_on', 'on/off writes unit_on');
  eq(byId(crst, 'setpoint').mqtt, 'setpoint', 'winter setpoint writes setpoint');
  eq(byId(crst, 'setpoint').step, 0.5, 'setpoint step is 0.5');
  eq(byId(crst, 'setpoint').max, 99, 'c.pCO setpoint max is 99');
  eq(byId(uaria, 'setpoint').max, 50, 'uAria setpoint max is 50');
  eq(byId(crst, 'setpoint_summer').mqtt, 'setpoint_summer', 'summer setpoint has its own command');
  eq(byId(crst, 'fan').mqtt, 'fan_supply', 'c.pCO fan writes fan_supply');
  eq(byId(uaria, 'fan').mqtt, 'fan_step', 'uAria fan writes fan_step');
  eq(byId(crst, 'fan').steps.map((s) => s.value).join(','), '20,40,70,100', 'c.pCO fan rungs are percents');
  eq(byId(uaria, 'fan').steps.map((s) => s.value).join(','), '2,4,7,10', 'uAria fan rungs are steps');
  eq(byId(unknown, 'fan').mqtt, 'fan_supply', 'unknown family does not guess uAria');
  const uSteps = byId(uaria, 'fan').steps;
  const cSteps = byId(crst, 'fan').steps;
  eq(nearestFanStep(uSteps, 6).id, 'high', 'step 6 is high, not medium');
  eq(nearestFanStep(cSteps, 55).id, 'high', '55% ties round up to high');
  eq(nearestFanStep(cSteps, 20).id, 'low', '20% is low');
  check(nearestFanStep(cSteps, null) === null, 'a missing fan reading is not a step');
}

console.log('# chart stays behind the button');
{
  const body = extractFn(SRC, 'buildCarelControlsHtml');
  const chartBtn = extractFn(SRC, 'chartButtonHtml');
  check(body.indexOf('chartButtonHtml()') >= 0, 'Carel card appends the chart button');
  check(body.indexOf('dev-act-row') >= 0 && body.indexOf('dev-act-row') < body.indexOf('chartButtonHtml()'), 'chart button is the left control of the action row');
  check(body.indexOf('ВКЛЮЧЕНА') < 0 && body.indexOf('ВЫКЛЮЧЕНА') >= 0, 'power starts as off, with no on-label baked in');
  const tile = extractFn(SRC, 'carelSetpointTileHtml');
  check(tile.indexOf('dev-kpi--sp') >= 0, 'setpoints occupy the sixth-tile cell');
  check(tile.indexOf('data-season="winter"') >= 0 && tile.indexOf('data-season="summer"') >= 0, 'season is winter and summer');
  check(tile.indexOf('dev-season-mark') >= 0 && tile.indexOf('dev-season-btn') < 0, 'setpoint season icons are not buttons');
  const wire = extractFn(SRC, 'wireCarelControls');
  check(wire.indexOf('data-role="season"') < 0, 'setpoint season icons have no click handler');
  const metrics = extractFn(SRC, 'buildCarelMetricsHtml');
  const seasonTile = extractFn(SRC, 'carelSeasonTileHtml');
  check(metrics.indexOf('season-now') < 0 && metrics.indexOf('carelSeasonTileHtml()') >= 0, 'season is a tile, not a mark on supply');
  check(seasonTile.indexOf('Сезон') >= 0 && seasonTile.indexOf('data-role="season-val"') >= 0 && seasonTile.indexOf('<button') < 0, 'season is its own parameter, not a button');
  const paintSeason = extractFn(SRC, 'paintCarelSeason');
  check(paintSeason.indexOf('ЗИМА') >= 0 && paintSeason.indexOf('ЛЕТО') >= 0 && paintSeason.indexOf('carelSeasonIcon') < 0, 'season value is the words, not an icon');
  const seasonNow = load('carelSeasonNow');
  eq(seasonNow({ season: 0 }), 'winter', 'coil 67 = 0 is winter');
  eq(seasonNow({ season: 1 }), 'summer', 'coil 67 = 1 is summer');
  eq(seasonNow({}), null, 'a missing season flag is not a guess');
  const orderDeviceCards = load('orderDeviceCards');
  const shared = orderDeviceCards([
    { id: 'c1', kind: 'carel' },
    { id: 'd1', kind: 'dtv' },
    { id: 'ce1', kind: 'ce' },
    { id: 'c2', kind: 'carel' },
  ], 3);
  eq(shared.map((d) => d.id).join(','), 'c1,c2,ce1,d1', 'two Carel cards and CE share the first row');
  const filled = orderDeviceCards([
    { id: 'c1', kind: 'carel' },
    { id: 'd1', kind: 'dtv' },
    { id: 'm1', kind: 'mr', ai_count: 12 },
  ], 3);
  eq(filled.map((d) => d.id).join(','), 'c1,m1,d1', 'without CE the row fills with the most loaded card');
  const phone = orderDeviceCards([
    { id: 'c1', kind: 'carel' },
    { id: 'd1', kind: 'dtv' },
    { id: 'ce1', kind: 'ce' },
  ], 1);
  eq(phone.map((d) => d.id).join(','), 'c1,d1,ce1', 'one column keeps the incoming order');
  check(tile.indexOf('setpoint_summer') >= 0 && tile.indexOf('data-f="setpoint"') >= 0, 'tile still writes both setpoints');
  const icon = extractFn(SRC, 'carelSeasonIcon');
  check(icon.indexOf('<svg') >= 0 && icon.indexOf('circle') >= 0, 'season icons are inline SVG');
  check(chartBtn.indexOf('data-role="chart"') >= 0, 'chart button is an explicit control');
  check(body.indexOf('<canvas') < 0 && chartBtn.indexOf('<canvas') < 0, 'control markup has no chart canvas');
  check(SRC.indexOf('mtd_illuminance') >= 0 && SRC.indexOf('mtd_target_distance') >= 0 && SRC.indexOf('mtd_presence') >= 0, 'MTD chart series are still declared');
  check(SRC.indexOf('Задержка выключения') >= 0, 'MTD off-delay label is still on the card');
  const mtd = extractFn(SRC, 'buildMtdMetricsHtml');
  check(mtd.indexOf('mtd_illuminance') >= 0 && mtd.indexOf('mtd_target_distance') >= 0 && mtd.indexOf('mtd_presence') >= 0, 'MTD card still shows the three readings');
}

console.log('# MTD stale refresh must not replace a just-written value');
{
  const mtdDropPending = load('mtdDropPending');
  const mtdSettingDisplay = new Function(
    'mtdDropPending',
    extractFn(SRC, 'mtdSettingDisplay') + '\nreturn mtdSettingDisplay;'
  )(mtdDropPending);
  const pending = { value: 8, until: 5000 };
  eq(mtdSettingDisplay(6, pending, 2, 1000, false, '6.00'), '8.00', 'stale 6 m does not replace a just-written 8 m');
  eq(mtdSettingDisplay(30, { value: 45, until: 5000 }, 0, 1000, false, '30'), '45', 'stale off-delay does not replace 45 s');
  eq(mtdSettingDisplay(8, pending, 2, 1000, false, '8'), '8.00', 'matching poll confirms the written value');
  eq(mtdSettingDisplay(6, pending, 2, 5001, false, '8'), '6.00', 'expired pending gives the register back');
  eq(mtdSettingDisplay(6, null, 2, 1000, true, '7.5'), '7.5', 'a focused field keeps what the operator is typing');
  eq(mtdSettingDisplay(6, null, 2, 1000, false, '8'), '6.00', 'without a pending write the poll paints the register');
  check(mtdDropPending(pending, 6, 2, 1000) === false, 'unconfirmed write stays pending');
  check(mtdDropPending(pending, 8, 2, 1000) === true, 'confirmed write drops the guard');
}

if (failures) {
  console.error('test-device-controls: ' + failures + ' FAILURE(S)');
  process.exit(1);
}
console.log('test-device-controls: ALL OK');
