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
       by design — see the ship report / backlog). Two ways: the page scrolling
       sideways (body scrollWidth > viewport) AND an element hanging past the
       right edge while an ancestor's `overflow:hidden` CLAMPS scrollWidth — the
       second is content clipped unreachable, which a scrollWidth-only test reads
       as green (ported from the cloud driver).
     • no VERTICAL content clipping: a box whose `overflow-y:hidden|clip` hides
       content taller than itself (`scrollHeight > clientHeight`) — the class of
       bug this driver MISSED (a mobile topbar/drawer swallowing its own controls,
       a KPI tile clipping its value). GATED at the supported widths, REPORT-only
       at phone-portrait and for boxes that legitimately scroll (`overflow-y:
       auto|scroll` — the user can reach the content).
     • every visible interactive control ≥ 44×44 CSS px, or a whitelisted
       deviation with a printed reason (ledger discipline)
     • services-control column alignment (the 1.0.5.19 column grid): manage,
       toggle and badge columns each share one x-left across every row
     • no pairwise overlap among the dashboard widget cards
     • mobile KPI tiles centre their value: at ≤560px (where the square-tile grid
       is in force) each direct-child `.widget-val` sits at the TRUE vertical
       centre (50%) of its tile, measured on the TEXT box (Range), not the padded
       element box — the guard behind the 1.0.5.33–35 "значения по центру" work
       (uptime/USB used to sit lower). CENTRE_EPS tolerance.
     • text below the HIG 11px floor (FONT_MIN) — measured and PRINTED across
       every cell, REPORT-ONLY: SA-02m is a compact desktop/tablet-first admin
       console at a 14px root, so a body of 0.72–0.78rem labels/hints + 9px
       micro-controls sits just under 11px by design, the same accepted posture
       the 44px touch ledger documents. Gating it (bump vs record each family) is
       an Operator product decision, not the driver's — so it prints, not fails.
     • WCAG 2.1 AA contrast (≥ 4.5:1) on the key text of both surfaces, in BOTH
       themes — the background SAMPLED FROM RENDERED PIXELS (blank the text,
       screenshot, median pixel under each run's own box), never derived from
       tokens, so a card tint / gradient / theme override is measured as painted.
       GATED (B3, 1.0.5.39 "закрепить контраст"): a measured pair below the floor
       that is NOT in CONTRAST_WHITELIST FAILS the run, named with element +
       ratio. CONTRAST_WHITELIST is the accepted-debt ledger — deliberately muted
       or disabled states (WCAG 1.4.3 inactive-component exemption), each with a
       `floor` ratchet and a reason; it is non-vacuous (a stale entry, or a pair
       deeper than its floor, FAILS), mirroring the touch-target ledger.

   NON-VACUOUS. No collection assertion passes on an empty collection: a selector
   that stops matching turns the run RED, not quietly to zero checks (ported from
   the cloud driver's assertNonEmpty rule) — this holds for the GATED checks
   (overflow, touch, columns, cards, KPI centring, vertical clipping, contrast)
   AND for the report-only font pass (a text selector matching nothing is a real
   regression and FAILS even there). Every gated check names the failing element
   and its
   measured-vs-expected numbers; a real violation exits non-zero.

   WHAT IT DOES NOT COVER (honest limits — mirror the cloud driver)
     • Chromium ONLY — no Firefox/WebKit; a WebKit-specific layout bug is invisible.
     • FIXED viewport list only — a break at an in-between width is not sampled.
     • GEOMETRY + type/contrast, not visual regression — pixel/spacing/radius/shadow
       drift is NOT caught (that is the characterization harness's soft screenshot
       signal); there is deliberately no pixel baseline here. Colour is checked only
       as CONTRAST, and font only as a SIZE floor.
     • Phone rows are desktop-Chromium AT a phone CSS width — NOT mobile
       emulation: no device DPR, no touch event model, no mobile UA. It checks
       that the layout reflows to the width, not that a real phone renders it.
       (So the cloud driver's <meta viewport> assertion is NOT ported — it is only
       observable under real device emulation, which this harness does not do.)
     • Screenshots are for the human eye; they are never asserted on (the contrast
       pass takes its own throwaway blanked-text screenshot, not compared to any
       baseline).

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
const CENTRE_EPS = 1.5; // sub-pixel slack for a value-centring claim, CSS px (cloud parity).
                        // The 1.0.5.34 fix measured the value text centre at
                        // fraction 0.499 of the tile; 1.5px catches "sitting lower"
                        // (the bug shifted it ~0.9em ≈ 29px) without flaking on
                        // line-height/Range sub-pixel rounding.
const CLIP_EPS = 1;     // px a box may under-run its own content before it counts (cloud parity)
                        // as clipping (absorbs sub-pixel scrollHeight rounding).
const FONT_MIN = 11;    // HIG minimum readable text size, CSS px. 11 is a FLOOR,
                        // not a target — text AT 11px passes (< FONT_MIN fails).
const CONTRAST_MIN = 4.5;  // WCAG 2.1 AA for normal-size text.
const CONTRAST_EPS = 0.02; // absorbs integer-channel rounding, not real drift.
// The KPI square-tile grid (centred value) is a `max-width:560px` rule in
// main.css, so the value-centring assertion is meaningful ONLY at/below this
// width; above it the tiles are ordinary widgets and the rule is not in force.
const KPI_CENTRE_MAX_W = 560;
// The KPI value elements that centre. Since the 1.0.5.64 KPI redesign the
// value nests inside `.widget-body` (dissolved on mobile via display:contents,
// so it is still a grid item of the symmetric 40px/1fr/40px tile); the legacy
// direct-child form is kept for any non-.dash-kpi tile. USB nests the value
// inside a storage/modem view that also holds the sub, so it centres the whole
// value+sub column, not the value alone — excluded by the direct-child `>`.
const KPI_TILE_VAL_SEL =
  '.dash-grid > .widget:not(.dash-wide):not(.dash-eth):not(.dash-span-top4) > .widget-val, ' +
  '.dash-grid > .widget.dash-kpi:not(.dash-wide):not(.dash-eth):not(.dash-span-top4) > .widget-body > .widget-val';

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

// FONT_WHITELIST — the families of sub-floor text this project ALREADY documents
// as deliberate compact-admin debt (mirrors the 9px entries in TOUCH_WHITELIST).
// The font floor is currently REPORT-ONLY (see the report section), so this list
// does NOT gate — it annotates a matched run as [known] in the printed ledger, so
// a NEW sub-floor family stands out from the documented posture. `match` is a CSS
// selector tested with Element.matches. When the Operator decides to gate the
// font floor, this list is the seed and the staleness rule can be turned on.
const FONT_WHITELIST = [
  { match: '.svc-ctl-btn',
    reason: 'Management services-table micro-buttons (Установить/Удалить/Пуск/Стоп): 9px label in a dense mouse-first admin grid on the desktop-oriented Управление tab.' },
  { match: '.widget-svc .svc-row .badge',
    reason: 'Dashboard Службы widget status badges: 9px status-badge type (--radius-xs), chosen to fit the compact services list on the dashboard card.' },
  { match: '.topbar-brand-ver, #app-version',
    reason: 'Topbar web-version tag next to the brand title: 0.82rem ≈ 11.5px at the 14px root (bumped with brand title; still a secondary label).' },
];

// CONTRAST_WHITELIST — text/background pairs recorded as accepted AA debt. The
// contrast pass GATES (see the report section): a measured pair below
// CONTRAST_MIN that is NOT matched here FAILS the run. Each entry carries a
// `match` selector, a `floor` ratchet and a `reason`, mirroring the touch/font
// ledger discipline. `floor` is a RATCHET, not a description: a matching pair
// measuring below `floor` FAILS even though it is whitelisted, so a token change
// cannot deepen recorded debt behind an entry written for a milder version; and
// an entry that no longer excuses anything sub-AA is stale and FAILS, so paying
// the debt forces the entry's removal. Every pair here is a DELIBERATELY muted or
// disabled state (WCAG 1.4.3 exempts inactive components) — never a fixable label
// buried to force green. Seeded from the 1.0.5.39 "закрепить контраст" run.
const CONTRAST_WHITELIST = [
  { match: '.hw-toggle-btn.na', floor: 1.5,
    reason: 'Unavailable HW-channel toggle «н/д»: --text-dim label by design (main.css .hw-toggle-btn.na) — a muted inactive state, WCAG 1.4.3 inactive-component exemption.' },
  { match: '.hw-status-val.na', floor: 1.5,
    reason: 'Unavailable HW-channel status value «н/д»: --text-dim readout by design (main.css .hw-status-val.na) — muted inactive state.' },
  { match: '.rs485-port-skeleton .rv', floor: 1.65,
    reason: 'RS-485 skeleton placeholder value «0» shown before the first real poll: --text-dim by design (main.css .rs485-port-skeleton .rv) — inactive/loading channel.' },
  { match: '#storage-format-toggle-label', floor: 1.8,
    reason: 'Storage auto-format toggle in its «НЕ УСТАНОВЛЕНО» (no storage-mount) state: the button is btn.disabled=true (app/status.js) — WCAG 1.4.3 disabled-control exemption.' },
  { match: '.kernel-apply-inline', floor: 1.25,
    reason: 'Kernel «Применить и перезагрузить» when no switchable kernel is staged: btn.disabled=true → .btn:disabled opacity .4 (main.css) — WCAG 1.4.3 disabled-control exemption.' },
  { match: '.kernel-refresh-inline', floor: 2.0,
    reason: 'Kernel «Обновить загрузочное ядро» (added 1.0.5.59) when the running kernel artifact is not valid: renderKernelControl (app/services.js) sets btn.disabled=true → .btn:disabled opacity .4 (main.css). Same disabled-control case as the sibling .kernel-apply-inline above (WCAG 1.4.3 exemption); the ENABLED button is --text on --bg-panel ≈ 12:1. floor 2.0 ratchets the measured worst (2.46 light / 3.40 dark).' },
  { match: '#alice-btn-enable', floor: 1.25,
    reason: 'Alice client enable/disable while status unknown (placeholder «…»): btn.disabled=true → .btn:disabled opacity .4 (main.css) — WCAG 1.4.3 disabled-control exemption; same measured wash as kernel-apply-inline (white on faded primary).' },
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
  const { surface, TOUCH_MIN, EPS, CLIP_EPS, CENTRE_EPS, FONT_MIN,
          KPI_CENTRE_MAX_W, KPI_TILE_VAL_SEL, wlSelectors, wlFont } = arg;
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
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' &&
           parseFloat(cs.opacity) !== 0;
  };
  const rnd = (n) => Math.round(n * 10) / 10;

  // (a) horizontal overflow, TWO ways. Names the widest element whose right edge
  // is beyond the viewport, then classifies:
  //   scroll  — the page itself scrolls sideways (documentElement scrollWidth >
  //             viewport). The plain "no sideways scroll" defect.
  //   clipped — an element hangs past the right edge while scrollWidth still FITS,
  //             because an ancestor's overflow:hidden CLAMPED it: the content is
  //             unreachable AND unscrollable, and a scrollWidth-only test reads
  //             green (ported from the cloud driver's second overflow check).
  // The caller gates or reports per viewport (overflowGate). `widest` is the
  // shallowest deepest-right element, for a human to grep.
  const scrollW = document.documentElement.scrollWidth;
  let widest = null;
  document.querySelectorAll('*').forEach((el) => {
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) return;
    if (r.right <= vw + EPS) return;
    if (!widest || r.right > widest.right) {
      widest = { desc: describe(el), right: rnd(r.right), left: rnd(r.left) };
    }
  });
  let overflow = null;
  if (scrollW > vw + 1) overflow = { kind: 'scroll', scrollW, vw, widest };
  else if (widest)      overflow = { kind: 'clipped', scrollW, vw, widest };

  // (b) touch-target sizes — collect the undersized visible controls
  const undersized = [];
  let controlCount = 0;
  document.querySelectorAll('button, a[href], input, select, [onclick]').forEach((el) => {
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) return;
    controlCount++;
    if (r.width >= TOUCH_MIN - EPS && r.height >= TOUCH_MIN - EPS) return;
    const matched = [];
    wlSelectors.forEach((sel, i) => { try { if (el.matches(sel)) matched.push(i); } catch (_) {} });
    undersized.push({ desc: describe(el), w: rnd(r.width), h: rnd(r.height), matched });
  });

  // (e) vertical content clipping — a box whose content is TALLER than its box
  // (scrollHeight > clientHeight) while its overflow-y would hide the surplus.
  //   hidden/clip — the surplus is invisible AND unreachable: a real defect
  //                 (the missed mobile topbar/drawer eating its controls, a KPI
  //                 tile clipping its value). Caller GATES it.
  //   auto/scroll — the surplus is reachable by scrolling: REPORT-only.
  // overflow-y:visible never clips (the box just spills, caught by overlap/
  // horizontal-overflow), so it is skipped. A single-line ellipsis clips
  // HORIZONTALLY (scrollHeight == clientHeight), so it does not trip this.
  const vclipHidden = [];   // gated
  const vclipScroll = [];   // report-only
  document.querySelectorAll('body *').forEach((el) => {
    if (el === document.body || el === document.documentElement) return;
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) return;
    if (el.clientHeight <= 0) return;
    if (el.scrollHeight <= el.clientHeight + CLIP_EPS) return;
    const oy = getComputedStyle(el).overflowY;
    const rec = { desc: describe(el), scrollH: el.scrollHeight, clientH: el.clientHeight,
                  hidden: rnd(el.scrollHeight - el.clientHeight) };
    if (oy === 'hidden' || oy === 'clip') vclipHidden.push(rec);
    else if (oy === 'auto' || oy === 'scroll') vclipScroll.push(rec);
  });

  // (f) mobile KPI value centring — only where the square-tile grid is in force
  // (vw <= KPI_CENTRE_MAX_W). Each direct-child `.widget-val` must sit at the
  // TRUE vertical centre of its tile's CONTENT box. Measured on the TEXT box
  // (Range), not the padded element box: the element fills the middle grid row,
  // so its own box is trivially centred whatever the text does — the text is
  // where "uptime sits lower" shows.
  let kpiCentres = null;
  if (surface === 'dashboard' && vw <= KPI_CENTRE_MAX_W) {
    kpiCentres = [];
    document.querySelectorAll(KPI_TILE_VAL_SEL).forEach((val) => {
      const vr = val.getBoundingClientRect();
      if (!visible(val, vr)) return;
      const tile = val.closest('.widget');
      if (!tile) return;
      const tr = tile.getBoundingClientRect();
      const cs = getComputedStyle(tile);
      const contentTop = tr.top + parseFloat(cs.borderTopWidth) + parseFloat(cs.paddingTop);
      const contentBottom = tr.bottom - parseFloat(cs.borderBottomWidth) - parseFloat(cs.paddingBottom);
      const contentCentreY = (contentTop + contentBottom) / 2;
      const rg = document.createRange();
      rg.selectNodeContents(val);
      const txt = rg.getBoundingClientRect();
      const valCentreY = (txt.top + txt.bottom) / 2;
      kpiCentres.push({
        desc: describe(tile), value: (val.textContent || '').trim().slice(0, 16),
        off: rnd(valCentreY - contentCentreY), valCentreY: rnd(valCentreY),
        contentCentreY: rnd(contentCentreY), tileH: rnd(contentBottom - contentTop),
      });
    });
  }

  // (g) font floor — every visible element that DIRECTLY contains text, with its
  // computed font-size. Only the sub-floor runs are returned (with their
  // whitelist matches); `textRunCount` is the non-empty denominator so a
  // selector that stops matching cannot pass this vacuously.
  const fontUnder = [];
  let textRunCount = 0;
  document.querySelectorAll('body *').forEach((el) => {
    const ownText = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (!ownText) return;
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) return;
    textRunCount++;
    const fontPx = parseFloat(getComputedStyle(el).fontSize);
    if (fontPx < FONT_MIN - 0.01) {
      const matched = [];
      wlFont.forEach((sel, i) => { try { if (el.matches(sel)) matched.push(i); } catch (_) {} });
      fontUnder.push({ desc: describe(el), fontPx: rnd(fontPx), matched,
                       text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 24) });
    }
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

  return { overflow, undersized, controlCount, svcColumns, cards,
           vclipHidden, vclipScroll, kpiCentres, fontUnder, textRunCount };
}

// ── Contrast: in-page text-run probe (runs in the browser) ──────────────────
// One entry per element that DIRECTLY paints text, with its computed colour,
// font size, cumulative opacity and page-space box. The BACKGROUND is NOT
// computed here: walking ancestor background-colors cannot see a card tint,
// gradient or theme override, so node samples it from rendered pixels instead
// (sampleBackdrops). Mirrors the cloud driver's probeTextRuns.
function probeContrastRuns(wlContrast) {
  const out = [];
  const ownText = (el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim().length > 0);
  for (const el of document.querySelectorAll('body *')) {
    if (!ownText(el)) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const rg = document.createRange();
    rg.selectNodeContents(el);
    const r = rg.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    // Skip runs an overflow ancestor clips ENTIRELY out of paint: they keep
    // layout geometry but no pixel of them is rendered, so the sampled median
    // would be whatever happens to lie at those coordinates (page background —
    // phantom white-on-white pairs in light). A partially visible run still
    // measures. Clipping ancestors include auto/scroll: content scrolled out
    // of an inner scrollport is equally absent from the screenshot.
    let clippedAway = false;
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const pcs = getComputedStyle(p);
      if (/(hidden|clip|auto|scroll)/.test(pcs.overflowX + ' ' + pcs.overflowY)) {
        const pr = p.getBoundingClientRect();
        const ix = Math.min(r.right, pr.right) - Math.max(r.left, pr.left);
        const iy = Math.min(r.bottom, pr.bottom) - Math.max(r.top, pr.top);
        if (ix < 2 || iy < 2) { clippedAway = true; break; }
      }
    }
    if (clippedAway) continue;
    let op = 1;
    for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
      op *= parseFloat(getComputedStyle(p).opacity);
    }
    if (op < 0.05) continue;   // effectively invisible — not a legibility claim
    let sel = el.tagName.toLowerCase();
    if (el.className && typeof el.className === 'string') {
      sel += '.' + el.className.trim().split(/\s+/).join('.');
    }
    let hit = null;
    for (const w of wlContrast) { try { if (el.matches(w)) { hit = w; break; } } catch (_) {} }
    out.push({
      sel, text: el.textContent.trim().replace(/\s+/g, ' ').slice(0, 24),
      color: cs.color, fontPx: parseFloat(cs.fontSize),
      weight: parseInt(cs.fontWeight, 10) || 400, opacity: op, whitelist: hit,
      x: r.left + window.scrollX, y: r.top + window.scrollY, w: r.width, h: r.height,
    });
  }
  return out;
}

// Sample the composited backdrop under every text run, from a screenshot taken
// with the text blanked (`color:transparent` changes no geometry). The median
// pixel inside a run's own box is exactly what is painted BEHIND its glyphs —
// card tint, gradient, theme override and all. Mirrors the cloud driver.
//
// Blanking is done INLINE per element, not via a `body *{color:transparent
// !important}` stylesheet: an app rule that sets `color` with `!important` at a
// higher specificity (e.g. `.hw-io-btn.hw-io-to-on{color:…!important}`) BEATS a
// `body *` stylesheet rule, so those glyphs would survive the blank and pollute
// their own sampled backdrop — understating the pair's contrast (the tiny green
// action buttons measured ~3.8:1 this way while really sitting at ~7:1 on their
// dark fill). An important INLINE declaration sits at the top of the author
// cascade and beats any stylesheet `!important`, so it blanks every glyph. The
// originals are saved and restored so the page is left as measured.
async function sampleBackdrops(page, runs, dsf) {
  if (!runs.length) return [];
  await page.evaluate(() => {
    const props = ['color', '-webkit-text-fill-color', 'text-shadow'];
    const vals = { color: 'transparent', '-webkit-text-fill-color': 'transparent', 'text-shadow': 'none' };
    const saved = [];
    for (const el of document.querySelectorAll('body *')) {
      const rec = [el];
      for (const p of props) rec.push(el.style.getPropertyValue(p), el.style.getPropertyPriority(p));
      saved.push(rec);
      for (const p of props) el.style.setProperty(p, vals[p], 'important');
    }
    window.__blankRestore = saved;
  });
  await page.waitForTimeout(80);
  const shot = await page.screenshot({ fullPage: true });
  await page.evaluate(() => {
    const props = ['color', '-webkit-text-fill-color', 'text-shadow'];
    for (const rec of (window.__blankRestore || [])) {
      const el = rec[0];
      props.forEach((p, i) => el.style.setProperty(p, rec[1 + i * 2], rec[2 + i * 2]));
    }
    delete window.__blankRestore;
  });
  return page.evaluate(async ({ b64, pts, dsf }) => {
    const img = new Image();
    img.src = 'data:image/png;base64,' + b64;
    await img.decode();
    const c = new OffscreenCanvas(img.width, img.height);
    const g = c.getContext('2d', { willReadFrequently: true });
    g.drawImage(img, 0, 0);
    return pts.map((p) => {
      const x = Math.max(0, Math.min(img.width - 1, Math.round(p.x * dsf)));
      const y = Math.max(0, Math.min(img.height - 1, Math.round(p.y * dsf)));
      const w = Math.max(1, Math.min(img.width - x, Math.round(p.w * dsf)));
      const h = Math.max(1, Math.min(img.height - y, Math.round(p.h * dsf)));
      const d = g.getImageData(x, y, w, h).data;
      const R = [], G = [], B = [];
      for (let i = 0; i < d.length; i += 4) { R.push(d[i]); G.push(d[i + 1]); B.push(d[i + 2]); }
      const med = (a) => { a.sort((m, n) => m - n); return a[a.length >> 1]; };
      return { r: med(R), g: med(G), b: med(B) };
    });
  }, { b64: shot.toString('base64'), pts: runs, dsf });
}

const _srgb = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
const _lum = (p) => 0.2126 * _srgb(p.r) + 0.7152 * _srgb(p.g) + 0.0722 * _srgb(p.b);
function contrastRatio(a, b) {
  const l1 = _lum(a), l2 = _lum(b);
  const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}
function parseColor(s) {
  const m = String(s).match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,/\s]+([\d.]+))?/);
  return m ? { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] } : null;
}
const _hex = (p) => '#' + [p.r, p.g, p.b].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');

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
  const wlFont = FONT_WHITELIST.map((w) => w.match);
  const wlFontHits = FONT_WHITELIST.map(() => 0);
  const failures = [];
  const overflowInfo = [];              // report-only overflow (non-gated viewports)
  const vclipReport = new Map();        // key → {kind,desc,hidden,cells} (report-only, deduped)
  const unlistedUndersized = new Map(); // desc → {w,h,cells:[]}
  const subFont = new Map();            // desc → {fontPx,text,cells:[],known} (report-only)
  const shots = [];

  const context = await browser.newContext();
  await context.addCookies([{ name: 'session_token', value: 'ui-layout-dev', url: BASE }]);
  // Kill transitions/animations so geometry is measured at rest, not mid-tween.
  // Also neutralise the app's INTERNAL scroller (.app is 100dvh with .main
  // overflow-y:auto): with it in force a fullPage screenshot is one viewport
  // tall, so every element below the fold is absent from the blanked shot and
  // sampleBackdrops clamps its coordinates to the image edge — sampling the
  // page background instead of the element's real fill (white-on-white
  // phantoms in light, and accidental passes both ways). Letting the document
  // itself grow makes fullPage capture the whole surface: backdrops sample
  // true pixels and the human screenshots really show the whole вёрстка.
  // Element-level geometry (rects, centring, touch, overflow-x) is unaffected.
  await context.addInitScript(() => {
    // At init-script time the document may not be parsed yet (no <head>), so
    // append on DOMContentLoaded — a direct appendChild here lands nowhere and
    // the whole override silently no-ops.
    const add = () => {
      const s = document.createElement('style');
      s.textContent = '*,*::before,*::after{transition:none!important;animation:none!important;}' +
        '.app{height:auto!important;min-height:100vh;}' +
        '.main{overflow-y:visible!important;}';
      (document.head || document.documentElement).appendChild(s);
    };
    if (document.head) add();
    else document.addEventListener('DOMContentLoaded', add);
  });
  const page = await context.newPage();

  for (const surface of SURFACES) {
    for (const vp of VIEWPORTS) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      for (const theme of THEMES) {
        for (const variant of VARIANTS) {
          const cellName = `${surface.id}__${vp.name}__${theme}__${variant}`;
          // Re-assert the cell's nominal viewport: the post-load doc-height
          // growth below persists across cells, and vh-dependent layout must
          // start from the same base every time.
          await page.setViewportSize({ width: vp.width, height: vp.height });
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

          // Grow the viewport to the full document height (capped) before the
          // shot: this Chromium's fullPage capture does not PAINT content below
          // the original viewport (the canvas grows, the pixels stay page-bg),
          // so a 100vh-app cell otherwise screenshots as one viewport of UI
          // over a blank tail. Width (the media-query axis) is untouched.
          const docH = await page.evaluate(() => Math.min(5000,
            Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)));
          if (docH > vp.height) await page.setViewportSize({ width: vp.width, height: docH });

          const shotPath = join(SHOTS_DIR, cellName + '.png');
          await page.screenshot({ path: shotPath, fullPage: true });
          shots.push(shotPath);

          const m = await page.evaluate(pageMeasure, {
            surface: surface.id, TOUCH_MIN, EPS, CLIP_EPS, CENTRE_EPS, FONT_MIN,
            KPI_CENTRE_MAX_W, KPI_TILE_VAL_SEL, wlSelectors, wlFont,
          });

          // (a) horizontal overflow — scroll (page moves) OR clipped (content
          // hangs past the edge, unreachable). Gate at supported widths,
          // report-only at phone-portrait (below the design floor).
          if (m.overflow) {
            const w = m.overflow.widest;
            const where = w ? ` — widest element: ${w.desc} (right=${w.right}, left=${w.left})` : ' — culprit not isolated';
            const line = m.overflow.kind === 'clipped'
              ? `[${cellName}] CLIPPED horizontal overflow: element past viewport ${m.overflow.vw} while scrollWidth ${m.overflow.scrollW} still fits (unreachable content)${where}`
              : `[${cellName}] horizontal overflow: scrollWidth ${m.overflow.scrollW} > viewport ${m.overflow.vw}${where}`;
            if (vp.overflowGate) failures.push(line);
            else overflowInfo.push(line);
          }
          // (b) touch targets — accumulate ledger + unlisted. Non-empty guard:
          // the interactive-control selector matching NOTHING is a rotted
          // selector or an unrendered surface, not a pass.
          if (m.controlCount === 0) {
            failures.push(`[${cellName}] no interactive controls matched (button/a[href]/input/select/[onclick]) — selector rotted or the surface did not render`);
          }
          for (const u of m.undersized) {
            if (u.matched.length) { for (const i of u.matched) wlHits[i]++; }
            else {
              const key = u.desc;
              const rec = unlistedUndersized.get(key) || { w: u.w, h: u.h, cells: [] };
              if (rec.cells.length < 3) rec.cells.push(cellName);
              unlistedUndersized.set(key, rec);
            }
          }
          // (c) column alignment — with a non-empty guard on the rows.
          if (surface.assertColumns) {
            if (!m.svcColumns || !m.svcColumns.rows) {
              failures.push(`[${cellName}] management has no .svc-ctl-row rows to align — the services-control grid did not render (selector rotted or empty)`);
            } else {
              for (const colName of ['manage', 'toggle', 'badge']) {
                const sp = spread(m.svcColumns[colName]);
                if (sp > EPS) {
                  failures.push(`[${cellName}] services ${colName} column not aligned: x-left spread ${sp.toFixed(2)}px > ${EPS}px ` +
                    `across ${m.svcColumns.rows} rows — lefts=[${m.svcColumns[colName].join(', ')}]`);
                }
              }
            }
          }
          // (d) card overlap — with a non-empty guard on the cards.
          if (surface.assertCards) {
            if (!m.cards || m.cards.length === 0) {
              failures.push(`[${cellName}] dashboard has no .dash-grid > .widget cards — the grid did not render (selector rotted or empty)`);
            } else {
              for (let i = 0; i < m.cards.length; i++) {
                for (let j = i + 1; j < m.cards.length; j++) {
                  if (overlaps(m.cards[i], m.cards[j])) {
                    failures.push(`[${cellName}] dashboard cards overlap: ${m.cards[i].desc} ∩ ${m.cards[j].desc}`);
                  }
                }
              }
            }
          }
          // (e) vertical content clipping — hidden/clip is unreachable (gate at
          // supported widths, report at phone-portrait); auto/scroll is reachable
          // (report-only always).
          for (const v of m.vclipHidden) {
            if (vp.overflowGate) {
              failures.push(`[${cellName}] vertical clipping: ${v.desc} hides ${v.hidden}px of its content (scrollHeight ${v.scrollH} > clientHeight ${v.clientH}, overflow-y hidden/clip) — unreachable`);
            } else {
              const key = 'clip|' + v.desc;
              const rec = vclipReport.get(key) || { kind: 'clip', desc: v.desc, hidden: v.hidden, cells: [] };
              if (rec.cells.length < 3) rec.cells.push(cellName);
              vclipReport.set(key, rec);
            }
          }
          for (const v of m.vclipScroll) {
            const key = 'scroll|' + v.desc;
            const rec = vclipReport.get(key) || { kind: 'scroll', desc: v.desc, hidden: v.hidden, cells: [] };
            if (rec.cells.length < 3) rec.cells.push(cellName);
            vclipReport.set(key, rec);
          }
          // (f) mobile KPI value centring — only present where the square-tile
          // grid is in force (vw ≤ 560). Non-empty guard: at a ≤560 dashboard
          // cell there MUST be KPI tiles, else the selector rotted.
          if (m.kpiCentres) {
            if (m.kpiCentres.length === 0) {
              failures.push(`[${cellName}] KPI centring: no ${KPI_TILE_VAL_SEL} tiles matched at ≤${KPI_CENTRE_MAX_W}px — the mobile square-tile grid or its value selector rotted`);
            } else {
              for (const k of m.kpiCentres) {
                if (Math.abs(k.off) > CENTRE_EPS) {
                  failures.push(`[${cellName}] KPI value not centred: ${k.desc} value «${k.value}» text centre off by ${k.off}px ` +
                    `(${k.off < 0 ? 'above' : 'below'} the tile centre; value centre ${k.valCentreY}, tile centre ${k.contentCentreY}, tile content height ${k.tileH}px) > ${CENTRE_EPS}px`);
                }
              }
            }
          }
          // (g) font floor — REPORT-ONLY (see the ledger note in the report
          // section). The non-empty guard still GATES: a text selector that
          // matches nothing is a rotted selector / unrendered surface, a real
          // regression. The sub-floor runs themselves are accumulated and
          // printed, not failed — they are the project's deliberate compact
          // admin scale, a scope/product decision to gate.
          if (m.textRunCount === 0) {
            failures.push(`[${cellName}] no text runs measured for the font floor — the surface rendered no visible text (selector rotted or unrendered)`);
          }
          for (const f of m.fontUnder) {
            if (f.matched.length) for (const i of f.matched) wlFontHits[i]++;
            const rec = subFont.get(f.desc) || { fontPx: f.fontPx, text: f.text, cells: [], known: f.matched.length > 0 };
            if (rec.cells.length < 3) rec.cells.push(cellName);
            subFont.set(f.desc, rec);
          }
        }
      }
    }
  }

  // ── Type + contrast pass (both themes, one desktop width) ─────────────────
  // Colour is theme-dependent, so run each surface in BOTH appearances; it is
  // NOT variant-dependent (the variant only hides widgets), so one variant per
  // surface is enough. Desktop width, non-emulated (dsf=1) so one image px == one
  // CSS px and no coordinate rounding. The background is SAMPLED FROM RENDERED
  // PIXELS (blank text, screenshot, median under each run's box), never derived
  // from tokens — see the header. Mirrors the cloud driver's section 4.
  const wlContrast = CONTRAST_WHITELIST.map((w) => w.match);
  const allContrast = [];   // every measured pair, for the ledger pass at the end
  {
    await page.setViewportSize({ width: 1440, height: 900 });
    for (const surface of SURFACES) {
      for (const theme of THEMES) {
        const cellName = `contrast__${surface.id}__${theme}`;
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.goto(`${BASE}/index.html`, { waitUntil: 'domcontentloaded' });
        await page.evaluate(pageSetup, {
          surface: surface.id, theme, variant: 'sa02m-2eth',
          states: {
            priority: DASHBOARD_PRIORITY, network: DASHBOARD_NETWORK,
            services: DASHBOARD_SERVICES, management: MANAGEMENT_SERVICES,
          },
        });
        await page.waitForTimeout(300);
        // Same paint-below-the-fold workaround as the geometry pass: grow the
        // viewport to the document height so every text run's real backdrop is
        // actually rendered in the blanked screenshot.
        const docH = await page.evaluate(() => Math.min(5000,
          Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)));
        if (docH > 900) { await page.setViewportSize({ width: 1440, height: docH }); await page.waitForTimeout(100); }
        const runs = await page.evaluate(probeContrastRuns, wlContrast);
        if (!runs.length) {
          failures.push(`[${cellName}] contrast: no text runs measured — the surface rendered no text (selector rotted or unrendered)`);
          continue;
        }
        const bgs = await sampleBackdrops(page, runs, 1);
        for (let i = 0; i < runs.length; i++) {
          const t = runs[i], bg = bgs[i], c = parseColor(t.color);
          if (!bg || !c) continue;
          const alpha = c.a * t.opacity;
          const fg = { r: c.r * alpha + bg.r * (1 - alpha),
                       g: c.g * alpha + bg.g * (1 - alpha),
                       b: c.b * alpha + bg.b * (1 - alpha) };
          allContrast.push({ ...t, cellName, theme, surface: surface.id, bg, ratio: contrastRatio(fg, bg) });
        }
      }
    }
  }
  // Contrast GATES on this project (B3, 1.0.5.39 "закрепить контраст"). A measured
  // pair below CONTRAST_MIN that is NOT matched by CONTRAST_WHITELIST FAILS the
  // run; whitelisted pairs are the accepted-debt ledger (deliberately muted or
  // disabled states, WCAG 1.4.3). The ledger is non-vacuous: an entry that excuses
  // nothing sub-AA is stale and FAILS, and a whitelisted pair deeper than its
  // recorded `floor` FAILS (the ratchet). Both passes run against allContrast,
  // built from BOTH themes above. The no-runs case is GATED inside the loop.
  {
    // (1) the gate: every sub-AA pair must be excused by a whitelist entry.
    const cUnder = allContrast.filter((m) => m.ratio < CONTRAST_MIN - CONTRAST_EPS);
    const unexcused = cUnder.filter((m) => !m.whitelist);
    const uSeen = new Set();
    for (const m of unexcused.sort((a, b) => a.ratio - b.ratio)) {
      const key = m.sel + '|' + m.text + '|' + m.theme;
      if (uSeen.has(key)) continue;
      uSeen.add(key);
      failures.push(`contrast below AA: ${m.ratio.toFixed(2)}:1 ${m.fontPx}px/${m.weight} ` +
        `${_hex(parseColor(m.color))} on ${_hex(m.bg)}  ${m.sel} «${m.text}» (${m.theme}) — ` +
        `fix the colour to ≥${CONTRAST_MIN}:1 or, if the state is deliberately muted/disabled, add it to CONTRAST_WHITELIST with a reason`);
    }
    // (2) the ledger: staleness AND the ratchet, mirroring the touch ledger.
    for (const w of CONTRAST_WHITELIST) {
      const mine = allContrast.filter((m) => m.whitelist === w.match);
      const under = mine.filter((m) => m.ratio < CONTRAST_MIN - CONTRAST_EPS);
      if (!under.length) {
        failures.push(`CONTRAST_WHITELIST entry excuses nothing sub-AA this run (stale): ${w.match} — ` +
          (mine.length
            ? `every matching pair now meets ${CONTRAST_MIN}:1, delete the entry (debt paid)`
            : `matched no measured text run on ANY surface, remove it or fix the selector`));
        continue;
      }
      const worst = under.reduce((a, b) => (a.ratio < b.ratio ? a : b));
      if (worst.ratio < w.floor - CONTRAST_EPS) {
        failures.push(`CONTRAST_WHITELIST entry '${w.match}' records floor ${w.floor.toFixed(2)}:1 ` +
          `but a matching pair now measures ${worst.ratio.toFixed(2)}:1 ` +
          `(${_hex(parseColor(worst.color))} on ${_hex(worst.bg)} «${worst.text}», ${worst.theme}) — ` +
          `debt has deepened; fix the regression or move the floor deliberately and say why`);
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

  // ── Report-only vertical clipping / scroll (deduped, one line per element) ─
  // CLIP = content hidden by overflow-y:hidden below the design floor (a real
  // finding, gated at supported widths); scroll = reachable via overflow-y:
  // auto/scroll (expected). Deduped like the touch/font ledgers.
  if (vclipReport.size) {
    console.log('\nvertical overflow (report-only — clipped below the design floor, or reachable by scrolling):');
    for (const rec of vclipReport.values()) {
      const what = rec.kind === 'clip'
        ? `CLIP hides ${rec.hidden}px (overflow-y hidden/clip)`
        : `scrolls ${rec.hidden}px (overflow-y auto/scroll — reachable)`;
      console.log(`  · ${rec.desc} — ${what}   e.g. ${rec.cells.join(', ')}`);
    }
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

  // ── Font floor — REPORT-ONLY ──────────────────────────────────────────────
  // SA-02m's web UI is a compact desktop/tablet-first embedded-browser admin
  // console at a 14px root, so a body of small text (0.72–0.78rem labels/hints,
  // 9px micro-controls) sits just under the 11px HIG floor by design — the same
  // accepted posture the 44px TOUCH_WHITELIST already documents. This is printed,
  // not gated: which of these to bump is a product decision, and promoting the
  // floor to a gate (add each family to FONT_WHITELIST with a reason, mirror the
  // touch ledger) is a deliberate step for the Operator to take, not the driver's.
  if (subFont.size) {
    console.log('\ntext below the ' + FONT_MIN + 'px HIG floor (report-only — compact-admin scale; not gated):');
    const rows = [...subFont.entries()].sort((a, b) => a[1].fontPx - b[1].fontPx);
    for (const [desc, rec] of rows) {
      console.log(`  ${rec.fontPx}px  ${rec.known ? '[known]' : '[     ]'}  ${desc}  «${rec.text}»   e.g. ${rec.cells.join(', ')}`);
    }
    console.log('  FONT_WHITELIST families annotated [known]:');
    FONT_WHITELIST.forEach((w, i) => console.log(`    [${wlFontHits[i]} hits] ${w.match} — ${w.reason}`));
  }

  // ── Contrast — GATED (B3, 1.0.5.39) ───────────────────────────────────────
  // The pixel-sampled AA measurement gates: an unexcused sub-AA pair is a FAILURE
  // above; a whitelisted pair is accepted debt (a deliberately muted/disabled
  // state). This block prints every sub-AA pair with its numbers and whether it
  // is [excused] by CONTRAST_WHITELIST or [FAIL] (unexcused) for the human eye —
  // the gate itself already fired above.
  const cUnder = allContrast.filter((m) => m.ratio < CONTRAST_MIN - CONTRAST_EPS)
    .sort((a, b) => a.ratio - b.ratio);
  console.log('\ncontrast AA (CONTRAST_MIN=' + CONTRAST_MIN + ':1, backdrop SAMPLED from rendered pixels) — GATED:');
  console.log('  ' + allContrast.length + ' text/background pairs measured across ' + SURFACES.length +
    ' surfaces × ' + THEMES.length + ' themes; ' + cUnder.length + ' below the AA floor ' +
    '(each must be fixed or whitelisted):');
  const cSeen = new Set();
  for (const m of cUnder) {
    const key = m.sel + '|' + m.text + '|' + m.theme;
    if (cSeen.has(key)) continue;
    cSeen.add(key);
    const tag = m.whitelist ? '[excused]' : '[ FAIL  ]';
    console.log(`  ${tag} ${m.ratio.toFixed(2)}:1  ${m.fontPx}px/${m.weight}  ${_hex(parseColor(m.color))} on ${_hex(m.bg)}  ${m.sel} «${m.text}» (${m.theme})`);
  }
  console.log('  CONTRAST_WHITELIST accepted-debt ledger:');
  CONTRAST_WHITELIST.forEach((w) => {
    const n = allContrast.filter((m) => m.whitelist === w.match && m.ratio < CONTRAST_MIN - CONTRAST_EPS).length;
    console.log(`    [${n} excused, floor ${w.floor.toFixed(2)}:1] ${w.match} — ${w.reason}`);
  });

  // ── Verdict ───────────────────────────────────────────────────────────────
  console.log(`\nscreenshots: ${shots.length} written to ${SHOTS_DIR}`);
  if (failures.length) {
    console.log(`\nui-layout: FAIL — ${failures.length} layout/type/contrast violation(s):`);
    for (const f of failures) console.log('  ✗ ' + f);
    process.exit(1);
  }
  console.log('\nui-layout: PASS — no violations across ' +
    `${SURFACES.length} surfaces × ${VIEWPORTS.length} viewports × ${THEMES.length} themes × ${VARIANTS.length} variants ` +
    `(geometry + KPI centring + vertical clipping + ${FONT_MIN}px font floor + ${CONTRAST_MIN}:1 contrast in both themes).`);
  process.exit(0);
}

run().catch((e) => { console.error(e); process.exit(10); });
