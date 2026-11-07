// comment-mutation-proof-exempt: Chromium drives shipped controls and HTTP fixtures, not source-text pins.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {chromium} from './node_modules/playwright/index.mjs';

const www = new URL('../../www/network_config/', import.meta.url);
let coil = 0, raw = 0, kind = 3, posts = [], readError = false, publishFailed = false;
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
    let data = {ok: true, jobs: [], entries: []};
    if (url.pathname.endsWith('/mqtt_live.cgi')) data = {ok: true,
      controls: {do_1: String(coil), ao_1: String(raw)},
      units: {ao_1: kind === 15 ? '‰' : 'V'},
      errors: readError ? {do_1: 'w', ao_1: 'w'} : {}};
    if (url.pathname.endsWith('/mqtt_set.cgi')) {
      let body = ''; for await (const chunk of req) body += chunk;
      posts.push(Object.fromEntries(new URLSearchParams(body)));
      if (publishFailed) data = {ok: false, error: 'publish_failed'};
    }
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); return;
  }
  try {
    const file = new URL('.' + url.pathname, www);
    assert.ok(file.href.startsWith(www.href));
    let data = await readFile(file);
    if (url.pathname.endsWith('/mqtt.js')) data = data.toString() + `
      window.readbackTest = {prefetchDeviceLive, doPending: _doPending, aoPending: _aoPending,
        expire() {for (const p of Object.values(_doPending)) p.deadline = 0;
                  for (const p of Object.values(_aoPending)) p.deadline = 0;},
        mount(kind) {
          const dev = {id: 'ordinary-id', type: 'mr02m', module_type: kind, channels: {}};
          const other = {id: 'mr02m-looking-id', type: 'dtv', channels: {}};
          _config = {devices: [dev, other]};
          const holder = document.createElement('div'); holder.id = 'acc-body-' + dev.id;
          holder.appendChild(appendMr02mDoGroup(dev, dev.channels, 1));
          holder.appendChild(appendMr02mAoGroup(dev, dev.channels, 1));
          document.body.appendChild(holder); _accordionBuilt.add(dev.id);
          document.body.appendChild(buildDoToggleBtn(other, 'do_1', true));
          setLiveValue(other.id, 'do_1', '0', false); refreshDoTogglesForDevice(other.id);
        }};`;
    res.setHeader('Content-Type', url.pathname.endsWith('.js') ? 'text/javascript' :
      url.pathname.endsWith('.css') ? 'text/css' : url.pathname.endsWith('.svg') ? 'image/svg+xml' : 'text/html');
    res.end(data);
  } catch {res.writeHead(404); res.end();}
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await chromium.launch({headless: true});
let failures = 0;
try {
  for (const theme of ['dark', 'light']) for (const lang of ['ru', 'en']) for (const width of [500, 1600]) {
    coil = 0; raw = 0; posts = []; readError = false; publishFailed = false; kind = theme === 'light' ? 15 : 3;
    const context = await browser.newContext({viewport: {width, height: 1000}});
    await context.addCookies([{name: 'session_token', value: 'test', url: origin}]);
    await context.addInitScript(({theme, lang}) => {
      localStorage.setItem('sa02m-theme', theme); localStorage.setItem('sa02m-lang', lang);
    }, {theme, lang});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const check = async (label, fn) => {try {await fn(); console.log('PASS', theme, lang, width, label);}
      catch (e) {failures++; console.error('FAIL', theme, lang, width, label, e.message);}};
    try {
      await page.goto(origin + '/index.html');
      await page.evaluate(kind => {teardownStatusPolling(); readbackTest.mount(kind);}, kind);
      const poll = () => page.evaluate(() => readbackTest.prefetchDeviceLive('ordinary-id'));
      await poll();
      const button = page.locator('[data-do-toggle="ordinary-id:do_1"]');
      const command = async action => {
        const response = page.waitForResponse(r => r.url().endsWith('/mqtt_set.cgi'));
        await action(); await response;
      };
      await command(() => button.click());
      await poll();
      await check('HTTP ok plus actual OFF stays visibly waiting', async () => {
        assert.equal(await button.innerText(), lang === 'ru' ? 'Ждём…' : 'Waiting…');
        assert.equal(await button.evaluate(el => el.classList.contains('hw-active-blue')), false);
        assert.equal(await button.getAttribute('aria-busy'), 'true');
        assert.equal(await page.evaluate(() => !!readbackTest.doPending['ordinary-id:do_1']), true);
        assert.equal(await button.evaluate(el => el.scrollWidth <= el.clientWidth), true);
        await button.press('Enter'); assert.equal(posts.length, 1);
      });
      coil = 1; await poll();
      await check('later actual ON resolves', async () => {
        assert.equal(await page.evaluate(() => !!readbackTest.doPending['ordinary-id:do_1']), false);
        assert.equal(await button.evaluate(el => el.classList.contains('hw-active-blue')), true);
      });
      await command(() => button.click());
      await check('OFF request retains last observed ON styling', async () => {
        assert.equal(await button.evaluate(el => el.classList.contains('hw-active-blue')), true);
      });
      coil = 0; await poll();
      await command(() => button.click());
      const before = posts.length;
      await page.evaluate(() => readbackTest.expire()); await poll();
      await check('always-OFF timeout is truthful and does not resend', async () => {
        assert.equal(posts.length, before);
        assert.match(await page.locator('#mqtt-toast').innerText(), lang === 'ru' ? /Заданное состояние не наблюдается/ : /Requested state was not observed/);
        assert.equal(await button.evaluate(el => el.classList.contains('hw-active-blue')), false);
      });
      await command(() => button.click()); readError = true; await poll();
      await check('read error remains unknown while pending', async () => {
        assert.equal(await button.evaluate(el => el.classList.contains('na')), true);
        assert.equal(await page.evaluate(() => !!readbackTest.doPending['ordinary-id:do_1']), true);
      });
      readError = false; await page.evaluate(() => readbackTest.expire()); await poll();
      publishFailed = true; await command(() => button.click());
      await page.waitForFunction(() => !readbackTest.doPending['ordinary-id:do_1']); publishFailed = false;
      const input = page.locator('[data-ao-input="ordinary-id:ao_1"]');
      await input.fill('201'); await command(() => input.press('Enter'));
      await check('AO request remains separate from observed value', async () => {
        assert.equal(await input.inputValue(), '201');
        assert.equal(await page.locator('[data-live="ordinary-id:ao_1"]').innerText(), kind === 15 ? '0.0' : '0.00');
      });
      raw = 200; await poll();
      await check('different actual AO and dimmer scale stay readable', async () => {
        assert.equal(await page.locator('[data-live="ordinary-id:ao_1"]').innerText(), kind === 15 ? '20.0' : '2.00');
        assert.equal(await page.evaluate(() => !!readbackTest.aoPending['ordinary-id:ao_1']), false);
        assert.equal(posts.at(-1).value, '201');
      });
      const other = page.locator('[data-do-toggle="mr02m-looking-id:do_1"]');
      await command(() => other.click());
      await check('non-MR control keeps existing behavior despite ID', async () => {
        assert.equal(await other.evaluate(el => el.classList.contains('hw-active-blue')), true);
        assert.equal(await other.innerText(), lang === 'ru' ? 'ВКЛ' : 'ON');
      });
      await check('no page errors', async () => assert.deepEqual(errors, []));
    } finally {await context.close();}
  }
} finally {await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));}
if (failures) process.exit(1);
