#!/usr/bin/env node
// comment-mutation-proof-exempt: behavioural browser harness — it drives the shipped devices.js in real Chromium against a stubbed /api/devices and asserts the rendered pill; it pins no source line by text, a commented-out line changes the behaviour it measures.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-devices-stale — a failed api/devices fetch must not look like live
   data (plan bridge-backoff-overflow P3, 1.0.7.12).

   Under test: www/network_config/static/js/devices.js refreshLive(). Before
   the fix a rejected / non-2xx fetch was swallowed (`.catch(() => {})`) and
   every card kept its last values and the green `online` pill for as long as
   the API stayed unreachable — a frozen card that read as live. Now, once the
   last successful fetch is older than STALE_BORDER_S, the last snapshot is
   re-rendered with age_s advanced by the elapsed time so isAgeStale() flips
   the pill to `stale` and the card gets `dev-stale` (the existing vocabulary:
   no new strings, no new ids). A later successful fetch overwrites it.

   HOW: real Chromium (the scripts/dev Playwright install, ui-layout's loader
   shape) over a tiny static server; /api/devices answers by MODE — the
   fixture (one ДТВ with age_s 1), a dropped socket (network error) or an
   HTTP 500 — while /cgi-bin/* is stubbed {}. The page clock is Playwright's
   fake clock: `clock.runFor` both advances Date.now() and fires the 5 s
   POLL_MS interval, so 35 s of outage cost no wall time. Assertions poll the
   DOM from node (real time) because in-page timers are the fake ones.

   SCENARIO: tab opens → pill `online` (fetch #1 ok) · API drops, +5 s → still
   `online` (inside the border — a blip is not an outage) · +35 s → `stale` +
   `dev-stale` · HTTP 500, +5 s → still `stale` · API back, +5 s → `online`
   and `dev-stale` gone. NON-VACUOUS: zero cards, zero requests reaching the
   stub in a phase, or a page error FAILS.

   PROVEN RED on the pre-fix devices.js: the «+35 s → stale» step fails with
   the pill still reading `online`.

   WHAT IT DOES NOT COVER: the 401 path (app.js's fetch wrapper redirects on a
   confirmed 401 — status-reliability-browser) and the daemon's own age_s.

   Run: node scripts/dev/test-devices-stale-on-fetch-fail.mjs
   ═══════════════════════════════════════════════════════════════════════════ */
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { appendFileSync, existsSync } from 'node:fs';

const TAG = 'devices-stale';
const WWW = new URL('../../www/network_config/', import.meta.url);
const POLL_MS = 5000;       // devices.js POLL_MS — the interval runFor drives
const BORDER_S = 30;        // devices.js STALE_BORDER_S
const T0 = new Date('2026-10-29T10:00:00Z').getTime();

let fails = 0;
let passes = 0;
const ok = (m) => { passes++; console.log(`${TAG}: ok    ${m}`); };
const bad = (m) => { fails++; console.log(`${TAG}: FAIL  ${m}`); };
const check = (cond, m, detail) => (cond ? ok(m) : bad(detail ? `${m} — ${detail}` : m));

function skip(reason) {
  console.log(`${TAG}: SKIP  ${reason}`);
  if (process.env.QUALITY_SKIP_LOG) appendFileSync(process.env.QUALITY_SKIP_LOG, reason + '\n');
  process.exit(77);
}

async function loadPlaywright() {
  const norm = (m) => (m && m.chromium ? m : (m && m.default) || m);
  const local = new URL('./node_modules/playwright/index.js', import.meta.url);
  if (existsSync(local)) { try { return norm(await import(local.href)); } catch { /* fall through */ } }
  try { return norm(await import('playwright')); } catch { return null; }
}

const pw = await loadPlaywright();
if (!pw || !pw.chromium) skip(`${TAG}: playwright not installed (npm run ui-layout:install)`);

// ── the stubbed backend ─────────────────────────────────────────────────────
const FIXTURE = {
  ok: true, available: [],
  dtv: [{ id: 'dtv-COM4-3', kind: 'dtv', label: 'ДТВ № 3 порт 4', ok: true, age_s: 1,
    room_temp: 22.5, humidity: 41, eco2_ppm: 612, tvoc_mg_m3: 0.12, pressure_mmhg: 748.2, light_pct: 37 }],
};
let mode = 'ok';            // 'ok' | 'drop' | 'http500'
let liveRequests = 0;       // /api/devices hits in the current phase
let snapshot = FIXTURE;
let heldReply;
const MIME = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.json': 'application/json' };

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.startsWith('/api/devices/widgets/') && req.method === 'POST') {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const {id} = JSON.parse(Buffer.concat(chunks));
    if (url.pathname.endsWith('/remove')) {
      const device = snapshot.dtv.find((d) => d.id === id);
      snapshot.available = [{...device, online: device.ok === true}];
      snapshot.dtv = snapshot.dtv.filter((d) => d.id !== id);
    } else if (url.pathname.endsWith('/add')) {
      snapshot.dtv = snapshot.available.filter((d) => d.id === id);
      snapshot.available = [];
    }
    res.setHeader('Content-Type', 'application/json');
    res.end('{"ok":true}');
    return;
  }
  if (url.pathname === '/api/devices') {
    liveRequests++;
    if (mode === 'drop') { req.socket.destroy(); return; }
    res.setHeader('Content-Type', 'application/json');
    if (mode === 'http500') { res.writeHead(500); res.end('{"ok":false,"error":"daemon down"}'); return; }
    if (mode === 'envelope') { res.end('{"ok":false}'); return; }
    if (mode === 'hold') { heldReply = res; return; }
    res.end(JSON.stringify(snapshot));
    return;
  }
  if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    res.end(url.pathname === '/api/devices/events' ? '{"ok":true,"events":[]}' : '{}');
    return;
  }
  try {
    const file = new URL('.' + url.pathname, WWW);
    if (!file.href.startsWith(WWW.href)) throw new Error('escape');
    const data = await readFile(file);
    const ext = url.pathname.slice(url.pathname.lastIndexOf('.'));
    res.setHeader('Content-Type', MIME[ext] || 'application/octet-stream');
    res.end(data);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const origin = `http://127.0.0.1:${server.address().port}`;

let browser;
try { browser = await pw.chromium.launch({ headless: true }); } catch (e) {
  const msg = String((e && e.message) || e);
  if (/Executable doesn't exist|npx playwright install|Looks like Playwright|browserType.launch/i.test(msg)) {
    skip(`${TAG}: chromium not installed for playwright (${msg.split('\n')[0]})`);
  }
  throw e;
}

// The pill + card state of every rendered device card, read in the page.
const readCards = () => Array.from(document.querySelectorAll('#dev-grid .dev-card')).map((c) => ({
  id: c.dataset.deviceId,
  pill: (c.querySelector('[data-role="status"]') || {}).textContent || '',
  stale: c.classList.contains('dev-stale'),
}));

// Poll the DOM from node (real time) until `want(cards)` holds or `ms` pass.
async function waitCards(page, want, ms = 4000) {
  const until = Date.now() + ms;
  let cards = [];
  for (;;) {
    cards = await page.evaluate(readCards);
    if (cards.length && want(cards)) return cards;
    if (Date.now() > until) return cards;
    await new Promise((r) => setTimeout(r, 50));
  }
}
const allPill = (cards, pill) => cards.every((c) => c.pill === pill && c.stale === (pill === 'stale'));
const fmt = (cards) => JSON.stringify(cards);

try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([{ name: 'session_token', value: 'test', url: origin }]);
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.clock.install({ time: T0 });
  await page.goto(origin + '/index.html');
  await page.evaluate(() => switchTab('devices'));

  // 1. The tab opens on a live API.
  let cards = await waitCards(page, (c) => allPill(c, 'online'));
  check(cards.length === 1 && allPill(cards, 'online'), 'tab open: one card, pill online', fmt(cards));
  check(liveRequests >= 1, `tab open: the stub saw the fetch (${liveRequests})`);

  // 2. The API drops; one poll later we are inside the border — not stale yet.
  mode = 'drop'; liveRequests = 0;
  await page.clock.runFor(POLL_MS);
  await waitCards(page, () => liveRequests >= 1, 2000);
  await new Promise((r) => setTimeout(r, 200));            // let the rejection land
  cards = await page.evaluate(readCards);
  check(liveRequests >= 1, `drop +5 s: the failed fetch reached the stub (${liveRequests})`);
  check(cards.length === 1 && allPill(cards, 'online'), 'drop +5 s: a blip inside the border keeps the pill online', fmt(cards));

  // 3. Past the border the last snapshot is re-rendered as stale.
  await page.clock.runFor(BORDER_S * 1000);               // +35 s since the last ok
  cards = await waitCards(page, (c) => allPill(c, 'stale'));
  check(cards.length === 1 && allPill(cards, 'stale'), `drop +${BORDER_S + 5} s: pill stale + dev-stale on the card`, fmt(cards));

  // 4. A non-2xx answer is a failed fetch too — stays stale.
  mode = 'http500'; liveRequests = 0;
  await page.clock.runFor(POLL_MS);
  await waitCards(page, () => liveRequests >= 1, 2000);
  await new Promise((r) => setTimeout(r, 200));
  cards = await page.evaluate(readCards);
  check(liveRequests >= 1, `http 500: the fetch reached the stub (${liveRequests})`);
  check(cards.length === 1 && allPill(cards, 'stale'), 'http 500 +5 s: pill stays stale', fmt(cards));

  // 5. The API is back: the next good fetch overwrites the stale render.
  mode = 'ok'; liveRequests = 0;
  await page.clock.runFor(POLL_MS);
  cards = await waitCards(page, (c) => allPill(c, 'online'));
  check(liveRequests >= 1, `recovery: the fetch reached the stub (${liveRequests})`);
  check(cards.length === 1 && allPill(cards, 'online'), 'recovery +5 s: pill online again, dev-stale gone', fmt(cards));

  async function tick() {
    const before = liveRequests;
    await page.clock.runFor(POLL_MS);
    await waitCards(page, () => liveRequests > before);
    await new Promise((resolve) => setTimeout(resolve, 75));
    return page.evaluate(readCards);
  }
  mode = 'envelope';
  await page.clock.runFor((BORDER_S + 5) * 1000);
  cards = await waitCards(page, (c) => allPill(c, 'stale'));
  check(allPill(cards, 'stale'), 'HTTP 200 error envelope ages the last sample', fmt(cards));
  mode = 'ok';
  for (const [age, file] of [[20, process.env.PRESENCE_CAPTURE_1], [1.4, process.env.PRESENCE_CAPTURE_2]]) {
    snapshot = file ? JSON.parse(await readFile(file, 'utf8')).reads['api/devices'] : {
      ok: true, dtv: [15, 3].map((addr, i) => ({kind: 'dtv', id: 'dtv-COM' + (3 + i) + '-' + addr, ok: true, age_s: 1})),
      mtd: [{kind: 'mtd', id: 'mtdx62-mb-COM3-20', age_s: age, ok: false, presence: 1}],
    };
    cards = await tick();
    const mtd = cards.find((c) => c.id === 'mtdx62-mb-COM3-20');
    check(mtd?.pill === 'offline' && mtd.stale, 'MTD fresh failure, age ' + age, fmt(cards));
    check(['dtv-COM3-15', 'dtv-COM4-3'].every((id) => cards.some((c) => c.id === id && c.pill === 'online')),
      'two answering DTV devices stay online', fmt(cards));
    if (age === 20 && process.env.PRESENCE_SCREEN_DIR) {
      await page.locator('#dev-grid').screenshot({path: process.env.PRESENCE_SCREEN_DIR + '/devices.png'});
    }
  }
  snapshot = {ok: true};
  for (const kind of ['dtv', 'mr', 'ce', 'spodes', 'carel', 'mtd']) {
    snapshot[kind] = [{kind, id: kind + '-one', ok: false, age_s: 30}];
  }
  cards = await tick();
  check(cards.length === 6 && cards.every((c) => c.pill === 'offline' && c.stale),
    'all six card kinds show an explicit failure', fmt(cards));
  for (const [okValue, age, expected] of [[true, 30, 'online'], [false, 31, 'stale'], [true, 30.01, 'stale'],
    [true, -1, 'stale'], [true, null, 'stale'], [true, '3', 'stale'], [true, undefined, 'stale'],
    [undefined, 0, 'stale']]) {
    snapshot = {ok: true, dtv: [{...FIXTURE.dtv[0], age_s: age, ok: okValue}]};
    cards = await tick();
    check(cards.length === 1 && cards[0].pill === expected, 'availability ' + JSON.stringify([okValue, age, expected]), fmt(cards));
  }
  mode = 'hold';
  await tick();
  mode = 'ok';
  snapshot.dtv[0].ok = false;
  snapshot.dtv[0].age_s = 0;
  snapshot.available = [{id: 'hidden', label: 'Hidden', kind: 'dtv', online: true, ok: false, age_s: 1}];
  await tick();
  heldReply.end(JSON.stringify(FIXTURE));
  await new Promise((resolve) => setTimeout(resolve, 150));
  cards = await page.evaluate(readCards);
  check(cards.length === 1 && cards[0].pill === 'offline', 'older successful poll cannot undo a newer failure', fmt(cards));
  await page.evaluate(() => document.getElementById('dev-widget-add-btn').click());
  const picker = await page.locator('#dev-add-list').textContent();
  check(picker.includes('offline'), 'legacy contradictory picker entry shows offline', picker);
  const list = page.locator('#dev-add-list');
  snapshot.available = [{id:'dtv-COM5-7', label:'ДТВ порт 5', port_num:5,
    kind:'dtv', location_source:'legacy_id', age_s:1, ok:true, online:true}];
  await tick();
  check((await list.textContent()).includes('online') && await list.locator('span[aria-label]').count() === 1,
    'hidden legacy port is visibly unconfirmed on a fresh sample', await list.textContent());
  for (const failure of ['envelope', 'drop']) {
    mode = failure;
    await page.clock.runFor(40000);
    await tick();
    check((await list.textContent()).includes('stale'), 'open picker ages after ' + failure, await list.textContent());
    await page.evaluate(() => {
      document.getElementById('dev-add-modal-close').click();
      document.getElementById('dev-widget-add-btn').click();
    });
    check((await list.textContent()).includes('stale'), 'reopened picker ages after ' + failure);
    mode = 'ok';
    snapshot.available[0].location_source = 'sample';
    await tick();
    check((await list.textContent()).includes('online') && await list.locator('span[aria-label]').count() === 0,
      'confirmed hidden device recovers after ' + failure);
  }
  snapshot = {ok: true, dtv: [{...FIXTURE.dtv[0], id: 'dtv-COM5-3', port_num: 4,
    location_source: 'sample', label: 'ДТВ № 3 порт 4'}]};
  await tick();
  const title = page.locator('#dev-grid .dev-card-title');
  check((await title.textContent()).endsWith('4') && !(await title.getAttribute('title')),
    'confirmed sample uses its COM4 label with the stable COM5 id');
  snapshot.dtv[0].port_num = 5;
  snapshot.dtv[0].label = 'ДТВ № 3 порт 5';
  for (const source of ['legacy_id', undefined]) {
    snapshot.dtv[0].location_source = source;
    await tick();
    check((await title.textContent()).endsWith('(?)') && await title.locator('span[title][aria-label]').count() === 1,
      'legacy address is visibly unconfirmed for source ' + source, await title.evaluate((n) => n.outerHTML));
  }
  snapshot.dtv[0].ok = false;
  await tick();
  page.once('dialog', (dialog) => dialog.accept());
  await page.evaluate(() => {
    document.getElementById('dev-add-modal-close')?.click();
    document.querySelector('#dev-grid [data-role="remove"]').click();
  });
  await page.locator('#dev-grid .dev-card').waitFor({state: 'detached', timeout: 4000});
  await page.evaluate(() => document.getElementById('dev-widget-add-btn').click());
  check((await page.locator('#dev-add-list').textContent()).includes('offline'), 'hidden failed device remains offline in picker');
  await page.evaluate(() => document.querySelector('#dev-add-list .dev-add-item').click());
  cards = await waitCards(page, (c) => c.length === 1 && c[0].pill === 'offline');
  check(cards.length === 1 && cards[0].pill === 'offline', 're-added widget retains offline state');
  if (process.env.PRESENCE_DASHBOARD) {
    const ports = JSON.parse(await readFile(process.env.PRESENCE_DASHBOARD, 'utf8'));
    await page.evaluate(() => switchTab('dashboard'));
    for (const count of [5, 4]) {
      for (const theme of ['dark', 'light']) {
        await page.setViewportSize({width: theme === 'dark' ? 1280 : 500, height: 900});
        await page.evaluate(({ports, count, theme}) => {
          document.documentElement.dataset.theme = theme;
          applyRs485Status({rs485: ports.slice(0, count)});
        }, {ports, count, theme});
        const grid = page.locator('#rs485-grid');
        check(await grid.locator('.rs485-port').count() === count, 'dashboard port count ' + count);
        check(await grid.locator('#rs485c-1 .is-on').count() === 1
          && await grid.locator('#rs485c-2 .rs485-chip').count() === 0, 'COM2/14 only on COM2');
        check(await grid.locator('#rs485c-3 .is-on').count() === 1
          && await grid.locator('#rs485c-3 .is-off').count() === 1
          && await grid.locator('#rs485c-3 .is-scan[title]').count() === 1, 'COM4 confirmed and moved statuses');
        check(!(await grid.innerHTML()).includes('последнему сканированию'), 'configured unknown has no scan tooltip');
        if (process.env.PRESENCE_SCREEN_DIR) {
          await grid.screenshot({path: process.env.PRESENCE_SCREEN_DIR + '/com-' + count + '-' + theme + '.png'});
        }
      }
    }
    await page.evaluate((ports) => applyRs485Status({rs485: ports.map((p) => ({
      ...p, modules: {source: 'bridge', live: true, ours: [{addr: 14, model: 'ghost', online: true}]}
    }))}), ports);
    check(await page.locator('#rs485-grid .rs485-chip').count() === 0, 'older mixed helper cannot claim configured chips');
  }
  check(errors.length === 0, 'no page errors', errors.join(' | '));
} finally {
  await browser.close();
  server.closeAllConnections();
  server.close();
}

console.log(`${TAG}: ${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
