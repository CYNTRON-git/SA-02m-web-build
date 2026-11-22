// comment-mutation-proof-exempt: real page, synthetic HTTP, visible card/control outcomes and late fetch rejection; no source-text assertions.
import {createServer} from 'node:http';
import {readFile, mkdir} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {chromium} from './node_modules/playwright/index.mjs';
const www = new URL('../../www/network_config/', import.meta.url);
const out = new URL('../../.ai-dev/worklogs/quiet-dashboard-browser/', import.meta.url);
let hw, answer, storageAnswer, coordinatorAnswer, held, release, posts = 0, storagePosts = 0, requests = 0;
const requestPaths = [];
function packet() {
  const now = Date.now() / 1000, parts = {blocks: {time: 0}};
  for (const part of ['priority','storage','uptime','network','load','system','services','hardware','rs485']) {
    parts[part] = {_status_meta: {measured_at: now, age_s: 0, ttl_s: 6, sample_started_uptime: now}};
  }
  Object.assign(parts.priority, {cpu_usage: 24, temp_c: 45, ram_total_kb: 524288, ram_used_kb: 131072, ram_free_kb: 393216, ram_pct: 25, swap_total_kb: 0, disk_total_kb: 8000000, disk_used_kb: 2000000, disk_free_kb: 6000000, disk_pct: 25});
  Object.assign(parts.network, {eth0_ip: '192.0.2.135', eth0_operstate: 'up', eth0_mac: '02:00:00:00:00:01', eth1_ip: '192.0.2.136', eth1_operstate: 'up'});
  Object.assign(parts.system, {cpu_model: 'Allwinner A40i', kernel: '6.6-fixture', armbian: 'Armbian fixture', storage_mount_installed: 1, storage_auto_format: hw.storage_auto_format});
  Object.assign(parts.hardware, hw);
  Object.assign(parts.load, {load1: '0.24', load5: '0.20', load15: '0.18'});
  Object.assign(parts.uptime, {uptime: '2 days, 04:32', uptime_sec: 189120});
  parts.coordinator = {port_coordinator: coordinatorAnswer || {available: true, leases: [], services: {}}, _status_meta: {...parts.services._status_meta}};
  return {served_at: now, served_uptime: now, snapshot: {schema: 1, boot_id: 'quiet-fixture', generated_at: now, generated_uptime: now, parts, errors: {}}};
}
const server = createServer(async (req, res) => {
  requests++;
  const url = new URL(req.url, 'http://localhost');
  requestPaths.push(req.method + ' ' + url.pathname + url.search);
  if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    let result = {ok: true, jobs: [], entries: []};
    if (url.pathname.endsWith('/hw_set.cgi')) {
      posts++;
      let body = ''; for await (const chunk of req) body += chunk;
      const params = new URLSearchParams(body), channel = params.get('channel');
      result = answer || {ok: true, channel, value: Number(params.get('value'))};
      if (result.ok && !result.resetting) hw['hw_' + channel] = result.value;
      if (held) { release = () => res.end(JSON.stringify(result)); return; }
    } else if (url.pathname.endsWith('/storage_format_set.cgi')) {
      storagePosts++;
      result = storageAnswer;
      if (result.ok) hw.storage_auto_format = result.storage_auto_format;
    } else if (url.pathname.endsWith('/status_snapshot.cgi')) result = packet();
    else if (url.pathname.endsWith('/status.cgi')) result = hw;
    else if (url.pathname.endsWith('/variant.cgi')) result = {variant: hw.hw_variant, serial_map: ''};
    else if (url.pathname.endsWith('/csrf_token.cgi')) result = {ok: true, csrf: 'b'.repeat(64)};
    res.end(JSON.stringify(result)); return;
  }
  try {
    const file = new URL('.' + url.pathname, www);
    assert.ok(file.href.startsWith(www.href));
    res.setHeader('Content-Type', url.pathname.endsWith('.js') ? 'text/javascript' : url.pathname.endsWith('.css') ? 'text/css' : url.pathname.endsWith('.svg') ? 'image/svg+xml' : url.pathname.endsWith('.png') ? 'image/png' : 'text/html');
    res.end(await readFile(file));
  } catch { res.writeHead(404); res.end(); }
});
await mkdir(out, {recursive: true});
const browser = await chromium.launch({headless: true});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const noNotices = async page => {
  assert.equal(await page.locator('#dashboard-poll-alert, #port-operation-status, #hw-block-status').count(), 0, 'obsolete dashboard banners must not exist');
  assert.equal(await page.locator('#toast-area .toast').count(), 0, 'dashboard actions must not create popups');
};
let cells = 0;
const accessibilityFailures = [];
async function checkQualityDisclosure(page, selector, expected, touch, label) {
  const card = page.locator(selector).first(), disclosure = card.locator('.dashboard-quality');
  if (await disclosure.count() !== 1) {
    accessibilityFailures.push(label + ': no in-card non-hover quality disclosure');
    return;
  }
  const summary = disclosure.locator('summary'), text = disclosure.locator('.dashboard-quality-text');
  assert.equal(await summary.isVisible(), true, label + ': quality control visible');
  assert.match(await summary.getAttribute('aria-label'), /Состояние|State/);
  assert.equal(await summary.evaluate(el => el.tabIndex), 0, label + ': control is keyboard reachable');
  const target = await summary.boundingBox();
  assert.ok(target.width >= 44 && target.height >= 44, label + ': touch target');
  assert.equal(await text.isVisible(), false, label + ': no unsolicited explanation');
  await summary.focus();
  await summary.press('Enter');
  assert.equal(await text.isVisible(), true, label + ': Enter reveals explanation');
  assert.match(await text.innerText(), expected);
  const openNode = await disclosure.evaluate(el => { window.__qualityOpenNode = el; return el.open; });
  assert.equal(openNode, true);
  await page.evaluate(() => renderStatusFreshness());
  assert.equal(await disclosure.evaluate(el => el === window.__qualityOpenNode && el.open), true, label + ': refresh retains disclosure and open state');
  const geometry = await text.evaluate(el => {
    const a = el.getBoundingClientRect(), b = el.closest('[data-status-parts]').getBoundingClientRect();
    return {left: a.left >= b.left, right: a.right <= b.right + 1, bottom: a.bottom <= b.bottom + 1,
      position: getComputedStyle(el).position};
  });
  assert.ok(geometry.left && geometry.right && geometry.bottom, label + ': explanation remains inside card');
  assert.notEqual(geometry.position, 'absolute');
  assert.notEqual(geometry.position, 'fixed');
  await summary.press('Space');
  assert.equal(await text.isVisible(), false, label + ': Space closes explanation');
  if (touch) {
    await summary.tap();
    assert.equal(await text.isVisible(), true, label + ': touch reveals explanation');
    await summary.tap();
    assert.equal(await text.isVisible(), false, label + ': touch closes explanation');
  }
}
async function checkInlineContrast(error, label) {
  const sample = await error.evaluate(el => {
    const rgb = value => value.match(/[\d.]+/g).map(Number);
    const luminance = c => c.slice(0, 3).map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
      .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
    const style = getComputedStyle(el), foreground = rgb(style.color);
    let parent = el, background;
    while (parent) {
      const value = rgb(getComputedStyle(parent).backgroundColor);
      if (value.length === 3 || value[3] === 1) { background = value; break; }
      parent = parent.parentElement;
    }
    if (!background || (foreground.length > 3 && foreground[3] !== 1)) throw new Error('Expected opaque text/background');
    const a = luminance(foreground), b = luminance(background);
    return {ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05), foreground, background, size: style.fontSize};
  });
  console.log('INLINE_CONTRAST', label, JSON.stringify(sample));
  if (sample.ratio < 4.5) accessibilityFailures.push(label + ': inline error contrast ' + sample.ratio.toFixed(5) + ' < 4.5');
}
try {
  for (const theme of ['dark', 'light']) for (const width of [1600, 500]) {
    const variant = width === 500 ? 'sa02m-2eth' : 'sa02m-1eth';
    hw = {hw_variant: variant, hw_backend: 'i2c_expander', hw_configured: 1, hw_i2c_busy: 0,
      hw_i2c_expander_absent: 0, hw_pin_do: variant === 'sa02m-1eth' ? 1 : 0,
      hw_pin_beeper: 1, hw_pin_alarm_led: 1, hw_pin_usb_power: 1,
      hw_do: 0, hw_beeper: 0, hw_alarm_led: 0, hw_usb_power: 1, storage_mount_installed: 1, storage_auto_format: 0};
    answer = null; coordinatorAnswer = null; held = false; release = null;
    const context = await browser.newContext({viewport: {width, height: 1000}, hasTouch: width === 500});
    await context.addCookies([{name: 'session_token', value: 'test', url: origin}, {name: 'sa02m_csrf', value: 'a'.repeat(64), url: origin}]);
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/index.html');
    await page.evaluate(({theme, hw}) => {
      teardownStatusPolling(); document.documentElement.setAttribute('data-theme', theme);
      sa02mI18n.setLang('ru'); applyHardwareStatus(hw);
    }, {theme, hw});
    await noNotices(page);
    // Storage belongs to System. Its success and failure notices are retained.
    await page.evaluate(() => {switchTab('system'); teardownStatusPolling(); applySystemStatus({storage_mount_installed: 1, storage_auto_format: 0});});
    storageAnswer = {ok: true, storage_auto_format: 1};
    const storageBefore = storagePosts;
    await page.locator('#storage-format-toggle').click();
    await page.waitForFunction(() => document.getElementById('storage-format-toggle').dataset.storageOn === '1');
    assert.equal(storagePosts, storageBefore + 1);
    assert.equal(await page.locator('#toast-area .toast').count(), 1);
    await page.evaluate(() => {document.getElementById('toast-area').textContent = '';});
    storageAnswer = {ok: false, error: 'fixture_storage_failure'};
    await page.locator('#storage-format-toggle').click();
    await page.waitForFunction(() => document.querySelector('#toast-area .toast.error'));
    assert.equal(await page.locator('#storage-format-toggle').getAttribute('data-storage-on'), '1');
    await page.evaluate(() => {switchTab('dashboard'); teardownStatusPolling();});
    await noNotices(page);
    await page.evaluate(() => {
      ['do', 'beeper', 'alarm_led', 'usb_power'].forEach(channel => setHwChannelStatus(channel, ''));
      fetchStatusSnapshot(true);
    });
    await page.waitForFunction(() => document.getElementById('cpu-val').textContent.includes('24') && statusFreshness.priority.state === 'fresh');
    await page.evaluate(() => { window.scrollTo(0, 0); document.querySelector('.main').scrollTop = 0; });
    assert.match(await page.locator('#cpu-val').innerText(), /24/);
    assert.equal(await page.locator('#tab-dashboard .dash-grid').evaluate(el => el.previousElementSibling.classList.contains('page-header')), true, 'no banner gap between the heading and cards');
    const quietRequests = requests;
    await page.evaluate(() => {toast('fixture suppressed dashboard notice');});
    assert.equal(requests, quietRequests, 'notification suppression itself makes no HTTP request');
    await noNotices(page);
    await page.waitForFunction(() => getComputedStyle(document.getElementById('tab-dashboard')).opacity === '1');
    await page.screenshot({path: new URL(`normal-${theme}-${width}.png`, out).pathname, fullPage: true});
    // Real reset handler: only the existing restoration timer is used.
    answer = {ok: true, resetting: true, reset_sec: 0.1};
    const usbBefore = posts;
    await page.locator('#hw-usb-reset-btn').click();
    await page.waitForFunction(() => document.getElementById('hw-usb-reset-btn').classList.contains('hw-active-blue'));
    assert.equal(await page.locator('#hw-usb-st').getAttribute('class'), 'hw-status-val off');
    assert.equal(posts, usbBefore + 1);
    await noNotices(page);
    await page.waitForFunction(() => !document.getElementById('hw-usb-reset-btn').classList.contains('hw-active-blue'));
    assert.equal(await page.locator('#hw-usb-reset-btn').isDisabled(), false);
    await page.waitForFunction(() => !_statusSnapshotBusy && !_statusFetchInFlight && !statusMainRoundBusy && !mainBundleRefreshQueued);
    answer = null;
    coordinatorAnswer = {available: false, error: 'snapshot_unavailable', leases: [], services: {}};
    await page.evaluate(() => fetchStatusSnapshot(true));
    await page.waitForFunction(() => !_statusSnapshotBusy && _lastPortCoordinator && _lastPortCoordinator.available === false);
    await page.evaluate(() => {
      noteStatusSuccess('priority', {_status_meta: {age_s: 100, ttl_s: 1}});
      noteStatusFailure('network', new Error('fixture failure'));
      statusFreshness.storage = {warming: true, receivedAt: performance.now(), sourceAgeMs: null};
      renderStatusFreshness();
      renderPortCoordinator({available: false, error: 'snapshot_unavailable', leases: [], services: {}});
    });
    assert.equal(await page.locator('[data-status-parts="priority"]').first().getAttribute('data-status-state'), 'stale');
    assert.equal(await page.locator('[data-status-parts="network"]').first().getAttribute('data-status-state'), 'error');
    assert.equal(await page.locator('[data-status-parts="storage"]').first().getAttribute('data-status-state'), 'warming');
    assert.match(await page.locator('.widget-rs485').getAttribute('title'), /Нет свежих данных/);
    await page.evaluate(() => { noteStatusSuccess('load', {}); renderStatusFreshness(); });
    const qualityRequests = requests;
    for (const lang of ['ru', 'en']) {
      await page.evaluate(lang => { sa02mI18n.setLang(lang); renderStatusFreshness(); }, lang);
      for (const [selector, ru, en] of [
        ['[data-status-parts="priority"]', /Данные устарели/, /Stale data/],
        ['[data-status-parts="load"]', /Возраст данных неизвестен/, /Data age unknown/],
        ['[data-status-parts="network"]', /Ошибка обновления/, /Refresh failed/],
        ['.widget-rs485', /Нет свежих данных/, /No fresh/]
      ]) await checkQualityDisclosure(page, selector, lang === 'ru' ? ru : en, width === 500, `${theme}/${width}/${lang}/${selector}`);
      await noNotices(page);
    }
    assert.equal(requests, qualityRequests, 'quality disclosure and language changes make no HTTP requests: ' + JSON.stringify(requestPaths.slice(qualityRequests)));
    await page.evaluate(() => { sa02mI18n.setLang('ru'); renderStatusFreshness(); });
    const recoveredQuality = page.locator('[data-status-parts="priority"]').first().locator('.dashboard-quality');
    if (await recoveredQuality.count()) {
      await recoveredQuality.locator('summary').press('Enter');
      await page.evaluate(() => noteStatusSuccess('priority', {_status_meta: {age_s: 0, ttl_s: 6}}));
      assert.equal(await recoveredQuality.isVisible(), false, 'fresh card has no quality notice/control');
      assert.equal(await recoveredQuality.evaluate(el => el.open), false, 'recovery clears obsolete expanded explanation');
      await page.evaluate(() => noteStatusSuccess('priority', {_status_meta: {age_s: 100, ttl_s: 1}}));
    }
    for (const channel of ['do', 'beeper', 'alarm_led']) {
      if (channel === 'do' && variant === 'sa02m-2eth') continue;
      const btn = page.locator('.hw-btns[data-hw-ch="' + channel + '"] button');
      const before = posts;
      await btn.click();
      await page.waitForFunction(ch => document.querySelector('.hw-btns[data-hw-ch="' + ch + '"] button').classList.contains('hw-on'), channel);
      assert.equal(posts, before + 1);
      await noNotices(page);
      answer = {ok: false, error: 'i2c_busy'};
      await btn.click();
      const error = page.locator('#hw-error-' + channel);
      await error.waitFor({state: 'visible'});
      assert.match(await error.innerText(), /Шина I2C занята/);
      await checkInlineContrast(error, `${theme}/${width}/${channel}`);
      assert.equal(await btn.getAttribute('aria-describedby'), 'hw-error-' + channel);
      assert.equal(await btn.getAttribute('data-cur'), '1', 'failed mutation must retain actual state');
      answer = null;
    }
    await page.screenshot({path: new URL(`${theme}-${width}.png`, out).pathname, fullPage: true});
    // A late dashboard command must stay local even after its tab is hidden.
    for (const response of [{ok: false, error: 'i2c_busy'}, {ok: false, error: 'csrf', error_code: 'E_CSRF', reason: 'no_header'}]) {
      answer = response; held = true; release = null;
      await page.locator('#hw-beeper-btn').click();
      const deadline = Date.now() + 5000;
      while (!release && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
      assert.equal(typeof release, 'function', 'actual hardware request reached isolated server');
      await page.evaluate(() => {switchTab('network'); teardownStatusPolling();});
      release(); held = false;
      await page.waitForFunction(token => document.getElementById('hw-error-beeper').textContent.includes(token), response.error_code ? 'csrf' : 'Шина I2C занята');
      await noNotices(page);
      await page.evaluate(() => {switchTab('dashboard'); teardownStatusPolling();});
    }
    await page.evaluate(() => {
      switchTab('network'); teardownStatusPolling(); toast('fixture other-tab notice', 'success', 60000);
    });
    assert.equal(await page.locator('#toast-area .toast').count(), 1, 'other tabs retain normal notices');
    await page.evaluate(() => {switchTab('dashboard'); teardownStatusPolling();});
    await noNotices(page);
    assert.equal(await page.locator('#tab-dashboard').evaluate(el => el.scrollWidth <= el.clientWidth + 1), true);
    assert.deepEqual(errors, []);
    await context.close(); cells++;
  }
  assert.deepEqual(accessibilityFailures, [], 'non-hover quality access and actual inline error contrast');
  console.log(`PASS quiet-dashboard-browser: ${cells} desktop/mobile/theme cells; real command success/failure, freshness, coordinator, late CSRF, navigation policy`);
} finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
