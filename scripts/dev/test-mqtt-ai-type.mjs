#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-mqtt-ai-type — the MQTT tab's AI sensor-type select shows the module.

   The bridge publishes the module's type register as is (sensor_types in
   mqtt_live.cgi; bridge_mr02m_map.py _resolve_ai_sensor_type). The select used
   to read 32 valid codes (1-7, 9-15, 21-38 — they overlap the pre-1.0.9.1
   ai_sensor_t enum) as «legacy» and show the YAML type instead, and to show the
   YAML type over a live 0 («Выключен»). Pinned here, against the SHIPPED
   mqtt.js (helpers are cut out of the served source and evaluated, the
   test-mqtt-tcp-dialog / test-alice-gateway-status idiom):
     - for every code 0..42 the select shows the live code, whatever the YAML;
     - an unconfirmed pick (pending) and an open edit still win over live;
     - with no live type the YAML type (and the N-leg mirror of a TC-K /
       3-wire P type) is shown, as before;
     - a live type change that the module did not confirm reverts the channel
       config to the live code for every code, the colliding ones included.
   Non-vacuous: a helper or declaration that stops being found, or a legacy
   table that stops colliding with 32 codes, FAILS.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const MQTT_JS = join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'mqtt.js');

function balancedEnd(src, from, stopAtSemicolon) {
  let depth = 0;
  for (let j = from; j < src.length; j++) {
    const c = src[j];
    if (c === '{' || c === '(' || c === '[') depth++;
    else if (c === '}' || c === ')' || c === ']') {
      depth--;
      if (!stopAtSemicolon && depth === 0) return j + 1;
    } else if (stopAtSemicolon && c === ';' && depth === 0) return j + 1;
  }
  throw new Error('unbalanced source after offset ' + from);
}

function extractFn(src, name) {
  const start = src.indexOf('\nfunction ' + name + '(');
  if (start < 0) throw new Error('function ' + name + '() not found in mqtt.js');
  return src.slice(start + 1, balancedEnd(src, src.indexOf('{', start), false));
}

function extractDecl(src, name) {
  const m = new RegExp('\\n(const|let) ' + name + ' =').exec(src);
  if (!m) throw new Error('declaration ' + name + ' not found in mqtt.js');
  return src.slice(m.index + 1, balancedEnd(src, m.index + 1, true));
}

let failures = 0;
function check(cond, msg) {
  if (cond) console.log('  ok   - ' + msg);
  else { failures++; console.error('  FAIL - ' + msg); }
}

const FNS = [
  'aiTypeControlKey', 'getOrCreateChannel', 'getModuleTypeCode',
  'mr02mAiSupportsPairs', 'mr02mAiPairParent', 'mr02mAiIsNLeg',
  'mr02mAiMirrorTypeToN', 'mr02mAiEffectiveSensorType', 'aiTypeRevertChannelToLive',
];
// Helpers the retired heuristic used: cut in when the source still has them, so
// a reintroduced legacy reading fails by its result, not by a ReferenceError.
const OPTIONAL_FNS = ['isLegacyAiRegisterCode', 'migrateLegacyAiSensorCode'];
const DECLS = [
  'MR02M_TYPES', 'MR02M_AI_PAIR_TYPES', '_AI_TC_K_CODES', '_AI_RTD_3WIRE',
  'AI_SENSOR_LEGACY_ENUM_MIGRATION',
  '_liveSensorTypes', '_aiTypeEditGuard', '_aiTypePending',
];

const src = readFileSync(MQTT_JS, 'utf8');
let api;
try {
  api = new Function([
    'function inferModuleTypeFromName() { return null; }',
    ...DECLS.map(n => extractDecl(src, n)),
    ...FNS.map(n => extractFn(src, n)),
    ...OPTIONAL_FNS.filter(n => src.includes('\nfunction ' + n + '(')).map(n => extractFn(src, n)),
    'return { ' + [...FNS, ...DECLS].join(', ') + ' };',
  ].join('\n'))();
} catch (e) {
  console.error('js-unit-mqtt-ai-type: extraction failed — ' + e.message);
  process.exit(1);
}

const DEV_ID = 'mr02m-ut-ai';
function dev(ai, moduleType = 6) {
  return { id: DEV_ID, type: 'mr02m', module_type: moduleType, channels: { ai } };
}
function reset(live) {
  for (const k of Object.keys(api._liveSensorTypes)) delete api._liveSensorTypes[k];
  for (const k of Object.keys(api._aiTypePending)) delete api._aiTypePending[k];
  api._aiTypeEditGuard.clear();
  if (live) api._liveSensorTypes[DEV_ID] = Object.assign(Object.create(null), live);
}
const shown = (d, ch) => api.mr02mAiEffectiveSensorType(d, ch, d.channels);

// Non-vacuity: the table the old heuristic read must still collide with 32 codes.
const colliding = Object.entries(api.AI_SENSOR_LEGACY_ENUM_MIGRATION)
  .filter(([k, v]) => Number(k) !== v).map(([k]) => Number(k));
check(colliding.length === 32, `legacy table collides with 32 valid codes (got ${colliding.length})`);

console.log('1. the live register code is shown for every code, whatever the YAML');
const wrong = [];
for (let code = 0; code <= 42; code++) {
  for (const yaml of [undefined, 0, 3, 22, 37]) {
    const ai = [{ ch: 1, enabled: true }];
    if (yaml !== undefined) ai[0].sensor_type = yaml;
    const d = dev(ai);
    reset({ ai_1: String(code) });
    const got = shown(d, 1);
    if (got !== code) wrong.push(`live ${code} yaml ${yaml} -> ${got}`);
  }
}
check(wrong.length === 0, `no YAML or legacy reading beats the live code (${wrong.length} wrong: ${wrong.slice(0, 6).join('; ')}${wrong.length > 6 ? '; …' : ''})`);

console.log('2. pending pick and open edit still win over live');
{
  const d = dev([{ ch: 1, enabled: true, sensor_type: 22 }]);
  reset({ ai_1: '37' });
  api._aiTypePending[api.aiTypeControlKey(DEV_ID, 1)] = 40;
  check(shown(d, 1) === 40, 'an unconfirmed pick is shown until the bus confirms it');
  reset({ ai_1: '37' });
  api._aiTypeEditGuard.add(api.aiTypeControlKey(DEV_ID, 1));
  check(shown(d, 1) === 22, 'a select under edit keeps the YAML choice');
}

console.log('3. no live type: YAML, and the N-leg mirror of a TC-K P type');
{
  const d = dev([{ ch: 1, enabled: true, sensor_type: 41 }, { ch: 3, enabled: true, sensor_type: 3 }]);
  reset(null);
  check(shown(d, 3) === 3, 'YAML type with no live data');
  check(shown(d, 2) === 41, 'N leg mirrors a TC-K P type with no live data');
  check(shown(d, 5) === 0, 'no YAML, no live: «Выключен»');
}

console.log('4. an unconfirmed live change reverts the channel to the live code');
{
  const reverted = [];
  for (const code of [3, 5, 22, 37, 0]) {
    const d = dev([{ ch: 1, enabled: true, sensor_type: 40 }]);
    reset({ ai_1: String(code) });
    api.aiTypeRevertChannelToLive(d, 1);
    if (d.channels.ai[0].sensor_type !== code) reverted.push(`${code} -> ${d.channels.ai[0].sensor_type}`);
  }
  check(reverted.length === 0, `revert takes the live code, colliding codes included (${reverted.join('; ')})`);
  const d = dev([{ ch: 1, enabled: true, sensor_type: 40 }]);
  reset({ ai_1: '' });
  api.aiTypeRevertChannelToLive(d, 1);
  check(d.channels.ai[0].sensor_type === 40, 'no live type: the channel config is left alone');
}

if (failures) {
  console.error(`js-unit-mqtt-ai-type: ${failures} failure(s)`);
  process.exit(1);
}
console.log('js-unit-mqtt-ai-type: all checks passed');
