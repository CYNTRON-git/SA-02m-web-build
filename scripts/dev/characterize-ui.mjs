#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   SA-02m Web UI — characterization harness (behaviour-preserving oracle)
   ───────────────────────────────────────────────────────────────────────────
   Purpose (backlog F10): before decomposing the god-files (app.js, flasher.js)
   into per-responsibility plain scripts, we need a net that proves each split
   step changed NOTHING the user can observe. This harness is that net.

   It serves the *repo* copy of www/network_config locally (so it characterizes
   the exact code being refactored, not whatever is deployed on a device) and
   proxies every /cgi-bin/* call and any non-static path to a live board, so the
   backend responses are real. Then it drives a headless Chromium across the
   whole surface — every tab × theme × board-variant — and records, per cell:

     • globals    — the full window-owned name inventory the scripts define.
                    A load-order refactor's #1 failure mode is a global that
                    stops being defined; this catches it exactly. (GATING)
     • errors     — console.error + pageerror + failed requests seen while the
                    cell was active. A ReferenceError from a broken split lands
                    here. (GATING)
     • dom        — a structural "skeleton" of the key containers (topbar,
                    sidebar, active tab pane): tag + id + classes + data-* only,
                    text and volatile attributes stripped, hashed. Catches a
                    section that fails to render. Robust to live-data churn
                    because values are stripped. (GATING)
     • shot       — a full-page screenshot + its sha256. Live data makes exact
                    pixels non-deterministic across runs, so this is a SOFT
                    signal (saved for the human eye + hash reported), never a
                    hard gate.

   Headless is necessary but NOT sufficient for a global-scope reorg — the ship
   beat still requires on-device click-through (backlog F10).

   Usage:
     node characterize-ui.mjs --label baseline --save-baseline
     node characterize-ui.mjs --compare baseline
   Env:
     SA02M_WEB_USER  (default admin)   SA02M_WEB_PASS  (required — UI login)
     DEVICE_URL      (default http://192.168.1.136:9999)   PORT (default 8099)
   ═══════════════════════════════════════════════════════════════════════════ */

import http from 'node:http';
import { existsSync, statSync, mkdirSync, writeFileSync, readFileSync, createReadStream } from 'node:fs';
import { join, resolve, extname, dirname } from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..', '..');
const WWW_ROOT = join(REPO_ROOT, 'www', 'network_config');

// ── Config ──────────────────────────────────────────────────────────────────
const DEVICE = (process.env.DEVICE_URL || 'http://192.168.1.136:9999').replace(/\/+$/, '');
const PORT = Number(process.env.PORT || 8099);
const USER = process.env.SA02M_WEB_USER || 'admin';
const PASS = process.env.SA02M_WEB_PASS || '';

// The characterization matrix.
const TABS = ['dashboard', 'network', 'time', 'system', 'flasher', 'mqtt', 'gateway'];
const THEMES = ['dark', 'light'];
// Board variant only changes visibility on tabs that show Ethernet #2 / port
// counts. Applying it elsewhere is a client-side no-op, so we only vary it where
// it is observable to keep the matrix small.
const VARIANT_TABS = new Set(['dashboard', 'network', 'system']);
const VARIANTS = ['sa02m-1eth', 'sa02m-2eth'];

// Key containers whose structure we treat as a gating oracle.
const CONTAINERS = { topbar: 'header.topbar', sidebar: 'nav.sidebar' };
// Subtrees (by id) collapsed in the skeleton — poll-driven, run-to-run variable,
// and out of scope for the F10 split. See pageSkeleton().
const PRUNE_IDS = ['nav-gateway-sub'];
// Harness lifecycle phases, not characterization cells. Errors here (e.g. the
// expected pre-authentication 401 the auth guard triggers before login) are not
// gating — the real oracle is the theme/tab/variant matrix. Their errors are
// still recorded in the manifest, just not compared.
const LIFECYCLE_CELLS = new Set(['init', 'login']);

// ── Args ──────────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
function argVal(name, def = null) {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : def;
}
const LABEL = argVal('--label', new Date().toISOString().replace(/[:.]/g, '-'));
const SAVE_BASELINE = args.includes('--save-baseline');
const COMPARE = argVal('--compare', null);
// --target device drives the deployed board directly (no local serve/proxy):
// the on-device end-to-end check the F10 ship beat requires. Default 'local'
// serves the repo files and proxies CGI to the board.
const TARGET = argVal('--target', 'local');
// What the browser navigates: the local serve+proxy, or the board directly.
const BASE = TARGET === 'device' ? DEVICE : `http://127.0.0.1:${PORT}`;

const ARTIFACTS = join(__dirname, 'artifacts', LABEL);
const BASELINE_DIR = join(__dirname, 'baseline');

// ── Tiny static-file + CGI-proxy server ─────────────────────────────────────
// GET of an existing static file under www/network_config (and not /cgi-bin/) is
// served from the repo; everything else is proxied to the live board verbatim,
// so login.cgi / status.cgi / etc. behave exactly as on the device.
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.map': 'application/json',
};
const deviceOrigin = new URL(DEVICE);

function safeStaticPath(urlPath) {
  const clean = decodeURIComponent(urlPath.split('?')[0]);
  if (clean.startsWith('/cgi-bin/')) return null;
  const rel = clean === '/' ? '/index.html' : clean;
  const abs = resolve(join(WWW_ROOT, '.' + rel));
  if (!abs.startsWith(WWW_ROOT)) return null;          // path-escape guard
  return existsSync(abs) && statSync(abs).isFile() ? abs : null;
}

function proxy(req, res) {
  const opts = {
    protocol: deviceOrigin.protocol, hostname: deviceOrigin.hostname,
    port: deviceOrigin.port, method: req.method, path: req.url,
    headers: { ...req.headers, host: deviceOrigin.host },
  };
  const up = http.request(opts, (upRes) => {
    res.writeHead(upRes.statusCode || 502, upRes.headers);
    upRes.pipe(res);
  });
  up.on('error', (e) => { res.writeHead(502); res.end('proxy error: ' + e.message); });
  req.pipe(up);
}

function startServer() {
  return new Promise((ok) => {
    const srv = http.createServer((req, res) => {
      const file = req.method === 'GET' ? safeStaticPath(req.url) : null;
      if (file) {
        res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' });
        createReadStream(file).pipe(res);
      } else {
        proxy(req, res);
      }
    });
    srv.listen(PORT, '127.0.0.1', () => ok(srv));
  });
}

// ── In-page capture helpers (run in the browser via page.evaluate) ──────────
// Structural skeleton: tag#id.classes plus sorted data-* keys, recursively,
// with all text and volatile attributes dropped. Stable under live-data churn.
//
// `prune` ids collapse a subtree to just `tag#id{pruned}` (no classes, no
// children): these hold async, poll-driven content whose structure legitimately
// varies run-to-run (e.g. the gateway COM sub-list toggled by gateway.js). They
// are out of scope for the F10 app.js/flasher.js split, so excluding them keeps
// the oracle a signal, not noise — while still asserting the node itself exists.
function pageSkeleton(opts) {
  const { sel, prune = [] } = opts;
  const root = document.querySelector(sel);
  if (!root) return null;
  const skel = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    if (el.id && prune.includes(el.id)) return s + '{pruned}';
    const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean).sort();
    if (cls.length) s += '.' + cls.join('.');
    const data = [...el.attributes].map((a) => a.name).filter((n) => n.startsWith('data-')).sort();
    if (data.length) s += '[' + data.join(',') + ']';
    const kids = [...el.children].map(skel);
    return kids.length ? s + '(' + kids.join(',') + ')' : s;
  };
  return skel(root);
}

// Full inventory of names the scripts put on window (functions and other own
// props). A load-order refactor's #1 failure mode is a global that stops being
// defined; this is the direct oracle for it.
function pageGlobals() {
  return Object.getOwnPropertyNames(window).sort().map((n) => {
    let t; try { t = typeof window[n]; } catch (_) { t = 'unreadable'; }
    return n + ':' + t;
  });
}

function sha(s) { return createHash('sha256').update(s).digest('hex').slice(0, 16); }

// ── Drive ───────────────────────────────────────────────────────────────────
async function run() {
  if (!PASS) {
    console.error('SA02M_WEB_PASS is empty — set the web-UI password to let the harness log in.');
    process.exit(2);
  }
  let chromium;
  try { ({ chromium } = await import('playwright')); }
  catch { console.error('playwright not installed — run: npm --prefix scripts/dev install'); process.exit(2); }

  mkdirSync(ARTIFACTS, { recursive: true });
  const srv = TARGET === 'device' ? null : await startServer();
  console.error(TARGET === 'device'
    ? `driving deployed board directly at ${BASE}`
    : `serving ${WWW_ROOT} on ${BASE}, /cgi-bin/* -> ${DEVICE}`);

  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  // Freeze the clock so time-derived rendering is at least run-stable.
  await context.addInitScript(() => {
    const FIXED = 1752000000000;
    const _Date = Date;
    // eslint-disable-next-line no-global-assign
    globalThis.Date = class extends _Date {
      constructor(...a) { super(...(a.length ? a : [FIXED])); }
      static now() { return FIXED; }
    };
  });
  const page = await context.newPage();

  // Error collection tagged with the currently-active cell.
  let cell = 'init';
  const errors = {};
  const note = (kind, text) => {
    (errors[cell] ||= []).push(`${kind}: ${String(text).slice(0, 300)}`);
  };
  page.on('console', (m) => { if (m.type() === 'error') note('console', m.text()); });
  page.on('pageerror', (e) => note('pageerror', e.message));
  page.on('requestfailed', (r) => {
    // Ignore navigations the harness itself aborts; report real asset/CGI failures.
    const f = r.failure();
    if (f && !/ERR_ABORTED/.test(f.errorText)) note('reqfail', `${r.url()} ${f.errorText}`);
  });

  const settle = async (ms = 700) => {
    try { await page.waitForLoadState('networkidle', { timeout: 4000 }); } catch (_) {}
    await page.waitForTimeout(ms);
  };

  // Login through the real login.cgi (proxied to the board).
  cell = 'login';
  await page.goto(`${BASE}/login.html`, { waitUntil: 'domcontentloaded' });
  // Environment reference: globals on login.html, which loads no app scripts.
  // Subtracting this from the dashboard set yields exactly the globals OUR
  // scripts define — origin-independent, so a localhost baseline and a plain-HTTP
  // device run compare cleanly (secure-context-only Web APIs cancel out).
  const envGlobals = await page.evaluate(pageGlobals);
  await page.fill('#username', USER);
  await page.fill('#password', PASS);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'domcontentloaded' }).catch(() => {}),
    page.click('button[type="submit"]'),
  ]);
  await settle();
  if (/login/.test(new URL(page.url()).pathname)) {
    const why = (errors.login || []).join(' | ');
    console.error('LOGIN FAILED — check SA02M_WEB_PASS / device reachability. ' + why);
    await browser.close(); srv?.close(); process.exit(3);
  }
  console.error('logged in as ' + USER);

  // Global inventory is defined at script-load time and is tab/theme independent
  // — capture it once, on the loaded dashboard, then keep only what our scripts
  // added on top of the environment reference.
  const fullGlobals = await page.evaluate(pageGlobals);
  const envNames = new Set(envGlobals.map((s) => s.split(':')[0]));
  const globals = fullGlobals.filter((s) => !envNames.has(s.split(':')[0]));

  const cells = {};
  for (const theme of THEMES) {
    await page.evaluate((t) => {
      try { localStorage.setItem('sa02m-theme', t); } catch (_) {}
      document.documentElement.setAttribute('data-theme', t);
    }, theme);
    for (const tab of TABS) {
      await page.evaluate((t) => window.switchTab && window.switchTab(t), tab);
      await settle();
      const variants = VARIANT_TABS.has(tab) ? VARIANTS : ['default'];
      for (const variant of variants) {
        cell = `${theme}/${tab}/${variant}`;
        if (variant !== 'default') {
          await page.evaluate((v) => window.applyVariantVisibility && window.applyVariantVisibility(v), variant);
          await page.waitForTimeout(200);
        }
        // Structural skeletons: shared chrome + the active pane.
        const dom = {};
        for (const [name, sel] of Object.entries({ ...CONTAINERS, pane: `#tab-${tab}` })) {
          const skel = await page.evaluate(pageSkeleton, { sel, prune: PRUNE_IDS });
          dom[name] = skel ? sha(skel) : null;
        }
        // Soft screenshot signal.
        const shotPath = join(ARTIFACTS, `${cell.replace(/\//g, '__')}.png`);
        const buf = await page.screenshot({ fullPage: true });
        writeFileSync(shotPath, buf);
        cells[cell] = { dom, shot: sha(buf.toString('latin1')) };
      }
    }
  }

  await browser.close();
  srv?.close();

  const manifest = {
    label: LABEL, at: new Date().toISOString(), device: DEVICE,
    tabs: TABS, themes: THEMES, variantTabs: [...VARIANT_TABS],
    globals, cells,
    // errors keyed by cell; a clean run has only benign/no entries.
    errors,
  };
  const manifestPath = join(ARTIFACTS, 'manifest.json');
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
  console.error(`manifest: ${manifestPath}`);
  console.error(`globals: ${globals.length}, cells: ${Object.keys(cells).length}, ` +
    `error-cells: ${Object.values(errors).filter(a => a.length).length}`);

  if (SAVE_BASELINE) {
    mkdirSync(BASELINE_DIR, { recursive: true });
    writeFileSync(join(BASELINE_DIR, 'manifest.json'), JSON.stringify(manifest, null, 2));
    console.error(`baseline saved: ${join(BASELINE_DIR, 'manifest.json')}`);
  }

  if (COMPARE) {
    // "--compare baseline" reads the oracle in baseline/ — captured by
    // --save-baseline at the start of THIS refactor session, never committed
    // (see README.md; the committed copy went stale and was removed in
    // 1.0.6.24 along with the `headless-smoke` row that never ran). Any other
    // value is treated as a prior run label under artifacts/.
    const baseFile = COMPARE === 'baseline'
      ? join(BASELINE_DIR, 'manifest.json')
      : join(__dirname, 'artifacts', COMPARE, 'manifest.json');
    if (!existsSync(baseFile)) { console.error(`compare baseline not found: ${baseFile}`); process.exit(4); }
    const base = JSON.parse(readFileSync(baseFile, 'utf8'));
    const diff = compare(base, manifest);
    reportDiff(diff);
    process.exit(diff.hardFail ? 1 : 0);
  }
}

// ── Compare (the oracle verdict) ────────────────────────────────────────────
function compare(base, cur) {
  const d = { globals: {}, cells: {}, errors: {}, shots: [], hardFail: false };

  // Globals: any name added/removed/retyped is a hard fail.
  const bg = new Map(base.globals.map((s) => [s.split(':')[0], s]));
  const cg = new Map(cur.globals.map((s) => [s.split(':')[0], s]));
  const removed = [...bg.keys()].filter((k) => !cg.has(k));
  const added = [...cg.keys()].filter((k) => !bg.has(k));
  const retyped = [...bg.keys()].filter((k) => cg.has(k) && bg.get(k) !== cg.get(k));
  if (removed.length || added.length || retyped.length) {
    d.globals = { removed, added, retyped }; d.hardFail = true;
  }

  // DOM skeletons: any change in a container's structure is a hard fail.
  for (const key of new Set([...Object.keys(base.cells), ...Object.keys(cur.cells)])) {
    const b = base.cells[key], c = cur.cells[key];
    if (!b || !c) { d.cells[key] = 'cell missing'; d.hardFail = true; continue; }
    const dd = {};
    for (const name of new Set([...Object.keys(b.dom), ...Object.keys(c.dom)])) {
      if (b.dom[name] !== c.dom[name]) dd[name] = `${b.dom[name]} -> ${c.dom[name]}`;
    }
    if (Object.keys(dd).length) { d.cells[key] = dd; d.hardFail = true; }
    if (b.shot !== c.shot) d.shots.push(key);            // soft
  }

  // New error signatures relative to baseline are a hard fail — but only on the
  // characterization matrix, not the transient login/init lifecycle phases.
  for (const key of Object.keys(cur.errors)) {
    if (LIFECYCLE_CELLS.has(key)) continue;
    const was = new Set(base.errors[key] || []);
    const isNew = (cur.errors[key] || []).filter((e) => !was.has(e));
    if (isNew.length) { d.errors[key] = isNew; d.hardFail = true; }
  }
  return d;
}

function reportDiff(d) {
  const line = (s) => console.log(s);
  if (Object.keys(d.globals).length) {
    line('✗ GLOBALS changed:');
    if (d.globals.removed?.length) line('   removed: ' + d.globals.removed.join(', '));
    if (d.globals.added?.length) line('   added:   ' + d.globals.added.join(', '));
    if (d.globals.retyped?.length) line('   retyped: ' + d.globals.retyped.join(', '));
  }
  for (const [k, v] of Object.entries(d.cells)) line(`✗ DOM ${k}: ${JSON.stringify(v)}`);
  for (const [k, v] of Object.entries(d.errors)) line(`✗ NEW ERROR ${k}: ${v.join(' | ')}`);
  if (d.shots.length) line(`~ screenshot differs (soft, review): ${d.shots.join(', ')}`);
  line(d.hardFail ? '\nRESULT: FAIL — revert this step.' : '\nRESULT: PASS (structure/globals/errors identical).');
}

run().catch((e) => { console.error(e); process.exit(10); });
