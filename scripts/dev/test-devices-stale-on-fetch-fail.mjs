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
const MIME = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.json': 'application/json' };

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/api/devices') {
    liveRequests++;
    if (mode === 'drop') { req.socket.destroy(); return; }
    res.setHeader('Content-Type', 'application/json');
    if (mode === 'http500') { res.writeHead(500); res.end('{"ok":false,"error":"daemon down"}'); return; }
    res.end(JSON.stringify(FIXTURE));
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

  check(errors.length === 0, 'no page errors', errors.join(' | '));
} finally {
  await browser.close();
  server.closeAllConnections();
  server.close();
}

console.log(`${TAG}: ${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
