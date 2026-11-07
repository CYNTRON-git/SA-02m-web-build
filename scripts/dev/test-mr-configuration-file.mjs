// comment-mutation-proof-exempt: shipped flasher UI, real HTTP requests and Chromium file inputs.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {chromium} from './node_modules/playwright/index.mjs';
const www = new URL('../../www/network_config/', import.meta.url);
const device = {address: 2, baudrate: 9600, signature: '16DO', module_type: 2, serial: 123};
let safe = false, mode = 'ok', operations = [];
const configuration = () => ({format: 'sa02m-mr-config', schema_version: 1,
  module: {type_code: 2, io_counts: {do: 16, di: 0, ao: 0, ai: 0}, app_version: '1.0.12.22'},
  settings: {do: {channels: [{channel: 1, safe}]}}, coverage: {included: ['do.1.safe'], unavailable: [], unsupported: []}});
const snapshot = () => ({kind: 'mr', snapshot_detail: 'full', info: device,
  network: {address: 2, baudrate: 9600, parity: 'N', stopbits: 1},
  mr: {module: {code: 2, name: '16DO', max_do: 16, max_di: 0, max_ao: 0, max_ai: 0},
    do: {bits: Array(16).fill(false), safe: [Number(safe), ...Array(15).fill(0)], counts: [], timer_words: Array(6).fill(0), redelay: Array(6).fill(0)},
    di: {}, ao: {}, ai: {channels: []}, relay: {}, mcu: {}, inactivity_s: 0}});
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    let data = {ok: true, jobs: [], entries: []};
    if (url.pathname.endsWith('/ports')) data = {ports: [{key: 'COM4', exists: true, reserved: true, busy_pids: [], active_services: []}]};
    if (url.pathname.endsWith('/ports/release')) data = {ok: true, port: {reserved: true}};
    if (url.pathname.includes('/device_config/')) {
      const chunks = []; for await (const chunk of req) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString());
      if (url.pathname.endsWith('/snapshot')) data = {ok: true, ...snapshot()};
      else {
        operations.push({route: url.pathname.split('/').pop(), body});
        if (mode === 'offline') {res.writeHead(409); res.end(JSON.stringify({error: 'Fresh MR identity unavailable'})); return;}
        if (mode === 'stall') {res.writeHead(200); res.write('{'); return;}
        if (url.pathname.endsWith('/export')) {
          data = {ok: true, complete: mode !== 'partial', configuration: configuration()};
          if (mode === 'partial') data.configuration.coverage.unavailable = ['do.2.safe'];
        } else if (url.pathname.endsWith('/import')) {
          const wanted = body.configuration.settings?.do?.channels?.[0]?.safe ?? Boolean(body.configuration.mr?.do?.safe?.[0]);
          data = {ok: true, complete: mode !== 'partial', module: configuration().module, changes: [{path: 'do.1.safe', previous: safe, requested: wanted, expected: wanted}],
            applied: [], unchanged: [], failed: [], remaining: [], unavailable: mode === 'partial' ? ['do.2.safe'] : [], unsupported: [], normalizations: []};
          if (mode === 'legacy_ai') Object.assign(data, {complete: false, apply_blocked: true,
            unsupported: ['ai.1.sensor_code', 'ai.1.limit_low', 'ai.1.filters.tau_ms']});
          if (!body.preview_only) {
            if (mode === 'failure') {data.ok = false; data.complete = false; data.failed = [{path: 'do.1.safe', status: 'unknown'}];}
            else {safe = wanted; data.applied = [{path: 'do.1.safe', observed: safe}];}
          }
        } else data = {ok: true, ...snapshot()};
      }
    }
    res.end(JSON.stringify(data)); return;
  }
  try {
    const file = new URL('.' + url.pathname, www); assert.ok(file.href.startsWith(www.href));
    let data = await readFile(file);
    if (url.pathname.endsWith('/flasher.js')) data = data.toString().replace(/\}\)\(\);\s*$/, 'window.mrTest = {state, openConfigModal, stopConfigPolling};\n})();');
    res.setHeader('Content-Type', url.pathname.endsWith('.js') ? 'text/javascript' : url.pathname.endsWith('.css') ? 'text/css' : url.pathname.endsWith('.svg') ? 'image/svg+xml' : 'text/html');
    res.end(data);
  } catch {res.writeHead(404); res.end();}
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await chromium.launch({headless: true});
let failures = 0;
try {
  for (const [theme, width, lang] of [['dark', 1600, 'ru'], ['light', 500, 'en']]) {
    const context = await browser.newContext({viewport: {width, height: 1000}, acceptDownloads: true});
    await context.addCookies([{name: 'session_token', value: 'test', url: origin}]);
    await context.addInitScript(() => {const timer = window.setTimeout; window.setTimeout = (fn, ms, ...args) => timer(fn, ms === 120000 ? 300 : ms, ...args);});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    safe = false; mode = 'ok'; operations = [];
    await page.goto(origin + '/index.html');
    await page.evaluate(({device, theme, lang}) => {
      teardownStatusPolling(); switchTab('flasher'); document.documentElement.setAttribute('data-theme', theme); sa02mI18n.setLang(lang); mrTest.state.devices = [device];
    }, {device, theme, lang});
    await page.locator('#flasher-port').selectOption('COM4');
    await page.evaluate(async () => {await mrTest.openConfigModal(0); mrTest.stopConfigPolling();});
    const check = async (name, fn) => {try {await fn(); console.log('PASS', theme, name);} catch (error) {failures++; console.error('FAIL', theme, name, error.message);}};
    const selectFile = async data => {
      await page.locator('#cfg-mr-import-file').setInputFiles({name: 'settings.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(data))});
      await page.waitForFunction(() => !mrTest.state.configBusy);
    };
    const legacy = {_meta: {signature: '16DO', serial_hex: '0x0000007B', app_version: '1.0.12.22', exported_at: 'today'}, mr: {inactivity_s: 0, do: {safe: [1]}, relay: {}}};
    await selectFile(legacy);
    await check('legacy import previews all settings without per-register writes', async () => {
      assert.equal(operations.filter(op => op.route === 'holding').length, 0);
      assert.equal(operations.filter(op => op.route === 'import' && op.body.preview_only).length, 1);
      assert.match(await page.locator('#cfg-mr-file-preview').innerText(), /do\.1\.safe/);
    });
    if (!await page.locator('#cfg-mr-file-apply').count()) {await context.close(); continue;}
    await page.locator('#cfg-mr-file-apply').click();
    await page.waitForFunction(() => !mrTest.state.configBusy);
    await check('explicit apply submits once and confirms fresh value', async () => {
      assert.equal(operations.filter(op => op.route === 'import' && !op.body.preview_only).length, 1);
      assert.equal(safe, true);
    });
    const downloaded = page.waitForEvent('download');
    await page.locator('#cfg-mr-export-btn').click();
    const download = await downloaded;
    const exported = JSON.parse(await readFile(await download.path(), 'utf8'));
    await check('export reads the server and roundtrips the applied value', async () => {
      assert.equal(exported.settings.do.channels[0].safe, true);
      assert.equal(operations.filter(op => op.route === 'export').length, 1);
    });
    mode = 'legacy_ai'; await selectFile(configuration());
    await check('unsupported AI restore lists fields and has no partial override', async () => {
      assert.equal(await page.locator('#cfg-mr-file-apply').isDisabled(), true);
      assert.equal(await page.locator('#cfg-mr-file-partial').count(), 0);
      assert.match(await page.locator('#cfg-mr-file-preview').innerText(), /ai\.1\.filters\.tau_ms/);
      assert.match(await page.locator('#cfg-mr-file-preview').innerText(), /только для чтения|read-only/);
    });
    mode = 'partial'; await selectFile(configuration());
    await check('partial file requires explicit acknowledgement', async () => assert.equal(await page.locator('#cfg-mr-file-apply').isDisabled(), true));
    await page.locator('#cfg-mr-file-partial').check();
    await check('partial acknowledgement enables apply', async () => assert.equal(await page.locator('#cfg-mr-file-apply').isDisabled(), false));
    mode = 'failure'; await page.locator('#cfg-mr-file-apply').click(); await page.waitForFunction(() => !mrTest.state.configBusy);
    await check('partial failure remains visible and cannot be automatically resent', async () => {
      assert.match(await page.locator('#cfg-mr-file-preview').innerText(), /unknown|неизвест/i);
      assert.equal(await page.locator('#cfg-mr-file-apply').isDisabled(), true);
    });
    mode = 'stall'; await selectFile(configuration());
    await check('stalled response releases busy and reports unknown outcome', async () => {
      assert.equal(await page.evaluate(() => mrTest.state.configBusy), false);
      assert.equal(await page.locator('#flasher-config-banner').isVisible(), true);
    });
    mode = 'ok'; const before = operations.length;
    await page.locator('#cfg-mr-import-file').setInputFiles({name: 'large.json', mimeType: 'application/json', buffer: Buffer.alloc(49153, 32)});
    await page.waitForFunction(() => !mrTest.state.configBusy);
    await check('oversize rejected before request and no page errors', async () => {assert.equal(operations.length, before); assert.deepEqual(errors, []);});
    await context.close();
  }
} finally {await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));}
process.exitCode = failures ? 1 : 0;
