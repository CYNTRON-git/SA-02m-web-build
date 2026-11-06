// comment-mutation-proof-exempt: renders real hardware widgets in Chromium and checks response-dependent state/error recovery.
import {createServer} from 'node:http';
import {readFile, mkdir} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {chromium} from './node_modules/playwright/index.mjs';
const www = new URL('../../www/network_config/', import.meta.url);
const out = new URL('../../.tmp/remediation-20261106/hardware-browser/', import.meta.url);
let state = {}, accepted = false, posts = 0;
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    let result = {ok: true, jobs: [], entries: []};
    if (url.pathname.endsWith('/hw_set.cgi')) {
      posts++;
      result = accepted ? {ok: true, channel: 'beeper', value: 1} : {ok: false, error: 'channel_unavailable'};
    } else if (url.pathname.endsWith('/status.cgi')) result = state;
    res.end(JSON.stringify(result));
    return;
  }
  try {
    const file = new URL('.' + url.pathname, www);
    assert.ok(file.href.startsWith(www.href));
    const data = await readFile(file);
    res.setHeader('Content-Type', url.pathname.endsWith('.js') ? 'text/javascript' : url.pathname.endsWith('.css') ? 'text/css' : url.pathname.endsWith('.svg') ? 'image/svg+xml' : url.pathname.endsWith('.png') ? 'image/png' : 'text/html');
    res.end(data);
  } catch {res.writeHead(404); res.end();}
});
await mkdir(out, {recursive: true});
const browser = await chromium.launch({headless: true});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
let cells = 0;
try {
  for (const theme of ['dark', 'light']) for (const variant of ['sa02m-1eth', 'sa02m-2eth']) for (const width of [1600, 500]) {
    state = {hw_variant: variant, hw_backend: 'i2c_expander', hw_configured: 1,
      hw_i2c_busy: 0, hw_i2c_expander_absent: 0, hw_pin_do: variant === 'sa02m-1eth' ? 1 : 0,
      hw_pin_beeper: 1, hw_pin_alarm_led: 1, hw_pin_usb_power: 1,
      hw_do: variant === 'sa02m-1eth' ? -1 : null, hw_beeper: -1, hw_alarm_led: -1, hw_usb_power: -1};
    accepted = false;
    const context = await browser.newContext({viewport: {width, height: 1000}});
    await context.addCookies([{name: 'session_token', value: 'test', url: origin}]);
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/index.html');
    await page.evaluate(({theme, state}) => {
      teardownStatusPolling();
      document.documentElement.setAttribute('data-theme', theme);
      sa02mI18n.setLang('ru');
      applyHardwareStatus(state);
    }, {theme, state});
    assert.equal(await page.locator('#hw-beeper-btn').innerText(), 'н/д');
    assert.equal(await page.locator('#hw-beeper-btn').isDisabled(), false);
    assert.equal(await page.locator('#hw-do-btn').isVisible(), variant === 'sa02m-1eth');
    assert.equal(await page.locator('#hw-do-btn').isDisabled(), variant !== 'sa02m-1eth');
    const before = posts;
    await page.locator('#hw-beeper-btn').click();
    await page.waitForFunction(() => document.getElementById('hw-block-status').textContent.includes('Канал недоступен'));
    assert.equal(posts, before + 1);
    assert.equal(await page.locator('#hw-beeper-btn').isDisabled(), false);
    await page.evaluate(() => sa02mI18n.setLang('en'));
    await page.waitForFunction(() => document.getElementById('hw-block-status').textContent.includes('Channel is unavailable for this hardware variant. Check the selected variant.'));
    assert.equal(await page.locator('#hw-do-btn').isDisabled(), variant !== 'sa02m-1eth');
    await page.screenshot({path: new URL(`${theme}-${variant}-${width}.png`, out).pathname.replace(/^\/([A-Za-z]:)/, '$1'), fullPage: true});
    accepted = true; state.hw_beeper = 1;
    await page.locator('#hw-beeper-btn').click();
    await page.waitForFunction(() => document.getElementById('hw-beeper-btn').classList.contains('hw-on'));
    assert.equal(await page.locator('#hw-beeper-btn').isDisabled(), false);
    assert.deepEqual(errors, []);
    assert.equal(await page.locator('.hw-block').evaluate(el => el.scrollWidth <= el.clientWidth + 1), true);
    await context.close(); cells++;
  }
  console.log(`PASS hardware-capabilities-browser: ${cells} cells; unknown/unavailable, RU/EN errors, recovery, no page errors`);
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
