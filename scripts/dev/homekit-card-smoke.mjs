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
       in running ∧ unpaired; `missing_deps` + `peer_package_outdated` shows
       ONE line «Обновите пакет Алисы», never the raw
       message; every status reason a fixture uses is in the §10 reason table;
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
       defaults to false, «Отметить все» sends ONE set_homekit_visible (the
       not-yet-exposed devices) and no upsert_device; a device name / id with `<img onerror>` and quotes renders as
       text (escHtml / escAttr).
     * «Устройства для HomeKit» on the card: with nothing ticked the one-line
       hint «Нет устройств: отметьте их ниже» shows and the list opens itself;
       with a device ticked but every ticked one skipped (0 accessories) the
       hint stays hidden and the row carries its reason;
       one line per device (box, name, a muted device-level skip reason —
       never `hidden`, never an item-level skip); unsaved ticks survive the
       Alice and HomeKit polls; «Сохранить» (locked with no change and while in
       flight) sends ONE set_homekit_visible with only the changed ids and no
       upsert_device; a refusal keeps the ticks; «Отметить все» in «Умный дом»
       also sends ONE set_homekit_visible; no «›» on «не передаётся: N» and no
       «Не сертифицировано Apple» on the card.
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
const DOC_REASONS = contractCodes('**Причины**', '**`status.json`**', 'status reason (§10)');

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
  conf_unreadable: st({ state: 'missing_deps', reason: 'conf_unreadable', enabled: true, message: 'conf unreadable' }),
  peer_outdated: st({ state: 'missing_deps', reason: 'peer_package_outdated', enabled: true,
    message: 'outdated: sa02m_alice.client.device_registry:DeviceRegistry.catalogue_items' }),
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
  { name: 'identity_regenerated', p: P.regenerated, badge: 'работает', line: 'Мост создан заново — добавьте в «Дом» снова', btn: 'Выключить', setup: true },
  { name: 'not_installed', p: P.not_installed, badge: 'не установлен', line: 'Нужна полная установка (install.sh)', btn: null, setup: false, netHidden: true, resetHidden: true },
  { name: 'disabled', p: P.disabled, badge: 'выключен', line: null, btn: 'Включить', setup: false },
  { name: 'starting', p: P.starting, badge: 'запуск…', line: null, btn: 'Выключить', setup: false },
  { name: 'missing_deps', p: P.missing_deps, badge: 'нет компонентов', line: 'Нужна полная установка (install.sh)', btn: 'Выключить', setup: false },
  // A stale Alice package (bench 1.135): one short line naming the fix, never the raw message.
  // The conf exists but the daemon cannot read it: one line, not «выключен».
  { name: 'missing_deps-conf_unreadable', p: P.conf_unreadable, badge: 'нет компонентов', line: 'Нет доступа к настройкам', oneLine: true, btn: 'Выключить', setup: false },
  { name: 'missing_deps-peer_package_outdated', p: P.peer_outdated, badge: 'нет компонентов', line: 'Обновите пакет Алисы', oneLine: true, btn: 'Выключить', setup: false },
  { name: 'no_interface', p: P.no_interface, badge: 'нет сети', line: 'Нет адреса на интерфейсе', btn: 'Выключить', setup: false },
  { name: 'port_in_use', p: P.port_in_use, badge: 'порт занят', line: 'Выберите другой порт', btn: 'Выключить', setup: false },
  { name: 'error', p: P.error, badge: 'ошибка', line: 'Журнал: sa02m-homekit', btn: 'Выключить', setup: false },
  { name: 'error-status_stale', p: P.stale, badge: 'не отвечает', line: 'Статус устарел', btn: 'Выключить', setup: false },
  // Follows error-status_stale. A missed probe keeps that status: the card
  // must not flip to «Нет ответа от платы».
  { name: 'no-answer', p: null, noanswer: true, badge: 'не отвечает', line: 'Статус устарел', btn: 'Выключить', setup: false },
];

// Non-vacuous both ways: the contract states and the fixture states are one set.
const fixturedStates = [...new Set(CASES.filter((c) => c.p).map((c) => c.p.state))];
for (const s of DOC_STATES) check(fixturedStates.includes(s), `contract state "${s}" has a card fixture`);
for (const s of fixturedStates) check(DOC_STATES.includes(s), `fixture state "${s}" is a documented contract state (no drift)`);
check(DOC_SKIP.length >= 8, `contract skip-reason table parsed (${DOC_SKIP.length} reasons)`);
const fixturedReasons = [...new Set(CASES.filter((c) => c.p && c.p.reason).map((c) => c.p.reason))];
check(fixturedReasons.length >= 3, `status-reason fixtures present (${fixturedReasons.join(',')})`);
for (const r of fixturedReasons) check(DOC_REASONS.includes(r), `fixture reason "${r}" is a documented §10 reason (no drift)`);
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
    alice: ALICE, aliceHold: null, aliceReply: null,
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
        if (S.aliceHold) await S.aliceHold;
        return json(r, S.aliceReply ? S.aliceReply(body) : { ok: true });
      }
      return json(r, S.alice);
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
      // Rendered text lines of the card line (distinct line boxes of its text).
      msgLines: (() => {
        const el = $('homekit-msg');
        if (!el || !el.firstChild) return 0;
        const r = document.createRange();
        r.selectNodeContents(el);
        return new Set([...r.getClientRects()].filter((b) => b.width > 0).map((b) => Math.round(b.top))).size;
      })(),
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
    if (c.oneLine) check(s.msgLines === 1, `${tag}: the card line fits one line (${s.msgLines} lines)`);
    if (c.p && c.p.message && c.line) check(!s.msg.includes(c.p.message), `${tag}: the raw status message is not shown`);
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
    const RESET_TEXT = 'Сбросить? iPhone и iPad придётся добавить заново.';
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
    // No «›» (Operator, 2026-09-28): the link itself stays the disclosure.
    check(k.link === `не передаётся: ${SKIPPED.length}`, `skipped: link reads "${k.link}"`);
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

  // «Отметить все»: ONE set_homekit_visible for the not-yet-exposed devices,
  // each true — never one upsert per device (HTTP 504 on a loaded board).
  S.alicePosts.length = 0;
  S.dialogAccept = true;
  await page.click('#sh-hk-all');
  const want = ALICE_DEVICES.filter((d) => d.homekit_visible !== true).map((d) => d.id).sort();
  const bulkOf = () => S.alicePosts.filter((b) => b.action === 'set_homekit_visible');
  for (const until = Date.now() + REACH_MS; Date.now() < until && !bulkOf().length;) await page.waitForTimeout(50);
  await page.waitForTimeout(300);
  const bulk = bulkOf();
  const got = bulk.length ? bulk[0].visible || {} : {};
  check(bulk.length === 1 && JSON.stringify(Object.keys(got).sort()) === JSON.stringify(want) && Object.values(got).every((v) => v === true),
    `sh/installed: «Отметить все» sends ONE set_homekit_visible with exactly ${JSON.stringify(want)} true (got ${JSON.stringify(bulk)})`);
  check(upserts(S).length === 0, `sh/installed: «Отметить все» sends no upsert_device (${upserts(S).length})`);

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

/* ── Pass 5: «Сцены в HomeKit» + the CO₂ threshold (Phase 3 C, E) ─────────
   The scenes block (#sh-hk-scenes) is shown only while the module is
   installed AND the board read its scenario store (`scene_catalog`); a tick
   POSTs set_scene_homekit, the box is disabled while the request is in
   flight, a poll landing meanwhile does not repaint the block, and a refusal
   reverts the box and says so (toast). The CO₂ field shows only with a CO₂
   reading on an installed board; its placeholder is the contract default. */
const SCENE_XSS = '<img src=x onerror="window.__scXss=1">';
const SCENES = [
  { scene_id: 's1', name: 'Вечер', enabled: true },
  { scene_id: 's2', name: 'Ночь', enabled: false },
  { scene_id: 's3', name: SCENE_XSS, enabled: true },
];
const CO2_DEVICE = {
  id: 'c1', name: 'Воздух', type: 'devices.types.sensor.climate', room_id: 'r1',
  capabilities: [],
  properties: [{ type: 'devices.properties.float', mqtt: '/devices/dtv-COM3-1/controls/co2', co2_alarm_ppm: 800,
    parameters: { instance: 'co2_level', unit: 'unit.ppm' } }],
};
const CO2_DEFAULT = (() => {
  const text = readFileSync(CONTRACT, 'utf8');
  const m = /CO2_ALARM_DEFAULT_PPM`\s*=\s*(\d+)/.exec(text);
  if (!m) die(1, `${TAG}: ERROR — ${CONTRACT} no longer states CO2_ALARM_DEFAULT_PPM (§3, M15)`);
  return m[1];
})();
const aliceWith = (extra) => ({ ...ALICE, ...extra, devices: { ...ALICE.devices, ...(extra.devices || {}) } });

/* ── Pass: «Устройства для HomeKit» on the card (bench 1.135 discoverability) ─
   The list rides the Alice poll; one line per device with a box, the name and
   a muted device-level skip reason; unsaved ticks survive every re-render;
   «Сохранить» sends ONE set_homekit_visible with only the changed ids. */
async function readDevs(page) {
  return page.evaluate(() => {
    const $ = (id) => document.getElementById(id);
    const vis = (el) => !!el && !el.hidden && el.getClientRects().length > 0;
    const lines = (el) => {
      if (!el || !el.firstChild) return 0;
      const r = document.createRange();
      r.selectNodeContents(el);
      return new Set([...r.getClientRects()].filter((b) => b.width > 0).map((b) => Math.round(b.top))).size;
    };
    const list = $('homekit-dev-list');
    const rows = list ? [...list.querySelectorAll('li')].map((li) => {
      const cb = li.querySelector('input[type="checkbox"]');
      const label = li.querySelector('label');
      const tap = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--tap')) || 44;
      return {
        id: cb ? cb.dataset.id : '', checked: !!(cb && cb.checked), disabled: !!(cb && cb.disabled),
        name: (li.querySelector('.homekit-dev-name') || {}).textContent || '',
        why: (li.querySelector('.homekit-dev-why') || {}).textContent || '',
        oneLine: !!label && label.getBoundingClientRect().height <= tap + 4
          && lines(li.querySelector('.homekit-dev-name')) === 1,
      };
    }) : [];
    const hint = $('homekit-devs-hint');
    const save = $('homekit-btn-devs');
    const card = $('homekit-card');
    return {
      shown: vis($('homekit-devs')), open: !!($('homekit-devs') || {}).open, rows,
      hintVisible: vis(hint), hint: hint ? hint.textContent.trim() : '', hintLines: lines(hint),
      saveDisabled: !save || save.disabled, imgs: list ? list.querySelectorAll('img').length : -1,
      xss: window.__shXss, note: !!card && /сертифицировано/i.test(card.textContent),
    };
  });
}

async function runDeviceList(browser, base, theme) {
  console.log(`\n[${theme}] «Устройства для HomeKit»`);
  const { ctx, page, S } = await openPage(browser, base, { theme });
  let renders = 0;
  const aliceRefresh = () => page.evaluate(async () => { if (window.sa02mAliceRefresh) await window.sa02mAliceRefresh(); });
  const waitRows = (n) => page.waitForFunction((k) => document.querySelectorAll('#homekit-dev-list li').length >= k, n, { timeout: REACH_MS }).catch(() => {});
  const tag = (s) => `${theme}/devs: ${s}`;

  // A — nothing ticked (the bench board after pairing): hint + the list open.
  const unticked = ALICE_DEVICES.map((d) => { const c = { ...d }; delete c.homekit_visible; return c; });
  S.alice = { ...ALICE, devices: { ...ALICE.devices, devices: unticked } };
  if (await reach(page, S, { p: st({ ...P.paired, accessories: 0 }), badge: 'работает' }, tag('none'))) {
    renders++;
    await aliceRefresh();
    await waitRows(ALICE_DEVICES.length);
    const v = await readDevs(page);
    check(v.shown && v.rows.length === ALICE_DEVICES.length, tag(`precondition — ${v.rows.length} rows listed (want ${ALICE_DEVICES.length})`));
    check(v.hintVisible && v.hint === 'Нет устройств: отметьте их ниже' && v.hintLines === 1, tag(`one-line hint "${v.hint}" (visible=${v.hintVisible}, lines=${v.hintLines})`));
    check(v.open, tag('the list opens itself while nothing is ticked'));
    check(v.rows.every((r) => !r.checked), tag('every box unticked'));
    check(v.rows.every((r) => r.oneLine), tag(`one line per row (${v.rows.filter((r) => !r.oneLine).map((r) => r.id).join(',') || 'all'})`));
    check(v.rows.some((r) => r.name === XSS_SH_NAME) && v.imgs === 0 && v.xss === undefined, tag(`a hostile name renders as text (imgs=${v.imgs}, xss=${v.xss})`));
    check(v.saveDisabled, tag('«Сохранить» locked with nothing changed'));
    check(!v.note, tag('no «Не сертифицировано Apple» on the card'));
    await page.locator('#homekit-card').screenshot({ path: join(SHOTS, `homekit-card-devs-none-${theme}.png`) });
  }

  // A2 — a device IS ticked but every ticked one is skipped (0 accessories):
  // no hint (it would ask for a tick that is already there); the row's own
  // skip reason is what explains the empty «Дом».
  S.alice = ALICE;
  const allSkipped = [{ device_id: 'd1', name: 'Свет кухня', item: null, reason: 'nothing_mappable' }];
  if (await reach(page, S, { p: st({ ...P.paired, accessories: 0, skipped: allSkipped, skipped_total: 1 }), badge: 'работает' }, tag('ticked-all-skipped'))) {
    renders++;
    await aliceRefresh();
    await waitRows(ALICE_DEVICES.length);
    await page.waitForFunction(() => {
      const li = document.querySelector('#homekit-dev-list input[data-id="d1"]');
      const why = li && li.closest('li').querySelector('.homekit-dev-why');
      return !!why && why.textContent.trim() !== '';
    }, null, { timeout: REACH_MS }).catch(() => {});
    const v = await readDevs(page);
    const d1 = v.rows.find((r) => r.id === 'd1') || {};
    check(d1.checked && d1.why === 'нечего передать', tag(`precondition — d1 ticked with its reason ("${d1.why}")`));
    check(!v.hintVisible, tag(`no «отметьте» hint while a device is ticked, even at 0 accessories (visible=${v.hintVisible})`));
  }

  // B — d1 exposed but not projectable, d2 hidden: reasons, edits, save.
  S.alice = ALICE;
  const skipped = [
    { device_id: 'd1', name: 'Свет кухня', item: null, reason: 'nothing_mappable' },
    { device_id: 'd2', name: 'Розетка', item: null, reason: 'hidden' },
    { device_id: 'd3', name: 'Вентилятор', item: 'properties.float:x', reason: 'unit_unsupported' },
  ];
  if (await reach(page, S, { p: st({ ...P.paired, skipped, skipped_total: skipped.length }), badge: 'работает' }, tag('mixed'))) {
    renders++;
    await aliceRefresh();
    await waitRows(ALICE_DEVICES.length);
    await page.waitForFunction(() => {
      const cb = document.querySelector('#homekit-dev-list input[data-id="d1"]');
      return !!cb && cb.checked;
    }, null, { timeout: REACH_MS }).catch(() => {});
    let v = await readDevs(page);
    const row = (id) => v.rows.find((r) => r.id === id) || {};
    check(!v.hintVisible, tag('no hint once a device is exposed and accessories > 0'));
    check(row('d1').checked && row('d1').why === 'нечего передать', tag(`d1 ticked with its reason ("${row('d1').why}")`));
    check(row('d2').why === '' && row('d3').why === '', tag(`no reason for a hidden device or an item-level skip ("${row('d2').why}", "${row('d3').why}")`));
    await page.click('#homekit-dev-list input[data-id="d2"]');
    await page.click('#homekit-dev-list input[data-id="d1"]');
    // Both polls land: the unsaved ticks must survive the re-render.
    await aliceRefresh();
    await refresh(page);
    v = await readDevs(page);
    check(row('d2').checked && !row('d1').checked, tag(`unsaved ticks survive a poll (d2=${row('d2').checked}, d1=${row('d1').checked})`));
    check(!v.saveDisabled, tag('«Сохранить» usable with a change'));
    S.alicePosts.length = 0;
    let release;
    S.aliceHold = new Promise((ok) => { release = ok; });
    await page.click('#homekit-btn-devs');
    for (const until = Date.now() + REACH_MS; Date.now() < until && !S.alicePosts.length;) await page.waitForTimeout(50);
    v = await readDevs(page);
    check(v.saveDisabled && v.rows.every((r) => r.disabled), tag('the list and «Сохранить» are locked while the save is in flight'));
    S.aliceHold = null;
    release();
    await page.waitForFunction(() => !document.getElementById('homekit-btn-enable').disabled, null, { timeout: REACH_MS }).catch(() => {});
    await page.waitForTimeout(300);
    const sent = S.alicePosts.filter((b) => b.action === 'set_homekit_visible');
    check(sent.length === 1 && JSON.stringify(sent[0].visible) === JSON.stringify({ d2: true, d1: false }),
      tag(`ONE set_homekit_visible with only the changed ids (got ${JSON.stringify(sent)})`));
    check(upserts(S).length === 0, tag(`no upsert_device (${upserts(S).length})`));
    await page.locator('#homekit-card').screenshot({ path: join(SHOTS, `homekit-card-devs-mixed-${theme}.png`) });

    // A refusal keeps the operator's ticks and says so.
    S.alicePosts.length = 0;
    S.aliceReply = (body) => (body.action === 'set_homekit_visible' ? { ok: false, error: 'not_found' } : { ok: true });
    await page.click('#homekit-dev-list input[data-id="d3"]');
    await page.click('#homekit-btn-devs');
    for (const until = Date.now() + REACH_MS; Date.now() < until && !S.alicePosts.length;) await page.waitForTimeout(50);
    await page.waitForTimeout(300);
    v = await readDevs(page);
    check(row('d3').checked && !v.saveDisabled, tag(`a refused save keeps the tick pending (d3=${row('d3').checked}, save usable=${!v.saveDisabled})`));
    S.aliceReply = null;
  }
  await ctx.close();
  return renders;
}

async function sceneView(page) {
  return page.evaluate(() => {
    const box = document.getElementById('sh-hk-scenes');
    const list = document.getElementById('sh-hk-scene-list');
    const rows = list ? [...list.querySelectorAll('.sh-hk-scene-row')] : [];
    return {
      shown: !!box && !box.hidden && box.getClientRects().length > 0,
      text: list ? list.textContent.trim() : '',
      rows: rows.map((r) => {
        const cb = r.querySelector('input[data-scene]');
        return { id: cb ? cb.getAttribute('data-scene') : null, checked: !!cb && cb.checked, disabled: !!cb && cb.disabled,
          name: (r.querySelector('.mono') || {}).textContent || '', badge: (r.querySelector('.badge') || {}).textContent || '' };
      }),
      imgs: list ? list.querySelectorAll('img').length : -1,
      xss: window.__scXss,
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      boxRight: box ? box.getBoundingClientRect().right : 0,
    };
  });
}

async function runScenesAndCo2(browser, base, theme, width) {
  const tag = `${theme}/${width}px`;
  console.log(`\n[${tag}] «Сцены в HomeKit» + CO₂ threshold`);
  const { ctx, page, S } = await openPage(browser, base, { theme, width });
  // Not installed: hidden even with a catalogue.
  S.hk = P.not_installed;
  S.alice = aliceWith({ scene_catalog: SCENES, devices: { homekit_scenes: ['s1'] } });
  await refresh(page);
  await page.waitForFunction(() => window.sa02mHomekitInstalled === false, null, { timeout: REACH_MS }).catch(() => {});
  await page.evaluate(() => window.shOpenModal());
  await page.waitForTimeout(400);
  let v = await sceneView(page);
  check(!v.shown, `${tag}: scenes block hidden while HomeKit is not installed`);

  // Installed, no rules stack (no scene_catalog key) — hidden.
  S.hk = P.disabled;
  S.alice = aliceWith({});
  await refresh(page);
  await page.waitForFunction(() => window.sa02mHomekitInstalled === true, null, { timeout: REACH_MS }).catch(() => {});
  await page.evaluate(() => window.sa02mAliceRefresh());
  await page.waitForTimeout(300);
  v = await sceneView(page);
  check(!v.shown, `${tag}: scenes block hidden without scene_catalog (no rules stack)`);

  // Installed, empty catalogue — the one-line empty state.
  S.alice = aliceWith({ scene_catalog: [] });
  await page.evaluate(() => window.sa02mAliceRefresh());
  await page.waitForFunction(() => { const b = document.getElementById('sh-hk-scenes'); return !!b && !b.hidden; }, null, { timeout: REACH_MS }).catch(() => {});
  v = await sceneView(page);
  check(v.shown && v.text === 'Сцен нет' && v.rows.length === 0, `${tag}: empty catalogue reads «Сцен нет» (shown=${v.shown}, text="${v.text}")`);

  // The list.
  S.alice = aliceWith({ scene_catalog: SCENES, devices: { homekit_scenes: ['s1'] } });
  await page.evaluate(() => window.sa02mAliceRefresh());
  await page.waitForFunction((n) => document.querySelectorAll('#sh-hk-scene-list .sh-hk-scene-row').length === n, SCENES.length, { timeout: REACH_MS }).catch(() => {});
  v = await sceneView(page);
  check(v.rows.length === SCENES.length, `${tag}: one row per scene (${v.rows.length})`);
  const byId = Object.fromEntries(v.rows.map((r) => [r.id, r]));
  check(byId.s1 && byId.s1.checked && byId.s2 && !byId.s2.checked, `${tag}: ticked = id ∈ homekit_scenes (s1=${byId.s1 && byId.s1.checked}, s2=${byId.s2 && byId.s2.checked})`);
  check(byId.s2 && byId.s2.badge === 'выключена' && byId.s1 && byId.s1.badge === '', `${tag}: a disabled scene carries «выключена» (s2="${byId.s2 && byId.s2.badge}")`);
  check(byId.s3 && byId.s3.name === SCENE_XSS && v.imgs === 0 && v.xss === undefined, `${tag}: a hostile scene name renders as text (imgs=${v.imgs})`);
  check(v.overflow <= 1 && v.boxRight <= width + 1, `${tag}: no horizontal overflow (scrollWidth-${v.overflow}, box right ${Math.round(v.boxRight)})`);

  // Tick s2 with the request held: disabled while pending, a poll does not repaint.
  let release;
  S.aliceHold = new Promise((ok) => { release = ok; });
  S.alicePosts.length = 0;
  S.aliceReply = (body) => (body.action === 'set_scene_homekit' ? { ok: true, homekit_scenes: ['s1', 's2'] } : { ok: true });
  await page.click('#sh-hk-scene-list input[data-scene="s2"]');
  await page.waitForTimeout(150);
  v = await sceneView(page);
  const pend = Object.fromEntries(v.rows.map((r) => [r.id, r]));
  check(pend.s2 && pend.s2.checked && pend.s2.disabled, `${tag}: the ticked box is disabled while the request is in flight (disabled=${pend.s2 && pend.s2.disabled})`);
  S.aliceHold = null;  // the poll below is answered at once
  await page.evaluate(() => window.sa02mAliceRefresh());
  await page.waitForTimeout(200);
  v = await sceneView(page);
  const mid = Object.fromEntries(v.rows.map((r) => [r.id, r]));
  check(mid.s2 && mid.s2.checked && mid.s2.disabled, `${tag}: a poll landing mid-request does not repaint the block (s2 checked=${mid.s2 && mid.s2.checked})`);
  S.alice = aliceWith({ scene_catalog: SCENES, devices: { homekit_scenes: ['s1', 's2'] } });
  release();
  await page.waitForFunction(() => { const cb = document.querySelector('#sh-hk-scene-list input[data-scene="s2"]'); return !!cb && !cb.disabled; }, null, { timeout: REACH_MS }).catch(() => {});
  const post = S.alicePosts.find((b) => b.action === 'set_scene_homekit');
  check(!!post && post.scene_id === 's2' && post.visible === true, `${tag}: the tick POSTs set_scene_homekit {s2, true} (${JSON.stringify(post)})`);

  // Refusal: the box reverts and a toast says so.
  S.alicePosts.length = 0;
  S.aliceReply = (body) => (body.action === 'set_scene_homekit' ? { ok: false, error: 'not_found' } : { ok: true });
  await page.waitForTimeout(200);
  await page.click('#sh-hk-scene-list input[data-scene="s1"]');
  await page.waitForFunction(() => { const cb = document.querySelector('#sh-hk-scene-list input[data-scene="s1"]'); return !!cb && !cb.disabled; }, null, { timeout: REACH_MS }).catch(() => {});
  await page.waitForTimeout(200);
  v = await sceneView(page);
  const after = Object.fromEntries(v.rows.map((r) => [r.id, r]));
  const toastText = await page.evaluate(() => [...document.querySelectorAll('.toast, #toast, .toast-container *')].map((t) => t.textContent).join(' | '));
  check(after.s1 && after.s1.checked, `${tag}: a refused untick reverts the box (s1 checked=${after.s1 && after.s1.checked})`);
  check(/Не удалось сохранить/.test(toastText), `${tag}: the refusal is shown as a toast ("${toastText.slice(0, 80)}")`);
  await page.locator('#sh-hk-scenes').screenshot({ path: join(SHOTS, `homekit-scenes-${theme}-${width}.png`) }).catch(() => {});

  // CO₂ threshold field: shown for a CO₂ device on an installed board, the
  // placeholder is the contract default, the stored value prefills, the save
  // writes the item-level key and an empty field drops it.
  S.aliceReply = null;
  S.alice = aliceWith({ scene_catalog: SCENES, devices: { devices: [...ALICE_DEVICES, CO2_DEVICE] } });
  await page.evaluate(() => window.sa02mAliceRefresh());
  await page.waitForFunction(() => !!document.querySelector('#sh-device-list .sh-dev-row[data-id="c1"]'), null, { timeout: REACH_MS }).catch(() => {});
  const co2Hidden0 = await page.evaluate(() => document.getElementById('sh-co2-field').hidden);
  check(co2Hidden0, `${tag}: CO₂ field hidden for a device without a CO₂ reading (add mode, temperature seed)`);
  const edit = async (mutate) => {
    S.alicePosts.length = 0;
    await page.evaluate(() => document.querySelector('#sh-device-list .sh-dev-row[data-id="c1"] button[data-act="edit"]').click());
    await page.waitForTimeout(150);
    const state = await page.evaluate(() => {
      const f = document.getElementById('sh-co2-field');
      const i = document.getElementById('sh-dev-co2-alarm');
      return { shown: !!f && !f.hidden, value: i ? i.value : null, placeholder: i ? i.placeholder : null };
    });
    if (mutate) await mutate();
    await page.click('#sh-dev-save');
    await waitUpserts(page, S, 1);
    const up = upserts(S)[0];
    const item = up ? (up.device.properties || []).find((it) => it.parameters && it.parameters.instance === 'co2_level') : null;
    return { state, item };
  };
  let e1 = await edit(() => page.fill('#sh-dev-co2-alarm', '1200'));
  check(e1.state.shown && e1.state.value === '800', `${tag}: CO₂ field shown with the stored 800 (shown=${e1.state.shown}, value=${e1.state.value})`);
  check(e1.state.placeholder === CO2_DEFAULT, `${tag}: CO₂ placeholder is the contract default ${CO2_DEFAULT} (${e1.state.placeholder})`);
  check(!!e1.item && e1.item.co2_alarm_ppm === 1200 && !('co2_alarm_ppm' in (e1.item.parameters || {})),
    `${tag}: the save writes co2_alarm_ppm=1200 beside mqtt (${JSON.stringify(e1.item)})`);
  e1 = await edit(() => page.fill('#sh-dev-co2-alarm', ''));
  check(!!e1.item && !('co2_alarm_ppm' in e1.item), `${tag}: an empty field drops the key (${JSON.stringify(e1.item)})`);
  S.alicePosts.length = 0;
  await page.evaluate(() => document.querySelector('#sh-device-list .sh-dev-row[data-id="c1"] button[data-act="edit"]').click());
  await page.waitForTimeout(150);
  await page.fill('#sh-dev-co2-alarm', '99');
  await page.click('#sh-dev-save');
  await page.waitForTimeout(400);
  const refused = await page.evaluate(() => (document.getElementById('sh-bind-msg') || {}).textContent || '');
  check(upserts(S).length === 0 && /400–5000/.test(refused), `${tag}: an out-of-range threshold is refused before any POST ("${refused}")`);
  await page.evaluate(() => window.shCancelEdit());

  check(S.errors.length === 0, `${tag}: no page errors (${S.errors.join(' | ')})`);
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
    for (const theme of THEMES) rendered += await runDeviceList(browser, base, theme);
    for (const width of [1280, 500]) {
      for (const theme of THEMES) rendered += await runScenesAndCo2(browser, base, theme, width);
    }
  } finally {
    await browser.close();
    srv.close();
  }
  if (rendered === 0) die(1, `${TAG}: ERROR — nothing was rendered; a pass without a render is not a pass`);
  if (failures) die(1, `\n${TAG}: ${failures} FAILURE(S) of ${assertions} assertions`);
  console.log(`\n${TAG}: PASS — ${assertions} assertions: ${DOC_STATES.length} contract states + ${CASES.length - DOC_STATES.length} extra cases × ${THEMES.length} themes, code lifecycle, interactions, 500 px, «Умный дом», «Сцены в HomeKit» + CO₂ × 2 widths × 2 themes (${rendered} renders)`);
  process.exit(0);
}

run().catch((e) => {
  console.error(`${TAG}: ERROR — ${e && e.stack || e}`);
  process.exit(1);
});
