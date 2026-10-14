#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it drives the shipped mqtt.js scan table in a vm sandbox and asserts the rendered result, pinning no source line by text; a commented-out line changes the result it measures.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-mqtt-scan-type — the MQTT-tab scan never guesses a device type.

   Found defect: appendScanDevice pre-selected «МР-02м» for every row the scan
   could NOT identify (`type: unknown`), and one «+ Добавить все» then wrote
   `type: mr02m` + `fast_modbus: true` entries for foreign devices — an MR
   poller and Fast Modbus broadcasts pointed at a device that is no MR-02m.
   The fix: an unidentified row shows «— тип —», its «+ Добавить»
   stays disabled until a type is chosen, «Добавить все» adds only rows that
   carry a type (its count says how many), and addDeviceFromScan refuses an
   empty type outright, on the COM and the gateway path alike. An identified
   row (МР-02м, the MTD262-MB template) keeps its pre-selection. The table's
   own strings («+ Добавить», «+ Добавить все (N)», «Устройство N», the
   placeholder) go through uiT(): section 6 loads the SHIPPED i18n.js in EN.

   Drives the SHIPPED mqtt.js through scripts/dev/mqtt-js-sandbox.mjs (vm +
   minimal fake DOM): beginScanResults + appendScanDevice, the same calls the
   streaming scan makes per device line. Stdlib-only.
   ═══════════════════════════════════════════════════════════════════════════ */
import { loadMqttJs } from './mqtt-js-sandbox.mjs';

let failures = 0;
let checks = 0;
function check(cond, msg) {
  checks++;
  if (cond) console.log('  ok   ' + msg);
  else { failures++; console.log('  FAIL ' + msg); }
}

const COM_TARGET = { via: 'com', port: '/dev/COM1', baud: 9600 };
const GW_TARGET = { via: 'gateway', host: '192.168.1.50', tcpPort: 502 };

function scanTable(devices, { listed = [], target = COM_TARGET, lang = 'ru' } = {}) {
  const sb = loadMqttJs({ lang });
  const results = sb.el('div');
  sb.ids['mqtt-scan-results'] = results;
  sb.run('_config.devices = ' + JSON.stringify(listed) + ';');
  sb.ctx.__devs = devices;
  sb.ctx.__target = target;
  sb.run('beginScanResults(__target); __devs.forEach((d) => appendScanDevice(d));');
  const rows = () => results.querySelectorAll('tr').filter((tr) => tr.querySelector('select'));
  const addAll = () => results.children.find((c) => c.tagName === 'BUTTON') || null;
  return { sb, rows: rows(), rowsNow: rows, addAll };
}

const UNKNOWN = { addr: 7, type: 'unknown', module_type: 0, type_name: 'unknown', signature: '' };
const MR = { addr: 3, type: 'mr02m', module_type: 6, type_name: '6AI6AO', signature: '' };
const MTD = { addr: 9, type: 'template', template: 'mtdx62-mb', module_type: 0,
              type_name: 'MTD262-MB', signature: 'MTDX62-MB', baudrate: 9600 };

console.log('1. an unidentified row is not pre-set to МР-02м');
{
  const { rows } = scanTable([UNKNOWN]);
  check(rows.length === 1, 'one scan row rendered');
  const sel = rows[0].querySelector('select');
  check(sel.value === '', `type select value is "" (got "${sel.value}")`);
  check(sel.options[0] && sel.options[0].value === '' &&
        sel.options[0].textContent === '— тип —',
        'first option is the «— тип —» placeholder');
  const btn = rows[0].querySelector('button');
  check(!!btn && btn.disabled === true, '«+ Добавить» is disabled until a type is chosen');
}

console.log('2. choosing a type enables the row');
{
  const { rows, addAll } = scanTable([UNKNOWN]);
  check(addAll() === null, 'no «Добавить все» while no row carries a type');
  const sel = rows[0].querySelector('select');
  sel.value = 'dtv';
  sel.dispatch('change');
  const btn = rows[0].querySelector('button');
  check(!!btn && btn.disabled === false, '«+ Добавить» enabled after picking ДТВ');
  check(rows[0].querySelector('input').value === 'ДТВ-RS-485', 'name follows the picked type');
  check(!!addAll() && /\(1\)/.test(addAll().textContent),
        `«Добавить все (1)» appears once the row is typed (got "${addAll() && addAll().textContent}")`);
}

console.log('3. an identified row keeps its pre-selection');
{
  const { rows } = scanTable([MR, MTD]);
  const sel = rows[0].querySelector('select');
  check(sel.value === 'mr02m', `МР-02м row pre-set to mr02m (got "${sel.value}")`);
  check(rows[0].querySelector('button').disabled === false, 'its «+ Добавить» is enabled');
  const mtdSel = rows[1].querySelector('select');
  check(mtdSel.value === 'template', `MTD262-MB row pre-set to template (got "${mtdSel.value}")`);
  check(rows[1].querySelector('button').disabled === false, 'the MTD row\'s «+ Добавить» is enabled');
}

console.log('4. «Добавить все» never writes an untyped row');
{
  const { sb, rowsNow, addAll } = scanTable([MR, UNKNOWN]);
  const btn = addAll();
  check(!!btn, '«Добавить все» rendered');
  check(!!btn && /\(1\)/.test(btn.textContent),
        `its count names only the typed row (got "${btn && btn.textContent}")`);
  if (btn) btn.dispatch('click');
  const devs = JSON.parse(sb.run('JSON.stringify(_config.devices)'));
  check(devs.length === 1 && devs[0].type === 'mr02m' && devs[0].address === 3,
        `only the МР-02м row was added (got ${JSON.stringify(devs.map((d) => [d.type, d.address]))})`);
  const unkBtn = rowsNow()[1].querySelector('button');
  check(!!unkBtn && unkBtn.disabled === true,
        'the untyped row keeps its disabled «+ Добавить» (no «listed» check mark for a row never added)');
}

console.log('5. addDeviceFromScan refuses an empty type, COM and gateway alike');
{
  const sb = loadMqttJs();
  sb.run('_config.devices = [];');
  sb.ctx.__dev = UNKNOWN;
  sb.ctx.__gw = GW_TARGET;
  sb.run('addDeviceFromScan(__dev, "", "", "/dev/COM1", 9600, null)');
  check(sb.run('_config.devices.length') === 0, 'no device written for an empty type (COM)');
  sb.run('addDeviceFromScan(__dev, "", "", "", 0, __gw)');
  check(sb.run('_config.devices.length') === 0, 'no device written for an empty type (gateway)');
}

console.log('6. the scan table speaks the UI language');
{
  const ru = scanTable([UNKNOWN]);
  const ruName = ru.rows[0].querySelector('input').value;
  check(ruName === 'Устройство 7', `RU: an unnamed device is «Устройство 7» (got "${ruName}")`);
  const { rows, addAll } = scanTable([MR, UNKNOWN], { lang: 'en' });
  const ph = rows[1].querySelector('select').options[0];
  check(!!ph && ph.textContent === '— type —', `EN: placeholder is «— type —» (got "${ph && ph.textContent}")`);
  const btn = rows[0].querySelector('button');
  check(!!btn && btn.textContent === '+ Add', `EN: row button is «+ Add» (got "${btn && btn.textContent}")`);
  const all = addAll();
  check(!!all && all.textContent === '+ Add all (1)', `EN: «+ Add all (1)» (got "${all && all.textContent}")`);
  const enName = rows[1].querySelector('input').value;
  check(enName === 'Device 7', `EN: an unnamed device is «Device 7» (got "${enName}")`);
}

console.log('7. a gateway row offers only what the bridge accepts over rtu_tcp');
{
  // The capabilities reach the page the real way: loadConfig() over a stubbed
  // GET of mqtt_config.cgi. The list is bridge_bus.capabilities()'s
  // rtu_tcp_types (pinned there by test_bridge_bus.py).
  const RTU_TCP = ['ce02m3', 'dtv', 'led', 'mr02m', 'template'];
  async function gatewayTable(devices, caps, target = GW_TARGET) {
    const sb = loadMqttJs();
    const results = sb.el('div');
    sb.ids['mqtt-scan-results'] = results;
    const body = { mqtt: {}, devices: [] };
    if (caps) body.capabilities = caps;
    sb.ctx.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)) });
    await sb.run('loadConfig()').catch(() => {});
    sb.ctx.__devs = devices;
    sb.ctx.__target = target;
    sb.run('beginScanResults(__target); __devs.forEach((d) => appendScanDevice(d));');
    return results.querySelectorAll('tr').filter((tr) => tr.querySelector('select'))
      .map((tr) => tr.querySelector('select'));
  }
  const vals = (sel) => sel.options.map((o) => o.value).filter(Boolean);
  const caps = { tcp_types: ['template', 'carel'], carel_families: ['crst', 'uaria'],
                 tcp_default_port: 502, rtu_tcp_types: RTU_TCP };

  let [gw] = await gatewayTable([MR], caps);
  check(!vals(gw).includes('carel'), `gateway row offers no Carel (got ${JSON.stringify(vals(gw))})`);
  check(vals(gw).includes('dtv') && vals(gw).includes('mr02m') && vals(gw).includes('template'),
        'gateway row still offers ДТВ, МР-02м and the template');
  check(gw.value === 'mr02m', `an identified МР-02м keeps its pre-selection on the gateway (got "${gw.value}")`);

  [gw] = await gatewayTable([{ addr: 4, type: 'carel', family: 'crst' }], caps);
  check(gw.value === '' && !vals(gw).includes('carel'),
        `a Carel hit on the gateway gets the placeholder, not an unsavable type (got "${gw.value}")`);

  const [com] = await gatewayTable([MR], caps, COM_TARGET);
  check(vals(com).includes('carel'), 'a COM row still offers Carel');

  const [old] = await gatewayTable([MR], { tcp_types: ['template', 'carel'] });
  check(vals(old).length === 6 && vals(old).includes('carel'),
        `an older CGI without rtu_tcp_types: the full list, never an empty select (got ${JSON.stringify(vals(old))})`);
  const [none] = await gatewayTable([MR], null);
  check(vals(none).length === 6, 'no capabilities at all: the full list');
}

console.log(`\n${checks - failures}/${checks} checks passed`);
if (checks < 27) { console.log('FAIL: fewer than 27 checks ran — the harness stopped measuring'); process.exit(1); }
process.exit(failures === 0 ? 0 : 1);
