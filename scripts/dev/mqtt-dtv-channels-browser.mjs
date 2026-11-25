#!/usr/bin/env node
// comment-mutation-proof-exempt: real-page behavioral assertions over config
// GET, accordion interactions and captured POST; no source-text pins.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));
const web = path.join(root, 'www/network_config');
const output = path.join(root, '.tmp/dtv-auto-channels-tests');
mkdirSync(output, { recursive: true });
const { chromium } = createRequire(import.meta.url)('playwright');
const caps = JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['-B', '-c',
  'import json,sys; sys.path.insert(0,sys.argv[1]); from bridge_dtv_catalog import configuration_capability; print(json.dumps(configuration_capability()))',
  path.join(root, 'opt/sa02m-modbus-mqtt')], { encoding: 'utf8' }));
const keys = caps.sensor_keys;
assert.equal(keys.length, 28);
const omitted = ['pressure_bme280_mmhg', 'pressure_bme680_mmhg', 'altitude_bme280',
  'altitude_bme680', 'etoh_zmod', 'input_pb2'];
const auto = { id: 'dtv-COM3-15', type: 'dtv', port: '/dev/COM3', address: 15,
  fast_modbus: true, custom: { keep: 'original' } };
const explicit = { id: 'dtv-COM4-1', type: 'dtv', port: '/dev/COM4', address: 1,
  sensors_present: [...keys.filter(k => !omitted.includes(k)), 'buzzer', 'leds'],
  poll_sensors_s: 7, poll_presence_s: 4, fast_modbus: false };
const unrelated = { id: 'led-COM2-1', type: 'led', port: '/dev/COM2', address: 1, custom: 17 };
const origin = 'http://dtv.test';
const clone = value => JSON.parse(JSON.stringify(value));

async function fixture(browser, { lang = 'ru', theme = 'dark', width = 1600,
  variant = 'sa02m-1eth', devices = [auto, explicit, unrelated], capability = caps } = {}) {
  const context = await browser.newContext({ viewport: { width, height: 1000 } });
  await context.addCookies([{ name: 'session_token', value: 'fixture', url: origin },
    { name: 'sa02m_csrf', value: 'fixture-csrf', url: origin }]);
  await context.addInitScript(({ lang, theme }) => {
    localStorage.setItem('sa02m-lang', lang); localStorage.setItem('sa02m-theme', theme);
  }, { lang, theme });
  const initial = { devices: clone(devices), mqtt: { broker: '127.0.0.1' } };
  let stored = clone(initial), failSave = false, liveDelay = 0, failedResponses = 0;
  const posts = [], commands = [], requests = [], errors = [], lifecycle = [], servicePosts = [];
  let saving = false, operation = null, bridgeActive = true;
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url());
    requests.push(url.href);
    assert.equal(url.origin, origin, 'all traffic stays in the local HTTP fixture');
    if (url.pathname.startsWith('/cgi-bin/') || url.pathname.startsWith('/api/')) {
      let json = {};
      if (url.pathname === '/cgi-bin/mqtt_config.cgi') {
        if (req.method() === 'POST') {
          assert.equal(saving, true, 'passive rendering cannot save config');
          posts.push(req.postDataJSON());
          const { restart, ...body } = posts[posts.length - 1];
          assert.equal(restart, false, 'explicit apply persists without an uncorrelated background restart');
          assert.equal(req.headers()['x-sa02m-csrf'], 'fixture-csrf');
          lifecycle.push('save');
          if (failSave) {
            failedResponses++;
            return route.fulfill({ status: 503, json: { ok: false, error: 'fixture_unavailable' } });
          }
          stored = clone(body); json = { ok: true };
        } else json = { ...stored, capabilities: { dtv_channels: capability } };
      } else if (url.pathname === '/cgi-bin/mqtt_live.cgi') {
        if (liveDelay) await new Promise(resolve => setTimeout(resolve, liveDelay));
        json = { ok: true, device: url.searchParams.get('device'), source: 'cache',
          controls: { temp_ext: 22, temp_hdc1080: 21, buzzer: 0, leds: 1 }, errors: {} };
      } else if (url.pathname === '/cgi-bin/mqtt_set.cgi') {
        commands.push({ body: req.postData(), csrf: req.headers()['x-sa02m-csrf'] }); json = { ok: true };
      } else if (url.pathname === '/cgi-bin/services_ctrl.cgi') {
        if (req.method() === 'POST') {
          assert.equal(saving, true, 'passive rendering cannot control services');
          assert.equal(failSave, false, 'a refused config save cannot restart the bridge');
          const body = req.postDataJSON();
          const action = servicePosts.length % 2 === 0 ? 'stop' : 'start';
          assert.deepEqual(body, { id: 'mqtt-bridge', action });
          assert.equal(req.headers()['x-sa02m-csrf'], 'fixture-csrf');
          if (action === 'start') assert.ok(operation?.listed, 'start waits for correlated stop and stopped-state confirmation');
          servicePosts.push(body);
          operation = { ...body, op_id: servicePosts.length.toString(16).padStart(32, '0'), result: false, listed: false, painted: false };
          lifecycle.push('post:' + action);
          json = { ok: true, pending: true, id: body.id, action, op_id: operation.op_id };
        } else if (url.searchParams.has('result')) {
          assert.ok(operation);
          assert.equal(url.searchParams.get('id'), operation.id);
          assert.equal(url.searchParams.get('op_id'), operation.op_id, 'results are correlated with this exact command');
          assert.equal(operation.result, false, 'successful fixture operation needs no retry');
          operation.result = true; bridgeActive = operation.action === 'start';
          lifecycle.push('result:' + operation.action);
          json = { ok: true, id: operation.id, action: operation.action, op_id: operation.op_id };
        } else {
          if (operation?.result && !operation.listed) {
            operation.listed = true; lifecycle.push('state:' + operation.action);
          }
          json = { ok: true, services: [{ id: 'mqtt-bridge', installed: true,
            active: bridgeActive ? 'active' : 'inactive', enabled: bridgeActive, user_disabled: !bridgeActive }] };
        }
      } else if (url.pathname === '/cgi-bin/mqtt_status.cgi') {
        if (operation?.action === 'start' && operation.listed && !operation.painted) {
          operation.painted = true; lifecycle.push('mqtt:start');
        }
        json = { mosquitto_active: 1, mosquitto_disabled: 0,
          bridge_active: bridgeActive ? 1 : 0, bridge_disabled: bridgeActive ? 0 : 1, clients_connected: 2 };
      } else if (url.pathname === '/cgi-bin/status.cgi') json = { hw_variant: variant };
      else assert.notEqual(req.method(), 'POST', 'no unrelated mutation from the DTV fixture');
      return route.fulfill({ json });
    }
    try { await route.fulfill({ path: path.join(web, url.pathname === '/' ? 'index.html' : url.pathname) }); }
    catch { await route.fulfill({ status: 404, body: '' }); }
  });
  const page = await context.newPage();
  page.setDefaultTimeout(7000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.goto(origin + '/index.html');
  await page.evaluate(() => window.switchTab('mqtt'));
  await page.locator('#mqtt-device-tbody tr').first().waitFor();
  const open = async id => {
    const before = servicePosts.length;
    const body = page.locator('[id="acc-body-' + id + '"]');
    if (!await body.isVisible()) await body.locator('..').locator('.mqtt-accordion-header').click();
    await body.waitFor({ state: 'visible' });
    assert.equal(servicePosts.length, before, 'opening an accordion sends no lifecycle command');
    return body;
  };
  const save = async expected => {
    const before = lifecycle.length, beforePosts = servicePosts.length, previous = clone(stored);
    saving = true;
    const response = page.waitForResponse(r => r.url().endsWith('/mqtt_config.cgi') && r.request().method() === 'POST');
    await page.locator('#mqtt-save-btn').click(); await response;
    await page.locator('#mqtt-save-btn:not([disabled])').waitFor();
    if (expected) assert.deepEqual(posts[posts.length - 1], { ...expected, restart: false });
    if (failSave) {
      assert.deepEqual(lifecycle.slice(before), ['save']);
      assert.equal(servicePosts.length, beforePosts);
      assert.deepEqual(stored, previous, 'failed persistence leaves previous config intact');
    } else {
      assert.deepEqual(lifecycle.slice(before), ['save', 'post:stop', 'result:stop', 'state:stop',
        'post:start', 'result:start', 'state:start', 'mqtt:start']);
      assert.equal(servicePosts.length, beforePosts + 2);
      assert.ok(operation.painted && bridgeActive, 'apply completes only after the independent fresh MQTT readback');
      assert.equal(await page.locator('#mqtt-bridge-badge').innerText(), lang === 'ru' ? 'Мост активен' : 'Bridge Active');
      assert.equal(await page.locator('#mqtt-broker-badge').innerText(), lang === 'ru' ? 'Работает' : 'Running');
    }
    saving = false;
  };
  const counts = async (id, io, poll) => {
    const row = page.locator('#mqtt-device-tbody tr[data-device-id="' + id + '"]');
    assert.equal(await row.locator('.mqtt-io-count').textContent(), io);
    assert.equal(await row.locator('.mqtt-poll-count').textContent(), poll);
  };
  const finish = async () => {
    assert.deepEqual(errors, Array(failedResponses).fill('Failed to load resource: the server responded with a status of 503 (Service Unavailable)'));
    assert.ok(!requests.some(u => /mqtt_scan|mqtt_tcp_probe/.test(u)));
    await context.close();
  };
  return { page, open, save, counts, finish, posts, commands, initial,
    fail: value => { failSave = value; }, delay: value => { liveDelay = value; } };
}

async function primary(browser, lang, theme, width, variant) {
  const f = await fixture(browser, { lang, theme, width, variant });
  const { page } = f, autoText = lang === 'en' ? 'Auto' : 'Авто';
  await f.counts(auto.id, autoText, autoText);
  await f.counts(explicit.id, '24/30', '27');
  const otherBody = await f.open(explicit.id);
  assert.equal(await otherBody.locator('[data-dtv-interval=poll_sensors_s]').inputValue(), '7');
  assert.equal(await otherBody.locator('[data-dtv-interval=poll_presence_s]').inputValue(), '4');
  let body = await f.open(auto.id);
  assert.equal(await body.locator('[data-dtv-sensor]').count(), 28);
  assert.equal(await body.locator('[data-dtv-output]').count(), 2);
  assert.equal(await body.locator('[data-dtv-output] input[type=checkbox]').count(), 0);
  assert.match(await body.locator('[data-dtv-policy]').textContent(), lang === 'ru' ? /автоматический/ : /automatically/);
  for (const key of omitted) {
    const cb = body.locator('[data-dtv-sensor="' + key + '"]');
    assert.equal(await cb.isChecked(), true);
    assert.ok((await cb.getAttribute('aria-label')).length > 2);
  }
  assert.equal(await body.locator('[data-dtv-interval=poll_sensors_s]').inputValue(), '10');
  assert.equal(await body.locator('[data-dtv-interval=poll_presence_s]').inputValue(), '2');
  const dimensions = await body.locator('[data-dtv-sensor]').evaluateAll(nodes => nodes.map(n => {
    const r = n.getBoundingClientRect(); return { x: r.x, right: r.right, width: r.width, screen: innerWidth };
  }));
  assert.ok(dimensions.length && dimensions.every(r => r.width > 0 && r.x >= 0 && r.right <= r.screen), 'checkboxes are visible and inside the viewport: ' + JSON.stringify(dimensions));
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'no horizontal page overflow');
  if (lang === 'en') {
    assert.doesNotMatch(await body.locator('[data-dtv-policy]').textContent(), /[а-яё]/i);
    assert.doesNotMatch((await body.locator('.widget-title').allTextContents()).join(' '), /[а-яё]/i);
    for (const input of await body.locator('.mqtt-ch-widget-sys input[type=text]').all()) {
      assert.doesNotMatch(await input.inputValue(), /[а-яё]/i);
    }
  }
  const prefix = [lang, theme, width, variant].join('-');
  await body.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, 'auto-' + prefix + '.png') });
  await body.locator('..').locator('.mqtt-accordion-header').click(); body = await f.open(auto.id);
  // Rebuilding via a real tab round trip reloads config and reconstructs both bodies.
  await page.evaluate(() => window.switchTab('dashboard'));
  await page.evaluate(() => window.switchTab('mqtt'));
  await f.open(explicit.id); body = await f.open(auto.id);
  await f.counts(auto.id, autoText, autoText); await f.counts(explicit.id, '24/30', '27');
  await f.save(f.initial);
  assert.deepEqual(f.commands, []);
  await page.evaluate(theme => document.documentElement.setAttribute('data-theme', theme === 'dark' ? 'light' : 'dark'), theme);
  await page.evaluate(theme => document.documentElement.setAttribute('data-theme', theme), theme);
  f.delay(150);
  const delayedLive = page.waitForResponse(r => r.url().includes('/mqtt_live.cgi?device=' + auto.id));
  await body.locator('..').locator('.mqtt-accordion-header').click();
  body = await f.open(auto.id);
  await body.locator('[data-dtv-sensor=temp_hdc1080]').uncheck();
  await delayedLive;
  assert.equal(await body.locator('[data-dtv-sensor=temp_hdc1080]').isChecked(), false);
  const expected = clone(f.initial); expected.devices[0].sensors_present = keys.filter(k => k !== 'temp_hdc1080');
  await f.counts(auto.id, '29/30', '32');
  await f.save(expected);
  await page.screenshot({ path: path.join(output, 'explicit-' + prefix + '.png') });
  assert.deepEqual(f.commands, []);
  const commandResponse = page.waitForResponse(r => r.url().endsWith('/mqtt_set.cgi'));
  await body.locator('[data-dtv-output=buzzer] button').click();
  await commandResponse;
  assert.deepEqual(f.commands, [{ body: 'device=dtv-COM3-15&control=buzzer&value=1', csrf: 'fixture-csrf' }]);
  await f.finish();
  console.log('PASS DTV primary ' + prefix);
}

async function edgeCases(browser) {
  for (const mask of [undefined, null, [], ['temp_ext', 'future_sensor', 'buzzer', 'leds'], ['future_only']]) {
    const dev = { ...auto }; delete dev.fast_modbus;
    if (mask !== undefined) dev.sensors_present = mask;
    const f = await fixture(browser, { devices: [dev, unrelated] });
    const body = await f.open(dev.id);
    await f.save(f.initial);
    assert.deepEqual(f.commands, []);
    if (mask && mask.length && mask[0] === 'temp_ext') {
      // Last recognized sensor cannot become the ALL sentinel, even with unknown keys.
      await body.locator('[data-dtv-sensor=temp_ext]').evaluate(node => node.click());
      assert.equal(await body.locator('[data-dtv-sensor=temp_ext]').isChecked(), true);
      await f.page.locator('#mqtt-toast').filter({ hasText: 'Нужно оставить хотя бы один датчик' }).waitFor();
      await f.page.screenshot({ path: path.join(output, 'last-toggle.png') });
      await f.save(f.initial);
      assert.equal(await body.locator('[data-dtv-sensor=temp_hdc1080]').isChecked(), false);
      await body.locator('[data-dtv-sensor=temp_hdc1080]').check();
      const added = clone(f.initial);
      added.devices[0].sensors_present.push('temp_hdc1080');
      await f.save(added);
      await body.locator('[data-dtv-sensor=temp_ext]').uncheck();
      const expected = clone(f.initial);
      expected.devices[0].sensors_present = ['future_sensor', 'buzzer', 'leds', 'temp_hdc1080'];
      await f.save(expected);
    } else if (Array.isArray(mask) && !mask.length) {
      await f.counts(dev.id, '30/30', '33');
      await body.locator('[data-dtv-sensor=temp_hdc1080]').uncheck();
      const expected = clone(f.initial); expected.devices[0].sensors_present = keys.filter(k => k !== 'temp_hdc1080');
      f.fail(true); await f.save(expected);
      await f.page.locator('#mqtt-bridge-control-status').filter({ hasText: 'fixture_unavailable' }).waitFor();
      f.fail(false); await f.save(expected);
      // Deliberate sys edits create only their own record and changed property.
      await body.locator('.mqtt-ch-widget-sys input[type=checkbox]').first().uncheck();
      expected.devices[0].channels = { sys: [{ key: 'uptime_s', enabled: false }] };
      await f.save(expected);
      await body.locator('.mqtt-ch-widget-sys input[type=text]').nth(1).fill('Custom MCU');
      expected.devices[0].channels.sys.push({ key: 'mcu_temp', label: 'Custom MCU' });
      await f.save(expected);
    }
    await f.finish();
  }
  for (const capability of [null, { ...caps, sensor_keys: ['bad/key'] }]) {
    const f = await fixture(browser, { capability });
    const body = await f.open(auto.id);
    assert.equal(await body.locator('[data-dtv-sensor]').count(), 0);
    assert.match(await body.locator('[data-dtv-policy]').textContent(), /недоступны/);
    await f.counts(explicit.id, '—', '—');
    await f.save(f.initial); assert.deepEqual(f.commands, []); await f.finish();
  }
  const disabled = { ...auto, sensors_present: ['temp_ext'], poll_sensors_s: 0,
    channels: { sys: [{ key: 'mcu_temp', enabled: false, label: 'Disabled MCU' }] } };
  const d = await fixture(browser, { devices: [disabled] });
  const disabledBody = await d.open(auto.id);
  assert.equal(await disabledBody.locator('.mqtt-ch-widget-sys input[type=checkbox]').nth(1).isChecked(), false);
  assert.equal(await disabledBody.locator('[data-dtv-interval=poll_sensors_s]').inputValue(), '0');
  await d.counts(auto.id, '3/30', '5'); await d.save(d.initial); await d.finish();
  // A future valid key is safely rendered and retained without a JS label entry.
  const f = await fixture(browser, { capability: { ...caps, sensor_keys: [...keys, '__proto__'] } });
  const body = await f.open(auto.id);
  assert.equal(await body.locator('[data-dtv-sensor="__proto__"]').getAttribute('aria-label'), '__proto__');
  await f.save(f.initial); await f.finish();
  console.log('PASS DTV ALL/null/missing/explicit/unknown/last-toggle/sys/errors/unavailable/future cases');
}

const browser = await chromium.launch({ headless: true });
try {
  for (const lang of ['ru', 'en']) for (const theme of ['dark', 'light']) for (const width of [1600, 500]) {
    await primary(browser, lang, theme, width, width === 1600 ? 'sa02m-1eth' : 'sa02m-2eth');
  }
  await edgeCases(browser);
} finally { await browser.close(); }
