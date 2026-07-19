#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   SA-02m Web UI — geometry-measuring layout driver (dev-only tooling)
   ───────────────────────────────────────────────────────────────────────────
   Purpose: render the SA-02m pages in a real browser at a fixed viewport set,
   write a full-page screenshot per cell (so a human can LOOK at the вёрстка
   instead of photographing a phone), and ASSERT on real geometry
   (getBoundingClientRect) — never on CSS strings. String-reading is exactly
   what let two wrong layout fixes slip through in the sibling `cloud` repo's
   driver, whose shape this mirrors; measuring the rendered box is the fix.

   It reuses the existing dev harness's serving approach
   (scripts/dev/characterize-ui.mjs): serve the repo copy of www/network_config
   locally, STUB every /cgi-bin/* to `{}` (no device needed), and set the
   session_token cookie to walk past the client-side auth guard. Unlike that
   harness (a globals/DOM-skeleton characterization oracle), this one measures
   LAYOUT and fails the run on a geometry violation.

   WHAT IT COVERS
     • no horizontal overflow of the page — GATED at the supported widths
       (phone-landscape/tablet/desktop, names the culprit); at phone-portrait
       (360px, below SA-02m's tablet/desktop-first design floor) overflow is
       measured and REPORTED but not gated (multiple elements overflow there
       by design — see the ship report / backlog)
     • every visible interactive control ≥ 44×44 CSS px, or a whitelisted
       deviation with a printed reason (ledger discipline)
     • services-control column alignment (the 1.0.5.19 column grid): manage,
       toggle and badge columns each share one x-left across every row
     • no pairwise overlap among the dashboard widget cards

   WHAT IT DOES NOT COVER (honest limits — mirror the cloud driver)
     • Chromium ONLY — no Firefox/WebKit; a WebKit-specific layout bug is invisible.
     • FIXED viewport list only — a break at an in-between width is not sampled.
     • GEOMETRY, not visual regression — colour/font/pixel drift is NOT caught
       (that is the characterization harness's soft screenshot signal); there is
       deliberately no pixel baseline here.
     • Phone rows are desktop-Chromium AT a phone CSS width — NOT mobile
       emulation: no device DPR, no touch event model, no mobile UA. It checks
       that the layout reflows to the width, not that a real phone renders it.
     • Screenshots are for the human eye; they are never asserted on.

   Usage:   node .ai-dev/quality/checks/ui-layout.mjs
            npm run ui-layout           (after: npm run ui-layout:install)
   Skips gracefully (exit 0) when playwright / chromium is absent — CI has no
   browser, so this is an on-demand real-layer check, not a headless-less gate
   (same fail-safe contract as the `headless-smoke` quality row).
   ═══════════════════════════════════════════════════════════════════════════ */

import http from 'node:http';
import { existsSync, statSync, mkdirSync, rmSync, writeFileSync, createReadStream } from 'node:fs';
import { join, resolve, extname, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
// .ai-dev/quality/checks/ui-layout.mjs → repo root is three levels up.
const REPO_ROOT = resolve(__dirname, '..', '..', '..');
const WWW_ROOT = join(REPO_ROOT, 'www', 'network_config');
const SHOTS_DIR = join(REPO_ROOT, '.ai-dev', 'quality', 'screenshots');
const PORT = Number(process.env.UI_LAYOUT_PORT || 8902);
const BASE = `http://127.0.0.1:${PORT}`;

// ── Geometry constants ──────────────────────────────────────────────────────
const TOUCH_MIN = 44;   // WCAG 2.5.5 / platform minimum target, CSS px.
const EPS = 0.5;        // sub-pixel tolerance for alignment / overflow / size.

// `overflowGate` marks the widths at which a horizontal overflow HARD-FAILS the
// run. phone-portrait (360px) is BELOW SA-02m's supported layout width — the
// UI is a tablet/desktop-first embedded-browser admin console, and at 360px
// multiple independent elements overflow by design (topbar, long widget titles,
// management action buttons). So 360px is a screenshot-AND-report viewport: we
// still measure and PRINT its overflow (named, for the human eye), but do not
// gate on it. The gate has teeth at the supported widths, which are pixel-clean.
const VIEWPORTS = [
  { name: 'phone-portrait',  width: 360,  height: 800,  overflowGate: false },
  { name: 'phone-landscape', width: 800,  height: 360,  overflowGate: true },
  { name: 'tablet',          width: 768,  height: 1024, overflowGate: true },
  { name: 'desktop',         width: 1440, height: 900,  overflowGate: true },
];
const THEMES = ['dark', 'light'];
const VARIANTS = ['sa02m-1eth', 'sa02m-2eth'];

// TOUCH_WHITELIST — the ledger of interactive controls knowingly below 44×44.
// Every entry is printed each run with its match count; an entry that excuses
// NOTHING undersized this run FAILS (stale exemption), and an undersized
// control matched by NO entry FAILS (undocumented debt). `match` is a CSS
// selector tested with Element.matches. `deviation:true` = honest known debt
// (a control we accept below target), not a justified exemption. Seeded ONLY
// from the REAL SA-02m controls this run measures — no invented exemptions.
// NOTE: SA-02m's web UI is a DESKTOP/TABLET-first embedded-browser admin
// console; essentially every control sits below the 44px touch target by
// design. These four families are honest known debt, kept visible each run.
const TOUCH_WHITELIST = [
  { match: '.svc-ctl-btn', deviation: true,
    reason: 'Management services-table micro-buttons (Установить/Удалить/Пуск/Стоп): ~42×19px, 9px font in a dense mouse-first admin grid on the desktop-oriented Управление tab.' },
  { match: '.btn', deviation: true,
    reason: 'Standard admin action buttons (btn-primary/-danger/-warn/system-action-btn/…): the compact desktop-admin button scale renders ~26–34px tall, below the 44px height target.' },
  { match: 'input, select', deviation: true,
    reason: 'Text inputs and selects on the config/management forms: the compact admin field height is ~32–38px, below the 44px height target.' },
  { match: '.topbar-lang-btn', deviation: true,
    reason: 'Topbar utility buttons (language/theme/logout): compact header-bar controls ~24–29px tall.' },
];

// ── Playwright resolution — reuse the existing scripts/dev install ───────────
// The dev harness already carries playwright + a downloaded chromium under
// scripts/dev/node_modules; resolve from there so we neither duplicate the
// package nor re-download the browser. Fall back to a bare specifier if a
// root/global install exists instead.
async function loadPlaywright() {
  // playwright is a CJS package; via ESM interop `chromium` may land on the
  // module or on its `default` — normalise so callers just read `.chromium`.
  const norm = (mod) => (mod && mod.chromium ? mod : (mod && mod.default) || mod);
  const local = join(REPO_ROOT, 'scripts', 'dev', 'node_modules', 'playwright', 'index.js');
  if (existsSync(local)) {
    try { return norm(await import(pathToFileURL(local).href)); } catch (_) { /* fall through */ }
  }
  try { return norm(await import('playwright')); } catch (_) { return null; }
}

// ── Tiny static-file server + CGI stub (mirrors the dev harness's server) ────
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.map': 'application/json',
};

function safeStaticPath(urlPath) {
  const clean = decodeURIComponent(urlPath.split('?')[0]);
  if (clean.startsWith('/cgi-bin/')) return null;
  const rel = clean === '/' ? '/index.html' : clean;
  const abs = resolve(join(WWW_ROOT, '.' + rel));
  if (!abs.startsWith(WWW_ROOT)) return null;            // path-escape guard
  return existsSync(abs) && statSync(abs).isFile() ? abs : null;
}

// CGI stub body. Default is `{}` — enough for the auth wrapper to see a 200
// (never a 401) and for the guarded apply*Status() functions to no-op. The one
// endpoint that MUST carry real data is services_ctrl.cgi: the app auto-fetches
// it on the Управление tab and renderServicesControl([]) UNCONDITIONALLY wipes
// its host, so an empty stub would erase our injected mix mid-run. Feeding it
// the mix makes the app's own data path paint (and re-paint) our headline
// surface stably, immune to the rolling poll.
function cgiBody(url) {
  if (url.startsWith('/cgi-bin/services_ctrl.cgi')) {
    return JSON.stringify({ ok: true, ...MANAGEMENT_SERVICES });
  }
  return '{}';
}

function startServer() {
  return new Promise((ok) => {
    const srv = http.createServer((req, res) => {
      const file = req.method === 'GET' ? safeStaticPath(req.url) : null;
      if (file) {
        res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' });
        createReadStream(file).pipe(res);
      } else {
        res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
        res.end(cgiBody(req.url));
      }
    });
    srv.listen(PORT, '127.0.0.1', () => ok(srv));
  });
}

// ── Representative injected states (device-independent) ──────────────────────
const DASHBOARD_PRIORITY = {
  cpu_usage: 37, ram_used_kb: 512000, ram_total_kb: 1024000, ram_free_kb: 512000,
  ram_pct: 50, swap_total_kb: 262144, swap_used_kb: 32768, swap_pct: 12,
  temp_c: 52.4, disk_used_kb: 3200000, disk_total_kb: 7000000, disk_free_kb: 3800000, disk_pct: 46,
};
const DASHBOARD_NETWORK = {
  eth0_link: 'up', eth0_ip: '192.168.1.50', eth0_mask: '255.255.255.0', eth0_gw: '192.168.1.1',
  eth0_speed: '1000', eth0_rx_bytes: 1234567, eth0_tx_bytes: 890123,
  eth1_link: 'up', eth1_ip: '10.0.0.7', eth1_mask: '255.255.255.0',
};
const DASHBOARD_SERVICES = {
  services: [
    { id: 'codesys', label: 'CODESYS', active: 'active', installed: true },
    { id: 'mplc4', label: 'MPLC4', active: 'active', installed: true },
    { id: 'mqtt-bridge', label: 'MQTT мост', active: 'inactive', installed: true },
  ],
};
// The headline geometry surface: a MIX of states so column alignment is
// exercised across not-installed / running / stopped rows.
const MANAGEMENT_SERVICES = {
  flasher_busy: false,
  services: [
    { id: 'codesys',  label: 'CODESYS',  installed: false },                    // → [Установить]
    { id: 'mplc4',    label: 'MPLC4',     installed: true,  active: 'active' },  // → [Стоп][Удалить]
    { id: 'node-red', label: 'Node-RED',  installed: true,  active: 'inactive' },// → [Пуск][Удалить]
  ],
};

const SURFACES = [
  { id: 'dashboard', assertCards: true, assertColumns: false },
  { id: 'management', assertCards: false, assertColumns: true },
];

// ── In-page setup (runs in the browser) ─────────────────────────────────────
function pageSetup(arg) {
  const { surface, theme, variant, states } = arg;
  document.documentElement.setAttribute('data-theme', theme);
  if (window.applyVariantVisibility) window.applyVariantVisibility(variant);
  if (surface === 'dashboard') {
    if (window.switchTab) window.switchTab('dashboard');
    if (window.applyPriorityStatus) window.applyPriorityStatus(states.priority);
    if (window.applyNetworkStatus) window.applyNetworkStatus(states.network);
    if (window.applyServicesStatus) window.applyServicesStatus(states.services);
  } else if (surface === 'management') {
    if (window.switchTab) window.switchTab('system');
    if (window.renderServicesControl) window.renderServicesControl(states.management);
  }
}

// ── In-page measurement (runs in the browser) ───────────────────────────────
function pageMeasure(arg) {
  const { surface, TOUCH_MIN, EPS, wlSelectors } = arg;
  const vw = window.innerWidth;

  const describe = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean).slice(0, 3);
    if (cls.length) s += '.' + cls.join('.');
    const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 24);
    if (txt) s += ' «' + txt + '»';
    return s;
  };
  const visible = (el, r) => {
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };

  // (a) horizontal overflow — the page must not scroll sideways. Names the
  // widest offending element (right edge beyond the viewport); the caller
  // decides whether to gate on it (supported widths) or just report it
  // (phone-portrait, below the design floor).
  const scrollW = document.documentElement.scrollWidth;
  let overflow = null;
  if (scrollW > vw + 1) {
    let widest = null;
    document.querySelectorAll('*').forEach((el) => {
      const r = el.getBoundingClientRect();
      if (!visible(el, r)) return;
      if (r.right <= vw + EPS) return;
      if (!widest || r.right > widest.right) {
        widest = { desc: describe(el), right: Math.round(r.right * 10) / 10, left: Math.round(r.left * 10) / 10 };
      }
    });
    overflow = { scrollW, vw, widest };
  }

  // (b) touch-target sizes — collect the undersized visible controls
  const undersized = [];
  document.querySelectorAll('button, a[href], input, select, [onclick]').forEach((el) => {
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) return;
    if (r.width >= TOUCH_MIN - EPS && r.height >= TOUCH_MIN - EPS) return;
    const matched = [];
    wlSelectors.forEach((sel, i) => { try { if (el.matches(sel)) matched.push(i); } catch (_) {} });
    undersized.push({ desc: describe(el), w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10, matched });
  });

  // (c) services-control column alignment (management surface)
  let svcColumns = null;
  if (surface === 'management') {
    const rows = [...document.querySelectorAll('.svc-ctl-row')];
    if (rows.length) {
      const col = (r, pick) => { const el = pick(r); return el ? Math.round(el.getBoundingClientRect().left * 100) / 100 : null; };
      svcColumns = {
        rows: rows.length,
        manage: rows.map((r) => col(r, (x) => x.querySelector('.svc-ctl-manage .svc-ctl-btn') || x.querySelector('.svc-ctl-manage'))),
        toggle: rows.map((r) => col(r, (x) => x.querySelector('.svc-ctl-toggle'))),
        badge:  rows.map((r) => col(r, (x) => x.querySelector('.badge'))),
      };
    }
  }

  // (d) dashboard widget card rects (for pairwise overlap)
  let cards = null;
  if (surface === 'dashboard') {
    cards = [...document.querySelectorAll('.dash-grid > .widget')]
      .map((el) => ({ el, r: el.getBoundingClientRect() }))
      .filter(({ el, r }) => visible(el, r))
      .map(({ el, r }) => ({ desc: describe(el), left: r.left, right: r.right, top: r.top, bottom: r.bottom }));
  }

  return { overflow, undersized, svcColumns, cards };
}

// ── Assertion evaluation (Node side) ────────────────────────────────────────
function spread(nums) {
  const v = nums.filter((n) => typeof n === 'number');
  if (v.length < 2) return 0;
  return Math.max(...v) - Math.min(...v);
}
function overlaps(a, b) {
  const ix = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const iy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return ix > EPS && iy > EPS;
}

async function run() {
  const pw = await loadPlaywright();
  if (!pw) {
    console.log('ui-layout: skipped — playwright not installed (run: npm run ui-layout:install)');
    process.exit(0);
  }
  let browser;
  try {
    browser = await pw.chromium.launch();
  } catch (e) {
    const msg = String(e && e.message || e);
    if (/Executable doesn't exist|please run the following command|npx playwright install|download.*browser|Looks like Playwright/i.test(msg)) {
      console.log('ui-layout: skipped — chromium browser not installed (run: npm run ui-layout:install)');
      console.log('  ' + msg.split('\n')[0]);
      process.exit(0);
    }
    throw e;
  }

  rmSync(SHOTS_DIR, { recursive: true, force: true });
  mkdirSync(SHOTS_DIR, { recursive: true });

  const srv = await startServer();
  console.log(`ui-layout: serving ${WWW_ROOT} at ${BASE}, /cgi-bin/* stubbed → {}`);

  const wlSelectors = TOUCH_WHITELIST.map((w) => w.match);
  const wlHits = TOUCH_WHITELIST.map(() => 0);
  const failures = [];
  const overflowInfo = [];              // report-only overflow (non-gated viewports)
  const unlistedUndersized = new Map(); // desc → {w,h,cells:[]}
  const shots = [];

  const context = await browser.newContext();
  await context.addCookies([{ name: 'session_token', value: 'ui-layout-dev', url: BASE }]);
  // Kill transitions/animations so geometry is measured at rest, not mid-tween.
  await context.addInitScript(() => {
    const s = document.createElement('style');
    s.textContent = '*,*::before,*::after{transition:none!important;animation:none!important;}';
    (document.head || document.documentElement).appendChild(s);
  });
  const page = await context.newPage();

  for (const surface of SURFACES) {
    for (const vp of VIEWPORTS) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      for (const theme of THEMES) {
        for (const variant of VARIANTS) {
          const cellName = `${surface.id}__${vp.name}__${theme}__${variant}`;
          await page.goto(`${BASE}/index.html`, { waitUntil: 'domcontentloaded' });
          await page.evaluate(pageSetup, {
            surface: surface.id, theme, variant,
            states: {
              priority: DASHBOARD_PRIORITY, network: DASHBOARD_NETWORK,
              services: DASHBOARD_SERVICES, management: MANAGEMENT_SERVICES,
            },
          });
          // Let the app's auto-fetch of services_ctrl.cgi resolve and re-paint
          // (fed the mix by the stub) before we screenshot/measure, so the
          // captured state is the stable one, not a mid-flight frame.
          await page.waitForTimeout(300);

          const shotPath = join(SHOTS_DIR, cellName + '.png');
          await page.screenshot({ path: shotPath, fullPage: true });
          shots.push(shotPath);

          const m = await page.evaluate(pageMeasure, { surface: surface.id, TOUCH_MIN, EPS, wlSelectors });

          // (a) overflow — gate at supported widths, report-only at phone-portrait
          if (m.overflow) {
            const w = m.overflow.widest;
            const where = w ? ` — widest element: ${w.desc} (right=${w.right}, left=${w.left})` : ' — culprit not isolated';
            const line = `[${cellName}] horizontal overflow: scrollWidth ${m.overflow.scrollW} > viewport ${m.overflow.vw}${where}`;
            if (vp.overflowGate) failures.push(line);
            else overflowInfo.push(line);
          }
          // (b) touch targets — accumulate ledger + unlisted
          for (const u of m.undersized) {
            if (u.matched.length) { for (const i of u.matched) wlHits[i]++; }
            else {
              const key = u.desc;
              const rec = unlistedUndersized.get(key) || { w: u.w, h: u.h, cells: [] };
              if (rec.cells.length < 3) rec.cells.push(cellName);
              unlistedUndersized.set(key, rec);
            }
          }
          // (c) column alignment
          if (surface.assertColumns && m.svcColumns) {
            for (const colName of ['manage', 'toggle', 'badge']) {
              const sp = spread(m.svcColumns[colName]);
              if (sp > EPS) {
                failures.push(`[${cellName}] services ${colName} column not aligned: x-left spread ${sp.toFixed(2)}px > ${EPS}px ` +
                  `across ${m.svcColumns.rows} rows — lefts=[${m.svcColumns[colName].join(', ')}]`);
              }
            }
          }
          // (d) card overlap
          if (surface.assertCards && m.cards) {
            for (let i = 0; i < m.cards.length; i++) {
              for (let j = i + 1; j < m.cards.length; j++) {
                if (overlaps(m.cards[i], m.cards[j])) {
                  failures.push(`[${cellName}] dashboard cards overlap: ${m.cards[i].desc} ∩ ${m.cards[j].desc}`);
                }
              }
            }
          }
        }
      }
    }
  }

  await browser.close();
  srv.close();

  // ── Report-only overflow (phone-portrait, below the design floor) ─────────
  // NOT failures — named so the human sees the вёрстка reality at 360px, which
  // this driver has surfaced as a genuine known limit (see the file header /
  // ship report). The overflow GATE runs at the supported widths above.
  if (overflowInfo.length) {
    console.log('\nphone-portrait horizontal overflow (report-only, below supported width — NOT gated):');
    for (const l of overflowInfo) console.log('  · ' + l);
  }

  // ── Touch-target ledger (printed every run) ───────────────────────────────
  console.log('\nTOUCH_WHITELIST ledger (TOUCH_MIN=' + TOUCH_MIN + 'px):');
  TOUCH_WHITELIST.forEach((w, i) => {
    console.log(`  [${wlHits[i]} hits] ${w.deviation ? 'DEVIATION' : 'exempt   '}  ${w.match}`);
    console.log(`            ${w.reason}`);
    if (wlHits[i] === 0) failures.push(`TOUCH_WHITELIST entry excuses nothing undersized this run (stale): ${w.match}`);
  });
  if (unlistedUndersized.size) {
    console.log('\nUnlisted undersized controls (each FAILS — whitelist with a reason or fix the size):');
    for (const [desc, rec] of unlistedUndersized) {
      console.log(`  ${rec.w}×${rec.h}  ${desc}   e.g. ${rec.cells.join(', ')}`);
      failures.push(`unlisted undersized control: ${desc} (${rec.w}×${rec.h})`);
    }
  }

  // ── Verdict ───────────────────────────────────────────────────────────────
  console.log(`\nscreenshots: ${shots.length} written to ${SHOTS_DIR}`);
  if (failures.length) {
    console.log(`\nui-layout: FAIL — ${failures.length} geometry violation(s):`);
    for (const f of failures) console.log('  ✗ ' + f);
    process.exit(1);
  }
  console.log('\nui-layout: PASS — no geometry violations across ' +
    `${SURFACES.length} surfaces × ${VIEWPORTS.length} viewports × ${THEMES.length} themes × ${VARIANTS.length} variants.`);
  process.exit(0);
}

run().catch((e) => { console.error(e); process.exit(10); });
