#!/usr/bin/env node
// comment-mutation-proof-exempt: behavioural harness - renders the SHIPPED bundles in headless Chromium and asserts computed geometry/text per contract state; it pins no source line by text, every state assertion is preceded by a reached-state precondition, and the state list is read from the contract so a missing or extra fixture FAILS.
/* ═══════════════════════════════════════════════════════════════════════════
   cloud-card-smoke — the «Облако» card's stand-down states, rendered for real.
   ───────────────────────────────────────────────────────────────────────────
   What it closes: no other gate renders the card's non-active states.
   ui-layout drives only the dashboard and Управление surfaces, never calls
   cloudRenderStatus, and stubs every CGI with `{}` — so the renderer rules
   that fixed the 1.0.6.26 bench leftover («Туннель: Работает / Последний
   отчёт: 19 сек. назад» on a board with no binding) were pinned by nothing
   in the tree. This row serves www/network_config statically, stubs ONLY
   cgi-bin/cloud.cgi per state (every other CGI -> `{}`), renders `active`
   first and then switches the stub INSIDE the same page — the bench
   transition — and asserts, per state and both themes:
     * the state label is reached (a non-vacuous precondition);
     * the «Туннель» / «Последний отчёт» rows are visible ONLY in `active`
       (computed box, never the `hidden` attribute) and their text is reset
       to «…» / «—» outside it;
     * the reason line is present in the four stand-down/409 states and
       absent elsewhere; no raw `Errno` reaches the card;
     * the pair button is disabled only in `unlink_failed`, and reads
       «Привязать заново» in `revoked` / `unlinked`.
   State list: docs/contracts/cloud-agent-status.md, the same `state ∈ …`
   line tests/test_status_contract.py parses — a documented state with no
   fixture here FAILS, and so does a fixture for an undocumented state.

   A second pass (runControlPlacement, 1.0.6.29) pins WHERE «Управление из
   облака» lives: on this card, gone from «Умный дом», honest on an absent
   `cloud_control` block, and really rendered from the Alice poll that carries
   it.

   A third pass (runAliceStandDown, 1.0.6.32) does for the «Яндекс Алиса» card
   what the first does for «Облако»: every state of ITS contract's `state ∈ …`
   line (docs/contracts/alice-mqtt-mapping.md) rendered for real, the «Причина»
   line present exactly in `unlinked` / `unlink_failed`, and — the Operator-
   visible half of that change — the «Привязать» row still VISIBLE after an
   unlink, because the local button no longer switches the client off. One home
   for card smoke, not a second driver; see that function's own header for its
   declared elsewhere-labelled state and its mutation proof.

   Exit codes (the runner has no skip signal distinct from success):
     0  every assertion passed (never without a real render);
     1  an assertion failed (state + assertion named), the contract line is
        missing, a fixture drifted from the contract, or ANY other error
        (a wrong served path, a stub error, an exception before the first
        render) — printed with the error;
     2  chromium/playwright missing — deliberately RED, never a vacuous
        green (plan cloud-card-smoke, decision 1); the two absences are told
        apart on the second line (package vs browser cache).

   Mutation proof: comment out `.system-manage-cloud-card .cloud-meta >
   div[hidden]` in main.css -> the rows stay visible after the in-page
   transition and every non-active state FAILS «rows absent».

   Harness: the standing Playwright install under scripts/dev (npm run
   ui-layout:install — the same chromium ui-layout reuses; no new dependency).
   Dev-only; never shipped to the device.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync, existsSync, mkdirSync, statSync } from 'node:fs';
import http from 'node:http';
import { join, dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const TAG = 'cloud-card-smoke';
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '..', '..');
// Overridable so a wrong path can be proven to exit 1 (not 2) without editing the tree.
const WWW = process.env.CLOUD_CARD_SMOKE_WWW
  ? resolve(process.env.CLOUD_CARD_SMOKE_WWW)
  : join(REPO, 'www', 'network_config');
const CONTRACT = join(REPO, 'docs', 'contracts', 'cloud-agent-status.md');
const SHOTS = join(REPO, '.ai-dev', 'quality', 'screenshots');
const MISSING_MSG = `${TAG}: chromium/playwright missing — run: npm run ui-layout:install`;
const THEMES = ['dark', 'light'];

function die(code, ...lines) {
  for (const l of lines) (code === 0 ? console.log : console.error)(l);
  process.exit(code);
}

/* ── Preflight — every failure here is exit 1, never 2 ──────────────────── */
if (!existsSync(join(WWW, 'index.html'))) {
  die(1, `${TAG}: ERROR — served path has no index.html: ${WWW}`);
}

/** The contract's state enum, from its one `state ∈ …` line. */
function documentedStates() {
  if (!existsSync(CONTRACT)) die(1, `${TAG}: ERROR — contract not found: ${CONTRACT}`);
  const m = readFileSync(CONTRACT, 'utf8').match(/`state ∈ ([^`]+)`/);
  const list = m ? m[1].split('|').map((s) => s.trim()).filter(Boolean) : [];
  if (!m || list.length === 0) {
    die(1, `${TAG}: ERROR — ${CONTRACT} has no \`state ∈ …\` line; refusing to run on an empty state list`);
  }
  return list;
}

/* ── Fixtures — one per contract state, every non-active one carrying the
      live-only keys ON PURPOSE (a stale file / an older agent): the renderer
      must not show them regardless. `label` = the «Соединение» badge text
      cloud.js renders for the state (the reached-state precondition). ───── */
const NOW = Math.floor(Date.now() / 1000);
const BASE = { serial: 'SN1', service_active: 'active', service_enabled: 'enabled', server_reachable: true, has_token_file: false, ts: NOW - 19 };
const STALE = { tunnel: 'running', last_heartbeat: NOW - 19 };
const iso = (secondsAgo) => new Date((NOW - secondsAgo) * 1000).toISOString();
const FIXTURES = {
  active:          { ...BASE, state: 'active', device_id: 'sa02m-abc', tunnel: 'running', last_heartbeat: NOW - 6, label: 'Подключено' },
  standby:         { ...BASE, ...STALE, state: 'standby', label: 'Не подключено' },
  pairing:         { ...BASE, ...STALE, state: 'pairing', claim_code: 'AB12-CD34', expires_at: NOW + 600, device_id: 'sa02m-abc', label: 'Ожидание' },
  pair_expired:    { ...BASE, ...STALE, state: 'pair_expired', device_id: 'sa02m-abc', label: 'Код истёк' },
  already_claimed: { ...BASE, ...STALE, state: 'already_claimed', device_id: 'sa02m-abc', reason: 'already claimed', reason_class: 'already_claimed', since: NOW - 19, label: 'Уже привязано' },
  claim_failed:    { ...BASE, ...STALE, state: 'claim_failed', label: 'Облако недоступно' },
  enrolling:       { ...BASE, ...STALE, state: 'enrolling', device_id: 'sa02m-abc', label: 'Активация' },
  enroll_failed:   { ...BASE, ...STALE, state: 'enroll_failed', device_id: 'sa02m-abc', label: 'Ошибка активации' },
  revoked:         { ...BASE, ...STALE, state: 'revoked', reason: 'device revoked', reason_class: 'revoked', unlinked_at: iso(120), label: 'Доступ отозван' },
  unlinked:        { ...BASE, ...STALE, state: 'unlinked', reason: 'subdomain not enrolled', reason_class: 'unlinked', unlinked_at: iso(300), label: 'Отвязано в облаке' },
  unlink_failed:   { ...BASE, ...STALE, state: 'unlink_failed', reason: 'wipe_failed', detail: "[Errno 13] Permission denied: '/etc/sa02m-cloud/device_secret'", reason_class: 'revoked', refusal: 'device revoked', label: 'Ошибка отвязки' },
};
const STAND_DOWN_LINE = new Set(['revoked', 'unlinked', 'unlink_failed', 'already_claimed']);

let failures = 0;
let assertions = 0;
function check(cond, msg) {
  assertions++;
  if (cond) console.log('  ok   - ' + msg);
  else { failures++; console.error('  FAIL - ' + msg); }
}

// Non-vacuous both ways: the contract and the fixture set must be the same set.
const documented = documentedStates();
const fixtured = Object.keys(FIXTURES);
for (const s of documented) check(fixtured.includes(s), `contract state "${s}" has a fixture`);
for (const s of fixtured) check(documented.includes(s), `fixture "${s}" is a documented contract state (no drift)`);
if (failures) die(1, `${TAG}: ${failures} FAILURE(S) — the state fixtures do not match the contract's \`state ∈ …\` line`);

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
    srv.listen(Number(process.env.CLOUD_CARD_SMOKE_PORT || 0), '127.0.0.1', () => ok(srv));
  });
}

/* ── The card as the browser computes it ─────────────────────────────────── */
async function readCard(page) {
  return page.evaluate(() => {
    // The computed box ONLY — never the `hidden` attribute. The attribute is
    // exactly what the bench defect defeated: a `display: grid` row rule
    // outranks the UA [hidden] rule, so an attribute-hidden row still had a
    // box and showed its stale value. Measuring the box is what lets the
    // mutation proof (the CSS override commented out) go red.
    const vis = (id) => {
      const el = document.getElementById(id);
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.height > 0 && r.width > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
    };
    const txt = (id) => document.getElementById(id).textContent.trim();
    const btn = document.getElementById('cloud-btn-pair');
    return {
      conn: txt('cloud-conn-state'),
      tunnelVisible: vis('cloud-row-tunnel'),
      tsVisible: vis('cloud-row-ts'),
      tunnelText: txt('cloud-tunnel-state'),
      tsText: txt('cloud-last-ts'),
      info: document.getElementById('cloud-unlink-info').textContent,
      infoHidden: document.getElementById('cloud-unlink-info').hidden,
      pairText: btn.textContent.trim(),
      pairReachable: vis('cloud-btn-pair') && vis('cloud-pair-idle'),
      pairDisabled: btn.disabled,
      pairTitle: btn.title,
    };
  });
}

/* ── «Управление из облака» — placement + honesty ─────────────────────────
   The control (unit sa02m-cloud-control) moved off the «Умный дом» card onto
   this one in 1.0.6.29. Its data does NOT come from cloud.cgi: the
   `cloud_control` block rides the Alice-API poll, so this pass stubs
   sa02m_alice_api.cgi (the state matrix above keeps its `{}` stub untouched)
   and asserts, per theme:
     * the button and the state badge are INSIDE #cloud-card — and no longer
       anywhere on #sh-card (both directions, so a copy-instead-of-move FAILS);
     * with no `cloud_control` in the payload the badge says «Нет данных» and
       the button is locked — never «Подключено» on absent live keys;
     * with a live block it really renders «Подключено» / «Выключить» — the
       moved render path is wired, not dead markup;
     * not enrolled: the button stays locked, with the hint that came with it.
   Mutation proof: put the button back inside #sh-card -> "inside #cloud-card"
   and "gone from #sh-card" both FAIL. */
const CTRL_CASES = [
  { name: 'no cloud_control block', cc: null, badge: 'Нет данных', disabled: true, label: null, hint: false },
  { name: 'enabled + connected', cc: { enabled: true, state: 'connected', cloud_enrolled: true }, badge: 'Подключено', disabled: false, label: 'Выключить', hint: false },
  { name: 'not enrolled', cc: { enabled: false, cloud_enrolled: false }, badge: 'Отключено', disabled: true, label: 'Включить', hint: true },
];

async function readControl(page) {
  return page.evaluate(() => {
    const btn = document.getElementById('cloud-btn-ctrl');
    const badge = document.getElementById('cloud-ctrl-state');
    const msg = document.getElementById('cloud-ctrl-msg');
    const card = document.getElementById('cloud-card');
    const sh = document.getElementById('sh-card');
    // Where each element ACTUALLY sits, so a failure names the card to look in
    // rather than restating that the element exists.
    const cardOf = (el) => {
      if (!el) return 'absent';
      const host = el.closest('.ctrl-card');
      return host ? (host.id || '(unnamed .ctrl-card)') : '(outside any card)';
    };
    return {
      btnExists: !!btn,
      badgeExists: !!badge,
      btnCard: cardOf(btn),
      badgeCard: cardOf(badge),
      inCloudCard: !!(card && btn && badge && card.contains(btn) && card.contains(badge)),
      onShCard: !!(sh && (sh.querySelector('#cloud-btn-ctrl, #cloud-ctrl-state, #cloud-ctrl-msg, #sh-btn-cloud, #sh-cloud-state, #sh-msg')
        || sh.textContent.includes('Управление из облака'))),
      legacyIds: ['sh-btn-cloud', 'sh-cloud-state', 'sh-msg'].filter((id) => !!document.getElementById(id)),
      badgeText: badge ? badge.textContent.trim() : '',
      btnText: btn ? btn.textContent.trim() : '',
      btnDisabled: btn ? btn.disabled : null,
      btnTitle: btn ? btn.title : '',
      msgText: msg && !msg.hidden ? msg.textContent.trim() : '',
    };
  });
}

async function runControlPlacement(browser, base) {
  let renders = 0;
  for (const theme of THEMES) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    // Switchable inside one page, exactly like the state matrix above.
    let alice = { ok: true, client_enabled: true, gateway: { available: true }, status: { state: 'connected' }, devices: { devices: [], rooms: [] } };
    await page.route('**/cgi-bin/**', (r) => {
      const url = r.request().url();
      const body = /cloud\.cgi/.test(url) ? JSON.stringify(FIXTURES.active)
        : /sa02m_alice_api\.cgi/.test(url) ? JSON.stringify(alice) : '{}';
      return r.fulfill({ status: 200, contentType: 'application/json', body });
    });
    await page.goto(`${base}/index.html`, { waitUntil: 'load' });
    if (theme === 'light') await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
    await page.evaluate(() => {
      document.querySelectorAll('.tab-pane').forEach((p) => p.classList.remove('active'));
      document.getElementById('tab-system').classList.add('active');
      window.cloudTabInit();
    });
    console.log(`\n[${theme}] «Управление из облака» placement`);
    for (const c of CTRL_CASES) {
      alice = c.cc ? { ...alice, cloud_control: c.cc } : { ...alice, cloud_control: undefined };
      if (!c.cc) delete alice.cloud_control;
      // Reached state, never a bare timeout: the badge is rendered from the poll.
      await page.waitForFunction(
        (want) => document.getElementById('cloud-ctrl-state').textContent.trim() === want,
        c.badge, { timeout: 12000 },
      );
      renders++;
      const s = await readControl(page);
      check(s.btnExists && s.badgeExists && s.inCloudCard,
        `${c.name}: the control is inside #cloud-card — button in ${s.btnCard}, badge in ${s.badgeCard}`);
      check(!s.onShCard && s.legacyIds.length === 0,
        `${c.name}: gone from the «Умный дом» card (on #sh-card: ${s.onShCard}, legacy ids left: ${s.legacyIds.join(',') || 'none'})`);
      check(s.badgeText === c.badge, `${c.name}: badge reads "${s.badgeText}"`);
      check(s.btnDisabled === c.disabled, `${c.name}: button ${c.disabled ? 'locked' : 'usable'} (disabled=${s.btnDisabled})`);
      if (c.label) check(s.btnText === c.label, `${c.name}: button reads «${s.btnText}»`);
      if (c.hint) {
        check(/привяжите устройство к облаку/.test(s.btnTitle) && /привяжите устройство к облаку/.test(s.msgText),
          `${c.name}: not-enrolled hint kept (title "${s.btnTitle}", line "${s.msgText}")`);
      }
    }
    check(errors.length === 0, `placement: no page errors (${errors.join(' | ')})`);
    await page.locator('#cloud-card').screenshot({ path: join(SHOTS, `cloud-card-control-${theme}.png`) });
    await ctx.close();
  }
  return renders;
}

/* ── The «Яндекс Алиса» card's stand-down states ───────────────────────────
   Same driver, same page, one more pass — this is the one home for card smoke,
   not a second harness. What it closes: the class the cloud half already paid
   for, a state the CLIENT writes that no card labels, which falls through to
   «Нет данных» and leaves the Operator with a blank where the explanation
   should be. `unlinked` / `unlink_failed` (1.0.6.32) are exactly that shape.

   Per state and both themes it asserts, after really reaching the state:
     * the «Соединение» badge carries that state's label (the precondition —
       a fallback to «Нет данных» fails here, not silently);
     * the «Причина» line is present in the two stand-down states and carries
       a mapped human phrase, and is absent (or at least not a «Причина» line)
       in every other state;
     * in `unlinked`: the «Привязать» row is VISIBLE and its button reads
       «Привязать», and «Сертификат» reads «Нет». That row is the Operator-
       visible point of the whole change: the local «Отвязать» no longer
       switches the client off, so отвязать → привязать заново works from the
       card. app/alice.js hides the row whenever the client is off.
   Mutation proof: put `client_enabled: false` back into the unlinked payload
   (what reverting the API change would produce) -> the link row is hidden and
   the assertion FAILS.

   The fixture set is compared BOTH WAYS against the contract's `state ∈ …`
   line, minus one declared exception: `missing_identity` is written only by
   the cloud profile into its own status file and is labelled on the «Облако»
   card (CLOUD_CTRL_STATE_MAP), never on this one. The exception is itself
   checked — if the contract ever stops documenting it, this pass FAILS rather
   than carrying a stale excuse. */
const ALICE_CONTRACT = join(REPO, 'docs', 'contracts', 'alice-mqtt-mapping.md');
const ALICE_CARD_ONLY_ELSEWHERE = ['missing_identity'];
const ALICE_BASE = {
  ok: true,
  client_enabled: true,
  gateway: { available: true, wss_url: 'wss://alice.cyntron.ru/controller/socket.io', http_url: 'https://alice.cyntron.ru' },
  mtls: { cert_present: false },
  devices: { devices: [], rooms: [] },
  link: { linked: false, pending: false, registration_url: null, state: 'unknown' },
};
const ALICE_FIXTURES = {
  disabled:        { client_enabled: false, status: { state: 'disabled' }, label: 'Отключено' },
  offline:         { status: { state: 'offline' }, label: 'Шлюз недоступен' },
  connecting:      { status: { state: 'connecting' }, label: 'Подключение' },
  connected:       { mtls: { cert_present: true }, status: { state: 'connected' }, label: 'Подключено' },
  error:           { status: { state: 'error', error: 'gateway_unreachable' }, label: 'Ошибка' },
  missing_deps:    { status: { state: 'missing_deps', error: 'missing_deps' }, label: 'Нет зависимостей' },
  missing_cert:    { status: { state: 'missing_cert', error: 'missing_cert' }, label: 'Нет сертификата' },
  unlinked:        { status: { state: 'unlinked', reason: 'controller_unlink', reason_class: 'unlinked', unlinked_at: '2026-09-03T10:00:00Z' }, label: 'Отвязано в облаке' },
  unlink_failed:   { status: { state: 'unlink_failed', reason: 'wipe_failed', detail: "[Errno 30] Read-only file system: '/var/lib/sa02m-alice/device.key.pem'", reason_class: 'unlinked' }, label: 'Ошибка отвязки' },
};
const ALICE_REASON_LINE = new Set(['unlinked', 'unlink_failed']);

function aliceDocumentedStates() {
  if (!existsSync(ALICE_CONTRACT)) die(1, `${TAG}: ERROR — contract not found: ${ALICE_CONTRACT}`);
  const m = readFileSync(ALICE_CONTRACT, 'utf8').match(/`state ∈ ([^`]+)`/);
  const list = m ? m[1].split('|').map((s) => s.trim()).filter(Boolean) : [];
  if (!m || list.length === 0) {
    die(1, `${TAG}: ERROR — ${ALICE_CONTRACT} has no \`state ∈ …\` line; refusing to run on an empty state list`);
  }
  return list;
}

async function readAliceCard(page) {
  return page.evaluate(() => {
    const vis = (id) => {
      const el = document.getElementById(id);
      if (!el) return false;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.height > 0 && r.width > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
    };
    const txt = (id) => (document.getElementById(id) || {}).textContent || '';
    const msg = document.getElementById('alice-msg');
    return {
      conn: txt('alice-conn-state').trim(),
      cert: txt('alice-cert-state').trim(),
      linkRowVisible: vis('alice-link-row'),
      linkBtnText: txt('alice-btn-link').trim(),
      linkStatus: txt('alice-link-status-val').trim(),
      msgVisible: !!(msg && !msg.hidden && msg.textContent.trim()),
      msgText: msg ? msg.textContent.trim() : '',
    };
  });
}

async function runAliceStandDown(browser, base) {
  const documented = aliceDocumentedStates();
  const fixtured = Object.keys(ALICE_FIXTURES);
  for (const s of ALICE_CARD_ONLY_ELSEWHERE) {
    check(documented.includes(s), `alice: the declared elsewhere-labelled state "${s}" is still documented (no stale exception)`);
  }
  const expected = documented.filter((s) => !ALICE_CARD_ONLY_ELSEWHERE.includes(s));
  for (const s of expected) check(fixtured.includes(s), `alice: contract state "${s}" has a card fixture`);
  for (const s of fixtured) check(expected.includes(s), `alice: fixture "${s}" is a documented contract state (no drift)`);

  let renders = 0;
  for (const theme of THEMES) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    let alice = { ...ALICE_BASE, mtls: { cert_present: true }, status: { state: 'connected' }, link: { ...ALICE_BASE.link, linked: true, state: 'connected' } };
    await page.route('**/cgi-bin/**', (r) => {
      const url = r.request().url();
      const body = /cloud\.cgi/.test(url) ? JSON.stringify(FIXTURES.active)
        : /sa02m_alice_api\.cgi/.test(url) ? JSON.stringify(alice) : '{}';
      return r.fulfill({ status: 200, contentType: 'application/json', body });
    });
    await page.goto(`${base}/index.html`, { waitUntil: 'load' });
    if (theme === 'light') await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
    await page.evaluate(() => {
      document.querySelectorAll('.tab-pane').forEach((p) => p.classList.remove('active'));
      document.getElementById('tab-system').classList.add('active');
    });
    // Reached state, never a bare timeout: the card is rendered from its poll.
    await page.waitForFunction(() => document.getElementById('alice-conn-state').textContent.trim() === 'Подключено', null, { timeout: 12000 });
    console.log(`\n[${theme}] «Яндекс Алиса» card states`);
    for (const name of expected) {
      const f = ALICE_FIXTURES[name];
      alice = {
        ...ALICE_BASE,
        ...f,
        link: { ...ALICE_BASE.link, state: name, linked: name === 'connected' },
      };
      delete alice.label;
      await page.waitForFunction(
        (want) => document.getElementById('alice-conn-state').textContent.trim() === want,
        f.label, { timeout: 12000 },
      );
      await page.waitForTimeout(120);
      renders++;
      const s = await readAliceCard(page);
      check(s.conn === f.label, `alice/${name}: state reached — «Соединение» reads "${s.conn}"`);
      if (ALICE_REASON_LINE.has(name)) {
        check(s.msgVisible && /^Причина: /.test(s.msgText) && s.msgText.length > 'Причина: '.length,
          `alice/${name}: «Причина» line present ("${s.msgText}")`);
        check(!/Errno/.test(s.msgText) && !/wipe_failed/.test(s.msgText),
          `alice/${name}: no raw errno / reason code on the card ("${s.msgText}")`);
      } else {
        check(!/^Причина: /.test(s.msgText),
          `alice/${name}: no «Причина» line outside the stand-down states ("${s.msgText}")`);
      }
      if (name === 'unlinked') {
        // The Operator-visible half of the change.
        check(s.linkRowVisible, 'alice/unlinked: the «Привязать» row is VISIBLE (client_enabled stays ON)');
        check(s.linkBtnText === 'Привязать', `alice/unlinked: the button reads «${s.linkBtnText}»`);
        check(s.cert === 'Нет', `alice/unlinked: «Сертификат» reads «${s.cert}»`);
        check(s.linkStatus === 'не привязан', `alice/unlinked: «Статус» reads «${s.linkStatus}»`);
      }
    }
    check(errors.length === 0, `alice: no page errors (${errors.join(' | ')})`);
    await page.locator('#alice-card').screenshot({ path: join(SHOTS, `alice-card-unlinked-${theme}.png`) });
    await ctx.close();
  }
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
  const matrix = [];
  try {
    for (const theme of THEMES) {
      for (const name of documented) {
        const payload = FIXTURES[name];
        const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
        await ctx.addCookies([{ name: 'session_token', value: 'test', domain: '127.0.0.1', path: '/' }]);
        const page = await ctx.newPage();
        const errors = [];
        page.on('pageerror', (e) => errors.push(String(e)));
        // The stub is SWITCHABLE: the page first sees `active`, then the
        // state under test — the bench transition, one page, no reload.
        let current = FIXTURES.active;
        await page.route('**/cgi-bin/**', (r) => r.fulfill({
          status: 200, contentType: 'application/json',
          body: /cloud\.cgi/.test(r.request().url()) ? JSON.stringify(current) : '{}',
        }));
        await page.goto(`${base}/index.html`, { waitUntil: 'load' });
        if (theme === 'light') await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
        await page.evaluate(() => {
          document.querySelectorAll('.tab-pane').forEach((p) => p.classList.remove('active'));
          document.getElementById('tab-system').classList.add('active');
          window.cloudTabInit();
        });
        await page.waitForFunction(() => document.getElementById('cloud-conn-state').textContent.trim() === 'Подключено', null, { timeout: 8000 });
        rendered++;
        const before = await readCard(page);
        console.log(`\n[${theme}] active → ${name}`);
        check(before.tunnelVisible && before.tunnelText === 'Работает' && before.tsVisible,
          `${name}: precondition — live rows visible in active (${before.tunnelText}, ${before.tsText})`);
        current = payload;
        await page.waitForFunction((label) => document.getElementById('cloud-conn-state').textContent.trim() === label, payload.label, { timeout: 8000 });
        await page.waitForTimeout(200);
        const s = await readCard(page);
        check(s.conn === payload.label, `${name}: state reached — conn badge "${s.conn}"`);
        if (name === 'active') {
          check(s.tunnelVisible && s.tsVisible, `${name}: live rows visible in active`);
        } else {
          check(!s.tunnelVisible && !s.tsVisible, `${name}: «Туннель»/«Последний отчёт» rows absent (visible: ${s.tunnelVisible}/${s.tsVisible})`);
          check(s.tunnelText === '…' && s.tsText === '—', `${name}: row text reset ("${s.tunnelText}", "${s.tsText}")`);
        }
        if (name === 'revoked' || name === 'unlinked') {
          check(!s.infoHidden && /Причина: /.test(s.info) && s.pairReachable && s.pairText === 'Привязать заново' && !s.pairDisabled,
            `${name}: stand-down line + «Привязать заново» ("${s.info}")`);
          check(/ · \d+ (сек|мин|ч)\. назад$/.test(s.info), `${name}: unlinked_at rendered as relative time ("${s.info.slice(-16)}")`);
        } else if (name === 'unlink_failed') {
          check(!s.infoHidden && /Причина: не удалось стереть файлы привязки/.test(s.info) && !/Errno/.test(s.info),
            `${name}: mapped reason, no errno on the card ("${s.info}")`);
          check(s.pairDisabled && /стереть привязку/.test(s.pairTitle), `${name}: pair button locked with reason ("${s.pairTitle}")`);
        } else if (name === 'already_claimed') {
          check(!s.infoHidden && /Причина: в облаке доступ отозван или устройство числится за владельцем; нажмите «Отвязать» в облаке/.test(s.info),
            `${name}: 409 reason line ("${s.info}")`);
          check(/ · \d+ (сек|мин|ч)\. назад$/.test(s.info), `${name}: 409 line carries the time ("${s.info.slice(-14)}")`);
        } else {
          check(s.infoHidden, `${name}: no reason line`);
        }
        if (!STAND_DOWN_LINE.has(name) && name !== 'active') check(!s.pairDisabled, `${name}: pair button not locked outside unlink_failed`);
        check(errors.length === 0, `${name}: no page errors (${errors.join(' | ')})`);
        matrix.push({ theme, state: name, label: s.conn, tunnel: s.tunnelVisible, ts: s.tsVisible });
        await page.locator('#cloud-card').screenshot({ path: join(SHOTS, `cloud-card-${name}-${theme}.png`) });
        await ctx.close();
      }
    }
    rendered += await runControlPlacement(browser, base);
    rendered += await runAliceStandDown(browser, base);
  } finally {
    await browser.close();
    srv.close();
  }
  console.log('\nstate → label | tunnel row visible | last-report row visible');
  for (const m of matrix) console.log(`  ${m.theme.padEnd(5)} ${m.state.padEnd(16)} ${m.label.padEnd(18)} ${m.tunnel} ${m.ts}`);
  if (rendered === 0) die(1, `${TAG}: ERROR — nothing was rendered; a pass without a render is not a pass`);
  if (failures) die(1, `\n${TAG}: ${failures} FAILURE(S) across ${documented.length} states × ${THEMES.length} themes`);
  console.log(`\n${TAG}: PASS — ${assertions} assertions across ${documented.length} «Облако» contract states + the «Управление из облака» placement pass + the «Яндекс Алиса» stand-down pass × ${THEMES.length} themes (in-page transitions, ${rendered} renders)`);
  process.exit(0);
}

run().catch((e) => {
  console.error(`${TAG}: ERROR — ${e && e.stack || e}`);
  process.exit(1);
});
