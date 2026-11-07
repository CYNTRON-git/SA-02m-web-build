// comment-mutation-proof-exempt: Chromium executes full shipped scripts and actual HTTP request flows.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {chromium} from './node_modules/playwright/index.mjs';
const www = new URL('../../www/network_config/', import.meta.url);
let kind = 15, raw = 500, writes = [], readError = false;
const names = {15: '4TO6DI', 3: '12AO', 12: '6AI2AO'};
const caps = {15: [4, 6, 4, 0], 3: [0, 0, 12, 0], 12: [0, 0, 2, 6]};
const device = () => ({address: 2, baudrate: 9600, signature: names[kind], module_type: kind, serial: 123});
function snapshot() {
  const [max_do, max_di, max_ao, max_ai] = caps[kind];
  return {kind: 'mr', snapshot_detail: 'full', info: device(), network: {address: 2, baudrate: 9600, parity: 'N', stopbits: 1},
    mr: {module: {code: kind, name: names[kind], max_do, max_di, max_ao, max_ai},
      do: {bits: Array(max_do).fill(false)}, di: {values: Array(max_di).fill(0)},
      ao: {current_raw: Array(max_ao).fill(raw), current_volts: Array(max_ao).fill(raw / 100),
        setpoint: Array(max_ao).fill(raw), safe: Array(max_ao).fill(0), safe_holding_regs: Array.from({length: max_ao}, (_, i) => 503 + i)},
      ai: {channels: Array.from({length: max_ai}, (_, i) => ({channel: i + 1, register_base: 400 + 7 * i,
        sensor_code: 34, measured_raw: 1250, scaled_raw: 1250, calibration: 0, limit_low: 0, limit_high: 10000}))},
      relay: {}, mcu: {}, inactivity_s: 0, inactivity_known: true}};
}
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    let data = {ok: true, jobs: [], entries: []};
    if (url.pathname.endsWith('/ports')) data = {ports: [{key: 'COM4', exists: true, reserved: true, busy_pids: [], active_services: []}]};
    if (url.pathname.endsWith('/ports/release')) data = {ok: true, port: {reserved: true}};
    if (url.pathname.includes('/device_config/')) {
      if (readError) {res.writeHead(409); res.end(JSON.stringify({error: 'no reply'})); return;}
      if (!url.pathname.endsWith('/snapshot')) {
        let body = ''; for await (const chunk of req) body += chunk;
        const command = JSON.parse(body); writes.push(command);
        if (command.reg === 33) raw = command.value;
      }
      data = {ok: true, ...snapshot()};
    }
    if (url.pathname.endsWith('/mqtt_live.cgi')) data = {ok: true, controls: {module_type: names[kind], ao_1: String(raw)}, units: {ao_1: kind === 15 ? '‰' : 'V'}};
    if (url.pathname.endsWith('/mqtt_set.cgi')) {
      let body = ''; for await (const chunk of req) body += chunk;
      const command = Object.fromEntries(new URLSearchParams(body)); writes.push(command); raw = Number(command.value);
    }
    res.end(JSON.stringify(data)); return;
  }
  try {
    const file = new URL('.' + url.pathname, www); assert.ok(file.href.startsWith(www.href));
    let data = await readFile(file);
    if (url.pathname.endsWith('/flasher.js')) data = data.toString().replace(/\}\)\(\);\s*$/, 'window.mrTest = {state, openConfigModal, closeConfigModal, stopConfigPolling, refreshConfigSnapshot};\n})();');
    if (url.pathname.endsWith('/mqtt.js')) data = data.toString() + `
      window.mqttUnitsTest = {prefetchDeviceLive, pending: _aoPending, mount(type) {
        const dev = {id: 'units', type: 'mr02m', module_type: type, channels: {}};
        _config = {devices: [dev]};
        const holder = document.createElement('div'); holder.id = 'acc-body-units';
        holder.appendChild(appendMr02mAoGroup(dev, dev.channels, 1)); document.body.appendChild(holder);
        _accordionBuilt.add(dev.id);
      }};`;
    res.setHeader('Content-Type', url.pathname.endsWith('.js') ? 'text/javascript' : url.pathname.endsWith('.css') ? 'text/css' : url.pathname.endsWith('.svg') ? 'image/svg+xml' : 'text/html');
    res.end(data);
  } catch {res.writeHead(404); res.end();}
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await chromium.launch({headless: true});
let failures = 0;
try {
  for (const type of [15, 3, 12]) {
    kind = type; raw = 500; writes = []; readError = false;
    const context = await browser.newContext({viewport: {width: type === 3 ? 500 : 1600, height: 1000}});
    await context.addCookies([{name: 'session_token', value: 'test', url: origin}]);
    const page = await context.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(origin + '/index.html');
    await page.evaluate(dev => {teardownStatusPolling(); switchTab('flasher'); mrTest.state.devices = [dev];}, device());
    await page.locator('#flasher-port').selectOption('COM4');
    await page.evaluate(async () => {await mrTest.openConfigModal(0); mrTest.stopConfigPolling();});
    const check = async (label, fn) => {try {await fn(); console.log('PASS', type, label);} catch (e) {failures++; console.error('FAIL', type, label, e.message);}};
    if (type === 12) {
      await check('six AI/two AO tabs and no invented wiring', async () => {
        assert.equal(await page.locator('[data-config-tab^="ai_"]').count(), 6);
        assert.equal(await page.locator('[data-config-tab^="ao_"]').count(), 2);
        await page.locator('[data-config-tab="ai_6"]').click();
        assert.equal(await page.locator('#flasher-config-body svg[data-wiring]').count(), 0);
      });
    }
    await page.locator('[data-config-tab="ao_1"]').click();
    await check('opening tabs never writes outputs', async () => assert.equal(writes.length, 0));
    for (const value of [0, 1, 500, 1000]) {
      raw = value; await page.evaluate(() => mrTest.refreshConfigSnapshot(true, 'panel'));
      await check('flasher raw ' + value, async () => {
        const expected = type === 15 ? (value / 10).toFixed(1) + ' %' : (value / 100).toFixed(2) + ' В';
        assert.equal(await page.locator('#cfg-mr-ao-live-1').innerText(), expected);
        assert.match(await page.locator('[data-config-tab="ao_1"]').innerText(), new RegExp(type === 15 ? '%' : 'В'));
      });
    }
    await page.locator('#cfg-mr-ao-set-1').fill('650');
    raw = 500; await page.evaluate(() => mrTest.refreshConfigSnapshot(true, 'panel'));
    assert.equal(await page.locator('#cfg-mr-ao-set-1').inputValue(), '650');
    await page.locator('[data-mr-ao-save="1"]').click();
    await page.waitForFunction(() => !mrTest.state.configBusy);
    await check('flasher write remains raw', async () => assert.ok(writes.some(c => c.reg === 33 && c.value === 650)));
    await page.evaluate(() => mrTest.closeConfigModal());
    await page.evaluate(type => mqttUnitsTest.mount(type), type);
    const beforeMqtt = writes.length;
    for (const value of [0, 1, 500, 1000]) {
      raw = value; await page.evaluate(() => mqttUnitsTest.prefetchDeviceLive('units'));
      await check('MQTT raw ' + value, async () => {
        assert.equal(await page.locator('[data-live="units:ao_1"]').innerText(), (value / (type === 15 ? 10 : 100)).toFixed(type === 15 ? 1 : 2));
        assert.equal(await page.locator('[data-unit="units:ao_1"]').innerText(), type === 15 ? '%' : 'В');
      });
    }
    await check('MQTT poll never writes output', async () => assert.equal(writes.length, beforeMqtt));
    await page.locator('[data-ao-input="units:ao_1"]').fill('650');
    raw = 500; await page.evaluate(() => mqttUnitsTest.prefetchDeviceLive('units'));
    assert.equal(await page.locator('[data-ao-input="units:ao_1"]').inputValue(), '650');
    const commandResponse = page.waitForResponse(response => response.url().endsWith('/mqtt_set.cgi'));
    await page.locator('[data-ao-input="units:ao_1"]').press('Enter');
    await page.waitForFunction(() => !!mqttUnitsTest.pending['units:ao_1']);
    await commandResponse;
    await page.evaluate(() => mqttUnitsTest.prefetchDeviceLive('units'));
    await check('MQTT raw command and readback', async () => {
      assert.deepEqual(writes.slice(beforeMqtt), [{device: 'units', control: 'ao_1', value: '650'}]);
      assert.equal(await page.evaluate(() => !!mqttUnitsTest.pending['units:ao_1']), false);
    });
    await check('no page errors', async () => assert.deepEqual(errors, []));
    await context.close();
  }
} finally {await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));}
if (failures) process.exit(1);
