// comment-mutation-proof-exempt: drives real Chromium through selectors/buttons and verifies response-dependent DOM in both themes/variants.
import {createServer} from 'node:http';
import {readFile, mkdir} from 'node:fs/promises';
import {appendFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const www = new URL('../../www/network_config/', import.meta.url);
const out = new URL('../../.tmp/audit-20261106/ports-browser/', import.meta.url);
function skip(reason) {
  console.log('SKIP ' + reason);
  if (process.env.QUALITY_SKIP_LOG) appendFileSync(process.env.QUALITY_SKIP_LOG, reason + '\n');
  process.exit(77);
}
let pw;
try { pw = await import('./node_modules/playwright/index.mjs'); }
catch { try { pw = await import('playwright'); } catch { skip('port-recovery-browser: Playwright absent'); } }
let reserved = false, count = 5, releaseCalls = 0, restoreCalls = 0;
let restorePending = false, restoreFail = false, coordinatorAvailable = true;
let servicePosts = 0, servicePending = false, serviceActive = true;
const svc = () => ({ok: true, services: [{id: 'vplc', label: 'vPLC', installed: true, active: serviceActive ? 'active' : 'inactive', enabled: serviceActive}]});
const ports = () => Array.from({length: count}, (_, i) => ({
  key: 'COM' + (i + 1), label: 'COM' + (i + 1), device_path: '/dev/COM' + (i + 1), exists: true,
  reserved: reserved && i !== 3, released_services: [], managed_services: ['MQTT'], active_job: null,
  busy_pids: i === 3 ? ['100'] : [], active_services: i === 3 ? ['RS-485 Gateway'] : [],
  restore_pending: restorePending && i < 2, restore_services: restorePending && i < 2 ? ['mplc4.service'] : [],
  restore_error: restorePending && i < 2 ? 'start failed' : '', coordinator_available: coordinatorAvailable,
}));
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    let body = {ok: true, _status_meta: {age_s: 0}, jobs: [], entries: []};
    if (url.pathname.endsWith('/services_ctrl.cgi')) {
      if (req.method === 'POST') {servicePosts++; servicePending = true; body = {ok: true, pending: true, id: 'vplc', action: 'stop', op_id: 'f'.repeat(32)};}
      else if (url.searchParams.has('result')) body = {ok: true, pending: servicePending, id: 'vplc', action: 'stop', op_id: 'f'.repeat(32)};
      else body = svc();
    } else if (url.pathname.endsWith('/ports/release')) {
      releaseCalls++; reserved = true;
      body = {ok: false, port: ports()[3], failed: ['/dev/COM4: UART already leased'],
        reserved_ports: ports().filter(p => p.reserved).map(p => p.device_path),
        manual_reserved_ports: ports().filter(p => p.reserved).map(p => p.device_path)};
    } else if (url.pathname.endsWith('/ports/restore')) {
      restoreCalls++; reserved = false;
      restorePending = restoreFail;
      body = {ok: !restorePending, port: ports()[3], restarted: [],
        failed: restorePending ? ['/dev/COM1: start failed'] : [], pending_restores: restorePending ? ['/dev/COM1', '/dev/COM2'] : []};
    } else if (url.pathname.endsWith('/ports')) body = {ports: ports()};
    res.end(JSON.stringify(body)); return;
  }
  try {
    const file = new URL('.' + url.pathname, www); assert.ok(file.href.startsWith(www.href));
    const data = await readFile(file);
    res.setHeader('Content-Type', url.pathname.endsWith('.js') ? 'text/javascript' : url.pathname.endsWith('.css') ? 'text/css' : url.pathname.endsWith('.svg') ? 'image/svg+xml' : url.pathname.endsWith('.png') ? 'image/png' : 'text/html');
    res.end(data);
  } catch { res.writeHead(404); res.end(); }
});
let browser;
try { browser = await pw.chromium.launch({headless: true}); }
catch (e) { skip('port-recovery-browser: Chromium unavailable: ' + String(e.message).split('\n')[0]); }
await mkdir(out, {recursive: true});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const origin = 'http://127.0.0.1:' + server.address().port;
try {
  for (const theme of ['dark', 'light']) for (const variant of ['sa02m-1eth', 'sa02m-2eth']) for (const width of [1600, 500]) {
    count = variant === 'sa02m-1eth' ? 5 : 4; reserved = false; servicePending = false; serviceActive = true;
    restorePending = false; restoreFail = false; coordinatorAvailable = true;
    const context = await browser.newContext({viewport: {width, height: 1000}});
    await context.addCookies([{name: 'session_token', value: 'test', url: origin}]);
    const page = await context.newPage(); const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(origin + '/index.html');
    await page.evaluate(({theme, variant}) => {
      teardownStatusPolling(); document.documentElement.setAttribute('data-theme', theme);
      applyVariantVisibility(variant); switchTab('flasher'); applyVplcNavLink({svc_vplc_ui: 'active'});
    }, {theme, variant});
    await page.locator('#flasher-port').selectOption('COM4');
    await page.waitForFunction(() => document.getElementById('flasher-port-hint').textContent.includes('Шлюз RS-485'));
    assert.match(await page.locator('#flasher-port-hint').innerText(), /RS-485 Gateway/);
    assert.equal(await page.locator('#nav-vplc .nav-label').innerText(), 'vPLC');
    assert.equal(await page.locator('#nav-vplc').getAttribute('target'), '_blank');
    assert.equal(await page.locator('#nav-vplc').getAttribute('href'), 'vplc/');
    const before = releaseCalls;
    await page.locator('#flasher-release-port-btn').click();
    await page.waitForFunction(() => !document.getElementById('flasher-restore-port-btn').disabled);
    assert.equal(releaseCalls, before + 1);
    assert.match(await page.locator('#flasher-port-hint').innerText(), /COM1/);
    assert.match(await page.locator('#flasher-port-hint').innerText(), /COM4: UART already leased/);
    await page.waitForTimeout(400);
    await page.screenshot({path: new URL(`${theme}-${variant}-${width}-partial.png`, out).pathname.replace(/^\/([A-Za-z]:)/, '$1'), fullPage: true});
    // A new page reconstructs the global restore action from the daemon flags.
    await page.reload();
    await page.evaluate(() => {teardownStatusPolling(); switchTab('flasher');});
    await page.locator('#flasher-port').selectOption('COM4');
    await page.waitForFunction(() => !document.getElementById('flasher-restore-port-btn').disabled);
    const restoreBefore = restoreCalls;
    restoreFail = true;
    await page.locator('#flasher-restore-port-btn').click();
    await page.waitForFunction(() => document.getElementById('flasher-port-hint').textContent.includes('Ожидается восстановление опроса:'));
    assert.equal(reserved, false);
    assert.equal(await page.locator('#flasher-restore-port-btn').isDisabled(), false);
    await page.locator('#flasher-restore-port-btn').click();
    await page.waitForFunction(() => !document.getElementById('flasher-release-port-btn').disabled);
    assert.equal(restoreCalls, restoreBefore + 2);
    await page.reload();
    await page.evaluate(({theme, variant}) => {
      teardownStatusPolling(); document.documentElement.setAttribute('data-theme', theme);
      applyVariantVisibility(variant); switchTab('flasher');
    }, {theme, variant});
    await page.locator('#flasher-port').selectOption('COM4');
    await page.waitForFunction(() => document.getElementById('flasher-port-hint').textContent.includes('Ожидается восстановление опроса:'));
    assert.doesNotMatch(await page.locator('#flasher-port-hint').innerText(), /Ручной резерв:/);
    assert.equal(await page.locator('#flasher-restore-port-btn').isDisabled(), false);
    coordinatorAvailable = false;
    await page.locator('#flasher-refresh-ports-btn').click();
    await page.waitForFunction(() => document.getElementById('flasher-port-hint').textContent.includes('Координатор недоступен'));
    await page.screenshot({path: new URL(`${theme}-${variant}-${width}-restore-pending.png`, out).pathname.replace(/^\/([A-Za-z]:)/, '$1'), fullPage: true});
    coordinatorAvailable = true; restorePending = false; restoreFail = false;
    await page.locator('#flasher-restore-port-btn').click();
    await page.waitForFunction(() => document.getElementById('flasher-restore-port-btn').disabled && !document.getElementById('flasher-release-port-btn').disabled);
    assert.equal(restoreCalls, restoreBefore + 3);
    assert.equal(reserved, false);
    assert.match(await page.locator('#flasher-port-hint').innerText(), /RS-485 Gateway/);
    assert.doesNotMatch(await page.locator('#flasher-port-hint').innerText(), /Ручной резерв:/);
    assert.doesNotMatch(await page.locator('#flasher-port-hint').innerText(), /Ожидается восстановление|start failed/);
    await page.evaluate(() => {switchTab('system'); systemSelectSub('services');});
    page.on('dialog', dialog => dialog.accept());
    const stop = page.locator('[data-svc-id="vplc"][data-svc-action="stop"]');
    await stop.waitFor();
    const postBefore = servicePosts;
    await stop.click();
    await page.waitForFunction(() => !!_svcCtlPending.vplc);
    await page.evaluate(data => renderServicesControl(data), svc());
    assert.equal(await stop.isDisabled(), true);
    assert.equal(await page.locator('[data-svc-id="vplc"][data-svc-action="uninstall"]').isDisabled(), true);
    await stop.evaluate(btn => serviceCtlAction(btn));
    assert.equal(servicePosts, postBefore + 1);
    servicePending = false; serviceActive = false;
    await page.waitForFunction(() => !_svcCtlPending.vplc);
    assert.equal(await page.locator('[data-svc-id="vplc"][data-svc-action="start"]').isDisabled(), false);
    assert.deepEqual(errors, []);
    await context.close();
    console.log(`PASS ${theme}/${variant}/${width}: gateway/partial/restore failure/repeat/reload/unavailable/recovery; service pending survives poll and duplicate click`);
  }
} finally { await browser.close(); server.closeAllConnections(); server.close(); }
