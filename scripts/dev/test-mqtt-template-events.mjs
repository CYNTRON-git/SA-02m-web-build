#!/usr/bin/env node
// Exercise the shipped MQTT accordion and save action across real page reloads.
// The simulated CGI stores exactly what the browser sends. The companion
// test-mqtt-config-transport.sh exercises the authenticated CGI and YAML writer.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const { chromium } = createRequire(import.meta.url)('playwright');
const webRoot = path.resolve(process.env.SA02M_UI_ROOT || fileURLToPath(new URL('../../www/network_config/', import.meta.url)));
const origin = 'http://sa02m.test';
const phase = 'Phase "A"';
const channels = [
  { name: phase, fast_eligible: true, readonly: true },
  { name: 'energy', fast_eligible: true, readonly: true },
  { name: 'external_only', fast_eligible: false, readonly: false, web_write: false },
  { name: 'exceeds_full_topic', fast_eligible: true, readonly: true, max_device_id_bytes: 1 },
  { name: 'trigger_sensitivity', fast_eligible: false, readonly: false, web_write: true },
];
const tcp = { type: 'template', id: '1.line_RTU-4', template: 'fixture', transport: 'tcp',
  host: '192.0.2.1', address: 7 };
const serialTemplate = { type: 'template', id: 'room/strip', template: 'fixture', address: 4,
  port: '/dev/COM4', poll_s: 9, operator_field: { original: true }, fast_modbus: false };
const mtd = { type: 'template', id: 'mtd', template: 'mtdx62-mb', port: '/dev/COM3', address: 1,
  fmb_event_channels: ['device_status'], fast_modbus: true };
const mtdChannels = ['presence_status', 'device_status'].map(name => ({ name, fast_eligible: true, readonly: true }));

async function exercise(browser, lang, theme, id = serialTemplate.id) {
  const context = await browser.newContext();
  try {
    const serial = { ...serialTemplate, id };
    let stored = { devices: [serial, tcp, mtd], topic_prefix: 'existing' };
    let available = true;
    let phaseValue = 223.4;
    let writes = 0;
    await context.addCookies([{ name: 'session_token', value: 'fixture', url: origin }]);
    await context.addInitScript(({ lang, theme }) => {
      localStorage.setItem('sa02m-lang', lang);
      localStorage.setItem('sa02m-theme', theme);
    }, { lang, theme });
    await context.route(origin + '/**', async route => {
      const url = new URL(route.request().url());
      const name = url.pathname;
      if (name.startsWith('/cgi-bin/')) {
        let json = {};
        if (name === '/cgi-bin/mqtt_templates.cgi') {
          json = available ? { ok: true, templates: [{ name: 'fixture', title: 'Fixture', channels },
            { name: 'mtdx62-mb', channels: mtdChannels }] } : { error: 'unavailable' };
        } else if (name === '/cgi-bin/mqtt_live.cgi' && url.searchParams.get('device') === String(serial.id)) {
          json = { ok: true, source: 'cache', device: serial.id, controls: { [phase]: phaseValue, energy: 409 },
            units: { [phase]: 'V', energy: 'Wh' }, errors: {} };
        } else if (name === '/cgi-bin/mqtt_config.cgi') {
          if (route.request().method() === 'POST') {
            const { restart, ...config } = route.request().postDataJSON();
            assert.equal(restart, true);
            if (config.devices.some(device => device.id === '+')) {
              json = { ok: false, error: 'invalid_device', reason: 'invalid_device_id', id: '+' };
            } else {
              stored = config;
              writes++;
              json = { ok: true };
            }
          } else {
            json = stored;
          }
        }
        return route.fulfill({ json });
      }
      try {
        await route.fulfill({ path: path.join(webRoot, name === '/' ? 'index.html' : name) });
      } catch {
        await route.fulfill({ status: 404, body: '' });
      }
    });
    const page = await context.newPage();
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    page.setDefaultTimeout(5000);
    const open = async id => {
      const body = page.locator('[id="acc-body-' + id + '"]');
      await body.locator('..').locator('.mqtt-accordion-header').click();
      await body.waitFor({ state: 'visible' });
      return body;
    };
    const load = async (first = false) => {
      if (first) await page.goto(origin + '/index.html');
      else await page.reload();
      await page.locator('.nav-item[data-tab="mqtt"]').click();
      return open(serial.id);
    };
    const save = async (names, enabled, expectedMtd = mtd) => {
      const before = writes;
      const response = page.waitForResponse(r => r.url().endsWith('/mqtt_config.cgi') && r.request().method() === 'POST');
      await page.locator('#mqtt-save-btn').click();
      await response;
      assert.equal(writes, before + 1);
      assert.deepEqual(stored, { devices: [{ ...serial, fmb_event_channels: names, fast_modbus: enabled }, tcp, expectedMtd],
        topic_prefix: 'existing' });
    };
    const checkbox = (body, name) => body.getByRole('checkbox', { name: 'Fast Modbus — ' + name, exact: true });
    let body = await load(true);
    await checkbox(body, phase).waitFor();
    assert.equal(await checkbox(body, phase).isChecked(), false);
    for (const [name, value, unit] of [[phase, '223.4', lang === 'ru' ? 'В' : 'V'], ['energy', '409', 'Wh']]) {
      const row = body.locator('.mqtt-ch-row').filter({ has: page.getByText(name, { exact: true }) });
      await row.locator('.mqtt-ch-live').filter({ hasText: value }).waitFor();
      await row.locator('.mqtt-ch-unit').filter({ hasText: unit }).waitFor();
      assert.equal(await row.locator('.mqtt-ch-live').innerText(), value);
      assert.equal(await row.locator('.mqtt-ch-unit').innerText(), unit);
    }
    if (typeof id === 'number') {
      phaseValue = 225.6;
      await body.locator('.mqtt-ch-live').filter({ hasText: '225.6' }).waitFor();
      await checkbox(body, phase).check();
      await save([phase], true);
      body = await load();
      assert.equal(await checkbox(body, phase).isChecked(), true);
      assert.equal(stored.devices[0].id, id);
      assert.deepEqual(pageErrors, []);
      console.log('mqtt-template-events: numeric ID ' + id + ' updated on the next poll and survived save/reload');
      return;
    }
    assert.equal(await body.getByRole('checkbox').count(), 2);
    assert.equal(await body.locator('.mqtt-ao-input').count(), 0);
    await checkbox(body, phase).check();
    await checkbox(body, 'energy').check();
    await save([phase, 'energy'], true);
    body = await load();
    assert.equal(await checkbox(body, phase).isChecked(), true);
    assert.equal(await checkbox(body, 'energy').isChecked(), true);
    await checkbox(body, 'energy').uncheck();
    await save([phase], true);
    const ethernet = await open(tcp.id);
    await ethernet.locator('.mqtt-ao-input').waitFor();
    assert.equal(await ethernet.locator('.mqtt-ao-input').count(), 1);
    assert.match(await ethernet.locator('.mqtt-ao-input').locator('..').innerText(), /trigger_sensitivity/);
    assert.equal(await ethernet.getByRole('checkbox').count(), 0);
    available = false;
    body = await load();
    const retry = body.getByRole('button', { name: lang === 'en' ? 'Retry' : 'Повторить', exact: true });
    await retry.waitFor();
    assert.equal(await body.getByRole('checkbox').count(), 0);
    await save([phase], true);
    available = true;
    await retry.click();
    assert.equal(await checkbox(body, phase).isChecked(), true);
    await checkbox(body, phase).uncheck();
    await save([], false);
    body = await load();
    assert.equal(await checkbox(body, phase).isChecked(), false);
    assert.equal(await checkbox(body, 'energy').isChecked(), false);
    let mtdBody = await open(mtd.id);
    assert.equal(await checkbox(mtdBody, 'device_status').isChecked(), true);
    await checkbox(mtdBody, 'presence_status').check();
    await save([], false, { ...mtd, fmb_event_channels: ['device_status', 'presence_status'] });
    await load();
    mtdBody = await open(mtd.id);
    assert.equal(await checkbox(mtdBody, 'device_status').isChecked(), true);
    assert.equal(await checkbox(mtdBody, 'presence_status').isChecked(), true);
    await page.locator('button[onclick="mqttShowAddModal()"]').click();
    await page.locator('#mqtt-add-type').selectOption('template');
    await page.locator('#mqtt-add-template').selectOption('fixture');
    await page.locator('#mqtt-add-id').fill('+');
    await page.locator('#mqtt-add-modal button[onclick="mqttConfirmAdd()"]').click();
    await page.locator('#mqtt-save-btn').click();
    const message = lang === 'ru' ? 'Проверьте путь в ID устройства. Символы + и # и служебные пути MQTT запрещены. (+)'
      : 'Check the device ID path. MQTT wildcards (+ and #) and reserved paths are not allowed. (+)';
    await page.locator('#mqtt-toast').filter({ hasText: message }).waitFor();
    assert.equal(stored.devices.length, 3);
    assert.deepEqual(pageErrors, []);
    assert.equal(writes, 5);
    console.log('mqtt-template-events: passed (' + lang + ', ' + theme + ', 5 saves and 4 reloads)');
  } finally {
    await context.close();
  }
}

// Windows Chromium can write debug.log into its working directory.
if (process.platform === 'win32') process.chdir(tmpdir());
const browser = await chromium.launch({ headless: true });
try {
  for (const id of [0, 42]) await exercise(browser, 'ru', 'light', id);
  for (const lang of ['ru', 'en']) {
    for (const theme of ['dark', 'light']) await exercise(browser, lang, theme);
  }
} finally {
  await browser.close();
}
