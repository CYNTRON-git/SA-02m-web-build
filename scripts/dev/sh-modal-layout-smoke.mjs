#!/usr/bin/env node
// comment-mutation-proof-exempt: behavioural harness - renders the SHIPPED bundles in headless Chromium and asserts computed geometry (pane boxes, wheel-reached rows), so it pins no source line a comment token could satisfy; every assertion is preceded by a reached-state precondition (11 rendered device rows AND a catalogue that really overflows its layer), and it cannot be cased here at all - its chromium lives in the gitignored scripts/dev/node_modules, absent from this check's pristine copy. Its two mutations were run by hand and are recorded in the header below.
/* ═══════════════════════════════════════════════════════════════════════════
   sh-modal-layout-smoke — the «Комнаты и устройства» modal, laid out for real.
   ───────────────────────────────────────────────────────────────────────────
   What it closes: the bench defect of 1.0.6.29 — with the board's catalogue
   (11 devices, 2 rooms) the dialog grew past the screen and NOTHING scrolled:
   the overlay is `position:fixed` with `align-items:center`, so head and foot
   sat outside the viewport and a fixed overlay adds no document scroll area.
   Measured before the fix at 1280x720: dialog height 1367 px in a 720 px
   viewport, rect top -324 / bottom 1044, `max-height: none`, every layer
   `overflow-y: visible`, wheel over the modal moved no scrollTop at all.
   No other row can witness it: ui-layout never opens this modal and stubs
   every CGI with `{}` (so the catalogue is empty), and cloud-card-smoke drives
   the «Облако» card only.

   What it asserts, per viewport and both themes, on the SHIPPED bundles:
     * preconditions (non-vacuous): 11 device rows and 2 room rows rendered,
       and the catalogue really is taller than the box that holds it;
     * the dialog fits the viewport (head and foot both on screen);
     * WIDE (>=900 px): the two panes sit side by side inside the dialog, a
       real wheel over the catalogue reaches its end, and the LAST device row
       then lies inside the catalogue's visible box and above the foot;
     * WIDE: the actions pane needs no scroll — «Добавить» is inside its box;
     * NARROW (<900 px): the panes are stacked (catalogue first), and the last
       device row is reachable by scrolling the modal body;
     * never a horizontal scrollbar; no page errors.

   Exit codes (the runner has no skip signal distinct from success):
     0  every assertion passed (never without a real render);
     1  an assertion failed (viewport + assertion named) or ANY other error
        (a wrong served path, a stub error, an exception before the render);
     2  chromium/playwright missing — deliberately RED, never a vacuous green
        (the cloud-card-smoke precedent); the two absences are told apart on
        the second line (package vs browser cache).

   Mutation proof for the MQTT pass — run by hand, SEEN RED: re-nest
   #mqtt-scan-modal under #tab-mqtt -> exit 1, 8 failures — «neither dialog is
   nested in a .tab-pane (scan: tab-mqtt)» plus «overlay covers the viewport
   box» at every viewport (measured 224,88 1032x770.9 vs 1280x720 and
   14,99 347x1104.5 vs 375x812). The dialog rides that displaced box: at
   375x812 the add-dialog bottom lands at 887.4 px in an 812 px viewport
   (measured 1.0.6.29 on the pre-change tree; this is the home the
   index.html comment cites for that figure).

   Mutation proof — run by hand (comment-mutation-proof cannot case a chromium
   gate: its pristine copy carries no scripts/dev/node_modules, so the harness
   would exit 2 there). Both mutations were applied to main.css and SEEN RED,
   1.0.6.29:
     * drop `overflow-y: auto` from `.sh-col-catalog` -> exit 1, 8 failures:
       "the wheel scrolls the layer (scrollTop 0 of 0)" and "last device row
       NEVER reached … (final row 756.1..802.1, box 100..624)" at both wide
       viewports, plus the overflow precondition (704 > 704);
     * collapse `grid-template-columns` to one column -> exit 1, 6 failures:
       "two panes side by side (catalogue right 1161 <= actions left 119,
       vertical overlap 0)" at both wide viewports.

   Second pass (runActionsForm, 1.0.6.29 review round): the add/edit form's two
   pulling-against-each-other properties, gated TOGETHER — the actions pane fits
   unscrolled in its tallest state AND the topic select stays wide enough to
   tell two `/devices/SA-02m/controls/…` topics apart. Full rationale and its
   hand-run mutation are at the pass itself.

   Third pass (runMqttAnchoring, 1.0.6.29): the two MQTT dialogs
   (#mqtt-scan-modal, #mqtt-add-modal) are anchored to the VIEWPORT, not to the
   transformed #tab-mqtt they used to live in, and are closed again when the
   tab is left. Mutation proof — run by hand, SEEN RED: re-nest
   #mqtt-scan-modal inside #tab-mqtt -> the overlay-box and dialog-inside-the-
   viewport assertions FAIL at both viewports.

   Fourth pass (runInvertSave, 1.0.6.29 review round 2): the «Инвертировать»
   flag's WRITE side, read off the real `upsert_device` POST — ticked saves
   `inverted: true`, unticked drops the KEY (never `false`). Rationale and its
   two hand-run mutations are at the pass itself.

   Fifth (inside runPicker, 1.0.6.39, audit C4): a channel whose topic carries
   `"` and the audit's verbatim onfocus/autofocus payload (INJECT_TAG) is
   rendered, picked and rebuilt through shAddRow — asserted on the PARSED DOM:
   one button, exact data-topic and title, no onfocus/autofocus anywhere in the
   two modals, window.__PWNED never set. PROVEN RED against the pristine
   1.0.6.38 bundles (SH_MODAL_SMOKE_WWW=<git archive HEAD copy>): 4 failures —
   data-topic truncated to `…/controls/x` (0 buttons match), 2 onfocus/
   autofocus attributes parsed inside the modals, and both round-trips lost
   the tail of the topic. The handler itself did not fire in this flow (an
   autofocus on a node inserted after load does not focus); the audit's PoC
   fired it by focusing — the parsed attributes are the defect either way.
   GREEN once every attribute site uses escAttr() (app.js). The static half
   is the registry row no-eschtml-in-attr.

   Sixth (runInventoryHang, 1.0.6.39, audit C7): the inventory route is never
   fulfilled; the default binding row must still be seeded (at the fetch
   budget, measured) and the picker must fall back to hand entry. PROVEN RED
   on the same pristine copy: no row after 12 014 ms (the fallback check is
   then unreachable); GREEN with fetchWithTimeout + the seed in a `finally`
   (smarthome.js shTopics / shOpenModal).

   Harness: the standing Playwright install under scripts/dev (npm run
   ui-layout:install — the same chromium ui-layout and cloud-card-smoke reuse;
   no new dependency). Dev-only; never shipped to the device.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync, existsSync, mkdirSync, statSync } from 'node:fs';
import http from 'node:http';
import { join, dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const TAG = 'sh-modal-layout-smoke';
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '..', '..');
// Overridable so a wrong path can be proven to exit 1 (not 2) without editing the tree.
const WWW = process.env.SH_MODAL_SMOKE_WWW
  ? resolve(process.env.SH_MODAL_SMOKE_WWW)
  : join(REPO, 'www', 'network_config');
const SHOTS = join(REPO, '.ai-dev', 'quality', 'screenshots');
const MISSING_MSG = `${TAG}: chromium/playwright missing — run: npm run ui-layout:install`;
const THEMES = ['dark', 'light'];
// The breakpoint main.css stacks at is 900 px — one viewport on each side of it
// plus the two the Operator named on the bench.
const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 720, wide: true },
  { name: 'laptop', width: 1440, height: 900, wide: true },
  { name: 'tablet', width: 768, height: 1024, wide: false },
  { name: 'phone', width: 375, height: 812, wide: false },
];
// The bench catalogue: 11 devices over 2 rooms — the list the Operator could
// not reach the bottom of.
const ROOM_COUNT = 2;
// A fit is not a margin: the 1.0.6.29 review round rejected an actions pane that
// only just fit, because the next control to arrive would overflow it. Measured
// after the layout was re-opened: 460.5 px used in a 524 px pane at 1280×720,
// so 63.5 px of headroom — this floor keeps roughly half a control row of it.
const PANE_HEADROOM_MIN = 24;
const DEVICE_COUNT = 11;

function die(code, ...lines) {
  for (const l of lines) (code === 0 ? console.log : console.error)(l);
  process.exit(code);
}

if (!existsSync(join(WWW, 'index.html'))) {
  die(1, `${TAG}: ERROR — served path has no index.html: ${WWW}`);
}

const ROOMS = [{ id: 'r1', name: 'Гостиная' }, { id: 'r2', name: 'Щитовая' }];
if (ROOMS.length !== ROOM_COUNT) die(1, `${TAG}: ERROR — the room fixture drifted from ROOM_COUNT`);
// Two REAL topics that share the 25-character `/devices/SA-02m/controls/`
// prefix every board offers — the reason the topic select's width is a
// legibility floor and not a taste call.
const TOPIC_A = '/devices/SA-02m/controls/alarm_led';
const TOPIC_B = '/devices/SA-02m/controls/do';
if (TOPIC_A === TOPIC_B) die(1, `${TAG}: ERROR — the two topic fixtures are identical; the legibility check would be vacuous`);
// The bench module and the one channel of it that is already bound — so the
// picker's «занят» marking is proven against a real stored binding.
const MODULE_ID = 'mr02m-COM3-10';
const BUSY_TOPIC = '/devices/' + MODULE_ID + '/controls/do_1';
const DEVICES = Array.from({ length: DEVICE_COUNT }, (_, i) => ({
  id: 'dev' + (i + 1),
  // Device 1 is the bench siren: a real on/off binding with the stored
  // `inverted` flag, so the pane check below can reach the TALLEST edit state.
  name: i === 0 ? 'Сирена стенд' : 'Устройство ' + (i + 1),
  type: i === 0 ? 'devices.types.other' : (i % 2 ? 'devices.types.sensor' : 'devices.types.light'),
  room_id: i % 2 ? 'r2' : 'r1',
  icon: i === 0 ? 'siren' : 'relay',
  export: true,
  capabilities: [i === 0
    ? { type: 'devices.capabilities.on_off', mqtt: TOPIC_A, parameters: { instance: 'on' }, inverted: true }
    : { type: 'devices.capabilities.on_off', topic: '/devices/SA-02m/controls/relay' + i }],
  // Device 2 holds the module's DO1 — the stored binding the picker marks as
  // «занят» (a managed item, so shDetectRows really sees it).
  properties: i === 1
    ? [{ type: 'devices.properties.float', mqtt: BUSY_TOPIC, parameters: { instance: 'temperature', unit: 'unit.temperature.celsius' } }]
    : [],
}));
// The status payload app/alice.js polls; smarthome.js rides it (sa02mAliceOnData).
const ALICE = {
  ok: true,
  client_enabled: true,
  gateway: { available: true },
  status: { state: 'connected' },
  cloud_control: { enabled: true, state: 'connected' },
  devices: { devices: DEVICES, rooms: ROOMS },
};
const TOPICS = { topics: [TOPIC_A, TOPIC_B] };
// The STRUCTURED inventory the picker asks for (`?format=inventory`) — the
// bench 1.135 case: `mr02m-COM3-10` is declared 16DO in the bridge yaml and
// answered 6DO8DI at register 0, so the picker must offer 6 DO + 8 DI (the
// yaml alone offered ONE topic for the whole module) and say which type it
// followed. Plus the board's own four controls, always present.
const CH = (tag, title, rw, extra) => Object.assign(
  { tag, topic: '/devices/mr02m-COM3-10/controls/' + tag, title, rw, enabled: true, sub: [] },
  extra || {});
const CTRL_CH = (tag, title, rw) => ({
  tag, topic: '/devices/SA-02m/controls/' + tag, title, rw, enabled: true, sub: [],
});
const LED_ID = 'led-COM3-13';
const LED_CH = (tag, title, rw) => ({
  tag, topic: '/devices/' + LED_ID + '/controls/' + tag, title, rw, enabled: true, sub: [],
});
const MODULE_DO = 6;
const MODULE_DI = 8;
// A topic carrying `"` (audit 2026-09-08 C4): the inventory's strings come from
// the bridge's live cache, which the LAN broker feeds — so the picker MUST treat
// them as attribute-hostile. This tag is the verbatim attribute-injection
// payload the audit executed against the 1.0.6.38 escHtml() picker: rendered
// into `data-topic="…"` unescaped it closes the attribute and adds onfocus +
// autofocus, and Chromium runs the handler.
const INJECT_TAG = 'x" onfocus="window.__PWNED=1" autofocus="y';
const INJECT_TOPIC = '/devices/SA-02m/controls/' + INJECT_TAG;
const INVENTORY = {
  ok: true,
  source: '/etc/sa02m-modbus-mqtt.yaml',
  devices: [
    {
      id: MODULE_ID, name: '', type: 'mr02m',
      model: '6DO8DI', model_ru: '6ДО 8ДИ',
      model_source: 'detected', yaml_model: '16DO',
      port: 'COM3', address: 10,
      channels: {
        di: Array.from({ length: MODULE_DI }, (_, i) => CH('di_' + (i + 1), 'DI' + (i + 1), 'r', {
          sub: [{
            tag: 'di_' + (i + 1) + '_count',
            topic: '/devices/' + MODULE_ID + '/controls/di_' + (i + 1) + '_count',
            title: 'DI' + (i + 1) + ': счётчик импульсов', rw: 'r', enabled: true, sub: [],
          }],
        })),
        do: Array.from({ length: MODULE_DO }, (_, i) => CH('do_' + (i + 1), 'DO' + (i + 1), 'rw')),
        ai: [], ao: [], other: [],
        diag: [CH('mcu_temp', 'Температура МК', 'r')],
      },
    },
    // The LED driver, exactly as the board serves it: its controls carry NO
    // human title (there is no offline Russian name for `power` / `led_count`),
    // so `title === tag` and the tag is not a `<kind>_<n>` one. That pair is
    // what rendered anonymous rows on bench 1.135 — hence LED_ANON below.
    {
      id: LED_ID, name: 'LED (COM3 addr=13)', type: 'led',
      model: 'LED', model_ru: '', model_source: '', yaml_model: '',
      port: 'COM3', address: 13,
      channels: {
        di: [1, 2, 3, 4].map((n) => LED_CH('di_' + n, 'DI' + n, 'r')),
        do: [], ai: [], ao: [],
        other: [
          LED_CH('power', 'power', 'rw'),
          LED_CH('brightness', 'brightness', 'rw'),
          LED_CH('led_count', 'led_count', 'r'),
        ],
        diag: [],
      },
    },
    {
      id: 'SA-02m', name: 'Контроллер SA-02m', type: 'controller',
      model: 'SA-02m', model_ru: '', model_source: '', yaml_model: '',
      port: '', address: null,
      channels: {
        di: [], do: [], ai: [], ao: [],
        other: [
          CTRL_CH('do', 'Дискретный выход', 'rw'),
          CTRL_CH('beeper', 'Пищалка контроллера', 'rw'),
          CTRL_CH('alarm_led', 'Светодиод «Авария»', 'rw'),
          CTRL_CH('temp_c', 'Температура платы', 'r'),
          CTRL_CH(INJECT_TAG, 'Реле "A" (кавычки в имени)', 'rw'),
        ],
        diag: [],
      },
    },
  ],
  count: 3,
};
function stubBody(url) {
  if (/sa02m_alice_api\.cgi/.test(url)) return JSON.stringify(ALICE);
  if (/sa02m_alice_topics\.cgi/.test(url)) {
    return JSON.stringify(/format=inventory/.test(url) ? INVENTORY : TOPICS);
  }
  return '{}';
}

let failures = 0;
let assertions = 0;
function check(cond, msg) {
  assertions++;
  if (cond) console.log('  ok   - ' + msg);
  else { failures++; console.error('  FAIL - ' + msg); }
}

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
  try {
    const p = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
    let file = resolve(join(WWW, p));
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
    srv.listen(Number(process.env.SH_MODAL_SMOKE_PORT || 0), '127.0.0.1', () => ok(srv));
  });
}

/* ── Geometry, as the browser computes it ────────────────────────────────── */
async function readLayout(page) {
  return page.evaluate(() => {
    const box = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {
        left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height,
        scrollHeight: el.scrollHeight, clientHeight: el.clientHeight,
        scrollTop: el.scrollTop, overflowY: getComputedStyle(el).overflowY,
      };
    };
    const rows = document.querySelectorAll('#sh-device-list .sh-dev-row');
    const last = rows.length ? rows[rows.length - 1].getBoundingClientRect() : null;
    return {
      vw: window.innerWidth, vh: window.innerHeight,
      docScrollWidth: document.documentElement.scrollWidth,
      deviceRows: rows.length,
      roomRows: document.querySelectorAll('#sh-room-list .sh-room-row').length,
      dialog: box('#sh-modal .mqtt-modal-dialog'),
      body: box('#sh-modal .mqtt-modal-body'),
      cat: box('#sh-modal .sh-col-catalog'),
      act: box('#sh-modal .sh-col-actions'),
      head: box('#sh-modal .mqtt-modal-head'),
      foot: box('#sh-modal .mqtt-modal-foot'),
      save: box('#sh-dev-save'),
      lastRow: last ? { top: last.top, bottom: last.bottom } : null,
    };
  });
}

const r1 = (n) => Math.round(n * 10) / 10;

/* ── The MQTT dialogs are anchored to the VIEWPORT ────────────────────────
   #mqtt-scan-modal and #mqtt-add-modal are `position: fixed; inset: 0`
   overlays. Until 1.0.6.29 they sat inside #tab-mqtt, and `.tab-pane` runs a
   `fadeIn` animation whose keyframes carry `transform: translateY(4px)` — a
   transformed ancestor becomes the containing block for `position: fixed`, so
   while that animation runs the overlay is laid out against the TAB, not the
   screen. Measured (1.0.6.29, 1280×720): a nested overlay's box goes from
   0,0 1280×720 to 224,88 1032×770.9 — 50.9 px taller than the viewport — and
   its dialog from top 233.7/bottom 486.3 to 347.1/599.8. The window is the
   animation's ~200 ms (the pane's transform reads `none` from ~250 ms on), so
   the exposure was a modal opened right after a tab switch, plus the standing
   fragility of any future ancestor transform. Moving them to the document root
   removes the class; mqttTabDestroy closes them when the tab is left (the role
   the tab's own display:none used to play). Asserted per viewport:
     * neither overlay has a `.tab-pane` ancestor — the structural guarantee;
     * with #tab-mqtt transformed ON PURPOSE (the same translateY(4px) fadeIn
       applies, applied deterministically instead of racing a 200 ms window)
       the overlay still covers exactly the viewport box and its dialog lies
       inside it (top >= 0, bottom <= viewport height);
     * leaving the tab closes the dialog again — a root overlay must not stay
       on screen over another tab. */
const MQTT_MODALS = [
  { id: 'mqtt-scan-modal', open: 'mqttShowScanModal' },
  { id: 'mqtt-add-modal', open: 'mqttShowAddModal' },
];

async function runMqttAnchoring(browser, base) {
  let renders = 0;
  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
    await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.route('**/cgi-bin/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
    await page.goto(`${base}/index.html`, { waitUntil: 'load' });
    await page.evaluate(() => window.switchTab('mqtt'));
    await page.waitForTimeout(400);   // let the pane's entry transform settle
    console.log(`\n[${vp.name} ${vp.width}x${vp.height}] MQTT dialogs anchored to the viewport`);
    const struct = await page.evaluate(() => {
      const pane = document.getElementById('tab-mqtt');
      const paneAncestor = (id) => {
        for (let e = document.getElementById(id).parentElement; e; e = e.parentElement) {
          if (e.classList.contains('tab-pane')) return e.id;
        }
        return null;
      };
      return {
        active: pane.classList.contains('active'),
        scan: paneAncestor('mqtt-scan-modal'),
        add: paneAncestor('mqtt-add-modal'),
      };
    });
    check(struct.active, `precondition — the MQTT tab is the active pane (${struct.active})`);
    check(struct.scan === null && struct.add === null,
      `neither dialog is nested in a .tab-pane (scan: ${struct.scan || 'root'}, add: ${struct.add || 'root'})`);
    for (const m of MQTT_MODALS) {
      const g = await page.evaluate((mm) => {
        window[mm.open]();
        // The pane's own fadeIn transform, applied deterministically: racing the
        // real ~200 ms animation would be flaky, and this is the exact condition
        // that re-anchors a nested fixed overlay.
        const pane = document.getElementById('tab-mqtt');
        pane.style.transform = 'translateY(4px)';
        const ov = document.getElementById(mm.id);
        const dlg = ov.querySelector('.mqtt-modal-dialog');
        const o = ov.getBoundingClientRect();
        const d = dlg.getBoundingClientRect();
        const paneTransform = getComputedStyle(pane).transform;
        pane.style.transform = '';
        return {
          hidden: ov.hasAttribute('hidden'),
          paneTransform,
          overlay: { top: o.top, left: o.left, width: o.width, height: o.height },
          dialog: { top: d.top, bottom: d.bottom, height: d.height },
          vw: window.innerWidth, vh: window.innerHeight,
        };
      }, m);
      renders++;
      check(!g.hidden && g.dialog.height > 20, `${m.id}: opened and rendered (height ${r1(g.dialog.height)})`);
      check(Math.abs(g.overlay.top) <= 1 && Math.abs(g.overlay.left) <= 1
        && Math.abs(g.overlay.width - g.vw) <= 1 && Math.abs(g.overlay.height - g.vh) <= 1,
        `${m.id}: overlay covers the viewport box with #tab-mqtt transformed (${g.paneTransform}): ${r1(g.overlay.left)},${r1(g.overlay.top)} ${r1(g.overlay.width)}×${r1(g.overlay.height)} vs ${g.vw}×${g.vh}`);
      check(g.dialog.top >= -1 && g.dialog.bottom <= g.vh + 1,
        `${m.id}: dialog inside the viewport (top ${r1(g.dialog.top)}, bottom ${r1(g.dialog.bottom)}, vh ${g.vh})`);
      // Leaving the tab must close it — at the root nothing else hides it.
      const afterLeave = await page.evaluate((mm) => {
        window.switchTab('system');
        const ov = document.getElementById(mm.id);
        const r = ov.getBoundingClientRect();
        return { hidden: ov.hasAttribute('hidden'), painted: r.width > 0 && r.height > 0 && getComputedStyle(ov).display !== 'none' };
      }, m);
      check(afterLeave.hidden && !afterLeave.painted,
        `${m.id}: closed when the MQTT tab is left (hidden ${afterLeave.hidden}, painted ${afterLeave.painted})`);
      await page.evaluate(() => window.switchTab('mqtt'));
      await page.waitForTimeout(150);
    }
    check(errors.length === 0, `mqtt anchoring: no page errors (${errors.join(' | ')})`);
    await ctx.close();
  }
  return renders;
}

/* ── Shared picker helpers ────────────────────────────────────────────────── */
// The row's binding button as the operator sees it: its label, the part of
// that label the button can actually SHOW at its own font and content width,
// and the topic the hidden input carries.
function readBinding(page, index = 0) {
  return page.evaluate((i) => {
    const row = document.querySelectorAll('#sh-rows .sh-bind-row')[i];
    const btn = row && row.querySelector('.sh-row-bind');
    const input = row && row.querySelector('.sh-row-topic');
    if (!btn) return null;
    const cs = getComputedStyle(btn);
    const inner = btn.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    const ctx2d = document.createElement('canvas').getContext('2d');
    ctx2d.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    const label = (btn.textContent || '').trim();
    let visible = '';
    for (const ch of label) {
      if (ctx2d.measureText(visible + ch).width > inner) break;
      visible += ch;
    }
    return {
      label, visible,
      width: +btn.getBoundingClientRect().width.toFixed(1),
      inner: +inner.toFixed(1),
      topic: input ? input.value : '',
      unknown: btn.classList.contains('is-unknown'),
    };
  }, index);
}

async function openPicker(page, index = 0) {
  await page.evaluate((i) => {
    document.querySelectorAll('#sh-rows .sh-bind-row')[i].querySelector('.sh-row-bind').click();
  }, index);
  await page.waitForFunction(
    () => !document.getElementById('sh-pick-modal').hasAttribute('hidden'), null, { timeout: 8000 });
  await page.waitForSelector('#sh-pick-list', { state: 'attached' });
}

// A REAL click on the channel row, expanding its device card first if the
// picker did not open on it — the operator's path, not a JS assignment.
async function pickTopic(page, topic, index = 0) {
  await openPicker(page, index);
  await page.evaluate((t) => {
    const sel = '#sh-pick-list button[data-act="ch"][data-topic="' + t + '"]';
    if (!document.querySelector(sel)) {
      const owner = t.split('/')[2];
      const head = document.querySelector('.sh-pick-dev[data-id="' + owner + '"] button[data-act="dev"]');
      if (head) head.click();
    }
    const btn = document.querySelector(sel);
    if (!btn) throw new Error('channel not offered by the picker: ' + t);
    btn.click();
  }, topic);
  await page.waitForFunction(
    () => document.getElementById('sh-pick-modal').hasAttribute('hidden'), null, { timeout: 8000 });
}

/* ── The add/edit form: pane fit AND topic legibility, gated together ──────
   These two pull against each other, and gating only one is how the 1.0.6.29
   review found a regression: «Инвертировать» started life on the binding row,
   which fit the pane (524 px of content in a 524 px pane) by taking the width
   from the topic select (282 px → 132 px at 1280×720). Every topic a board
   offers begins with the same 25-character `/devices/SA-02m/controls/`, so at
   132 px every binding read alike in the closed select. The control is now a
   device field paired with the export toggle, and BOTH properties are asserted
   here, in the TALLEST state (editing an on/off device: icon picker and the
   invert field both showing):
     * the actions pane needs no scroll and «Сохранить» sits inside its box;
     * two different real bindings render DIFFERENTLY on the row's binding
       BUTTON (a <select> of raw paths until 1.0.6.38) — the visible prefix is
       measured with canvas measureText at the button's own computed font,
       inside its real content box, and the label must not be the raw topic:
       the two fixtures share the 25-character `/devices/SA-02m/controls/`
       prefix, which is exactly what made the old select unreadable.
   Mutation proof — run by hand, SEEN RED (1.0.6.29 review round): put the
   checkbox back on the binding row (the `.sh-bind-row` label markup plus
   `.sh-row-inv { flex: 1 1 100% }`) → exit 1, 4 failures, one per wide viewport
   × theme:
     FAIL - two real topics are distinguishable in the CLOSED select
            (132.3 px wide): "/devices/SA-02" vs "/devices/SA-02"
   Both topics collapse to the same visible string, which is the regression this
   pass exists for. The headroom assertion stays GREEN under that mutation — the
   row layout bought its fit with the select's width, which is exactly why the
   two properties have to be gated together. */
async function runActionsForm(browser, base) {
  let renders = 0;
  for (const vp of VIEWPORTS.filter((v) => v.wide)) {
    for (const theme of THEMES) {
      const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
      await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
      const page = await ctx.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(String(e)));
      await page.route('**/cgi-bin/**', (r) => r.fulfill({
        status: 200, contentType: 'application/json', body: stubBody(r.request().url()),
      }));
      await page.goto(`${base}/index.html`, { waitUntil: 'load' });
      if (theme === 'light') await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
      await page.evaluate(() => window.shOpenModal());
      await page.waitForFunction((n) => document.querySelectorAll('#sh-device-list .sh-dev-row').length === n,
        DEVICE_COUNT, { timeout: 8000 });
      // Edit the siren — a stored on/off binding with `inverted: true`.
      await page.evaluate(() => document.querySelector('#sh-device-list .sh-dev-row[data-id="dev1"] button[data-act="edit"]').click());
      await page.waitForTimeout(200);
      renders++;

      console.log(`\n[${vp.name} ${vp.width}x${vp.height} ${theme}] add/edit form — pane fit + binding legibility`);
      const g = await page.evaluate(() => {
        const act = document.querySelector('#sh-modal .sh-col-actions');
        const save = document.getElementById('sh-dev-save');
        const inv = document.getElementById('sh-inv-field');
        const invBox = document.getElementById('sh-dev-inverted');
        const row = document.querySelector('#sh-rows .sh-bind-row');
        const topic = row && row.querySelector('.sh-row-topic');
        const kind = row && row.querySelector('.sh-row-kind');
        if (!act || !save || !inv || !topic) return { missing: true };
        const ar = act.getBoundingClientRect();
        const sr = save.getBoundingClientRect();
        // scrollHeight FLOORS at clientHeight, so it can only say "fits" — never
        // by how much. The true extent is the last laid-out child's bottom (plus
        // its margin) against the pane's content top; that is what shows whether
        // the fit has a margin or is one control away from overflowing.
        const acs = getComputedStyle(act);
        const kids = [...act.children].filter((e) => !e.hidden && e.getBoundingClientRect().height > 0);
        const lastKid = kids[kids.length - 1];
        const lastR = lastKid.getBoundingClientRect();
        const used = lastR.bottom + parseFloat(getComputedStyle(lastKid).marginBottom)
          - (ar.top + parseFloat(acs.paddingTop));
        return {
          paneUsed: +used.toFixed(1), paneHeadroom: +(act.clientHeight - used).toFixed(1),
          paneContent: act.scrollHeight, paneBox: act.clientHeight, paneScrollTop: act.scrollTop,
          saveInside: sr.top >= ar.top - 1 && sr.bottom <= ar.bottom + 1,
          invVisible: !inv.hidden && inv.getBoundingClientRect().height > 0,
          invChecked: !!(invBox && invBox.checked),
          kind: kind ? kind.value : '',
        };
      });
      check(!g.missing, 'the add/edit form and its controls are present');
      if (!g.missing) {
        check(g.kind === 'switch' && g.invVisible && g.invChecked,
          `precondition — the TALLEST state is reached: on/off row, «Инвертировать» shown and restored ticked (kind ${g.kind}, shown ${g.invVisible}, ticked ${g.invChecked})`);
        check(g.paneContent <= g.paneBox + 1 && g.paneScrollTop === 0,
          `actions pane needs no scroll (${g.paneUsed} of ${g.paneBox} used)`);
        check(g.paneHeadroom >= PANE_HEADROOM_MIN,
          `the fit has a real margin, not a pixel: ${g.paneHeadroom} px headroom (floor ${PANE_HEADROOM_MIN})`);
        check(g.saveInside, '«Сохранить» is inside the pane without scrolling');
        // What the BUTTON can show at its own font and real content width —
        // the same question the closed select used to be asked.
        const a = await readBinding(page);
        check(!!a && a.topic === TOPIC_A,
          `precondition — the row is bound to the stored topic (${a && a.topic})`);
        check(!!a && a.label.indexOf('/devices/') !== 0 && a.label.length > 0,
          `the button names the channel, not the raw path ("${a && a.label}")`);
        // Re-bind through the real picker: TOPIC_B is another control of the
        // same device, so both labels share their leading words — the honest
        // hard case for a one-line button.
        await pickTopic(page, TOPIC_B);
        const b = await readBinding(page);
        check(!!b && b.topic === TOPIC_B,
          `the picker really re-bound the row (${b && b.topic})`);
        check(!!a && !!b && a.visible !== b.visible,
          `two real bindings are distinguishable on the button (${b && b.width} px wide): "${a && a.visible}" vs "${b && b.visible}"`);
      }
      check(errors.length === 0, `add/edit form: no page errors (${errors.join(' | ')})`);
      await page.locator('#sh-modal .sh-col-actions').screenshot({ path: join(SHOTS, `sh-modal-form-${vp.name}-${theme}.png`) });
      await ctx.close();
    }
  }
  return renders;
}

/* ── The invert flag's SAVE side — what the form WRITES ───────────────────
   runActionsForm proves a STORED `inverted: true` comes back as a ticked box.
   The opposite direction had no gate at all until the 1.0.6.29 review drove
   it by hand: `shCollectRows` sets the flag only on a capability row, and
   `shRowItem` writes `inverted: true` when ticked and DELETES the key when
   not. Both halves are asserted here from the real `upsert_device` POST body
   — the wire, never a JS internal — because a regression that kept the flag
   on untick, or wrote `inverted: false` instead of dropping it, would ship
   green: the Python validator strips a stored `false`, so the document on the
   board would look right while the operator's untick had silently done
   nothing (or, the other way, an active-low output would stay inverted).
   Non-vacuous by construction: the tallest edit state is a precondition, a
   save that emits no POST FAILS, and a POST whose device carries no on/off
   capability FAILS. Geometry-free — the write path has no viewport or theme
   dimension — so it runs once, on the first wide viewport.

   Mutation proof — run by hand, SEEN RED (1.0.6.29), both directions, in
   smarthome.js `shRowItem`:
     * `else delete item.inverted;` -> `else item.inverted = false;` — exit 1,
       1 failure: «unticked — the key is ABSENT from the saved on/off item,
       never written as false (inverted=false)»;
     * `if (row.inverted) item.inverted = true;` -> `if (false) …` — exit 1,
       1 failure: «ticked — the saved on/off item carries inverted: true
       (inverted=undefined)». */
async function runInvertSave(browser, base) {
  const vp = VIEWPORTS.find((v) => v.wide) || VIEWPORTS[0];
  const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
  await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  // Every upsert_device body is captured off the wire; the poll keeps serving
  // the stored catalogue, so re-editing the siren starts from `inverted: true`
  // both times and the two saves differ only by the operator's click.
  const saved = [];
  await page.route('**/cgi-bin/**', (r) => {
    const req = r.request();
    const url = req.url();
    const isApi = /sa02m_alice_api\.cgi/.test(url);
    if (isApi && req.method() === 'POST') {
      let sent = null;
      try { sent = JSON.parse(req.postData() || 'null'); } catch { sent = null; }
      if (sent && sent.action === 'upsert_device') {
        saved.push(sent.device);
        return r.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' });
      }
    }
    return r.fulfill({ status: 200, contentType: 'application/json', body: stubBody(url) });
  });
  await page.goto(`${base}/index.html`, { waitUntil: 'load' });
  await page.evaluate(() => window.shOpenModal());
  await page.waitForFunction((n) => document.querySelectorAll('#sh-device-list .sh-dev-row').length === n,
    DEVICE_COUNT, { timeout: 8000 });

  const editSiren = async () => {
    await page.waitForSelector('#sh-device-list .sh-dev-row[data-id="dev1"] button[data-act="edit"]');
    await page.evaluate(() => document.querySelector('#sh-device-list .sh-dev-row[data-id="dev1"] button[data-act="edit"]').click());
    await page.waitForFunction(() => {
      const f = document.getElementById('sh-inv-field');
      return !!f && !f.hidden && f.getBoundingClientRect().height > 0;
    }, null, { timeout: 8000 });
  };
  const waitForSave = async (n) => {
    for (let i = 0; i < 80 && saved.length < n; i++) await page.waitForTimeout(50);
    return saved.length >= n;
  };
  const onOff = (dev) => ((dev && dev.capabilities) || []).find((it) => it && it.type === 'devices.capabilities.on_off') || null;
  const show = (it) => JSON.stringify(it);

  console.log(`\n[${vp.name} ${vp.width}x${vp.height}] invert flag — the SAVE side (what upsert_device carries)`);

  // (1) Ticked (the stored state), saved untouched.
  await editSiren();
  check(await page.evaluate(() => !!document.getElementById('sh-dev-inverted').checked),
    'precondition — editing the stored active-low device shows «Инвертировать» ticked');
  await page.click('#sh-dev-save');
  const got1 = await waitForSave(1);
  check(got1, `saving emits a real upsert_device POST (${saved.length} captured)`);
  const item1 = got1 ? onOff(saved[0]) : null;
  check(!!item1, `precondition — the saved document carries the on/off capability (${show(saved[0])})`);
  check(!!item1 && item1.inverted === true,
    `ticked — the saved on/off item carries inverted: true (inverted=${item1 && JSON.stringify(item1.inverted)})`);

  // (2) The operator unticks it — a real click on the checkbox, then save.
  await page.waitForTimeout(200);
  await editSiren();
  await page.uncheck('#sh-dev-inverted');
  check(await page.evaluate(() => !document.getElementById('sh-dev-inverted').checked),
    'precondition — the real click really unticks the box');
  await page.click('#sh-dev-save');
  const got2 = await waitForSave(2);
  check(got2, `the second save emits its own upsert_device POST (${saved.length} captured)`);
  const item2 = got2 ? onOff(saved[1]) : null;
  check(!!item2, `precondition — the second saved document carries the on/off capability (${show(saved[1])})`);
  check(!!item2 && !('inverted' in item2),
    `unticked — the key is ABSENT from the saved on/off item, never written as false (inverted=${item2 && JSON.stringify(item2.inverted)})`);
  // The rest of the item survives the write: only the flag was meant to change.
  check(!!item2 && item2.mqtt === TOPIC_A && item2.parameters && item2.parameters.instance === 'on',
    `the untick changes nothing else on the item (${show(item2)})`);

  check(errors.length === 0, `invert save: no page errors (${errors.join(' | ')})`);
  await ctx.close();
  return 1;
}

/* ── The channel picker (#sh-pick-modal) ──────────────────────────────────
   What it closes: the bench 1.135 defect this pass was written for — the
   binding row was a <select> of raw topic paths, filled from the bridge yaml
   ALONE. `mr02m-COM3-10` carries no `channels` block there and a wrong
   `module_type: 2` (16DO), while the module answers 6DO8DI: the operator was
   offered ONE option (`mcu_temp`) for a module with 6 DO and 8 DI, and the
   two DO bindings that did work were only selectable because the row appended
   the already-stored topic to the list.

   Asserted on the SHIPPED bundles, against the inventory the CGI now serves:
     * the module's full channel set is offered — 6 DO and 8 DI, from the
       DETECTED type, not the yaml's 16DO — and the card says which it
       followed («YAML: 16DO · обнаружено: 6DO8DI»);
     * groups render in the DI → DO → AI → AO order inside a device, with the
       reading kind's preferred group first and «Диагностика» collapsed;
     * one device is expanded at a time (opening the second collapses the
       first) — the phone behaviour, one tap back;
     * search flattens the tree and matches the module, the COM/address and
       the channel number;
     * an already-bound channel is MARKED «занят» and stays clickable;
     * a stored topic outside the inventory keeps its own label and is not
       silently dropped (the round-trip invariant);
     * `ok:false` from the CGI falls back to hand entry with the binding
       intact (fail-closed), never to an empty picker.
   Geometry-free, so it runs once, at the first wide viewport. */
async function runPicker(browser, base) {
  const vp = VIEWPORTS.find((v) => v.wide) || VIEWPORTS[0];
  const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
  await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  let inventoryOk = true;
  await page.route('**/cgi-bin/**', (r) => {
    const url = r.request().url();
    if (!inventoryOk && /format=inventory/.test(url)) {
      return r.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":false,"error":"topics_failed"}' });
    }
    return r.fulfill({ status: 200, contentType: 'application/json', body: stubBody(url) });
  });
  await page.goto(`${base}/index.html`, { waitUntil: 'load' });
  await page.evaluate(() => window.shOpenModal());
  await page.waitForFunction((n) => document.querySelectorAll('#sh-device-list .sh-dev-row').length === n,
    DEVICE_COUNT, { timeout: 8000 });
  // Edit the siren: a real stored binding, so the picker opens on its device.
  await page.evaluate(() => document.querySelector('#sh-device-list .sh-dev-row[data-id="dev1"] button[data-act="edit"]').click());
  await page.waitForTimeout(150);

  console.log(`\n[${vp.name} ${vp.width}x${vp.height}] channel picker — inventory, grouping, search, «занят»`);
  await openPicker(page);
  const tree = await page.evaluate((mod) => {
    const cards = [...document.querySelectorAll('.sh-pick-dev')];
    const head = document.querySelector('.sh-pick-dev[data-id="' + mod + '"] button[data-act="dev"]');
    head.click();
    const card = document.querySelector('.sh-pick-dev[data-id="' + mod + '"]');
    const groups = [...card.querySelectorAll('.sh-pick-group')].map((g) => ({
      label: g.querySelector('.sh-pick-group-head').textContent.trim(),
      pref: !!g.querySelector('.sh-pick-pref'),
      collapsed: g.classList.contains('is-collapsed'),
      tags: [...g.querySelectorAll(':scope > .sh-pick-group-body > .sh-pick-ch-wrap > .sh-pick-ch-line > button[data-act="ch"]')]
        .map((b) => (b.getAttribute('data-topic') || '').split('/').pop()),
    }));
    return {
      cards: cards.map((c) => c.getAttribute('data-id')),
      expanded: [...document.querySelectorAll('.sh-pick-dev.is-open')].map((c) => c.getAttribute('data-id')),
      mismatch: (card.querySelector('.sh-pick-mismatch') || {}).textContent || '',
      badges: [...card.querySelectorAll('.sh-pick-badge')].map((b) => b.textContent.trim()),
      groups,
      busy: [...card.querySelectorAll('button[data-act="ch"].is-busy')]
        .map((b) => (b.getAttribute('data-topic') || '')),
      busyText: (card.querySelector('.sh-pick-ch-busy') || {}).textContent || '',
      subToggles: card.querySelectorAll('.sh-pick-subs-toggle').length,
    };
  }, MODULE_ID);
  const byKey = (k) => tree.groups.find((g) => g.tags.some((t) => t.indexOf(k + '_') === 0));
  const dos = byKey('do');
  const dis = byKey('di');
  check(tree.cards.length === INVENTORY.devices.length,
    `precondition — every inventory device has a card (${tree.cards.join(', ')})`);
  check(tree.expanded.length === 1 && tree.expanded[0] === MODULE_ID,
    `one device expanded at a time — the module, and only it (${tree.expanded.join(', ') || 'none'})`);
  check(!!dos && dos.tags.length === MODULE_DO,
    `the DETECTED type's ${MODULE_DO} DO channels are offered, not the yaml's 16 (${dos ? dos.tags.join(',') : 'no DO group'})`);
  check(!!dis && dis.tags.length === MODULE_DI,
    `all ${MODULE_DI} DI channels are offered (${dis ? dis.tags.length : 0})`);
  check(/16DO/.test(tree.mismatch) && /6DO8DI/.test(tree.mismatch),
    `the card names the yaml/detected disagreement ("${tree.mismatch.trim()}")`);
  check(tree.badges.some((b) => /6 DO/.test(b)) && tree.badges.some((b) => /8 DI/.test(b)),
    `the collapsed card already says what the module is (${tree.badges.join(' · ')})`);
  check(!!dos && dos.tags.join(',') === Array.from({ length: MODULE_DO }, (_, i) => 'do_' + (i + 1)).join(','),
    `channels ascend inside a group (${dos ? dos.tags.join(',') : ''})`);
  // The order is the terminal block's, DI first — fixed, whatever the kind is.
  check(tree.groups.length >= 3
    && /входы/i.test(tree.groups[0].label) && /выходы/i.test(tree.groups[1].label)
    && /Диагностика/i.test(tree.groups[2].label),
    `groups keep the fixed DI → DO → … order (${tree.groups.map((g) => g.label.replace(/[▾▸]/g, '').trim()).join(' | ')})`);
  // The edited row is an on/off binding: DI and DO are its groups, and the
  // kind marks them without reordering or hiding anything.
  check(tree.groups[0].pref && tree.groups[1].pref && !tree.groups[2].pref,
    `the kind marks DI/DO «рекомендуется» and leaves diagnostics unmarked (${tree.groups.map((g) => g.pref).join(',')})`);
  check(tree.groups.some((g) => /Диагностика/i.test(g.label) && g.collapsed),
    'the diagnostics group is collapsed');
  check(tree.busy.length === 1 && tree.busy[0] === BUSY_TOPIC && /занят/.test(tree.busyText),
    `the channel bound elsewhere is marked and still clickable ("${tree.busyText.trim()}")`);
  check(tree.subToggles === MODULE_DI,
    `every DI offers its counters as a collapsed sub-list (${tree.subToggles})`);

  // A row the operator cannot read is not an offer. The tag column exists only
  // for `<kind>_<n>` channels, so a named control's title is its ONLY label —
  // print neither and the row is an anonymous «чт/зп» (bench 1.135: the LED
  // driver's command group). Checked across EVERY device, expanded.
  const anon = await page.evaluate(async () => {
    const out = [];
    const ids = [...document.querySelectorAll('.sh-pick-dev')].map((c) => c.getAttribute('data-id'));
    for (const id of ids) {
      // Expanding re-renders the whole list, so the card is re-queried by id:
      // a click on a detached node never reaches the list's own listener, and
      // a collapsed card renders no channels — the check would read as passing.
      const sel = '.sh-pick-dev[data-id="' + id + '"]';
      if (!document.querySelector(sel).classList.contains('is-open')) {
        document.querySelector(sel + ' button[data-act="dev"]').click();
        await new Promise((r) => setTimeout(r, 30));
      }
      const open = document.querySelector(sel);
      const rows = open.querySelectorAll('button[data-act="ch"]');
      if (!rows.length) out.push(id + ': not expanded');
      for (const b of rows) {
        const label = [...b.querySelectorAll('.sh-pick-ch-tag, .sh-pick-ch-title')]
          .map((s) => s.textContent.trim()).join(' ').trim();
        if (!label) out.push(b.getAttribute('data-topic'));
      }
    }
    return out;
  });
  check(anon.length === 0,
    `every offered channel carries a label (${anon.length ? anon.join(', ') : 'all named'})`);

  // Attribute context (audit C4): every card is expanded here, so the `"`-
  // bearing channel is rendered. What is asserted is the PARSED result — the
  // attributes Chromium actually gave the button, the exact data-topic and
  // title, and that nothing ran — not the source text of the escaper.
  const inj = await page.evaluate((t) => {
    const btns = [...document.querySelectorAll('#sh-pick-list button[data-act="ch"]')]
      .filter((b) => b.dataset.topic === t);
    const b = btns[0];
    return {
      matches: btns.length,
      title: b ? b.getAttribute('title') : null,
      attrs: b ? [...b.attributes].map((a) => a.name).sort().join(' ') : '(no button)',
      leaked: document.querySelectorAll('#sh-modal [onfocus], #sh-modal [autofocus], #sh-pick-modal [onfocus], #sh-pick-modal [autofocus]').length,
      executed: window.__PWNED !== undefined,
    };
  }, INJECT_TOPIC);
  check(inj.matches === 1 && inj.title === INJECT_TOPIC,
    `a topic carrying «"» renders as ONE channel with its exact topic in data-topic and title (${inj.matches} match, title ${JSON.stringify(inj.title)})`);
  check(inj.leaked === 0 && !inj.executed,
    `no attribute escapes the quoted value — parsed [${inj.attrs}], onfocus/autofocus in the modals: ${inj.leaked}, handler executed: ${inj.executed}`);
  await page.locator('#sh-pick-modal .mqtt-modal-dialog').screenshot({ path: join(SHOTS, 'sh-pick-modal.png') });

  // Search: the tree flattens, and the query reaches the COM/address and the
  // channel number — the words on the module's label plate.
  const hits = await page.evaluate(async () => {
    const input = document.getElementById('sh-pick-search');
    const set = (v) => {
      input.value = v;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return [...document.querySelectorAll('#sh-pick-list button[data-act="ch"]')]
        .map((b) => b.getAttribute('data-topic'));
    };
    const flat = () => document.querySelectorAll('#sh-pick-list .sh-pick-hit').length;
    const cards = () => document.querySelectorAll('#sh-pick-list .sh-pick-dev').length;
    const com = set('COM3 do_4');
    const comFlat = flat();
    const addr = set('10');
    const none = set('нетакогоканала');
    set('');
    return { com, comFlat, addr, none, treeCards: cards(), treeFlat: flat() };
  });
  check(hits.com.length === 1 && /do_4$/.test(hits.com[0]) && hits.comFlat === 1,
    `search by COM + channel gives ONE flat hit, not a tree (${hits.com.join(', ')})`);
  check(hits.addr.length > 1, `search by address matches the module's channels (${hits.addr.length})`);
  check(hits.none.length === 0, 'a query with no match shows no channels');
  check(hits.treeCards === INVENTORY.devices.length && hits.treeFlat === 0,
    `clearing the query returns to the tree (${hits.treeCards} cards, ${hits.treeFlat} flat hits)`);

  // A stored topic no inventory knows: the label keeps it and says so.
  await page.evaluate(() => window.shPickClose());
  const ghost = await page.evaluate(() => {
    const row = document.querySelector('#sh-rows .sh-bind-row');
    row.querySelector('.sh-row-topic').value = '/devices/gone-away/controls/do_9';
    window.shAddRow('switch', '/devices/gone-away/controls/do_9', null);
    const rows = document.querySelectorAll('#sh-rows .sh-bind-row');
    const btn = rows[rows.length - 1].querySelector('.sh-row-bind');
    return { label: btn.textContent.trim(), unknown: btn.classList.contains('is-unknown') };
  });
  check(/gone-away/.test(ghost.label) && ghost.unknown,
    `a stored topic outside the inventory keeps its own label ("${ghost.label}")`);

  // The `"`-bearing topic ROUND-TRIPS: picked from the tree it lands verbatim
  // in the row's hidden input (the value shBindButtonHtml writes into
  // value="…"), and a row built for it by shAddRow renders the same value
  // with no attribute leaking out of the binding row either.
  await openPicker(page);
  // Reached through the SEARCH (the reopened picker collapses every card, and
  // a flat hit is the other attribute site the audit named). A truncated
  // data-topic (the defect) means no hit matches: recorded as a failure
  // below, never an exception that hides the later passes.
  const clicked = await page.evaluate((t) => {
    const input = document.getElementById('sh-pick-search');
    input.value = 'кавычки';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    const b = [...document.querySelectorAll('#sh-pick-list .sh-pick-hit[data-act="ch"]')]
      .find((x) => x.dataset.topic === t);
    if (b) b.click();
    return !!b;
  }, INJECT_TOPIC);
  if (clicked) {
    await page.waitForFunction(
      () => document.getElementById('sh-pick-modal').hasAttribute('hidden'), null, { timeout: 8000 });
  } else {
    await page.evaluate(() => window.shPickClose());
  }
  const trip = await page.evaluate((t) => {
    const first = document.querySelector('#sh-rows .sh-bind-row');
    window.shAddRow('switch', t, null);
    const rows = document.querySelectorAll('#sh-rows .sh-bind-row');
    const added = rows[rows.length - 1];
    return {
      picked: first.querySelector('.sh-row-topic').value,
      pickedTitle: first.querySelector('.sh-row-bind').title,
      built: added.querySelector('.sh-row-topic').value,
      leaked: document.querySelectorAll('#sh-rows [onfocus], #sh-rows [autofocus]').length,
      executed: window.__PWNED !== undefined,
    };
  }, INJECT_TOPIC);
  check(trip.picked === INJECT_TOPIC && trip.pickedTitle === INJECT_TOPIC,
    `picking the «"» topic stores it verbatim (${JSON.stringify(trip.picked)})`);
  check(trip.built === INJECT_TOPIC && trip.leaked === 0 && !trip.executed,
    `a binding row built for the «"» topic keeps it verbatim and leaks no attribute (value ${JSON.stringify(trip.built)}, leaked ${trip.leaked}, executed ${trip.executed})`);

  // Fail-closed: the CGI cannot answer → hand entry, binding intact.
  inventoryOk = false;
  await page.evaluate(() => { window.shCloseModal(); window.shOpenModal(); });
  await page.waitForTimeout(300);
  await page.evaluate(() => document.querySelector('#sh-device-list .sh-dev-row[data-id="dev1"] button[data-act="edit"]').click());
  await page.waitForTimeout(150);
  await openPicker(page);
  const failClosed = await page.evaluate(() => {
    const manual = document.getElementById('sh-pick-manual');
    const input = document.getElementById('sh-pick-manual-in');
    return {
      manualShown: !!manual && !manual.hidden,
      prefilled: input ? input.value : '',
      bound: document.querySelector('#sh-rows .sh-bind-row .sh-row-topic').value,
      channels: document.querySelectorAll('#sh-pick-list button[data-act="ch"]').length,
    };
  });
  check(failClosed.manualShown && failClosed.prefilled === TOPIC_A && failClosed.channels === 0,
    `inventory outage falls back to hand entry, prefilled with the binding ("${failClosed.prefilled}")`);
  check(failClosed.bound === TOPIC_A,
    `the binding survives the outage untouched (${failClosed.bound})`);

  check(errors.length === 0, `picker: no page errors (${errors.join(' | ')})`);
  await ctx.close();
  return 1;
}

/* ── The inventory that never answers (audit C7) ──────────────────────────
   shOpenModal() awaits the inventory before seeding the default binding row.
   Until 1.0.6.39 that fetch had no timeout, so a hung sa02m_alice_topics.cgi
   (its budget is 15 s under nginx's 20 s) left the operator a «Комнаты и
   устройства» with NO binding row and NO error — the 500 / auth / bad-JSON
   paths were handled, only the hang was not. Here the inventory route is
   simply never fulfilled; the row must appear within the fetch budget plus
   slack (smarthome.js SH_TOPICS_TIMEOUT_MS, 8 s) and the picker must offer
   hand entry, the same fail-closed shape the outage case above proves.
   Non-vacuous: the elapsed time is measured — a row that appears before the
   budget would mean the harness did not really hang the fetch. */
const HANG_BUDGET_MS = 8000;
async function runInventoryHang(browser, base) {
  const vp = VIEWPORTS.find((v) => v.wide) || VIEWPORTS[0];
  const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
  await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const pending = [];
  await page.route('**/cgi-bin/**', (r) => {
    const url = r.request().url();
    if (/format=inventory/.test(url)) { pending.push(r); return; }   // never answered
    return r.fulfill({ status: 200, contentType: 'application/json', body: stubBody(url) });
  });
  await page.goto(`${base}/index.html`, { waitUntil: 'load' });
  console.log(`\n[${vp.name} ${vp.width}x${vp.height}] inventory that never answers — the modal still seeds its row`);
  const t0 = Date.now();
  // Not awaited on purpose: on the pre-fix bundle this promise never settles.
  await page.evaluate(() => { window.shOpenModal(); return 0; });
  let seeded = false;
  try {
    await page.waitForFunction(() => document.querySelectorAll('#sh-rows .sh-bind-row').length >= 1,
      null, { timeout: HANG_BUDGET_MS + 4000 });
    seeded = true;
  } catch (e) { /* reported below */ }
  const elapsed = Date.now() - t0;
  check(seeded, `the default binding row is seeded although the inventory never answered (after ${elapsed} ms)`);
  check(!seeded || (elapsed >= HANG_BUDGET_MS - 500 && elapsed <= HANG_BUDGET_MS + 4000),
    `the row arrives at the fetch budget, not before it (${elapsed} ms vs ${HANG_BUDGET_MS} ms) — the hang was real`);
  if (seeded) {
    await openPicker(page);
    const fb = await page.evaluate(() => ({
      manualShown: !(document.getElementById('sh-pick-manual') || { hidden: true }).hidden,
      channels: document.querySelectorAll('#sh-pick-list button[data-act="ch"]').length,
    }));
    check(fb.manualShown && fb.channels === 0,
      `the picker falls back to hand entry (manual ${fb.manualShown}, channels ${fb.channels})`);
  }
  check(errors.length === 0, `inventory hang: no page errors (${errors.join(' | ')})`);
  for (const r of pending) { try { await r.abort(); } catch (e) { /* context closing */ } }
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
    const msg = String((e && e.message) || e);
    if (/Executable doesn't exist|npx playwright install|Looks like Playwright/i.test(msg)) {
      srv.close();
      die(2, MISSING_MSG, `  reason: the chromium browser failed to launch from playwright's cache — ${msg.split('\n')[0]}`);
    }
    throw e;
  }
  mkdirSync(SHOTS, { recursive: true });
  let rendered = 0;
  const matrix = [];
  try {
    for (const vp of VIEWPORTS) {
      for (const theme of THEMES) {
        const label = `${vp.name} ${vp.width}x${vp.height} ${theme}`;
        const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
        await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
        const page = await ctx.newPage();
        const errors = [];
        page.on('pageerror', (e) => errors.push(String(e)));
        await page.route('**/cgi-bin/**', (r) => r.fulfill({
          status: 200, contentType: 'application/json', body: stubBody(r.request().url()),
        }));
        await page.goto(`${base}/index.html`, { waitUntil: 'load' });
        if (theme === 'light') await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
        await page.evaluate(() => window.shOpenModal());
        // Reached state, not a timeout: the catalogue is rendered from the poll.
        await page.waitForFunction(
          (n) => document.querySelectorAll('#sh-device-list .sh-dev-row').length === n,
          DEVICE_COUNT, { timeout: 8000 },
        );
        await page.waitForTimeout(150);
        rendered++;

        console.log(`\n[${label}]`);
        const g = await readLayout(page);
        check(g.deviceRows === DEVICE_COUNT, `precondition — ${DEVICE_COUNT} device rows rendered (${g.deviceRows})`);
        check(g.roomRows === ROOM_COUNT, `precondition — ${ROOM_COUNT} room rows rendered (${g.roomRows})`);
        check(g.dialog.top >= -1 && g.dialog.bottom <= g.vh + 1,
          `dialog fits the viewport (top ${r1(g.dialog.top)}, bottom ${r1(g.dialog.bottom)}, vh ${g.vh})`);
        check(g.head.top >= -1 && g.foot.bottom <= g.vh + 1,
          `head and foot both on screen (head top ${r1(g.head.top)}, foot bottom ${r1(g.foot.bottom)})`);
        check(g.docScrollWidth <= g.vw + 1, `no horizontal overflow (scrollWidth ${g.docScrollWidth} vs ${g.vw})`);

        // The layer that must scroll: the catalogue pane when the panes sit
        // side by side, the modal body when they are stacked.
        const scroller = vp.wide ? '#sh-modal .sh-col-catalog' : '#sh-modal .mqtt-modal-body';
        const overflow = await page.evaluate((sel) => {
          const el = document.querySelector(sel);
          return { scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, overflowY: getComputedStyle(el).overflowY };
        }, scroller);
        check(overflow.scrollHeight > overflow.clientHeight + 1,
          `precondition — the catalogue overflows its scrolling layer (${overflow.scrollHeight} > ${overflow.clientHeight}), else nothing is proven`);

        if (vp.wide) {
          const vOverlap = Math.min(g.cat.bottom, g.act.bottom) - Math.max(g.cat.top, g.act.top);
          check(g.cat.right <= g.act.left + 1 && vOverlap > Math.min(g.cat.height, g.act.height) * 0.9,
            `two panes side by side (catalogue right ${r1(g.cat.right)} <= actions left ${r1(g.act.left)}, vertical overlap ${r1(vOverlap)})`);
          check(g.cat.left >= g.dialog.left - 1 && g.act.right <= g.dialog.right + 1,
            `both panes inside the dialog box (${r1(g.cat.left)}..${r1(g.act.right)} within ${r1(g.dialog.left)}..${r1(g.dialog.right)})`);
          check(g.act.scrollHeight <= g.act.clientHeight + 1 && g.act.scrollTop === 0,
            `actions pane needs no scroll (${g.act.scrollHeight} content in ${g.act.clientHeight})`);
          check(g.save.top >= g.act.top - 1 && g.save.bottom <= g.act.bottom + 1,
            `«Добавить» visible without scrolling (${r1(g.save.top)}..${r1(g.save.bottom)} within pane ${r1(g.act.top)}..${r1(g.act.bottom)})`);
        } else {
          check(g.cat.bottom <= g.act.top + 1,
            `panes stacked, catalogue first (catalogue bottom ${r1(g.cat.bottom)} <= actions top ${r1(g.act.top)})`);
          check(Math.abs(g.cat.left - g.act.left) <= 1 && Math.abs(g.cat.right - g.act.right) <= 1,
            `stacked panes share the full width (${r1(g.cat.left)}..${r1(g.cat.right)} vs ${r1(g.act.left)}..${r1(g.act.right)})`);
        }

        // A REAL wheel over the scrolling layer — never a scrollTop assignment:
        // what is proven is that the operator can reach the end, not that the
        // element is programmatically scrollable.
        const sBox = vp.wide ? g.cat : g.body;
        await page.mouse.move((sBox.left + sBox.right) / 2, (Math.max(sBox.top, 0) + Math.min(sBox.bottom, g.vh)) / 2);
        const probe = (sel) => {
          const el = document.querySelector(sel);
          const rows = document.querySelectorAll('#sh-device-list .sh-dev-row');
          const last = rows[rows.length - 1].getBoundingClientRect();
          const box = el.getBoundingClientRect();
          const foot = document.querySelector('#sh-modal .mqtt-modal-foot').getBoundingClientRect();
          return {
            scrollTop: el.scrollTop, max: el.scrollHeight - el.clientHeight,
            last: { top: last.top, bottom: last.bottom },
            box: { top: box.top, bottom: box.bottom },
            footTop: foot.top, vh: window.innerHeight,
          };
        };
        // Reachability, step by step: the stacked layout scrolls PAST the
        // catalogue into the actions, so "visible at the very end" is the wrong
        // question — what matters is that the wheel brings the last row into
        // the visible box at some point on the way down.
        let seen = null;
        let last = await page.evaluate(probe, scroller);
        const inBox = (s) => s.last.bottom <= s.box.bottom + 1 && s.last.top >= s.box.top - 1
          && s.last.bottom <= s.footTop + 1 && s.last.bottom <= s.vh + 1 && s.last.top >= 0;
        if (inBox(last)) seen = last;
        let guard = 0;
        while (guard++ < 25) {
          const before = last.scrollTop;
          await page.mouse.wheel(0, 240);
          await page.waitForTimeout(70);
          last = await page.evaluate(probe, scroller);
          if (!seen && inBox(last)) seen = last;
          if (last.scrollTop === before) break;
        }
        check(last.scrollTop > 0, `the wheel scrolls the layer (scrollTop ${Math.round(last.scrollTop)} of ${Math.round(last.max)})`);
        check(!!seen,
          seen
            ? `last device row reached inside the scrolling layer's visible box at scrollTop ${Math.round(seen.scrollTop)} (row ${r1(seen.last.top)}..${r1(seen.last.bottom)}, box ${r1(seen.box.top)}..${r1(seen.box.bottom)}, foot top ${r1(seen.footTop)})`
            : `last device row NEVER reached the scrolling layer's visible box (final row ${r1(last.last.top)}..${r1(last.last.bottom)}, box ${r1(last.box.top)}..${r1(last.box.bottom)}, scrollTop ${Math.round(last.scrollTop)} of ${Math.round(last.max)})`);
        check(errors.length === 0, `no page errors (${errors.join(' | ')})`);

        matrix.push({ label, cols: vp.wide ? 'side-by-side' : 'stacked', scrolled: Math.round(last.scrollTop) });
        await page.locator('#sh-modal .mqtt-modal-dialog').screenshot({ path: join(SHOTS, `sh-modal-${vp.name}-${theme}.png`) });
        await ctx.close();
      }
    }
    rendered += await runActionsForm(browser, base);
    rendered += await runPicker(browser, base);
    rendered += await runInvertSave(browser, base);
    rendered += await runMqttAnchoring(browser, base);
    rendered += await runInventoryHang(browser, base);
  } finally {
    await browser.close();
    srv.close();
  }
  console.log('\nviewport | arrangement | scrolled px');
  for (const m of matrix) console.log(`  ${m.label.padEnd(28)} ${m.cols.padEnd(14)} ${m.scrolled}`);
  if (rendered === 0) die(1, `${TAG}: ERROR — nothing was rendered; a pass without a render is not a pass`);
  if (failures) die(1, `\n${TAG}: ${failures} FAILURE(S) across ${VIEWPORTS.length} viewports × ${THEMES.length} themes`);
  console.log(`\n${TAG}: PASS — ${assertions} assertions: the «Комнаты и устройства» panes across ${VIEWPORTS.length} viewports × ${THEMES.length} themes plus the channel picker (with a «"»-bearing topic), the invert flag's save side, the MQTT-dialog viewport anchoring and the inventory hang (${rendered} renders, ${DEVICE_COUNT} devices / ${ROOM_COUNT} rooms)`);
  process.exit(0);
}

run().catch((e) => {
  console.error(`${TAG}: ERROR — ${(e && e.stack) || e}`);
  process.exit(1);
});
