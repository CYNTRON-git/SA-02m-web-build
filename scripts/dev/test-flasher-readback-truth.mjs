// comment-mutation-proof-exempt: full shipped flasher script, real HTTP bodies and Chromium DOM.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {chromium} from './node_modules/playwright/index.mjs';
const www = new URL('../../www/network_config/', import.meta.url);
let mode = 'ok', on = true, requests = 0, writes = 0, countersKnown = true;
let globalMutations = 0;
const device = {address: 2, baudrate: 9600, signature: '16DO', module_type: 2, serial: 123};
const snapshot = () => ({kind: 'mr', snapshot_detail: 'full', info: {...device},
  network: {address: 2, baudrate: 9600, parity: 'N', stopbits: 1},
  mr: {module: {code: 2, name: '16DO', max_do: 16, max_di: 0, max_ao: 0, max_ai: 0},
    do: {bits: [on, ...Array(15).fill(false)], counts: countersKnown ? Array(16).fill(7) : [], safe: Array(16).fill(0), timer_words: Array(6).fill(0), redelay: Array(6).fill(0)},
    di: {}, ao: {}, ai: {channels: []}, relay: {}, mcu: {}, inactivity_s: 0}});
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    let data = {ok: true, jobs: [], entries: []};
    if (url.pathname.endsWith('/ports')) data = {ports: [
      {key: 'COM4', exists: true, reserved: false, busy_pids: [], active_services: [], whole_service_scope: []},
      {key: 'COM5', exists: true, reserved: true, busy_pids: ['99'], active_services: []},
    ]};
    if (/\/ports\/(release|restore)$/.test(url.pathname)) {
      globalMutations++;
      data = {ok: false, failed: ['COM5 already owned']};
    }
    if (url.pathname.includes('/device_config/')) {
      requests++;
      assert.equal(req.method, 'POST');
      if (!url.pathname.endsWith('/snapshot')) writes++;
      if (mode.startsWith('stall')) {res.writeHead(mode === 'stall-error' ? 503 : 200); res.write('{'); return;}
      if (mode === 'fail') {res.writeHead(503); res.end(JSON.stringify({ok: false, error: 'bus unavailable'})); return;}
      data = {ok: true, ...snapshot()};
    }
    res.end(JSON.stringify(data)); return;
  }
  try {
    const file = new URL('.' + url.pathname, www); assert.ok(file.href.startsWith(www.href));
    let data = await readFile(file);
    if (url.pathname.endsWith('/flasher.js')) {
      // Expose existing closures only; all request/render/poll code runs unchanged.
      data = data.toString().replace(/\}\)\(\);\s*$/, 'window.mrTest = {state, openConfigModal, closeConfigModal, stopConfigPolling, refreshConfigSnapshot, setConfigBanner};\n})();');
    }
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
    const context = await browser.newContext({viewport: {width, height: 1000}});
    await context.addCookies([{name: 'session_token', value: 'test', url: origin}]);
    await context.addInitScript(() => {
      const original = window.setTimeout;
      window.setTimeout = (fn, ms, ...args) => original(fn, ms === 120000 ? 200 : ms, ...args);
    });
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    mode = 'ok'; on = true; countersKnown = true;
    await page.goto(origin + '/index.html');
    await page.evaluate(({device, theme, lang}) => {
      teardownStatusPolling(); switchTab('flasher');
      document.documentElement.setAttribute('data-theme', theme); sa02mI18n.setLang(lang);
      mrTest.state.devices = [device];
    }, {device, theme, lang});
    await page.locator('#flasher-port').selectOption('COM4');
    await page.evaluate(async () => {await mrTest.openConfigModal(0); mrTest.stopConfigPolling();});
    const check = async (name, fn) => {try {await fn(); console.log('PASS', theme, name);} catch (e) {failures++; console.error('FAIL', theme, name, e.message);}};
    await page.locator('[data-config-tab="do_1"]').click();
    await page.locator('#cfg-mr-do-time-1').fill('77');
    mode = 'fail';
    await page.evaluate(() => mrTest.refreshConfigSnapshot(true, 'panel'));
    await check('failed poll retains last ON and typed input, and marks stale', async () => {
      assert.equal(await page.evaluate(() => mrTest.state.configSnapshot.mr.do.bits[0]), true);
      assert.equal(await page.locator('#cfg-mr-do-time-1').inputValue(), '77');
      assert.equal(await page.locator('#flasher-config-banner').isVisible(), true);
      assert.match(await page.locator('#flasher-config-banner').innerText(), /bus unavailable/);
    });
    mode = 'ok'; on = false; countersKnown = false;
    await page.evaluate(() => mrTest.refreshConfigSnapshot(true, 'panel'));
    await check('successful OFF recovery clears only polling error', async () => {
      assert.equal(await page.evaluate(() => mrTest.state.configSnapshot.mr.do.bits[0]), false);
      assert.equal(await page.locator('#cfg-mr-do-time-1').inputValue(), '77');
      assert.equal(await page.locator('#flasher-config-banner').isVisible(), false);
    });
    await check('missing optional counters display unknown', async () => {
      const row = page.locator('.flasher-config-row').filter({hasText: lang === 'en' ? 'Activation counter' : 'Счетчик включений'});
      assert.equal(await row.locator('strong').innerText(), '—');
    });
    await page.evaluate(() => mrTest.setConfigBanner('write rejected', 'error'));
    mode = 'fail'; await page.evaluate(() => mrTest.refreshConfigSnapshot(true, 'panel'));
    mode = 'ok'; await page.evaluate(() => mrTest.refreshConfigSnapshot(true, 'panel'));
    await check('poll recovery preserves independent write error', async () => assert.match(await page.locator('#flasher-config-banner').innerText(), /write rejected/));
    for (const stalled of ['stall', 'stall-error']) {
      mode = stalled;
      const before = requests;
      const finished = await page.evaluate(async () => Promise.race([
        mrTest.refreshConfigSnapshot(true, 'panel').then(() => true),
        new Promise(resolve => setTimeout(() => resolve(false), 1200)),
      ]));
      await check(stalled + ' body respects total request deadline', async () => {
        assert.equal(finished, true); assert.equal(requests, before + 1);
        assert.equal(await page.evaluate(() => mrTest.state.configBackgroundBusy), false);
      });
      if (!finished) break;
      mode = 'ok';
      await page.evaluate(() => mrTest.refreshConfigSnapshot(true, 'panel'));
      await check('queue recovers after body timeout', async () => assert.equal(requests, before + 2));
    }
    await check('no actuator writes or page errors', async () => {assert.equal(writes, 0); assert.deepEqual(errors, []);});
    await page.evaluate(() => mrTest.closeConfigModal());
    mode = 'fail';
    await page.evaluate(async () => {await mrTest.openConfigModal(0); mrTest.stopConfigPolling();});
    await page.locator('[data-config-tab="do_1"]').click();
    await check('failed first read is unknown, not OFF', async () => {
      assert.equal(await page.locator('#cfg-mr-do-state-1').innerText(), '—');
      assert.match(await page.locator('[data-config-tab="do_1"]').innerText(), /—/);
      assert.equal(await page.locator('#flasher-config-banner').isVisible(), true);
    });
    await page.evaluate(() => mrTest.closeConfigModal());
    await check('modal lifecycle preserves the independent COM5 owner', async () => assert.equal(globalMutations, 0));
    await context.close();
  }
} finally {await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));}
if (failures) process.exit(1);
