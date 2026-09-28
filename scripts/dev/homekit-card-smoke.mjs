#!/usr/bin/env node
// comment-mutation-proof-exempt: behavioural harness - renders the SHIPPED bundles in headless Chromium and asserts rendered DOM/text per contract state; it pins no source line by text, every state assertion is preceded by a reached-state precondition, and the state and skip-reason lists are read from the contract so a missing or extra fixture FAILS.
/* ═══════════════════════════════════════════════════════════════════════════
   homekit-card-smoke — the «Apple HomeKit» card and the «Умный дом» HomeKit
   toggle, rendered for real.
   ───────────────────────────────────────────────────────────────────────────
   What it closes: the card's promises live in app/homekit.js + index.html and
   no other gate renders them (ui-layout stubs every CGI with `{}`, so the card
   never leaves «…»). This row serves www/network_config statically, stubs
   cgi-bin/sa02m_homekit_api.cgi (switchable INSIDE one page, like the bench)
   and cgi-bin/sa02m_alice_api.cgi, and asserts:
     * every contract state (docs/contracts/homekit-bridge.md §10 table) has a
       fixture and none is undocumented; per state and both themes: the «Мост»
       badge (reached-state precondition), the standing card line
       (not_installed / missing_deps / no_interface / port_in_use / error, the
       stale-heartbeat «не отвечает», the no-answer «н/д» + «Нет ответа от
       платы»), the footer button label and enabled state, the setup block only
       in running ∧ unpaired;
     * the setup code is NOT in the DOM and was never even requested before
       «Показать код»; after the click it is shown with a QR of > 0 module rects
       (dark on light in both themes); it leaves the DOM when the status turns
       paired and when the tab is left;
     * the interface picker offers only the wired ports the status reports
       (never ppp/wwan/usb);
     * every control is disabled while an action is pending, and a second
       action during it sends nothing;
     * «Сбросить сопряжение» asks the plan's confirm text; dismiss sends
       nothing, accept sends `reset_pairing`;
     * every contract skip reason renders its Russian label (never the raw
       code); a device name / reason carrying `<img onerror>` and quotes
       renders as text;
     * the card stays visible when the service catalogue reports the HomeKit
       unit stopped or not installed (its own card renders those states and
       carries «Включить»), while #alice-card IS hidden by the same list — the
       non-vacuous proof the visibility pass ran;
     * ≤560 px: the card spans its column and overflows nothing;
     * «Умный дом»: while the module is not installed the toggle is hidden, an
       edited device keeps its stored `homekit_visible` (true AND false) and a
       new device is saved WITHOUT the key; once installed the toggle prefills
       from the stored flag, its value is saved (both directions), a new device
       defaults to false, «Отметить все» upserts only the not-yet-exposed
       devices; a device name / id with `<img onerror>` and quotes renders as
       text (escHtml / escAttr).
   The QR matrix is a synthetic 25×25 bit pattern (finder squares + a fixed
   fill): the card draws whatever valid matrix the dispatch returns, so a
   scannable code is not what is under test here.

   Exit codes (the runner has no skip signal distinct from success):
     0  every assertion passed (never without a real render);
     1  an assertion failed (named), a contract table is missing/empty, a
        fixture drifted from the contract, or ANY other error;
     2  chromium/playwright missing — deliberately RED, never a vacuous green
        (same policy as cloud-card-smoke).

   Mutation proof (recorded in the commit body): make hkRender call
   homekitShowCode() whenever the setup block opens (the code rendered on
   load) -> "setup code absent before «Показать код»" FAILS in every theme.
   HOMEKIT_CARD_SMOKE_WWW points the static server at a scratch copy for that.

   Harness: the standing Playwright install under scripts/dev (npm run
   ui-layout:install — the chromium ui-layout and cloud-card-smoke reuse).
   Dev-only; never shipped to the device.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync, existsSync, mkdirSync, statSync } from 'node:fs';
import http from 'node:http';
import { join, dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const TAG = 'homekit-card-smoke';
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '..', '..');
const WWW = process.env.HOMEKIT_CARD_SMOKE_WWW
  ? resolve(process.env.HOMEKIT_CARD_SMOKE_WWW)
  : join(REPO, 'www', 'network_config');
const CONTRACT = join(REPO, 'docs', 'contracts', 'homekit-bridge.md');
const SHOTS = join(REPO, '.ai-dev', 'quality', 'screenshots');
const MISSING_MSG = `${TAG}: chromium/playwright missing — run: npm run ui-layout:install`;
const THEMES = ['dark', 'light'];
const REACH_MS = 6000;

function die(code, ...lines) {
  for (const l of lines) (code === 0 ? console.log : console.error)(l);
  process.exit(code);
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

/** First-column `code` tokens of the table that follows `startMarker`, up to
    `endMarker`. Empty ⇒ exit 1: an empty list would make every drift check
    vacuous. */
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
const DOC_STATES = contractCodes('**Состояния**', '**Причины**', 'state (§10)');
const DOC_SKIP = contractCodes('## 4. Причины пропуска', '\n## 5.', 'skip-reason (§4)');

/* ── Fixtures ────────────────────────────────────────────────────────────── */
const CODE = '482-91-736';
// Separate globals per surface, so an injection on one cannot fail the other's check.
const XSS_NAME = '<img src=x onerror="window.__hkXss=1">"\'';
const XSS_REASON = '<img src=x onerror="window.__hkXss=2">';
const XSS_SH_NAME = '<img src=x onerror="window.__shXss=1">"\'';
const WIRED_1ETH = [
  { name: 'eth0', present: true, address: '192.168.1.136' },
  { name: 'eth1', present: false, address: '' },
  // Never offered, whatever the dispatch sends: the bridge binds one wired port.
  { name: 'ppp0', present: true, address: '10.64.0.2' },
  { name: 'wwan0', present: true, address: '10.64.0.3' },
  { name: 'usb0', present: true, address: '192.168.8.100' },
];
const WIRED_2ETH = WIRED_1ETH.map((it) => (it.name === 'eth1' ? { ...it, present: true, address: '10.0.0.5' } : it));

// Synthetic 25×25 matrix: three finder squares + a fixed pseudo-random fill.
const QR = (() => {
  const n = 25;
  const rows = [];
  const finder = (x, y, ox, oy) => {
    const dx = x - ox; const dy = y - oy;
    if (dx < 0 || dy < 0 || dx > 6 || dy > 6) return null;
    const ring = Math.max(Math.abs(dx - 3), Math.abs(dy - 3));
    return ring === 2 ? '0' : '1';
  };
  for (let y = 0; y < n; y++) {
    let r = '';
    for (let x = 0; x < n; x++) {
      r += finder(x, y, 0, 0) || finder(x, y, n - 7, 0) || finder(x, y, 0, n - 7) || (((x * 7 + y * 13 + x * y) % 5) < 2 ? '1' : '0');
    }
    rows.push(r);
  }
  return rows;
})();

const SKIPPED = [
  { device_id: 'd7', name: XSS_NAME, item: 'properties.float:voltage', reason: 'no_homekit_type' },
  { device_id: 'd8', name: 'Насос подпитки', item: null, reason: XSS_REASON },
  // One row per contract reason: each must render its Russian label.
  ...DOC_SKIP.map((r, i) => ({ device_id: 'dr' + i, name: 'Устройство ' + i, item: 'properties.float:x' + i, reason: r })),
];

const COMMON = {
  ok: true, reason: '', message: '', interface: 'eth0', address: '', port: 21064, bridge_name: 'SA-02m 1a2b3c',
  paired: null, pairings: 0, accessories: 0, skipped_total: 0, skipped: [], accessory_list: [],
  pair_setup_locked: false, setup_available: false, interfaces: WIRED_1ETH,
  port_default: 21064, port_min: 1024, port_max: 65535, forbidden_ports: [502, 1880, 1883, 1884, 4840, 8765, 9999],
  version: '1.0.6.57', ts: 0,
};
const st = (o) => ({ ...COMMON, ...o });
const P = {
  not_installed: { ok: true, state: 'not_installed', reason: '', message: 'HomeKit module is not installed', enabled: false, setup_available: false },
  disabled: st({ state: 'disabled', enabled: false }),
  starting: st({ state: 'starting', enabled: true }),
  unpaired: st({ state: 'running', enabled: true, address: '192.168.1.136', paired: false, accessories: 6, setup_available: true }),
  paired: st({ state: 'running', enabled: true, address: '192.168.1.136', paired: true, pairings: 2, accessories: 6 }),
  regenerated: st({ state: 'running', reason: 'identity_regenerated', enabled: true, address: '192.168.1.136', paired: false, setup_available: true }),
  missing_deps: st({ state: 'missing_deps', enabled: true, message: 'missing: segno (needs segno)' }),
  no_interface: st({ state: 'no_interface', enabled: true }),
  port_in_use: st({ state: 'port_in_use', enabled: true }),
  error: st({ state: 'error', enabled: true }),
  stale: st({ state: 'error', reason: 'status_stale', enabled: true }),
};

/* The state matrix. `badge` = the «Мост» label (plan card spec,
   HOMEKIT_STATE_MAP); `line` = the standing card line (null = none);
   `btn` = footer label or null when disabled/not asserted; `setup` = the setup
   block is shown. `noanswer` = the CGI answers 502 HTML. */
const CASES = [
  { name: 'running-unpaired', p: P.unpaired, badge: 'работает', line: null, btn: 'Выключить', setup: true, paired: 'не сопряжено' },
  { name: 'running-paired', p: P.paired, badge: 'работает', line: null, btn: 'Выключить', setup: false, paired: 'сопряжено: 2', resetVisible: true },
  { name: 'identity_regenerated', p: P.regenerated, badge: 'работает', line: 'Мост создан заново — добавьте его в «Дом» повторно', btn: 'Выключить', setup: true },
  { name: 'not_installed', p: P.not_installed, badge: 'не установлен', line: 'Нужна полная установка (install.sh)', btn: null, setup: false, netHidden: true, resetHidden: true },
  { name: 'disabled', p: P.disabled, badge: 'выключен', line: null, btn: 'Включить', setup: false },
  { name: 'starting', p: P.starting, badge: 'запуск…', line: null, btn: 'Выключить', setup: false },
  { name: 'missing_deps', p: P.missing_deps, badge: 'нет компонентов', line: 'Не установлены компоненты HomeKit — нужна полная установка (install.sh)', btn: 'Выключить', setup: false },
  { name: 'no_interface', p: P.no_interface, badge: 'нет сети', line: 'На выбранном интерфейсе нет адреса', btn: 'Выключить', setup: false },
  { name: 'port_in_use', p: P.port_in_use, badge: 'порт занят', line: 'Порт занят другой программой — выберите другой', btn: 'Выключить', setup: false },
  { name: 'error', p: P.error, badge: 'ошибка', line: 'Ошибка моста — подробности в журнале sa02m-homekit', btn: 'Выключить', setup: false },
  { name: 'error-status_stale', p: P.stale, badge: 'не отвечает', line: 'Мост не отвечает — статус устарел', btn: 'Выключить', setup: false },
  { name: 'no-answer', p: null, noanswer: true, badge: 'н/д', line: 'Нет ответа от платы', btn: null, setup: false, allDisabled: true },
];

// Non-vacuous both ways: the contract states and the fixture states are one set.
const fixturedStates = [...new Set(CASES.filter((c) => c.p).map((c) => c.p.state))];
for (const s of DOC_STATES) check(fixturedStates.includes(s), `contract state "${s}" has a card fixture`);
for (const s of fixturedStates) check(DOC_STATES.includes(s), `fixture state "${s}" is a documented contract state (no drift)`);
check(DOC_SKIP.length >= 8, `contract skip-reason table parsed (${DOC_SKIP.length} reasons)`);
if (failures) die(1, `${TAG}: ${failures} FAILURE(S) — the fixtures do not match the contract tables`);

const ALICE_DEVICES = [
  { id: 'd1', name: 'Свет кухня', type: 'devices.types.light', room_id: 'r1', homekit_visible: true,
    capabilities: [{ type: 'devices.capabilities.on_off', mqtt: '/devices/SA-02m/controls/DO1' }], properties: [] },
  { id: 'd2', name: 'Розетка', type: 'devices.types.socket', room_id: 'r1',
    capabilities: [{ type: 'devices.capabilities.on_off', mqtt: '/devices/SA-02m/controls/DO2' }], properties: [] },
  { id: 'd3', name: 'Вентилятор', type: 'devices.types.socket', room_id: 'r1', homekit_visible: false,
    capabilities: [{ type: 'devices.capabilities.on_off', mqtt: '/devices/SA-02m/controls/DO3' }], properties: [] },
  { id: 'd"\'x', name: XSS_SH_NAME, type: 'devices.types.socket', room_id: 'r1',
    capabilities: [{ type: 'devices.capabilities.on_off', mqtt: '/devices/SA-02m/controls/DO4' }], properties: [] },
];
const ALICE = {
  ok: true, client_enabled: true, gateway: { available: true }, status: { state: 'connected' }, mtls: { cert_present: true },
  link: { linked: true, pending: false, registration_url: null, state: 'connected' },
  devices: { rooms: [{ id: 'r1', name: 'Кухня' }], devices: ALICE_DEVICES },
};

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
    srv.listen(Number(process.env.HOMEKIT_CARD_SMOKE_PORT || 0), '127.0.0.1', () => ok(srv));
  });
}

/* ── Page with switchable stubs ──────────────────────────────────────────── */
async function openPage(browser, base, { width = 1280, theme = 'dark' } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 1000 } });
  await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
  const page = await ctx.newPage();
  const S = {
    hk: P.disabled, noanswer: false, hold: null, posts: [], alicePosts: [], dialogs: [], dialogAccept: true, errors: [],
  };
  page.on('pageerror', (e) => S.errors.push(String(e && e.message || e)));
  page.on('dialog', (d) => { S.dialogs.push(d.message()); (S.dialogAccept ? d.accept() : d.dismiss()).catch(() => {}); });
  const json = (r, o) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(o) });
  await page.route('**/cgi-bin/**', async (r) => {
    const req = r.request();
    const url = req.url();
    if (/sa02m_homekit_api\.cgi/.test(url)) {
      if (req.method() === 'POST') {
        let body = {};
        try { body = JSON.parse(req.postData() || '{}'); } catch { body = {}; }
        S.posts.push(body);
        if (S.hold) await S.hold;
        if (body.action === 'setup') {
          const open = S.hk && S.hk.state === 'running' && S.hk.paired === false && S.hk.setup_available;
          return json(r, open
            ? { ok: true, available: true, code: CODE, setup_uri: 'X-HM://0023ISYWYABCD', setup_id: 'ABCD', qr: QR }
            : { ok: false, error: 'not_available' });
        }
        return json(r, { ok: true, action: body.action, status: S.hk });
      }
      if (S.noanswer) return r.fulfill({ status: 502, contentType: 'text/html', body: '<html>502 Bad Gateway</html>' });
      return json(r, S.hk);
    }
    if (/sa02m_alice_api\.cgi/.test(url)) {
      if (req.method() === 'POST') {
        let body = {};
        try { body = JSON.parse(req.postData() || '{}'); } catch { body = {}; }
        S.alicePosts.push(body);
        return json(r, { ok: true });
      }
      return json(r, ALICE);
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

// Two sequential refreshes: the second can never be an in-flight request that
// was issued for the previous stub.
async function refresh(page) {
  await page.evaluate(async () => { await window.homekitRefresh(); await window.homekitRefresh(); });
}

async function reach(page, S, c, tag) {
  S.noanswer = !!c.noanswer;
  if (c.p) S.hk = c.p;
  await refresh(page);
  try {
    await page.waitForFunction((want) => {
      const el = document.getElementById('homekit-state');
      return !!el && el.textContent.trim() === want;
    }, c.badge, { timeout: REACH_MS });
    return true;
  } catch {
    const got = await page.evaluate(() => (document.getElementById('homekit-state') || {}).textContent || '(absent)');
    check(false, `${tag}: state reached — «Мост» expected "${c.badge}", got "${String(got).trim()}"`);
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
    return {
      exists: ['homekit-card', 'homekit-state', 'homekit-paired', 'homekit-msg', 'homekit-setup', 'homekit-btn-code',
        'homekit-code-box', 'homekit-qr', 'homekit-code', 'homekit-net', 'homekit-iface-sel', 'homekit-port-in',
        'homekit-btn-net', 'homekit-btn-reset', 'homekit-btn-enable', 'homekit-skip-line', 'homekit-skip-list',
        'homekit-skip-link'].filter((id) => !$(id)),
      badge: txt('homekit-state'),
      paired: txt('homekit-paired'),
      msgVisible: vis('homekit-msg'),
      msg: txt('homekit-msg'),
      setupVisible: vis('homekit-setup'),
      codeBtnVisible: vis('homekit-btn-code'),
      codeBoxVisible: vis('homekit-code-box'),
      codeInDom: document.documentElement.outerHTML.includes(code),
      codeText: txt('homekit-code'),
      qrRects: document.querySelectorAll('#homekit-qr rect.homekit-qr-m').length,
      netVisible: vis('homekit-net'),
      resetVisible: vis('homekit-btn-reset'),
      enVisible: vis('homekit-btn-enable'),
      enText: txt('homekit-btn-enable'),
      disabled: {
        enable: dis('homekit-btn-enable'), reset: dis('homekit-btn-reset'), code: dis('homekit-btn-code'),
        net: dis('homekit-btn-net'), iface: dis('homekit-iface-sel'), port: dis('homekit-port-in'),
      },
      ifaceOptions: [...document.querySelectorAll('#homekit-iface-sel option')].map((o) => o.value),
      ifaceValue: $('homekit-iface-sel') ? $('homekit-iface-sel').value : null,
    };
  }, CODE);
}

const setupPosts = (S) => S.posts.filter((b) => b.action === 'setup').length;

async function clickShowCode(page, S, tag) {
  const before = setupPosts(S);
  await page.click('#homekit-btn-code');
  try {
    await page.waitForFunction(() => document.querySelectorAll('#homekit-qr rect.homekit-qr-m').length > 0, null, { timeout: REACH_MS });
  } catch { /* asserted below */ }
  check(setupPosts(S) === before + 1, `${tag}: «Показать код» requested the code exactly once`);
}

/* ── Pass 1: state matrix + code lifecycle, per theme ────────────────────── */
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
    check(s.badge === c.badge, `${tag}: «Мост» reads "${s.badge}"`);
    if (c.line) check(s.msgVisible && s.msg === c.line, `${tag}: card line "${s.msg}" (visible=${s.msgVisible})`);
    else check(!s.msgVisible, `${tag}: no card line (got "${s.msg}")`);
    if (c.paired) check(s.paired === c.paired, `${tag}: «Сопряжение» reads "${s.paired}"`);
    check(s.setupVisible === c.setup, `${tag}: setup block ${c.setup ? 'shown' : 'hidden'} (visible=${s.setupVisible})`);
    check(!s.codeInDom && s.qrRects === 0 && s.codeText === '', `${tag}: setup code absent before «Показать код» (inDom=${s.codeInDom}, rects=${s.qrRects})`);
    check(setupPosts(S) === 0, `${tag}: the code was never requested without a click (setup POSTs=${setupPosts(S)})`);
    if (c.btn) check(s.enVisible && s.enText === c.btn && s.disabled.enable === false, `${tag}: footer «${s.enText}» usable (disabled=${s.disabled.enable})`);
    if (c.name === 'not_installed') check(s.disabled.enable === true, `${tag}: «Включить» locked (disabled=${s.disabled.enable})`);
    if (c.netHidden) check(!s.netVisible, `${tag}: interface/port editor hidden`);
    else if (!c.noanswer) check(s.netVisible, `${tag}: interface/port editor present`);
    if (c.resetHidden) check(!s.resetVisible, `${tag}: «Сбросить сопряжение» hidden`);
    if (c.resetVisible) check(s.resetVisible && s.disabled.reset === false, `${tag}: «Сбросить сопряжение» usable`);
    if (c.setup) check(s.codeBtnVisible && s.disabled.code === false, `${tag}: «Показать код» usable`);
    if (c.allDisabled) {
      const open = Object.entries(s.disabled).filter(([, v]) => v === false).map(([k]) => k);
      check(open.length === 0, `${tag}: every action locked while the board does not answer (usable: ${open.join(',') || 'none'})`);
    }
    await page.locator('#homekit-card').screenshot({ path: join(SHOTS, `homekit-card-${c.name}-${theme}.png`) });
  }

  // Code lifecycle: click → shown; paired → gone; tab leave → gone.
  console.log(`\n[${theme}] setup code lifecycle`);
  const unpaired = CASES[0];
  if (await reach(page, S, unpaired, `${theme}/code`)) {
    renders++;
    let s = await readCard(page);
    check(!s.codeInDom && setupPosts(S) === 0, `${theme}/code: absent from the DOM and never requested before the click`);
    await clickShowCode(page, S, `${theme}/code`);
    s = await readCard(page);
    check(s.codeInDom && s.codeText === CODE && s.codeBoxVisible, `${theme}/code: code shown after the click ("${s.codeText}")`);
    check(s.qrRects > 0, `${theme}/code: QR drawn with ${s.qrRects} module rects (> 0)`);
    const qr = await page.evaluate(() => {
      const m = document.querySelector('#homekit-qr rect.homekit-qr-m');
      const bg = document.querySelector('#homekit-qr rect.homekit-qr-bg');
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
    check(lf < lb && ratio >= 4.5, `${theme}/code: QR is dark on light, ${ratio.toFixed(1)}:1 (fg ${qr.fg}, bg ${qr.bg})`);
    await page.locator('#homekit-card').screenshot({ path: join(SHOTS, `homekit-card-code-${theme}.png`) });

    await reach(page, S, CASES[1], `${theme}/code→paired`);
    s = await readCard(page);
    check(!s.codeInDom && s.qrRects === 0 && !s.setupVisible, `${theme}/code: removed from the DOM once paired (inDom=${s.codeInDom}, rects=${s.qrRects})`);

    await reach(page, S, unpaired, `${theme}/code→unpaired`);
    s = await readCard(page);
    check(!s.codeInDom, `${theme}/code: not re-shown on its own after unpair`);
    await clickShowCode(page, S, `${theme}/code-again`);
    s = await readCard(page);
    check(s.codeInDom, `${theme}/code: shown again on a second click`);
    await page.evaluate(() => window.switchTab('dashboard'));
    s = await readCard(page);
    check(!s.codeInDom && s.qrRects === 0, `${theme}/code: removed from the DOM on tab leave (inDom=${s.codeInDom})`);
    await page.evaluate(() => window.switchTab('system'));
  }
  check(S.errors.length === 0, `${theme}: no page errors (${S.errors.join(' | ')})`);
  await ctx.close();
  return renders;
}

/* ── Pass 2: interactions (dark) ─────────────────────────────────────────── */
async function runInteractions(browser, base) {
  console.log('\n[dark] interactions');
  const { ctx, page, S } = await openPage(browser, base, { theme: 'dark' });
  let renders = 0;

  // Interface picker: wired ports from the status only.
  const d1 = { name: 'iface-1eth', p: st({ ...P.disabled, interfaces: WIRED_1ETH }), badge: 'выключен' };
  if (await reach(page, S, d1, 'iface-1eth')) {
    renders++;
    const s = await readCard(page);
    check(JSON.stringify(s.ifaceOptions) === '["eth0"]', `iface-1eth: picker offers ${JSON.stringify(s.ifaceOptions)} (eth0 only; no ppp/wwan/usb, absent eth1 not offered)`);
  }
  const d2 = { name: 'iface-2eth', p: st({ ...P.disabled, interfaces: WIRED_2ETH }), badge: 'выключен' };
  S.hk = d2.p;
  await refresh(page);
  await page.waitForFunction(() => document.querySelectorAll('#homekit-iface-sel option').length === 2, null, { timeout: REACH_MS }).catch(() => {});
  {
    const s = await readCard(page);
    check(JSON.stringify(s.ifaceOptions) === '["eth0","eth1"]', `iface-2eth: picker offers ${JSON.stringify(s.ifaceOptions)}`);
  }
  S.hk = st({ ...P.disabled, interface: 'eth1', interfaces: WIRED_1ETH });
  await refresh(page);
  await page.waitForFunction(() => document.querySelectorAll('#homekit-iface-sel option').length === 2, null, { timeout: REACH_MS }).catch(() => {});
  {
    const s = await readCard(page);
    check(JSON.stringify(s.ifaceOptions) === '["eth0","eth1"]' && s.ifaceValue === 'eth1',
      `iface-configured-absent: the stored eth1 stays representable (${JSON.stringify(s.ifaceOptions)}, value ${s.ifaceValue}); still no modem`);
  }

  // Pending action: every control locked, a second action sends nothing.
  if (await reach(page, S, CASES[0], 'pending')) {
    renders++;
    let s = await readCard(page);
    const pre = Object.entries(s.disabled).filter(([k, v]) => k !== 'reset' && v !== false).map(([k]) => k);
    check(pre.length === 0, `pending: precondition — controls usable before the action (locked: ${pre.join(',') || 'none'})`);
    let release;
    S.hold = new Promise((ok) => { release = ok; });
    const nBefore = S.posts.length;
    await page.click('#homekit-btn-enable');
    await page.waitForFunction(() => document.getElementById('homekit-btn-enable').disabled, null, { timeout: REACH_MS }).catch(() => {});
    s = await readCard(page);
    const open = Object.entries(s.disabled).filter(([, v]) => v === false).map(([k]) => k);
    check(S.posts.length === nBefore + 1 && S.posts[S.posts.length - 1].action === 'disable', `pending: the action was sent once (${JSON.stringify(S.posts.slice(nBefore))})`);
    check(open.length === 0, `pending: every control disabled while the action is in flight (usable: ${open.join(',') || 'none'})`);
    await page.evaluate(() => { window.homekitToggle(); window.homekitShowCode(); window.homekitResetPairing(); });
    await page.waitForTimeout(200);
    check(S.posts.length === nBefore + 1, `pending: a second action during the first sent nothing (POSTs ${S.posts.length - nBefore})`);
    S.hold = null;
    release();
    await page.waitForFunction(() => !document.getElementById('homekit-btn-enable').disabled, null, { timeout: REACH_MS }).catch(() => {});
    s = await readCard(page);
    check(s.disabled.enable === false, 'pending: controls unlocked once the answer arrived');
  }

  // Reset confirm.
  if (await reach(page, S, CASES[1], 'reset')) {
    renders++;
    const RESET_TEXT = 'Все iPhone и iPad потеряют доступ к мосту. Их придётся добавить заново.';
    S.dialogs.length = 0;
    S.dialogAccept = false;
    const n0 = S.posts.filter((b) => b.action === 'reset_pairing').length;
    await page.click('#homekit-btn-reset');
    await page.waitForTimeout(250);
    check(S.dialogs.length === 1 && S.dialogs[0] === RESET_TEXT, `reset: confirm asked with the plan text ("${S.dialogs[0] || '(none)'}")`);
    check(S.posts.filter((b) => b.action === 'reset_pairing').length === n0, 'reset: dismissing the confirm sends nothing');
    S.dialogAccept = true;
    await page.click('#homekit-btn-reset');
    await page.waitForFunction(() => !document.getElementById('homekit-btn-enable').disabled, null, { timeout: REACH_MS }).catch(() => {});
    check(S.posts.filter((b) => b.action === 'reset_pairing').length === n0 + 1, 'reset: accepting the confirm sends reset_pairing once');
  }

  // Skipped list: every contract reason labelled; hostile names render as text.
  const skipCase = { name: 'skipped', p: st({ ...P.paired, skipped: SKIPPED, skipped_total: SKIPPED.length }), badge: 'работает' };
  if (await reach(page, S, skipCase, 'skipped')) {
    renders++;
    await page.click('#homekit-skip-link');
    await page.waitForFunction((n) => document.querySelectorAll('#homekit-skip-list li').length >= n, SKIPPED.length, { timeout: REACH_MS }).catch(() => {});
    await page.waitForTimeout(150);
    const k = await page.evaluate(() => {
      const list = document.getElementById('homekit-skip-list');
      const rows = [...list.querySelectorAll('li')].map((li) => ({
        name: (li.querySelector('.homekit-skip-name') || {}).textContent || '',
        why: (li.querySelector('.homekit-skip-why') || {}).textContent || '',
      }));
      return { rows, imgs: list.querySelectorAll('img').length, xss: window.__hkXss, link: document.getElementById('homekit-skip-text').textContent.trim() };
    });
    check(k.rows.length === SKIPPED.length, `skipped: ${k.rows.length} rows listed (want ${SKIPPED.length})`);
    check(k.link === `не передаётся: ${SKIPPED.length} ›`, `skipped: link reads "${k.link}"`);
    for (let i = 0; i < DOC_SKIP.length; i++) {
      const row = k.rows[i + 2] || { why: '' };
      check(row.why && row.why !== DOC_SKIP[i] && /[а-яё]/i.test(row.why), `skipped: reason "${DOC_SKIP[i]}" renders a Russian label ("${row.why}")`);
    }
    check(k.imgs === 0 && k.xss === undefined, `skipped: no <img> injected, no handler ran (imgs=${k.imgs}, xss=${k.xss})`);
    check((k.rows[0] || {}).name.startsWith(XSS_NAME), `skipped: a hostile device name renders as text ("${(k.rows[0] || {}).name}")`);
    check((k.rows[1] || {}).why === XSS_REASON, `skipped: an unknown hostile reason renders as text ("${(k.rows[1] || {}).why}")`);
    await page.locator('#homekit-card').screenshot({ path: join(SHOTS, 'homekit-card-skipped-dark.png') });
  }

  // Service catalogue visibility: the HomeKit card is never hidden by it.
  console.log('\n[dark] service-catalogue visibility');
  for (const hk of [
    { id: 'homekit', name: 'Apple HomeKit', unit: 'sa02m-homekit.service', installed: true, active: 'inactive', enabled: 'disabled' },
    { id: 'homekit', name: 'Apple HomeKit', unit: 'sa02m-homekit.service', installed: false, active: 'inactive', enabled: 'disabled' },
    { id: 'homekit', name: 'Apple HomeKit', unit: 'sa02m-homekit.service', installed: true, active: 'active', enabled: 'enabled' },
  ]) {
    const v = await page.evaluate((svc) => {
      window.renderServicesControl({ ok: true, services: [
        { id: 'alice', name: 'Яндекс Алиса', unit: 'sa02m-alice-client.service', installed: true, active: 'inactive', enabled: 'disabled' },
        svc,
      ] });
      const box = (id) => {
        const el = document.getElementById(id);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return !el.hidden && r.width > 0 && r.height > 0 && getComputedStyle(el).display !== 'none';
      };
      return { alice: box('alice-card'), homekit: box('homekit-card') };
    }, hk);
    const tag = `catalogue homekit installed=${hk.installed} active=${hk.active}`;
    check(v.alice === false, `${tag}: precondition — the same list hides the stopped #alice-card (visible=${v.alice})`);
    check(v.homekit === true, `${tag}: #homekit-card stays visible (visible=${v.homekit})`);
  }
  await page.evaluate(() => { const a = document.getElementById('alice-card'); if (a) a.hidden = false; });

  check(S.errors.length === 0, `interactions: no page errors (${S.errors.join(' | ')})`);
  await ctx.close();
  return renders;
}

/* ── Pass 3: ≤560 px, both themes ────────────────────────────────────────── */
async function runNarrow(browser, base, theme) {
  console.log(`\n[${theme}] 500 px`);
  const { ctx, page, S } = await openPage(browser, base, { theme, width: 500 });
  let renders = 0;
  if (await reach(page, S, CASES[0], `${theme}/narrow`)) {
    renders++;
    await clickShowCode(page, S, `${theme}/narrow`);
    const g = await page.evaluate(() => {
      const card = document.getElementById('homekit-card');
      const host = card.parentElement;
      const cs = getComputedStyle(host);
      const content = host.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
      const cr = card.getBoundingClientRect();
      const qr = document.getElementById('homekit-qr').getBoundingClientRect();
      return { cardW: cr.width, content, overflow: card.scrollWidth - card.clientWidth, qrInside: qr.left >= cr.left - 0.5 && qr.right <= cr.right + 0.5 && qr.width > 0, pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
    });
    check(Math.abs(g.cardW - g.content) <= 2, `${theme}/narrow: card spans its column (${g.cardW.toFixed(1)} of ${g.content.toFixed(1)} px)`);
    check(g.overflow <= 1 && g.pageOverflow <= 1, `${theme}/narrow: nothing overflows (card +${g.overflow}px, page +${g.pageOverflow}px)`);
    check(g.qrInside, `${theme}/narrow: the QR sits inside the card`);
    await page.locator('#homekit-card').screenshot({ path: join(SHOTS, `homekit-card-narrow-${theme}.png`) });
  }
  check(S.errors.length === 0, `${theme}/narrow: no page errors (${S.errors.join(' | ')})`);
  await ctx.close();
  return renders;
}

/* ── Pass 4: «Умный дом» save rules ──────────────────────────────────────── */
// The stub records a POST when it is SENT; the save's answer then resets the
// form (shCancelEdit) and re-polls. Starting the next edit before that lands
// would have the reset wipe it — so wait for the form's add mode as well.
async function waitUpserts(page, S, n) {
  const until = Date.now() + REACH_MS;
  while (Date.now() < until) {
    if (S.alicePosts.filter((b) => b.action === 'upsert_device').length >= n) break;
    await page.waitForTimeout(50);
  }
  await page.waitForFunction(() => {
    const save = document.getElementById('sh-dev-save');
    const cancel = document.getElementById('sh-dev-cancel');
    return !!save && save.textContent.trim() === 'Добавить' && (!cancel || cancel.hidden);
  }, null, { timeout: REACH_MS }).catch(() => {});
  await page.waitForTimeout(250);
}
const upserts = (S) => S.alicePosts.filter((b) => b.action === 'upsert_device');

async function editAndSave(page, S, id, mutate) {
  S.alicePosts.length = 0;
  await page.evaluate((i) => {
    const row = [...document.querySelectorAll('#sh-device-list .sh-dev-row')].find((r) => r.getAttribute('data-id') === i);
    row.querySelector('button[data-act="edit"]').click();
  }, id);
  const checked = await page.isChecked('#sh-dev-homekit');
  if (mutate) await mutate();
  await page.click('#sh-dev-save');
  await waitUpserts(page, S, 1);
  return { checked, up: upserts(S)[0] || null };
}

async function addNew(page, S) {
  S.alicePosts.length = 0;
  const checked = await page.isChecked('#sh-dev-homekit');
  await page.fill('#sh-dev-name', 'Новый датчик');
  await page.evaluate(() => {
    const inp = document.querySelector('#sh-modal .sh-bind-row .sh-row-topic');
    if (inp) inp.value = '/devices/SA-02m/controls/AI1';
  });
  await page.click('#sh-dev-save');
  await waitUpserts(page, S, 1);
  return { checked, up: upserts(S)[0] || null };
}

async function runSmartHome(browser, base) {
  console.log('\n[dark] «Умный дом» HomeKit toggle');
  const { ctx, page, S } = await openPage(browser, base, { theme: 'dark' });
  const hasKey = (d) => !!d && Object.prototype.hasOwnProperty.call(d, 'homekit_visible');

  // A — module not installed.
  S.hk = P.not_installed;
  await refresh(page);
  await page.waitForFunction(() => window.sa02mHomekitInstalled === false, null, { timeout: REACH_MS }).catch(() => {});
  check(await page.evaluate(() => window.sa02mHomekitInstalled === false), 'sh/not_installed: precondition — the card published installed=false');
  await page.evaluate(() => window.shOpenModal());
  await page.waitForFunction((n) => document.querySelectorAll('#sh-device-list .sh-dev-row').length >= n, ALICE_DEVICES.length, { timeout: REACH_MS }).catch(() => {});
  const rowsA = await page.evaluate(() => document.querySelectorAll('#sh-device-list .sh-dev-row').length);
  check(rowsA === ALICE_DEVICES.length, `sh/not_installed: precondition — ${rowsA} device rows rendered`);
  const hid = await page.evaluate(() => {
    const shown = (id) => { const el = document.getElementById(id); return !!el && !el.hidden && el.getClientRects().length > 0; };
    return { field: shown('sh-hk-field'), all: shown('sh-hk-all'), exists: !!document.getElementById('sh-hk-field') && !!document.getElementById('sh-dev-homekit'),
      badges: [...document.querySelectorAll('#sh-device-list .badge')].filter((b) => b.textContent.trim() === 'HomeKit').length };
  });
  check(hid.exists && !hid.field && !hid.all && hid.badges === 0, `sh/not_installed: toggle, «Отметить все» and row badges hidden (field=${hid.field}, all=${hid.all}, badges=${hid.badges})`);
  let r = await editAndSave(page, S, 'd1');
  check(r.up && r.up.device.id === 'd1' && r.up.device.homekit_visible === true, `sh/not_installed: edited d1 keeps stored homekit_visible=true (${r.up && JSON.stringify(r.up.device.homekit_visible)})`);
  r = await editAndSave(page, S, 'd3');
  check(r.up && r.up.device.id === 'd3' && r.up.device.homekit_visible === false, `sh/not_installed: edited d3 keeps stored homekit_visible=false (${r.up && JSON.stringify(r.up.device.homekit_visible)})`);
  r = await editAndSave(page, S, 'd2');
  check(r.up && r.up.device.id === 'd2' && !hasKey(r.up.device), `sh/not_installed: edited d2 (no key) stays without the key`);
  r = await addNew(page, S);
  check(!!r.up && r.up.device.name === 'Новый датчик', `sh/not_installed: precondition — the new device was saved (${r.up ? 'yes' : 'no upsert'})`);
  check(!!r.up && !hasKey(r.up.device), `sh/not_installed: a new device is saved WITHOUT homekit_visible (${r.up && JSON.stringify(r.up.device)})`);

  // B — module installed.
  S.hk = P.disabled;
  await refresh(page);
  await page.waitForFunction(() => window.sa02mHomekitInstalled === true, null, { timeout: REACH_MS }).catch(() => {});
  await page.waitForFunction(() => { const el = document.getElementById('sh-hk-field'); return !!el && !el.hidden; }, null, { timeout: REACH_MS }).catch(() => {});
  const shownB = await page.evaluate(() => {
    const shown = (id) => { const el = document.getElementById(id); return !!el && !el.hidden && el.getClientRects().length > 0; };
    const d1 = [...document.querySelectorAll('#sh-device-list .sh-dev-row')].find((x) => x.getAttribute('data-id') === 'd1');
    return { field: shown('sh-hk-field'), all: shown('sh-hk-all'),
      d1Badge: !!d1 && [...d1.querySelectorAll('.badge')].some((b) => b.textContent.trim() === 'HomeKit') };
  });
  check(shownB.field && shownB.all, `sh/installed: toggle and «Отметить все» shown (field=${shownB.field}, all=${shownB.all})`);
  check(shownB.d1Badge, 'sh/installed: the exposed device carries the HomeKit badge');
  r = await editAndSave(page, S, 'd1', () => page.uncheck('#sh-dev-homekit'));
  check(r.checked === true, 'sh/installed: editing d1 prefills the toggle from the stored true');
  check(r.up && r.up.device.homekit_visible === false, `sh/installed: unticking saves homekit_visible=false (${r.up && JSON.stringify(r.up.device.homekit_visible)})`);
  r = await editAndSave(page, S, 'd2', () => page.check('#sh-dev-homekit'));
  check(r.checked === false, 'sh/installed: editing d2 (no key) prefills unticked');
  check(r.up && r.up.device.homekit_visible === true, `sh/installed: ticking saves homekit_visible=true (${r.up && JSON.stringify(r.up.device.homekit_visible)})`);
  r = await addNew(page, S);
  check(r.checked === false, 'sh/installed: a new device defaults unticked');
  check(!!r.up && r.up.device.homekit_visible === false, `sh/installed: the new device saves the toggle value false (${r.up && JSON.stringify(r.up.device.homekit_visible)})`);

  // «Отметить все»: only the not-yet-exposed devices, each with true.
  S.alicePosts.length = 0;
  S.dialogAccept = true;
  await page.click('#sh-hk-all');
  const want = ALICE_DEVICES.filter((d) => d.homekit_visible !== true).map((d) => d.id).sort();
  await waitUpserts(page, S, want.length);
  await page.waitForTimeout(200);
  const ups = upserts(S);
  check(JSON.stringify(ups.map((u) => u.device.id).sort()) === JSON.stringify(want) && ups.every((u) => u.device.homekit_visible === true),
    `sh/installed: «Отметить все» upserts exactly ${JSON.stringify(want)} with true (got ${JSON.stringify(ups.map((u) => [u.device.id, u.device.homekit_visible]))})`);

  // Hostile device name / id in the rows.
  const x = await page.evaluate((id) => {
    const list = document.getElementById('sh-device-list');
    const row = [...list.querySelectorAll('.sh-dev-row')].find((el) => el.getAttribute('data-id') === id);
    return { found: !!row, name: row ? (row.querySelector('.mono') || {}).textContent : '', imgs: list.querySelectorAll('img').length, xss: window.__shXss };
  }, ALICE_DEVICES[3].id);
  check(x.found, 'sh/xss: the row with a quoted id keeps its exact data-id (escAttr)');
  check(x.name === XSS_SH_NAME && x.imgs === 0 && x.xss === undefined, `sh/xss: the hostile name renders as text (imgs=${x.imgs}, xss=${x.xss}, text="${x.name}")`);
  await page.locator('#sh-modal').screenshot({ path: join(SHOTS, 'homekit-smarthome-dark.png') }).catch(() => {});

  check(S.errors.length === 0, `sh: no page errors (${S.errors.join(' | ')})`);
  await ctx.close();
  return 1;
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
    rendered += await runInteractions(browser, base);
    for (const theme of THEMES) rendered += await runNarrow(browser, base, theme);
    rendered += await runSmartHome(browser, base);
  } finally {
    await browser.close();
    srv.close();
  }
  if (rendered === 0) die(1, `${TAG}: ERROR — nothing was rendered; a pass without a render is not a pass`);
  if (failures) die(1, `\n${TAG}: ${failures} FAILURE(S) of ${assertions} assertions`);
  console.log(`\n${TAG}: PASS — ${assertions} assertions: ${DOC_STATES.length} contract states + ${CASES.length - DOC_STATES.length} extra cases × ${THEMES.length} themes, code lifecycle, interactions, 500 px, «Умный дом» (${rendered} renders)`);
  process.exit(0);
}

run().catch((e) => {
  console.error(`${TAG}: ERROR — ${e && e.stack || e}`);
  process.exit(1);
});
