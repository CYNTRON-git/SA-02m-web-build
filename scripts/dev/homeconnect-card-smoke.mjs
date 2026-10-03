#!/usr/bin/env node
// comment-mutation-proof-exempt: behavioural harness - renders the SHIPPED bundles in headless Chromium and asserts rendered DOM/text per contract state; it pins no source line by text, every state assertion is preceded by a reached-state precondition, and the state, reason and error-code lists are read from the contract so a missing or extra fixture FAILS.
/* ═══════════════════════════════════════════════════════════════════════════
   homeconnect-card-smoke — the «Home Connect» card (read-only BSH client),
   rendered for real.
   ───────────────────────────────────────────────────────────────────────────
   What it closes: the card's promises live in app/homeconnect.js + index.html
   and no other gate renders them (ui-layout stubs every CGI with `{}`). This
   row serves www/network_config statically, stubs
   cgi-bin/sa02m_homeconnect_api.cgi (switchable INSIDE one page) and asserts:
     * every contract state (docs/contracts/home-connect.md §6 table) has a
       fixture and none is undocumented; per state and both themes: the
       «Аккаунт» badge (reached-state precondition), the standing card line,
       the footer button label and enabled state, «Подключить» only while
       enabled ∧ not linked, «Отключить аккаунт» only with an account;
     * every contract reason (§6) renders a Russian line (never the raw code);
       every dispatch error code (§9) answers a Russian toast;
     * the sign-in code, its link and QR are NOT in the DOM outside
       `awaiting_user`; in it they are shown with the link opening BSH's page
       in a new tab (rel noopener) and a QR whose modules reconstruct, bit for
       bit, the golden matrix below; they leave the DOM on `connected`, on tab
       leave, and are never drawn for a non-https / foreign-domain URL or an
       already-expired code;
     * appliance names / types / brands carrying `<img onerror>` and quotes
       render as text;
     * every control is disabled while an action is pending, and a second
       action during it sends nothing;
     * «Отключить аккаунт» asks the confirm text; dismiss sends nothing, accept
       sends `unlink` once;
     * the budget line reads «N из 1000»; `rate_limited` shows «до HH:MM»;
     * an invalid Client ID is refused before any POST; a valid one is sent;
     * the card polls while «Управление» is shown and never after it is left;
     * ≤560 px: the card spans its column, nothing overflows, the QR fits.
   Golden QR: the card computes the code itself (the CGI returns no matrix).
   GOLDEN_QR is the independent `qrcode` 8.2 library's matrix for LINK_URL at
   EC level M, byte mode, mask 4 (the mask the card's penalty picks) — any
   encoder regression changes the drawn modules and FAILS here.

   Exit codes (the runner has no skip signal distinct from success):
     0  every assertion passed (never without a real render);
     1  an assertion failed (named), a contract table is missing/empty, a
        fixture drifted from the contract, or ANY other error;
     2  chromium/playwright missing — deliberately RED, never a vacuous green
        (same policy as homekit-card-smoke).

   HOMECONNECT_CARD_SMOKE_WWW points the static server at a scratch copy (the
   mutation proof recorded in the commit body).
   Harness: the standing Playwright install under scripts/dev (npm run
   ui-layout:install). Dev-only; never shipped to the device.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync, existsSync, mkdirSync, statSync } from 'node:fs';
import http from 'node:http';
import { join, dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const TAG = 'homeconnect-card-smoke';
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '..', '..');
const WWW = process.env.HOMECONNECT_CARD_SMOKE_WWW
  ? resolve(process.env.HOMECONNECT_CARD_SMOKE_WWW)
  : join(REPO, 'www', 'network_config');
const CONTRACT = join(REPO, 'docs', 'contracts', 'home-connect.md');
const SHOTS = join(REPO, '.ai-dev', 'quality', 'screenshots');
const MISSING_MSG = `${TAG}: chromium/playwright missing — run: npm run ui-layout:install`;
const THEMES = ['dark', 'light'];
const REACH_MS = 6000;

function die(code, ...lines) {
  for (const l of lines) (code === 0 ? console.log : console.error)(l);
  process.exit(code); // exit-status: every die() caller passes a literal (1 = FAIL, 2 = infra), never a count
}

let failures = 0;
let assertions = 0;
function check(cond, msg) {
  assertions++;
  if (cond) console.log('  ok   - ' + msg);
  else { failures++; console.error('  FAIL - ' + msg); }
}

/* ── Preflight — every failure here is exit 1, never 2 ──────────────────── */
if (!existsSync(join(WWW, 'index.html'))) die(1, `${TAG}: ERROR — served path has no index.html: ${WWW}`);
if (!existsSync(CONTRACT)) die(1, `${TAG}: ERROR — contract not found: ${CONTRACT}`);
const CONTRACT_TEXT = readFileSync(CONTRACT, 'utf8');

/** First-column `code` tokens of the table after `startMarker`, up to
    `endMarker`. Empty ⇒ exit 1: an empty list makes every drift check vacuous. */
function contractCodes(startMarker, endMarker, what) {
  const a = CONTRACT_TEXT.indexOf(startMarker);
  const b = a < 0 ? -1 : CONTRACT_TEXT.indexOf(endMarker, a + startMarker.length);
  if (a < 0 || b < 0) die(1, `${TAG}: ERROR — ${CONTRACT} has no ${what} section (${startMarker} … ${endMarker})`);
  const out = [];
  const re = /^\|\s*`([a-z_]+)`\s*\|/gm;
  const seg = CONTRACT_TEXT.slice(a, b);
  let m;
  while ((m = re.exec(seg))) out.push(m[1]);
  if (!out.length) die(1, `${TAG}: ERROR — the ${what} table in ${CONTRACT} is empty; refusing to run on an empty list`);
  return out;
}
const DOC_STATES = contractCodes('**Состояния**', '**Причины**', 'state (§6)');
const DOC_REASONS = contractCodes('**Причины**', '**`status.json`**', 'reason (§6)');
const DOC_ERRORS = contractCodes('**Коды ошибок диспетчера**', 'Коды самого CGI', 'dispatch error (§9)');

/* ── Fixtures ────────────────────────────────────────────────────────────── */
const USER_CODE = 'HCQR-7342';
const LINK_URL = 'https://verify.home-connect.com/device?user_code=HCQR-7342';
// qrcode 8.2: QRCode(error_correction=M, border=0, mask_pattern=4) over
// QRData(LINK_URL, MODE_8BIT_BYTE) — version 4, 33×33.
const GOLDEN_QR = [
  '111111101111001110100011001111111',
  '100000100011000011110100101000001',
  '101110100100010011010100101011101',
  '101110101011101100001001001011101',
  '101110101110001001110101001011101',
  '100000101101000011010000001000001',
  '111111101010101010101010101111111',
  '000000001101101111100100100000000',
  '100010111110000001011101111111001',
  '100101010111011111100101010001110',
  '101110111110100111101001111001010',
  '011001010000101001101101011000000',
  '101001111010011101100111001111001',
  '110101010011000011010100000001100',
  '100100100101010010101001011010010',
  '100010010001110001010110011010000',
  '010101101001101101011110101011010',
  '001011001000011100101001000001110',
  '011101111011000110101101010101010',
  '111000011101001101101110010100011',
  '011110100110000001111110001011000',
  '101011001101100001010101000101110',
  '000010100011010110100111110001110',
  '001111001101000011000111101110001',
  '111011110100001011001110111110010',
  '000000001111001110100001100010110',
  '111111101110001100001100101011000',
  '100000100010100011101100100010010',
  '101110101100101011101100111111000',
  '101110100111110100010111101110000',
  '101110100111110111101000001110000',
  '100000100110100101101101100000000',
  '111111101000001001001111100000001',
];
const XSS_NAME = '<img src=x onerror="window.__hcXss=1">"\'';
const XSS_TYPE = '<img src=x onerror="window.__hcXss=2">';
const XSS_BRAND = '"><img src=x onerror="window.__hcXss=3">';

const BUDGET = { day: '2026-09-28', used: 137, limit: 1000, local: 800, remaining: 863 };
const APPS = [
  { device_id: 'hc-siemens-sn65-1', name: 'Посудомойка', type: 'Dishwasher', brand: 'Siemens', connected: true, controls: ['connected', 'running'] },
  { device_id: 'hc-bosch-wat-2', name: XSS_NAME, type: XSS_TYPE, brand: XSS_BRAND, connected: false, controls: ['connected'] },
];
const COMMON = {
  ok: true, reason: '', message: '', enabled: true, linked: false, host: 'api', client_id: 'MyHcClientId_0001',
  client_id_source: 'own', vendor_preset: false, appliances: 0, appliances_connected: 0, appliance_list: [],
  stream: 'off', budget: BUDGET, last_event_ts: 0, link: null, read_only: true, version: '1.0.0', ts: 0,
};
const st = (o) => ({ ...COMMON, ...o });
const LINKED = { linked: true, appliances: 2, appliances_connected: 1, appliance_list: APPS };
const future = () => Math.floor(Date.now() / 1000) + 600;
const UNTIL = Math.floor(Date.now() / 1000) + 3 * 3600;
const P = {
  not_installed: { ok: true, state: 'not_installed', enabled: false, read_only: true },
  disabled: st({ state: 'disabled', enabled: false }),
  missing_deps: st({ state: 'missing_deps', message: 'missing: paho.mqtt.client' }),
  missing_client_id: st({ state: 'missing_client_id', client_id: '', client_id_source: '' }),
  unlinked: st({ state: 'unlinked' }),
  awaiting_user: () => st({ state: 'awaiting_user', link: { user_code: USER_CODE, verification_uri: 'https://verify.home-connect.com/device', verification_uri_complete: LINK_URL, expires_at: future() } }),
  // A lingering link object (link.json not yet removed) must still never be
  // drawn outside awaiting_user.
  link_expired: () => st({ state: 'link_expired', link: { user_code: USER_CODE, verification_uri: LINK_URL, verification_uri_complete: LINK_URL, expires_at: future() } }),
  connecting: st({ state: 'connecting', ...LINKED, stream: 'down' }),
  connected: st({ state: 'connected', ...LINKED, stream: 'up' }),
  rate_limited: st({ state: 'rate_limited', reason: 'daily_limit', ...LINKED, stream: 'up', rate_limited_until: UNTIL }),
  offline: st({ state: 'offline', ...LINKED, stream: 'down', message: 'cloud unreachable: timed out' }),
  // Sign-in against a cloud that never answers (bench G6, RU IP): not linked.
  offline_unlinked: st({ state: 'offline', message: 'sign-in could not start: timed out' }),
  token_revoked: st({ state: 'token_revoked' }),
  error: st({ state: 'error' }),
};

/* The state matrix. `badge` = «Аккаунт» label (plan card spec); `line` = the
   standing card line (null = none); `btn` = footer label (null = locked /
   not asserted); `link` = «Подключить» shown; `unlink` = «Отключить аккаунт»
   shown; `code` = the sign-in code box shown. `badge` for rate_limited is
   computed in the page (local HH:MM). */
const CASES = [
  { name: 'not_installed', p: P.not_installed, badge: 'не установлен', line: 'Нужна полная установка (install.sh)', btn: null, link: false, unlink: false, cidHidden: true },
  { name: 'disabled', p: P.disabled, badge: 'выключен', line: null, btn: 'Включить', link: false, unlink: false },
  { name: 'missing_deps', p: P.missing_deps, badge: 'нет компонентов', line: 'Нужна полная установка (install.sh)', btn: 'Выключить', link: false, unlink: false },
  { name: 'missing_client_id', p: P.missing_client_id, badge: 'нет Client ID', line: 'Введите Client ID', btn: 'Выключить', link: true, linkDisabled: true, unlink: false },
  { name: 'unlinked', p: P.unlinked, badge: 'не подключён', line: 'Нажмите «Подключить»', btn: 'Выключить', link: true, unlink: false },
  { name: 'awaiting_user', p: P.awaiting_user, badge: 'ждёт входа', line: null, btn: 'Выключить', link: false, unlink: true, code: true },
  { name: 'link_expired', p: P.link_expired, badge: 'код истёк', line: 'Нажмите «Подключить» ещё раз', btn: 'Выключить', link: true, unlink: false },
  { name: 'connecting', p: P.connecting, badge: 'подключение…', line: null, btn: 'Выключить', link: false, unlink: true },
  { name: 'connected', p: P.connected, badge: 'подключён', line: null, btn: 'Выключить', link: false, unlink: true, apps: true },
  { name: 'rate_limited', p: P.rate_limited, badge: '@RATE@', line: '@RATELINE@', btn: 'Выключить', link: false, unlink: true },
  { name: 'offline', p: P.offline, badge: 'нет связи с облаком', line: 'BSH недоступно из этой сети', oneLine: true, btn: 'Выключить', link: false, unlink: true },
  { name: 'offline-unlinked', p: P.offline_unlinked, badge: 'нет связи с облаком', line: 'BSH недоступно из этой сети', oneLine: true, btn: 'Выключить', link: true, unlink: false },
  { name: 'token_revoked', p: P.token_revoked, badge: 'доступ отозван', line: 'Подключите аккаунт заново', btn: 'Выключить', link: true, unlink: true },
  { name: 'error', p: P.error, badge: 'ошибка', line: 'Журнал: sa02m-homeconnect', btn: 'Выключить', link: false, unlink: false },
  // Follows error. A missed probe keeps that status: the card must not flip
  // to «Нет ответа от платы».
  { name: 'no-answer', p: null, noanswer: true, badge: 'ошибка', line: 'Журнал: sa02m-homeconnect', btn: 'Выключить', link: false },
];

/* Every contract reason, in a state that carries it, with its expected line
   (the card's Russian, never the raw code). */
const REASON_CASES = {
  access_denied: { p: st({ state: 'unlinked', reason: 'access_denied' }), badge: 'не подключён', line: 'Вход отклонён' },
  client_id_rejected: { p: st({ state: 'unlinked', reason: 'client_id_rejected' }), badge: 'не подключён', line: 'Client ID не принят' },
  token_store_insecure: { p: st({ state: 'error', reason: 'token_store_insecure' }), badge: 'ошибка', line: 'Небезопасный файл входа — журнал sa02m-homeconnect' },
  token_store_corrupt: { p: st({ state: 'unlinked', reason: 'token_store_corrupt' }), badge: 'не подключён', line: 'Данные входа повреждены — подключите заново' },
  budget_local_reached: { p: st({ state: 'connected', reason: 'budget_local_reached', ...LINKED, stream: 'up', budget: { ...BUDGET, used: 812, remaining: 188 } }), badge: 'подключён', line: 'Опрос на паузе до конца суток (UTC)', budget: '812 из 1000' },
  retry_after: { p: st({ state: 'rate_limited', reason: 'retry_after', ...LINKED, stream: 'down', rate_limited_until: UNTIL }), badge: '@RATE@', line: '@RETRYLINE@' },
  daily_limit: { p: P.rate_limited, badge: '@RATE@', line: '@RATELINE@' },
  conf_unreadable: { p: st({ state: 'missing_deps', reason: 'conf_unreadable', message: 'conf unreadable' }), badge: 'нет компонентов', line: 'Нет доступа к настройкам' },
  stream_down: { p: st({ state: 'connecting', reason: 'stream_down', ...LINKED, stream: 'down' }), badge: 'подключение…', line: 'Нет потока событий — данные могут устареть' },
  status_stale: { p: st({ state: 'error', reason: 'status_stale' }), badge: 'не отвечает', line: 'Статус устарел' },
};

// Non-vacuous both ways: contract states/reasons and the fixtures are one set.
const fixturedStates = [...new Set(CASES.filter((c) => c.p).map((c) => (typeof c.p === 'function' ? c.p() : c.p).state))];
for (const s of DOC_STATES) check(fixturedStates.includes(s), `contract state "${s}" has a card fixture`);
for (const s of fixturedStates) check(DOC_STATES.includes(s), `fixture state "${s}" is a documented contract state (no drift)`);
for (const r of DOC_REASONS) check(!!REASON_CASES[r], `contract reason "${r}" has a card fixture`);
for (const r of Object.keys(REASON_CASES)) check(DOC_REASONS.includes(r), `fixture reason "${r}" is a documented contract reason (no drift)`);
check(DOC_ERRORS.length >= 8, `contract dispatch-error table parsed (${DOC_ERRORS.length} codes)`);
check(GOLDEN_QR.length === 33 && GOLDEN_QR.every((r) => /^[01]{33}$/.test(r)), 'golden QR is a 33×33 bit matrix');
if (failures) die(1, `${TAG}: ${failures} FAILURE(S) — the fixtures do not match the contract tables`);

/* ── Harness: playwright + chromium — the ONLY exit-2 causes ─────────────── */
const require = createRequire(import.meta.url);
let pw;
try {
  pw = require('./node_modules/playwright');
} catch (e) {
  if (e && (e.code === 'MODULE_NOT_FOUND' || e.code === 'ERR_MODULE_NOT_FOUND') && /playwright/.test(String(e.message))) {
    die(2, MISSING_MSG, '  reason: the playwright package is not installed under scripts/dev/node_modules');
  }
  throw e;
}
const chromiumExe = (() => { try { return pw.chromium.executablePath(); } catch { return ''; } })();
if (!chromiumExe || !existsSync(chromiumExe)) {
  die(2, MISSING_MSG, `  reason: the chromium browser is not in playwright's browser cache (expected: ${chromiumExe || 'unknown path'})`);
}

/* ── Static server on a free port (stdlib) ───────────────────────────────── */
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.json': 'application/json', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff', '.txt': 'text/plain; charset=utf-8',
};
function serve(req, res) {
  let file;
  try {
    const p = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
    file = resolve(join(WWW, p));
    if (!(file + sep).startsWith(WWW + sep)) { res.writeHead(403); res.end(); return; }
    if (existsSync(file) && statSync(file).isDirectory()) file = join(file, 'index.html');
    if (!existsSync(file)) { res.writeHead(404); res.end('not found'); return; }
    res.writeHead(200, { 'content-type': MIME[extname(file).toLowerCase()] || 'application/octet-stream' });
    res.end(readFileSync(file));
  } catch (e) {
    res.writeHead(500); res.end(String(e));
  }
}
function listen() {
  return new Promise((ok, fail) => {
    const srv = http.createServer(serve);
    srv.on('error', fail);
    srv.listen(Number(process.env.HOMECONNECT_CARD_SMOKE_PORT || 0), '127.0.0.1', () => ok(srv));
  });
}

/* ── Page with switchable stubs ──────────────────────────────────────────── */
async function openPage(browser, base, { width = 1280, theme = 'dark' } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 1000 } });
  await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
  const page = await ctx.newPage();
  const S = {
    hc: P.disabled, noanswer: false, hold: null, reply: null, gets: 0, posts: [], dialogs: [], dialogAccept: true, errors: [],
  };
  page.on('pageerror', (e) => S.errors.push(String(e && e.message || e)));
  page.on('dialog', (d) => { S.dialogs.push(d.message()); (S.dialogAccept ? d.accept() : d.dismiss()).catch(() => {}); });
  const json = (r, o) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(o) });
  const cur = () => (typeof S.hc === 'function' ? S.hc() : S.hc);
  await page.route('**/cgi-bin/**', async (r) => {
    const req = r.request();
    if (/sa02m_homeconnect_api\.cgi/.test(req.url())) {
      if (req.method() === 'POST') {
        let body = {};
        try { body = JSON.parse(req.postData() || '{}'); } catch { body = {}; }
        S.posts.push(body);
        if (S.hold) await S.hold;
        return json(r, S.reply ? S.reply(body) : { ok: true, action: body.action, status: cur() });
      }
      S.gets++;
      if (S.noanswer) return r.fulfill({ status: 502, contentType: 'text/html', body: '<html>502 Bad Gateway</html>' });
      return json(r, cur());
    }
    return json(r, {});
  });
  await page.goto(`${base}/index.html`, { waitUntil: 'load' });
  await page.evaluate((t) => {
    if (t === 'light') document.documentElement.setAttribute('data-theme', 'light');
    else document.documentElement.removeAttribute('data-theme');
  }, theme);
  await page.evaluate(() => window.switchTab('system'));
  return { ctx, page, S };
}

// Two sequential refreshes: the second can never be an in-flight request
// issued for the previous stub.
async function refresh(page) {
  await page.evaluate(async () => { await window.homeconnectRefresh(); await window.homeconnectRefresh(); });
}

// Expected «лимит API до HH:MM» and lines — local time of the page.
async function expandDynamic(page, text) {
  if (!text || !text.startsWith('@')) return text;
  const hhmm = await page.evaluate((u) => { const d = new Date(u * 1000); const p = (n) => (n < 10 ? '0' : '') + n; return p(d.getHours()) + ':' + p(d.getMinutes()); }, UNTIL);
  return {
    '@RATE@': `лимит API до ${hhmm}`,
    '@RATELINE@': `Суточный лимит исчерпан — до ${hhmm}`,
    '@RETRYLINE@': `Облако попросило подождать — до ${hhmm}`,
  }[text];
}

async function reach(page, S, c, tag) {
  S.noanswer = !!c.noanswer;
  if (c.p) S.hc = c.p;
  const want = await expandDynamic(page, c.badge);
  await refresh(page);
  try {
    await page.waitForFunction((w) => {
      const el = document.getElementById('homeconnect-state');
      return !!el && el.textContent.trim() === w;
    }, want, { timeout: REACH_MS });
    return true;
  } catch {
    const got = await page.evaluate(() => (document.getElementById('homeconnect-state') || {}).textContent || '(absent)');
    check(false, `${tag}: state reached — «Аккаунт» expected "${want}", got "${String(got).trim()}"`);
    return false;
  }
}

async function readCard(page) {
  return page.evaluate((code) => {
    const $ = (id) => document.getElementById(id);
    const vis = (id) => {
      const el = $(id);
      if (!el) return false;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
    };
    const txt = (id) => ($(id) ? $(id).textContent.trim() : null);
    const dis = (id) => ($(id) ? !!$(id).disabled : null);
    const a = $('homeconnect-verify-link');
    return {
      exists: ['homeconnect-card', 'homeconnect-state', 'homeconnect-apps', 'homeconnect-budget', 'homeconnect-stream',
        'homeconnect-apps-list', 'homeconnect-link-row', 'homeconnect-btn-link', 'homeconnect-code-box', 'homeconnect-qr',
        'homeconnect-code', 'homeconnect-verify-link', 'homeconnect-countdown', 'homeconnect-cid', 'homeconnect-client-in',
        'homeconnect-btn-client', 'homeconnect-msg', 'homeconnect-btn-unlink', 'homeconnect-btn-enable'].filter((id) => !$(id)),
      badge: txt('homeconnect-state'),
      apps: txt('homeconnect-apps'),
      budget: txt('homeconnect-budget'),
      stream: txt('homeconnect-stream'),
      msgVisible: vis('homeconnect-msg'),
      msg: txt('homeconnect-msg'),
      // Rendered text lines of the card line (distinct line boxes of its text).
      msgLines: (() => {
        const el = $('homeconnect-msg');
        if (!el || !el.firstChild) return 0;
        const r = document.createRange();
        r.selectNodeContents(el);
        return new Set([...r.getClientRects()].filter((b) => b.width > 0).map((b) => Math.round(b.top))).size;
      })(),
      linkVisible: vis('homeconnect-btn-link'),
      codeBoxVisible: vis('homeconnect-code-box'),
      codeInDom: document.documentElement.outerHTML.includes(code),
      codeText: txt('homeconnect-code'),
      href: a ? a.getAttribute('href') : null,
      target: a ? a.getAttribute('target') : null,
      rel: a ? a.getAttribute('rel') : null,
      qrRects: document.querySelectorAll('#homeconnect-qr rect.homekit-qr-m').length,
      countdown: txt('homeconnect-countdown'),
      cidVisible: vis('homeconnect-cid'),
      unlinkVisible: vis('homeconnect-btn-unlink'),
      enVisible: vis('homeconnect-btn-enable'),
      enText: txt('homeconnect-btn-enable'),
      appsListVisible: vis('homeconnect-apps-list'),
      appsRows: document.querySelectorAll('#homeconnect-apps-list li').length,
      disabled: {
        enable: dis('homeconnect-btn-enable'), link: dis('homeconnect-btn-link'), unlink: dis('homeconnect-btn-unlink'),
        client: dis('homeconnect-btn-client'), clientIn: dis('homeconnect-client-in'),
      },
    };
  }, USER_CODE);
}

// The drawn QR back to rows: each rect is one horizontal run of dark modules.
async function readQr(page) {
  return page.evaluate(() => {
    const svg = document.getElementById('homeconnect-qr');
    const vb = (svg.getAttribute('viewBox') || '').split(' ').map(Number);
    const quiet = 4;
    const n = (vb[2] || 0) - 2 * quiet;
    if (!(n > 0)) return [];
    const rows = Array.from({ length: n }, () => new Array(n).fill('0'));
    svg.querySelectorAll('rect.homekit-qr-m').forEach((r) => {
      const x = +r.getAttribute('x') - quiet; const y = +r.getAttribute('y') - quiet; const w = +r.getAttribute('width');
      for (let i = 0; i < w; i++) if (rows[y] && x + i < n) rows[y][x + i] = '1';
    });
    return rows.map((r) => r.join(''));
  });
}

async function toasts(page) {
  return page.evaluate(() => [...document.querySelectorAll('#toast-area .toast')].map((t) => t.textContent.trim()));
}

/* ── Pass 1: state matrix + reasons, per theme ───────────────────────────── */
async function runStateMatrix(browser, base, theme) {
  console.log(`\n[${theme}] state matrix`);
  const { ctx, page, S } = await openPage(browser, base, { theme });
  let renders = 0;
  const r0 = await readCard(page);
  check(r0.exists.length === 0, `${theme}: every card element exists (missing: ${r0.exists.join(',') || 'none'})`);
  for (const c of CASES) {
    const tag = `${theme}/${c.name}`;
    if (!(await reach(page, S, c, tag))) continue;
    renders++;
    const s = await readCard(page);
    const line = await expandDynamic(page, c.line);
    if (line) check(s.msgVisible && s.msg === line, `${tag}: card line "${s.msg}" (visible=${s.msgVisible})`);
    else check(!s.msgVisible, `${tag}: no card line (got "${s.msg}")`);
    if (c.oneLine) check(s.msgLines === 1, `${tag}: the card line fits one line (${s.msgLines} lines)`);
    if (c.p && c.p.message) check(!String(s.msg || '').includes(c.p.message), `${tag}: the raw status message is not shown`);
    if (c.btn) check(s.enVisible && s.enText === c.btn && s.disabled.enable === false, `${tag}: footer «${s.enText}» usable (disabled=${s.disabled.enable})`);
    if (c.name === 'not_installed') check(s.disabled.enable === true, `${tag}: «Включить» locked (disabled=${s.disabled.enable})`);
    if (!c.noanswer) {
      check(s.linkVisible === c.link, `${tag}: «Подключить» ${c.link ? 'shown' : 'hidden'} (visible=${s.linkVisible})`);
      if (c.link) check(s.disabled.link === !!c.linkDisabled, `${tag}: «Подключить» ${c.linkDisabled ? 'locked (no Client ID)' : 'usable'} (disabled=${s.disabled.link})`);
      check(s.unlinkVisible === c.unlink, `${tag}: «Отключить аккаунт» ${c.unlink ? 'shown' : 'hidden'} (visible=${s.unlinkVisible})`);
      check(s.cidVisible === !c.cidHidden, `${tag}: Client ID editor ${c.cidHidden ? 'hidden' : 'present'}`);
    }
    if (c.code) {
      check(s.codeBoxVisible && s.codeText === USER_CODE, `${tag}: sign-in code shown ("${s.codeText}")`);
    } else {
      check(!s.codeInDom && !s.codeBoxVisible && s.qrRects === 0 && s.codeText === '' && !s.href,
        `${tag}: no sign-in code / link / QR in the DOM (inDom=${s.codeInDom}, rects=${s.qrRects}, href=${s.href})`);
    }
    if (c.apps) {
      check(s.appsListVisible && s.appsRows === APPS.length, `${tag}: appliance list shows ${s.appsRows} rows`);
      check(s.apps === '2 · в сети 1', `${tag}: «Приборы» reads "${s.apps}"`);
      check(s.stream === 'открыт', `${tag}: «Поток событий» reads "${s.stream}"`);
    }
    if (!c.noanswer && c.name !== 'not_installed') check(s.budget === '137 из 1000', `${tag}: budget line "${s.budget}"`);
    if (c.allDisabled) {
      const open = Object.entries(s.disabled).filter(([, v]) => v === false).map(([k]) => k);
      check(open.length === 0, `${tag}: every action locked while the board does not answer (usable: ${open.join(',') || 'none'})`);
    }
    await page.locator('#homeconnect-card').screenshot({ path: join(SHOTS, `homeconnect-card-${c.name}-${theme}.png`) });
  }

  console.log(`\n[${theme}] reasons`);
  for (const [reason, c] of Object.entries(REASON_CASES)) {
    const tag = `${theme}/reason:${reason}`;
    if (!(await reach(page, S, c, tag))) continue;
    renders++;
    const s = await readCard(page);
    const line = await expandDynamic(page, c.line);
    check(s.msgVisible && s.msg === line && !s.msg.includes(reason), `${tag}: line "${s.msg}"`);
    if (c.budget) check(s.budget === c.budget, `${tag}: budget line "${s.budget}"`);
  }
  check(S.errors.length === 0, `${theme}: no page errors (${S.errors.join(' | ')})`);
  await ctx.close();
  return renders;
}

/* ── Pass 2: the sign-in code lifecycle, per theme ───────────────────────── */
async function runCode(browser, base, theme) {
  console.log(`\n[${theme}] sign-in code lifecycle`);
  const { ctx, page, S } = await openPage(browser, base, { theme });
  let renders = 0;
  const awaiting = CASES.find((c) => c.name === 'awaiting_user');
  if (await reach(page, S, awaiting, `${theme}/code`)) {
    renders++;
    await page.waitForFunction(() => document.querySelectorAll('#homeconnect-qr rect.homekit-qr-m').length > 0, null, { timeout: REACH_MS }).catch(() => {});
    const s = await readCard(page);
    check(s.codeInDom && s.codeText === USER_CODE && s.codeBoxVisible, `${theme}/code: code shown in awaiting_user ("${s.codeText}")`);
    check(s.href === LINK_URL && s.target === '_blank' && /noopener/.test(s.rel || ''), `${theme}/code: link opens BSH's page in a new tab (href=${s.href}, target=${s.target}, rel=${s.rel})`);
    check(/^Код действует ещё \d+:\d\d$/.test(s.countdown || ''), `${theme}/code: countdown "${s.countdown}"`);
    const rows = await readQr(page);
    const diff = rows.length === GOLDEN_QR.length ? rows.reduce((n, r, y) => n + [...r].filter((ch, x) => ch !== GOLDEN_QR[y][x]).length, 0) : -1;
    check(diff === 0, `${theme}/code: drawn QR equals the golden matrix module for module (size ${rows.length}, differing modules ${diff})`);
    const qrBox = await page.evaluate(() => { const r = document.getElementById('homeconnect-qr').getBoundingClientRect(); return { w: r.width, h: r.height, d: getComputedStyle(document.getElementById('homeconnect-qr')).display }; });
    check(qrBox.w >= 120 && qrBox.h >= 120 && qrBox.d !== 'none', `${theme}/code: the QR is visible on screen (${qrBox.w.toFixed(0)}×${qrBox.h.toFixed(0)} px, display ${qrBox.d})`);
    const qr = await page.evaluate(() => {
      const m = document.querySelector('#homeconnect-qr rect.homekit-qr-m');
      const bg = document.querySelector('#homeconnect-qr rect.homekit-qr-bg');
      const rgb = (el) => (el ? (getComputedStyle(el).fill.match(/\d+(\.\d+)?/g) || []).slice(0, 3).map(Number) : []);
      return { fg: rgb(m), bg: rgb(bg) };
    });
    const lum = (c) => {
      if (c.length !== 3) return NaN;
      const [r, g, b] = c.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const lf = lum(qr.fg); const lb = lum(qr.bg);
    const ratio = (Math.max(lf, lb) + 0.05) / (Math.min(lf, lb) + 0.05);
    check(lf < lb && ratio >= 4.5, `${theme}/code: QR is dark on light, ${ratio.toFixed(1)}:1`);
    await page.locator('#homeconnect-card').screenshot({ path: join(SHOTS, `homeconnect-card-code-${theme}.png`) });

    // awaiting_user → connected: the code leaves the DOM.
    await reach(page, S, CASES.find((c) => c.name === 'connected'), `${theme}/code→connected`);
    let t = await readCard(page);
    check(!t.codeInDom && t.qrRects === 0 && !t.href && !t.codeBoxVisible, `${theme}/code: removed once connected (inDom=${t.codeInDom}, rects=${t.qrRects})`);

    // Back to awaiting, then leave the tab: removed.
    await reach(page, S, awaiting, `${theme}/code-again`);
    await page.waitForFunction((c) => document.documentElement.outerHTML.includes(c), USER_CODE, { timeout: REACH_MS }).catch(() => {});
    t = await readCard(page);
    check(t.codeInDom, `${theme}/code: shown again in awaiting_user`);
    await page.evaluate(() => window.switchTab('dashboard'));
    t = await readCard(page);
    check(!t.codeInDom && t.qrRects === 0 && !t.href, `${theme}/code: removed from the DOM on tab leave (inDom=${t.codeInDom})`);
    await page.evaluate(() => window.switchTab('system'));

    // Hostile or dead links are never drawn.
    const bad = [
      ['http (not https)', 'http://verify.home-connect.com/device?user_code=HCQR-7342'],
      ['foreign domain', 'https://home-connect.com.evil.example/device?user_code=HCQR-7342'],
      ['javascript: URL', 'javascript:window.__hcXss=9'],
    ];
    for (const [what, url] of bad) {
      const c = { p: () => st({ state: 'awaiting_user', link: { user_code: USER_CODE, verification_uri: url, verification_uri_complete: url, expires_at: future() } }), badge: 'ждёт входа' };
      if (!(await reach(page, S, c, `${theme}/bad-link:${what}`))) continue;
      t = await readCard(page);
      check(!t.codeInDom && !t.href && t.qrRects === 0, `${theme}/bad-link: ${what} → no code, link or QR (href=${t.href})`);
    }
    const expired = { p: () => st({ state: 'awaiting_user', link: { user_code: USER_CODE, verification_uri: LINK_URL, verification_uri_complete: LINK_URL, expires_at: Math.floor(Date.now() / 1000) - 5 } }), badge: 'ждёт входа' };
    if (await reach(page, S, expired, `${theme}/expired`)) {
      t = await readCard(page);
      check(!t.codeInDom && t.qrRects === 0, `${theme}/expired: an expired code is never drawn`);
    }
    const badCode = { p: () => st({ state: 'awaiting_user', link: { user_code: '<b>X</b>', verification_uri: LINK_URL, verification_uri_complete: LINK_URL, expires_at: future() } }), badge: 'ждёт входа' };
    if (await reach(page, S, badCode, `${theme}/bad-code`)) {
      t = await readCard(page);
      check(t.qrRects === 0 && !t.href && t.codeText === '', `${theme}/bad-code: a user_code outside the grammar is never drawn`);
    }
  }
  check(S.errors.length === 0, `${theme}/code: no page errors (${S.errors.join(' | ')})`);
  await ctx.close();
  return renders;
}

/* ── Pass 3: interactions (dark) ─────────────────────────────────────────── */
async function runInteractions(browser, base) {
  console.log('\n[dark] interactions');
  const { ctx, page, S } = await openPage(browser, base, { theme: 'dark' });
  let renders = 0;
  const connected = CASES.find((c) => c.name === 'connected');

  // Hostile appliance strings render as text.
  if (await reach(page, S, connected, 'xss')) {
    renders++;
    const x = await page.evaluate(() => {
      const list = document.getElementById('homeconnect-apps-list');
      const li = [...list.querySelectorAll('li')];
      return {
        rows: li.map((l) => ({
          name: (l.querySelector('.homeconnect-app-name') || {}).textContent || '',
          meta: (l.querySelector('.homeconnect-app-meta') || {}).textContent || '',
          on: (l.querySelector('.badge') || {}).textContent || '',
        })),
        imgs: list.querySelectorAll('img').length,
        xss: window.__hcXss,
      };
    });
    check(x.rows.length === 2, `xss: precondition — 2 appliance rows (${x.rows.length})`);
    check(x.imgs === 0 && x.xss === undefined, `xss: no <img> injected, no handler ran (imgs=${x.imgs}, xss=${x.xss})`);
    check((x.rows[1] || {}).name === XSS_NAME, `xss: a hostile name renders as text ("${(x.rows[1] || {}).name}")`);
    check((x.rows[1] || {}).meta === `${XSS_TYPE} · ${XSS_BRAND}`, `xss: hostile type/brand render as text ("${(x.rows[1] || {}).meta}")`);
    check((x.rows[0] || {}).on === 'в сети' && (x.rows[1] || {}).on === 'не в сети', `xss: connected badges "${(x.rows[0] || {}).on}" / "${(x.rows[1] || {}).on}"`);
  }

  // Pending action: every control locked, a second action sends nothing.
  const unlinked = CASES.find((c) => c.name === 'unlinked');
  if (await reach(page, S, unlinked, 'pending')) {
    renders++;
    let s = await readCard(page);
    check(s.disabled.link === false && s.disabled.enable === false, 'pending: precondition — «Подключить» and the footer usable');
    let release;
    S.hold = new Promise((ok) => { release = ok; });
    const n0 = S.posts.length;
    await page.click('#homeconnect-btn-link');
    await page.waitForFunction(() => document.getElementById('homeconnect-btn-enable').disabled, null, { timeout: REACH_MS }).catch(() => {});
    s = await readCard(page);
    const open = Object.entries(s.disabled).filter(([, v]) => v === false).map(([k]) => k);
    check(S.posts.length === n0 + 1 && S.posts[n0].action === 'link', `pending: the action was sent once (${JSON.stringify(S.posts.slice(n0))})`);
    check(open.length === 0, `pending: every control disabled while the action is in flight (usable: ${open.join(',') || 'none'})`);
    await page.evaluate(() => { window.homeconnectToggle(); window.homeconnectLink(); window.homeconnectSaveClientId(); window.homeconnectUnlink(); });
    await page.waitForTimeout(200);
    check(S.posts.length === n0 + 1, `pending: a second action during the first sent nothing (POSTs ${S.posts.length - n0})`);
    S.hold = null;
    release();
    await page.waitForFunction(() => !document.getElementById('homeconnect-btn-enable').disabled, null, { timeout: REACH_MS }).catch(() => {});
    s = await readCard(page);
    check(s.disabled.enable === false, 'pending: controls unlocked once the answer arrived');
  }

  // Unlink confirm.
  if (await reach(page, S, connected, 'unlink')) {
    renders++;
    const TEXT = 'Отключить аккаунт? Приборы пропадут из MQTT до нового входа.';
    S.dialogs.length = 0;
    S.dialogAccept = false;
    const count = () => S.posts.filter((b) => b.action === 'unlink').length;
    const n0 = count();
    await page.click('#homeconnect-btn-unlink');
    await page.waitForTimeout(250);
    check(S.dialogs.length === 1 && S.dialogs[0] === TEXT, `unlink: confirm asked with the plan text ("${S.dialogs[0] || '(none)'}")`);
    check(count() === n0, 'unlink: dismissing the confirm sends nothing');
    S.dialogAccept = true;
    await page.click('#homeconnect-btn-unlink');
    await page.waitForFunction(() => !document.getElementById('homeconnect-btn-enable').disabled, null, { timeout: REACH_MS }).catch(() => {});
    check(count() === n0 + 1, 'unlink: accepting the confirm sends unlink once');
  }

  // Client ID: refused locally when malformed, sent when valid.
  if (await reach(page, S, CASES.find((c) => c.name === 'missing_client_id'), 'client-id')) {
    renders++;
    const open = await page.evaluate(() => document.getElementById('homeconnect-cid').open);
    check(open === true, 'client-id: the editor opens by itself for missing_client_id');
    const n0 = S.posts.length;
    await page.fill('#homeconnect-client-in', 'bad id!');
    await page.click('#homeconnect-btn-client');
    await page.waitForTimeout(250);
    const t = await toasts(page);
    check(S.posts.length === n0, `client-id: a malformed id sends nothing (POSTs ${S.posts.length - n0})`);
    check(t.includes('Недопустимый Client ID'), `client-id: the refusal is explained (${JSON.stringify(t.slice(-1))})`);
    await page.fill('#homeconnect-client-in', 'MyHcClientId_0002');
    await page.click('#homeconnect-btn-client');
    await page.waitForFunction((n) => !document.getElementById('homeconnect-btn-enable').disabled, n0, { timeout: REACH_MS }).catch(() => {});
    await page.waitForTimeout(150);
    const sent = S.posts.slice(n0);
    check(sent.length === 1 && sent[0].action === 'set_client_id' && sent[0].client_id === 'MyHcClientId_0002', `client-id: a valid id is sent once (${JSON.stringify(sent)})`);
  }

  // Every dispatch error code answers a Russian toast, never the raw code.
  console.log('\n[dark] dispatch error codes');
  if (await reach(page, S, unlinked, 'errors')) {
    for (const code of DOC_ERRORS) {
      S.reply = () => ({ ok: false, error: code });
      await page.evaluate(() => { const a = document.getElementById('toast-area'); if (a) a.textContent = ''; });
      await page.evaluate(() => window.homeconnectLink());
      await page.waitForTimeout(80);
      const t = await toasts(page);
      const last = t[t.length - 1] || '';
      check(last && !last.includes(code) && /[а-яё]/i.test(last), `error "${code}" → toast "${last}"`);
      if (code === 'not_installed') await reach(page, S, unlinked, 'errors-restore');
    }
    S.reply = null;
  }

  // Both hardware variants: the card carries no data-hide-for and stays shown.
  console.log('\n[dark] hardware variants');
  for (const v of ['sa02m-1eth', 'sa02m-2eth']) {
    const shown = await page.evaluate((variant) => {
      window.applyVariantVisibility(variant);
      const el = document.getElementById('homeconnect-card');
      const r = el.getBoundingClientRect();
      return !el.hidden && r.width > 0 && r.height > 0 && getComputedStyle(el).display !== 'none';
    }, v);
    check(shown, `variant ${v}: #homeconnect-card shown`);
    await page.locator('#homeconnect-card').screenshot({ path: join(SHOTS, `homeconnect-card-variant-${v}-dark.png`) });
  }

  // Poll only while «Управление» is shown.
  console.log('\n[dark] polling');
  S.hc = P.connected;
  const g0 = S.gets;
  await page.waitForTimeout(10600);
  const onTab = S.gets - g0;
  check(onTab >= 1, `poll: at least one status GET in 10.6 s on «Управление» (${onTab})`);
  await page.evaluate(() => window.switchTab('dashboard'));
  const g1 = S.gets;
  await page.waitForTimeout(10600);
  check(S.gets === g1, `poll: no status GET in 10.6 s after leaving «Управление» (${S.gets - g1})`);
  await page.evaluate(() => window.switchTab('system'));

  check(S.errors.length === 0, `interactions: no page errors (${S.errors.join(' | ')})`);
  await ctx.close();
  return renders;
}

/* ── Pass 4: ≤560 px, both themes ────────────────────────────────────────── */
async function runNarrow(browser, base, theme) {
  console.log(`\n[${theme}] 500 px`);
  const { ctx, page, S } = await openPage(browser, base, { theme, width: 500 });
  let renders = 0;
  if (await reach(page, S, CASES.find((c) => c.name === 'awaiting_user'), `${theme}/narrow`)) {
    renders++;
    await page.waitForFunction(() => document.querySelectorAll('#homeconnect-qr rect.homekit-qr-m').length > 0, null, { timeout: REACH_MS }).catch(() => {});
    const g = await page.evaluate(() => {
      const card = document.getElementById('homeconnect-card');
      const host = card.parentElement;
      const cs = getComputedStyle(host);
      const content = host.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
      const cr = card.getBoundingClientRect();
      const qr = document.getElementById('homeconnect-qr').getBoundingClientRect();
      return { cardW: cr.width, content, overflow: card.scrollWidth - card.clientWidth, qrInside: qr.left >= cr.left - 0.5 && qr.right <= cr.right + 0.5 && qr.width > 0, pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
    });
    check(Math.abs(g.cardW - g.content) <= 2, `${theme}/narrow: card spans its column (${g.cardW.toFixed(1)} of ${g.content.toFixed(1)} px)`);
    check(g.overflow <= 1 && g.pageOverflow <= 1, `${theme}/narrow: nothing overflows (card +${g.overflow}px, page +${g.pageOverflow}px)`);
    check(g.qrInside, `${theme}/narrow: the QR sits inside the card`);
    await page.locator('#homeconnect-card').screenshot({ path: join(SHOTS, `homeconnect-card-narrow-${theme}.png`) });
  }
  if (await reach(page, S, CASES.find((c) => c.name === 'connected'), `${theme}/narrow-connected`)) {
    renders++;
    const g = await page.evaluate(() => {
      const card = document.getElementById('homeconnect-card');
      return { overflow: card.scrollWidth - card.clientWidth, pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
    });
    check(g.overflow <= 1 && g.pageOverflow <= 1, `${theme}/narrow-connected: the appliance list overflows nothing (card +${g.overflow}px, page +${g.pageOverflow}px)`);
    await page.locator('#homeconnect-card').screenshot({ path: join(SHOTS, `homeconnect-card-narrow-connected-${theme}.png`) });
  }
  check(S.errors.length === 0, `${theme}/narrow: no page errors (${S.errors.join(' | ')})`);
  await ctx.close();
  return renders;
}

async function run() {
  const srv = await listen();
  const base = `http://127.0.0.1:${srv.address().port}`;
  let browser;
  try {
    browser = await pw.chromium.launch();
  } catch (e) {
    const msg = String(e && e.message || e);
    if (/Executable doesn't exist|npx playwright install|Looks like Playwright/i.test(msg)) {
      srv.close();
      die(2, MISSING_MSG, `  reason: the chromium browser failed to launch from playwright's cache — ${msg.split('\n')[0]}`);
    }
    throw e;
  }
  mkdirSync(SHOTS, { recursive: true });
  let rendered = 0;
  try {
    for (const theme of THEMES) rendered += await runStateMatrix(browser, base, theme);
    for (const theme of THEMES) rendered += await runCode(browser, base, theme);
    rendered += await runInteractions(browser, base);
    for (const theme of THEMES) rendered += await runNarrow(browser, base, theme);
  } finally {
    await browser.close();
    srv.close();
  }
  if (rendered === 0) die(1, `${TAG}: ERROR — nothing was rendered; a pass without a render is not a pass`);
  if (failures) die(1, `\n${TAG}: ${failures} FAILURE(S) of ${assertions} assertions`);
  console.log(`\n${TAG}: PASS — ${assertions} assertions: ${DOC_STATES.length} contract states + ${DOC_REASONS.length} reasons × ${THEMES.length} themes, code lifecycle, ${DOC_ERRORS.length} error codes, interactions, polling, 500 px (${rendered} renders)`);
  process.exit(0);
}

run().catch((e) => {
  console.error(`${TAG}: ERROR — ${e && e.stack || e}`);
  process.exit(1);
});
