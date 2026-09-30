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

   COMMENT-BLINDNESS AUDIT (1.0.6.24): N/A. This driver asserts on measured
   geometry in a real browser, never on a needle grepped out of source, so
   there is no pin here a `#`/`//` in front of a line could satisfy.
   // comment-mutation-proof-exempt: geometry/contrast renderer, no source-line needle — measures rendered boxes; every collection check is non-vacuous (an empty selector FAILS).

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
     • (h) no INTRA-BOX horizontal clipping: a box whose `overflow-x:hidden|clip`
       hides content wider than itself (`scrollWidth > clientWidth + HCLIP_EPS`)
       — the class the (a) overflow test cannot see, because the clipped label
       sits INSIDE the viewport (right=360 < 393) and only its own card clamps
       it (1.0.6.53: «USB / microSD», «Перезапустить», «Включить клиент» cut off
       inside the ≤414px «Управление» tiles). The single-line ellipsis idiom
       (`text-overflow:ellipsis` + `white-space:nowrap`) is the one sanctioned
       clip and is skipped. GATED at the supported widths on both surfaces AND
       at phone-portrait on the management surface (the phone IS that tab's use
       case — F-B1, 1.0.6.53); REPORT-only at phone-portrait on the dashboard.
       Non-vacuous: a cell where no `overflow-x:hidden|clip` box is measured
       at all FAILS (the management card bodies are such boxes by design).
     • (i) management ONE COLUMN at ≤560px: every visible tile of
       `.system-manage-grid` spans the grid's full content width (± EPS). The
       defect: `.system-manage-services-card { grid-column: span 2 }` on a grid
       whose auto-fill template resolves to ONE track creates an IMPLICIT
       second column and every auto-placed tile flows into it — two ~60/35 %
       tiles per row on every phone (1.0.6.53). Non-vacuous floor: ≥ 5 tiles.
       RED/GREEN record: see the "RED, measured" note beside MANAGE_ONE_COL_MAX_W.
     • (j) «Устройства» card VALUES fit their tiles — its own surface, the
       devices tab rendered from a stubbed /api/devices (DEVICES_LIVE: every
       card kind with its realistic worst-case values — «780.5 mmHg»,
       «1234.5 кВт·ч», «1013.3 mbar», the Carel state «Остановлена») in RU AND
       EN, at its own width list (DEVICES_VIEWPORTS — incl. 1280, the reported
       width, and 1101, the tightest tile of the 3-card grid). Per value+unit
       run: the value is not truncated (scrollWidth ≤ clientWidth + 1), the
       row does not clip its content, and the value and unit boxes lie inside
       the tile's content box. The defect: the centred, overflow-hidden row cut
       «0.12 mg/m³» to «.12 mg/m» at 1280 px (1.0.6.64). Plus NO LAYOUT JUMP:
       every card is as tall with the live values as with «—» placeholders
       (fitting a value may shrink it or put its unit on the tile's spare line,
       never grow the tile). Plus each card HEADER: title on one line (an
       ellipsis is fine), rename pen on the title's line, no overlap among the
       painted title text, pen, badges, status pill and remove button. Plus, in
       EN cells, no tile label/value/unit/index/caption still Russian (the
       i18n floor; device names are data). GATED at every devices width, together with (h);
       (a)/(e) follow the viewport's overflowGate. Non-vacuous: every card kind
       rendered, ≥ DEV_ROWS_MIN runs measured AND populated (a value still «—»
       fits trivially), every card header measured, ≥ DEV_ROWS_MIN EN texts. The devices surface is geometry-only: touch, font and
       contrast are NOT measured there (honest limit — its controls and
       colours are outside this check).
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
   (overflow, touch, columns, cards, KPI centring, vertical clipping, intra-box
   horizontal clipping, management one-column, contrast)
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
   Skips (exit 77, run.mjs SKIP_EXIT — the runner prints SKIP, never PASS) when
   playwright / chromium is absent: a dev-box courtesy only. Linux CI installs the
   scripts/dev harness + chromium (web-quality.yml, since 1.0.6.49), so there the
   row RUNS; a skip is reported as a skip, never as a pass
   (.ai-dev/notes/quality-gate-environment.md). The row that used to sit
   beside this one under the same contract, `headless-smoke`, was RETIRED in
   1.0.6.24 for exactly that reason — it needed a live board too, so it skipped
   in every environment it was ever invoked in, CI included, while reporting
   PASS (2026-08-28 audit, C4). This row survives because a dev box CAN run it
   and it is the only automation over the rendered UI; the CI-runnable floors
   are carried by `html-id-contract` and `i18n-dict-contract`, which need no
   browser.
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
// Port 0 = an ephemeral port the OS picks, so a second run (or any other
// listener on a fixed port) cannot make this row RED. UI_LAYOUT_PORT pins one
// for a human who wants a stable URL; a bind failure is an INFRA error, exit 3,
// never a layout verdict.
const PORT = Number(process.env.UI_LAYOUT_PORT || 0);
let BASE = '';   // set from the bound port inside startServer()

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
const HCLIP_EPS = 2;    // px of HORIZONTAL surplus an overflow-x:hidden box may hide before it
                        // counts as clipping (h): main.css `.system-manage-grid .ctrl-card-main`
                        // documents a 1–2 px-wide child that its overflow-x:hidden exists to
                        // swallow (the ::-webkit-scrollbar thumb sliver), so 2 is the floor
                        // under which the clip is the design, not a defect.
// The «Управление» grid is ONE column at/below this width (main.css, the ≤560
// phone-portrait block — F-B2, 1.0.6.53), so the one-column assertion (i) is
// meaningful only there; above it the auto-fill template packs 2–3 tiles per row.
// RED, measured 2026-09-23 on the 1.0.6.52 CSS (before the ≤560 rule): 52
// failures, all four management phone-portrait cells (dark/light ×
// 1eth/2eth) — (i) 10 of 11 tiles at 230px or 88px in a 332px grid (tracks
// `230px 88px`; only the `span 2` Службы card filled the row) and (h) three
// card bodies (`.ctrl-card-main` of Действия / Яндекс Алиса / MPLC-проект,
// clientWidth 50) hiding 23–48px of their content. Both surfaces were clean
// under (h) at 800/768/1440; the dashboard at 360 printed one report-only hit
// (the Температура KPI tile hides 3px, pre-existing, below the floor). GREEN
// (0 failures) once main.css took `grid-template-columns: minmax(0, 1fr)` +
// Службы `1 / -1` at ≤560.
const MANAGE_ONE_COL_MAX_W = 560;
const MANAGE_TILE_MIN = 5;  // non-vacuity floor for (i) on the «Система» pane (6 tiles); the other panes set their own `tileMin` in SURFACES.
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

// «Устройства» widths (j). The tile width is set by the card grid (3 cards per
// row above 1100 px, 2 at 1025–1100, 1 below) and the metric grid inside a
// card (3 columns, 2 at ≤400), so the tight cells are the narrow end of each
// band: 1101 (3 cards) and 401 (3 metric columns on a phone). 1280 is the
// reported width; 1240 is the narrowest 4-column 12AI card (below it the
// @container rule in main.css drops it to 3 columns). The devices tab carries
// no data-hide-for, so one variant.
// RED, measured 2026-09-30 on a pristine copy of main 43be80d7: 680 failures,
// all on this surface — (j) 216 (e.g. at 1280 «12.34 mg/m³» — row
// scrollWidth 91 > clientWidth 82, value box starting 9 px left of the tile)
// at 1440/1280/1240/1101 in both themes and locales and 401 in RU; (h) 176 at
// the same cells; (e) 288: the overflow-hidden row also cut ~2 px of glyph ink
// at every width ≥ 768. GREEN (0) after the fit. Mutations, each RED on the
// fix: devices-kpi-fit.js not loaded → 120 (j) «value truncated» failures;
// the @container 12AI rule disabled → 8 «layout jump» failures at 1240 (a
// wrapped «1013.3 mbar» unit grows the MR card 267.4 → 302.1 px).
// Header + EN checks — RED on 62f9352c: 56 failures. 20 headers: at 1101 the
// rename pen of all four cards sits under the ONLINE pill (a stale
// `padding-right: 2rem` later in main.css overrode the 92 px reserve) and the
// СЭ pen drops to its own line; the same at 1240 for СЭ. 36 EN texts: «Энергия»
// and «кВт·ч» at every width and theme. GREEN (0) after. Mutations, each RED on
// the fix: title nowrap removed → «title wraps onto 2–3 lines» (28); title
// max-width removed → «rename pen is not on the title's line» (28); the
// 'Энергия' DICT entry removed → 18 «still Russian in EN».
const DEVICES_VIEWPORTS = [
  { name: 'desktop',        width: 1440, height: 900,  overflowGate: true },
  { name: 'laptop',         width: 1280, height: 800,  overflowGate: true },
  { name: 'mr12-4col-min',  width: 1240, height: 800,  overflowGate: true },
  { name: 'three-card-min', width: 1101, height: 800,  overflowGate: true },
  { name: 'tablet-land',    width: 1024, height: 768,  overflowGate: true },
  { name: 'tablet',         width: 768,  height: 1024, overflowGate: true },
  { name: 'phone-wide',     width: 500,  height: 900,  overflowGate: false },
  { name: 'phone-3col-min', width: 401,  height: 900,  overflowGate: false },
  { name: 'phone',          width: 375,  height: 812,  overflowGate: false },
];
const DEV_ROWS_MIN = 30;   // non-vacuity floor: DEVICES_LIVE renders 39 value runs.
const DEV_KINDS = ['carel', 'dtv', 'ce', 'mr'];

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
  { match: '#cloud-btn-ctrl', floor: 1.25,
    reason: '«Управление из облака» button (on the «Умный дом» card as #sh-btn-cloud from 1.0.6.26, moved onto the «Облако» card in 1.0.6.29) while it cannot act: the harness stubs the CGI with {} so the status payload carries no cloud_control block, and cloud.js cloudRenderControl sets btn.disabled=true (the same lock a not-cloud-enrolled board gets) → .btn:disabled opacity .4 (main.css) — WCAG 1.4.3 disabled-control exemption, the .kernel-apply-inline case. The ENABLED button is the standard btn-primary pair. floor 1.25 ratchets the measured worst (1.29 light / 3.08 dark).' },
  { match: '#homeconnect-btn-enable', floor: 1.25,
    reason: '«Home Connect» card «Включить» (added with the read-only BSH client, 1.0.6.57 branch) while the board gives no usable status answer: the harness stubs the CGI with {} (no ok:true), so app/homeconnect.js hcRenderUnavailable draws the button as a btn-primary «Включить» and hcSetButtonsDisabled sets btn.disabled=true (nothing is sent blind to a board that is not answering; the markup ships it disabled too) → .btn:disabled opacity .4 (main.css) — WCAG 1.4.3 disabled-control exemption, the #homekit-btn-enable / #cloud-btn-ctrl case. The ENABLED button is the standard btn-primary / btn-danger pair. floor 1.25 ratchets the measured worst (1.29 light / 3.08 dark, 2026-09-28).' },
  { match: '#homekit-btn-enable', floor: 1.25,
    reason: '«Apple HomeKit» card «Включить» (added 1.0.6.57) while the board gives no usable status answer: the harness stubs the CGI with {} (no ok:true), so app/homekit.js hkRenderUnavailable draws the button as a btn-primary «Включить» and hkSetButtonsDisabled sets btn.disabled=true (nothing is sent blind to a board that is not answering; the markup ships it disabled too) → .btn:disabled opacity .4 (main.css) — WCAG 1.4.3 disabled-control exemption, the #cloud-btn-ctrl case. The ENABLED button is the standard btn-primary / btn-danger pair. floor 1.25 ratchets the measured worst (1.29 light / 3.08 dark).' },
  { match: '.kernel-refresh-inline', floor: 2.0,
    reason: 'Kernel «Обновить загрузочное ядро» (added 1.0.5.59) when the running kernel artifact is not valid: renderKernelControl (app/services.js) sets btn.disabled=true → .btn:disabled opacity .4 (main.css). Same disabled-control case as the sibling .kernel-apply-inline above (WCAG 1.4.3 exemption); the ENABLED button is --text on --bg-panel ≈ 12:1. floor 2.0 ratchets the measured worst (2.46 light / 3.40 dark).' },
  { match: '#web-upd-apply-btn', floor: 1.7,
    reason: '«Обновление веб» → «Применить» while the last check found nothing to apply: since 1.0.6.37 (a7183c1) the button is rendered DISABLED instead of hidden — webUpdSetOnlineApplyEnabled (app/status.js) sets btn.disabled=true whenever webUpdResolveAvailable(_webUpdLastCheck) !== true, which is every render in this harness (the CGI stub carries no check payload) → .btn:disabled opacity .4 (main.css) — WCAG 1.4.3 disabled-control exemption, the .kernel-apply-inline case. The ENABLED pair is AA in both themes (#ffd60a on --warn-btn-bg #2a1e00 = 11.58:1 dark; #b45309 on #fff8e6 = 4.74:1 light). floor 1.7 ratchets the measured worst (1.78 light / 2.98 dark, audit 2026-09-08 C3).' },
  { match: '#agentapi-copy', floor: 2.0,
    reason: '«API для ИИ-агентов» card «Скопировать конфиг MCP» (added 1.0.7.1) before a token exists: the markup ships it disabled and app/agentapi.js enables it only after «Создать токен» returns a secret, which the {} stub never does → .btn:disabled opacity .4 (main.css) — WCAG 1.4.3 disabled-control exemption, the .kernel-refresh-inline case (same plain .btn). The ENABLED pair is AA in both themes (#f2f2f7 on #2c2c2e = 12.49:1 dark; #1c1c1e on #f2f2f7 = 15.25:1 light, measured 2026-10-07). floor 2.0 ratchets the measured worst (2.46 light / 3.40 dark).' },
  // NOTE: no #cloud-btn-activate entry — it lives inside a collapsed
  // <details id="cloud-token-fallback"> (installer-only token fallback) that
  // the harness never opens, so its disabled low-contrast run is never
  // measured. An exemption for an unmeasured element is vacuous; the non-vacuity
  // check correctly flagged it. Re-add ONLY if the harness is extended to open
  // that <details> and drive the button to its server-unreachable disabled state.
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
  // The «Устройства» tab (devices.js) polls the devices API, not a CGI: feed it
  // the worst-case card set (j), and an empty events journal.
  const path = url.split('?')[0];
  if (path === '/api/devices') return JSON.stringify(DEVICES_LIVE);
  if (path === '/api/devices/events') return JSON.stringify({ ok: true, events: [] });
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
    srv.on('error', (err) => {
      console.error(`ui-layout: INFRA ERROR — cannot bind 127.0.0.1:${PORT} (${err.code || err.message}) — not a layout result`);
      process.exit(3);
    });
    srv.listen(PORT, '127.0.0.1', () => {
      BASE = `http://127.0.0.1:${srv.address().port}`;
      ok(srv);
    });
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

// «Устройства» live payload (j): one card of every kind, each value at the
// longest a real device plausibly reports — sign, full precision, the longest
// units (mg/m³, mmHg, кВт·ч, mbar, «%, RH») and the Carel text state. The
// shape is the one devices.js reads (docs/contracts/devices-mr-history.md,
// carel-ahu.md); precision comes from its fmt()/aiUnitPrecision(). Four MR
// channels carry sensor codes whose card caption is Russian source text
// (34/38/40/41), so the EN cells exercise their translation too.
const DEVICES_LIVE = {
  ok: true,
  available: [],
  carel: [{ id: 'carel-COM3-1', kind: 'carel', label: 'Carel c.pCOmini № 1', ok: true, age_s: 2,
    plant_state_text: 'Остановлена', supply_temp: -12.3, return_water_temp: 34.2, setpoint: 21,
    heat_valve: 100, fan_supply: 100, alarm: 0 }],
  dtv: [{ id: 'dtv-COM2-3', kind: 'dtv', label: 'ДТВ № 3 порт 2', ok: true, age_s: 1,
    room_temp: -12.5, humidity: 100, eco2_ppm: 5000, tvoc_mg_m3: 12.34, pressure_mmhg: 780.5, light_pct: 100 }],
  ce: [{ id: 'ce02m3-COM2-14', kind: 'ce', label: 'СЭ-02м-3 № 14 порт 2', ok: true, age_s: 1,
    voltage: { a: 231.4, b: 229.8, c: 230.1 }, current: { a: 12.345, b: 10.2, c: 9.876 },
    power_w: { total: 12345 }, frequency_hz: 50.0, energy_kwh_import: 1234.5 }],
  mr: [
    { id: 'mr02m-COM4-12', kind: 'mr', label: 'MR-02m 12AI № 12', module_type: '12AI', ai_count: 12, ok: true, age_s: 1,
      channels: [
        [-50.5, '°C', 3], [10, 'V', 34], [20, 'mA', 40], [336.45, 'kΩ', 6], [99.9, '%, RH', 5], [1013.3, 'mbar', 1],
        [101.33, 'kPa', 1], [-40.1, '°F', 41], [2.345, 'kW', 1], [999.9, 'ppm', 1], [0.123, 'A', 38], [49.99, 'Hz', 1],
      ].map(([value, unit, code], i) => ({ ch: i + 1, key: `ai_${i + 1}`, enabled: true, value, unit, sensor_code: code })) },
    { id: 'mr02m-COM4-7', kind: 'mr', label: 'MR-02m 6AI6AO № 7', module_type: '6AI6AO', ai_count: 6, ok: true, age_s: 1,
      channels: [[-50.5, '°C'], [10, 'V'], [20, 'mA'], [336.45, 'kΩ'], [1013.3, 'mbar'], [101.33, 'kPa']]
        .map(([value, unit], i) => ({ ch: i + 1, key: `ai_${i + 1}`, enabled: true, value, unit, sensor_code: 1 })) },
  ],
};

// «Управление» is five sub-panes since the sub-nav split (Система / Облако /
// Умный дом / Службы / Обновление) and only the selected one is rendered, so
// each pane is its own surface. Measuring only the default «Система» pane left
// the other four under no test while the ledgers still excused their buttons
// (stale entries), and the services column check read the hidden rows of
// #svc-ctl-list — all at x=0, a vacuous pass. `tileMin` is the per-pane
// non-vacuity floor for the one-column check (i).
const SURFACES = [
  { id: 'dashboard', kind: 'dashboard', assertCards: true, assertColumns: false },
  { id: 'management', kind: 'management', sub: 'system', tileMin: 5, assertCards: false, assertColumns: false },
  { id: 'management-cloud', kind: 'management', sub: 'cloud', tileMin: 1, assertCards: false, assertColumns: false },
  { id: 'management-home', kind: 'management', sub: 'home', tileMin: 1, assertCards: false, assertColumns: false },
  { id: 'management-services', kind: 'management', sub: 'services', tileMin: 1, assertCards: false, assertColumns: true },
  { id: 'management-update', kind: 'management', sub: 'update', tileMin: 1, assertCards: false, assertColumns: false },
  // «Устройства» (j): geometry-only, its own width list, one variant (the tab
  // carries no data-hide-for), RU and EN. `viewports` / `variants` / `langs`
  // override the shared matrix; `geometryOnly` keeps a surface out of the
  // touch, font and contrast passes.
  { id: 'devices', kind: 'devices', assertCards: false, assertColumns: false, geometryOnly: true,
    viewports: DEVICES_VIEWPORTS, variants: ['sa02m-2eth'], langs: ['ru', 'en'] },
];

// ── In-page setup (runs in the browser) ─────────────────────────────────────
function pageSetup(arg) {
  const { surface, sub, theme, variant, states, lang } = arg;
  document.documentElement.setAttribute('data-theme', theme);
  // setLang persists to localStorage, so every cell names its language — an EN
  // devices cell must not leak into the next RU cell of any surface.
  if (window.sa02mI18n && window.sa02mI18n.lang !== lang) window.sa02mI18n.setLang(lang);
  if (window.applyVariantVisibility) window.applyVariantVisibility(variant);
  if (surface === 'dashboard') {
    if (window.switchTab) window.switchTab('dashboard');
    if (window.applyPriorityStatus) window.applyPriorityStatus(states.priority);
    if (window.applyNetworkStatus) window.applyNetworkStatus(states.network);
    if (window.applyServicesStatus) window.applyServicesStatus(states.services);
  } else if (surface === 'management') {
    if (window.switchTab) window.switchTab('system');
    if (sub && sub !== 'system' && window.systemSelectSub) window.systemSelectSub(sub);
    if (window.renderServicesControl) window.renderServicesControl(states.management);
  } else if (surface === 'devices') {
    // devices.js fetches the stubbed /api/devices itself on tab init.
    if (window.switchTab) window.switchTab('devices');
  }
}

// The shared-matrix axes a surface's cells take: variant × language.
function cellAxes(surface) {
  const out = [];
  for (const variant of (surface.variants || VARIANTS)) {
    for (const lang of (surface.langs || ['ru'])) out.push([variant, lang]);
  }
  return out;
}

// In-page: resolve after two animation frames — the devices value fit runs in
// the frame after the last text write (devices-kpi-fit.js), so two frames see it applied.
function settleFrames() {
  return new Promise((ok) => requestAnimationFrame(() => requestAnimationFrame(ok)));
}

// In-page: every «Устройства» card's rendered height, in DOM order.
function devCardHeights() {
  return Array.from(document.querySelectorAll('#dev-grid .dev-card'))
    .map((c) => ({ id: c.dataset.deviceId || '?', h: c.getBoundingClientRect().height }));
}
const rnd1 = (n) => Math.round(n * 10) / 10;

// ── In-page measurement (runs in the browser) ───────────────────────────────
function pageMeasure(arg) {
  const { surface, sub, TOUCH_MIN, EPS, CLIP_EPS, HCLIP_EPS, CENTRE_EPS, FONT_MIN,
          KPI_CENTRE_MAX_W, KPI_TILE_VAL_SEL, MANAGE_ONE_COL_MAX_W,
          wlSelectors, wlFont, lang } = arg;
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

  // (h) intra-box HORIZONTAL clipping — a box whose overflow-x is hidden|clip
  // and whose content is WIDER than itself (scrollWidth > clientWidth) hides the
  // surplus INSIDE the viewport. (a) only sees an element hanging past the
  // viewport edge, so a label clipped by its own card (the ≤414px «Управление»
  // tiles, 1.0.6.53) was invisible to it; (e) is the vertical twin. The single-
  // line ellipsis idiom (text-overflow:ellipsis + white-space:nowrap) is the one
  // sanctioned clip — the text is deliberately truncated — and is skipped.
  // `hclipBoxCount` is the non-vacuity denominator: zero measured boxes means
  // the selector rotted or the surface did not render, never a pass.
  const hclip = [];
  let hclipBoxCount = 0;
  document.querySelectorAll('body *').forEach((el) => {
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) return;
    const cs = getComputedStyle(el);
    const ox = cs.overflowX;
    if (ox !== 'hidden' && ox !== 'clip') return;
    hclipBoxCount++;
    if (el.clientWidth <= 0) return;
    if (el.scrollWidth <= el.clientWidth + HCLIP_EPS) return;
    if (cs.textOverflow === 'ellipsis' && cs.whiteSpace === 'nowrap') return;
    hclip.push({ desc: describe(el), scrollW: el.scrollWidth, clientW: el.clientWidth,
                 hidden: rnd(el.scrollWidth - el.clientWidth) });
  });

  // (i) management ONE COLUMN at ≤ MANAGE_ONE_COL_MAX_W — every visible tile of
  // .system-manage-grid is as wide as the grid's CONTENT box. Measured on the
  // rendered rect, never on grid-template-columns: the 1.0.6.53 defect was an
  // IMPLICIT column (a `span 2` item on a one-track auto-fill grid), which no
  // template string shows. `null` above the width (the check is not in force).
  let manageTiles = null;
  if (surface === 'management' && vw <= MANAGE_ONE_COL_MAX_W) {
    manageTiles = [];
    const grid = document.querySelector('#sys-pane-' + (sub || 'system') + ' .system-manage-grid');
    if (grid) {
      const gr = grid.getBoundingClientRect();
      const gcs = getComputedStyle(grid);
      const contentW = gr.width - parseFloat(gcs.paddingLeft) - parseFloat(gcs.paddingRight)
                     - parseFloat(gcs.borderLeftWidth) - parseFloat(gcs.borderRightWidth);
      grid.querySelectorAll(':scope > .system-manage-tile').forEach((t) => {
        const tr = t.getBoundingClientRect();
        if (!visible(t, tr)) return;
        manageTiles.push({ desc: describe(t), w: rnd(tr.width), gridW: rnd(contentW),
                           off: rnd(tr.width - contentW) });
      });
    }
  }

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
    // Visible rows only: a hidden pane's rows all report left=0 and would align
    // trivially (the vacuous pass this check used to give).
    const rows = [...document.querySelectorAll('.svc-ctl-row')]
      .filter((r) => visible(r, r.getBoundingClientRect()));
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

  // (j) «Устройства» value fit — every visible value+unit run of a device card:
  // the value not truncated, the row not clipping, value and unit boxes inside
  // the tile's CONTENT box. Measured on rendered boxes; `null` off the surface.
  let devFit = null;
  if (surface === 'devices') {
    devFit = { rows: 0, populated: 0, kinds: [], bad: [] };
    const kinds = new Set();
    document.querySelectorAll('#dev-grid .dev-card .dev-kpi-row').forEach((row) => {
      const rr = row.getBoundingClientRect();
      if (!visible(row, rr)) return;
      const val = row.querySelector('.dev-kpi-val');
      const kpi = row.closest('.dev-kpi');
      const card = row.closest('.dev-card');
      if (!val || !kpi || !card) return;
      devFit.rows++;
      kinds.add(card.dataset.kind);
      const v = (val.textContent || '').trim();
      if (v && v !== '—') devFit.populated++;
      const kr = kpi.getBoundingClientRect();
      const kcs = getComputedStyle(kpi);
      const cl = kr.left + parseFloat(kcs.borderLeftWidth) + parseFloat(kcs.paddingLeft);
      const cr = kr.right - parseFloat(kcs.borderRightWidth) - parseFloat(kcs.paddingRight);
      const why = [];
      if (val.scrollWidth > val.clientWidth + 1) {
        why.push(`value truncated (scrollWidth ${val.scrollWidth} > clientWidth ${val.clientWidth})`);
      }
      if (row.scrollWidth > row.clientWidth + 1) {
        why.push(`row clips its content (scrollWidth ${row.scrollWidth} > clientWidth ${row.clientWidth})`);
      }
      for (const el of [val, row.querySelector('.dev-kpi-unit')]) {
        if (!el) continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0) continue;
        if (r.left < cl - EPS || r.right > cr + EPS) {
          why.push(`.${el.className.split(' ')[0]} box ${rnd(r.left)}..${rnd(r.right)} outside the tile content ${rnd(cl)}..${rnd(cr)}`);
        }
      }
      if (why.length) {
        devFit.bad.push({ tile: `${card.dataset.kind}/${kpi.dataset.key || '?'}`,
                          text: (row.textContent || '').replace(/\s+/g, ' ').trim(), why });
      }
    });
    devFit.kinds = [...kinds];

    // (j2) card HEADER: the title is one line (an ellipsis is fine), the rename
    // pen sits on the title's line, and no two header pieces overlap — title
    // TEXT (Range, what is painted), pen, presence/alert badges, status pill,
    // remove button — nor leave the card's content box. The defect: at 1101 px
    // the СЭ title wrapped and ran under the absolutely placed ONLINE pill,
    // and the pen dropped to its own line (1.0.6.64).
    devFit.heads = 0;
    const lineTops = (rects) => [...new Set([...rects].filter((r) => r.width > 0.5)
      .map((r) => Math.round(r.top)))];
    document.querySelectorAll('#dev-grid .dev-card').forEach((card) => {
      const title = card.querySelector('.dev-card-title');
      const cr0 = card.getBoundingClientRect();
      if (!title || !visible(card, cr0)) return;
      devFit.heads++;
      const why = [];
      const rg = document.createRange();
      rg.selectNodeContents(title);
      const lines = lineTops(rg.getClientRects());
      if (lines.length > 1) why.push(`title wraps onto ${lines.length} lines`);
      // The painted text: the laid-out run, cut to the title's own box when the
      // title clips it (an ellipsized name keeps its full run in the Range).
      const tr0 = rg.getBoundingClientRect();
      const tb = title.getBoundingClientRect();
      const clips = /hidden|clip/.test(getComputedStyle(title).overflowX);
      const tr = { top: tr0.top, bottom: tr0.bottom,
                   left: clips ? Math.max(tr0.left, tb.left) : tr0.left,
                   right: clips ? Math.min(tr0.right, tb.right) : tr0.right };
      const parts = [['title text', tr]];
      for (const [name, sel] of [['rename pen', '.dev-card-rename'], ['presence badge', '[data-role="presence"]'],
        ['alert badge', '.dev-alert-badge'], ['status pill', '[data-role="status"]'], ['remove button', '.dev-card-remove']]) {
        const el = card.querySelector(sel);
        if (!el) continue;
        const r = el.getBoundingClientRect();
        if (visible(el, r)) parts.push([name, r]);
      }
      const pen = parts.find((p) => p[0] === 'rename pen');
      if (pen && !(pen[1].top < tr.bottom && pen[1].bottom > tr.top)) {
        why.push(`rename pen is not on the title's line (pen ${rnd(pen[1].top)}..${rnd(pen[1].bottom)}, title ${rnd(tr.top)}..${rnd(tr.bottom)})`);
      }
      for (let i = 0; i < parts.length; i++) {
        for (let j = i + 1; j < parts.length; j++) {
          const a = parts[i][1], b = parts[j][1];
          const ix = Math.min(a.right, b.right) - Math.max(a.left, b.left);
          const iy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
          if (ix > EPS && iy > EPS) why.push(`${parts[i][0]} overlaps ${parts[j][0]} by ${rnd(ix)}×${rnd(iy)}px`);
        }
      }
      const ccs = getComputedStyle(card);
      const cl = cr0.left + parseFloat(ccs.borderLeftWidth) + parseFloat(ccs.paddingLeft);
      const crr = cr0.right - parseFloat(ccs.borderRightWidth) - parseFloat(ccs.paddingRight);
      for (const [name, r] of parts) {
        if (r.left < cl - EPS || r.right > crr + EPS) why.push(`${name} ${rnd(r.left)}..${rnd(r.right)} leaves the card content ${rnd(cl)}..${rnd(crr)}`);
      }
      if (why.length) {
        devFit.bad.push({ check: 'header', tile: `${card.dataset.kind} header`,
                          text: (title.textContent || '').trim(), why });
      }
    });

    // (j3) EN cells: no Russian source text left in a tile's label, value, unit,
    // index or caption — every one of them has its DICT entry (the i18n floor,
    // web-code-rigor.md). Device NAMES are data and are not checked.
    devFit.enRuns = 0;
    if (lang === 'en') {
      document.querySelectorAll('#dev-grid .dev-kpi').forEach((kpi) => {
        const card = kpi.closest('.dev-card');
        kpi.querySelectorAll('.dev-kpi-lbl, .dev-kpi-val, .dev-kpi-unit, .dev-kpi-idx, .dev-kpi-sub').forEach((el) => {
          const t = (el.textContent || '').trim();
          if (!t) return;
          devFit.enRuns++;
          if (/[А-Яа-яЁё]/.test(t)) {
            devFit.bad.push({ check: 'i18n', tile: `${card ? card.dataset.kind : '?'}/${kpi.dataset.key || '?'}`, text: t,
                              why: [`.${el.className.split(' ')[0]} still Russian in EN — no DICT translation`] });
          }
        });
      });
    }
  }

  return { overflow, undersized, controlCount, svcColumns, cards,
           vclipHidden, vclipScroll, hclip, hclipBoxCount, manageTiles,
           kpiCentres, fontUnder, textRunCount, devFit };
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
    // The same phantom class as the clipped-away guard below, via a different
    // mechanism: a subtree the browser skips painting (content-visibility, the
    // UA hiding inside a CLOSED <details>) still reports computed style and a
    // laid-out Range box, so the backdrop sampled at those coordinates belongs
    // to whatever IS painted there — the token-fallback labels inside the
    // closed #cloud-token-fallback measured against the button that follows it
    // in flow (1.0.6.29). Ask the browser itself whether the run is rendered.
    if (typeof el.checkVisibility === 'function'
        && !el.checkVisibility({ contentVisibilityAuto: true, opacityProperty: false, visibilityProperty: true })) continue;
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
    process.exit(77); // run.mjs SKIP_EXIT: reported SKIP, never PASS
  }
  let browser;
  try {
    browser = await pw.chromium.launch();
  } catch (e) {
    const msg = String(e && e.message || e);
    if (/Executable doesn't exist|please run the following command|npx playwright install|download.*browser|Looks like Playwright/i.test(msg)) {
      console.log('ui-layout: skipped — chromium browser not installed (run: npm run ui-layout:install)');
      console.log('  ' + msg.split('\n')[0]);
      process.exit(77); // run.mjs SKIP_EXIT
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
  const hclipReport = new Map();        // desc → {hidden,cells} (report-only: dashboard phone-portrait)
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
    for (const vp of (surface.viewports || VIEWPORTS)) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      for (const theme of THEMES) {
        for (const [variant, lang] of cellAxes(surface)) {
          const cellName = `${surface.id}__${vp.name}__${theme}__${variant}` + (surface.langs ? `__${lang}` : '');
          // Re-assert the cell's nominal viewport: the post-load doc-height
          // growth below persists across cells, and vh-dependent layout must
          // start from the same base every time.
          await page.setViewportSize({ width: vp.width, height: vp.height });
          await page.goto(`${BASE}/index.html`, { waitUntil: 'domcontentloaded' });
          await page.evaluate(pageSetup, {
            surface: surface.kind, sub: surface.sub, theme, variant, lang,
            states: {
              priority: DASHBOARD_PRIORITY, network: DASHBOARD_NETWORK,
              services: DASHBOARD_SERVICES, management: MANAGEMENT_SERVICES,
            },
          });
          // Let the app's auto-fetch of services_ctrl.cgi resolve and re-paint
          // (fed the mix by the stub) before we screenshot/measure, so the
          // captured state is the stable one, not a mid-flight frame.
          await page.waitForTimeout(300);
          // «Устройства»: the cards arrive with the devices.js fetch; then let the
          // value-fit pass (one rAF after the last text write) settle. A cell
          // whose cards never render is caught by the (j) non-vacuity floor.
          if (surface.id === 'devices') {
            await page.waitForSelector('#dev-grid .dev-card[data-kind="mr"]', { timeout: 5000 }).catch(() => {});
            await page.evaluate(settleFrames);
          }

          // Grow the viewport to the full document height (capped) before the
          // shot: this Chromium's fullPage capture does not PAINT content below
          // the original viewport (the canvas grows, the pixels stay page-bg),
          // so a 100vh-app cell otherwise screenshots as one viewport of UI
          // over a blank tail. Width (the media-query axis) is untouched.
          const docH = await page.evaluate(() => Math.min(5000,
            Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)));
          if (docH > vp.height) {
            await page.setViewportSize({ width: vp.width, height: docH });
            if (surface.id === 'devices') await page.evaluate(settleFrames);
          }

          const shotPath = join(SHOTS_DIR, cellName + '.png');
          await page.screenshot({ path: shotPath, fullPage: true });
          shots.push(shotPath);

          const m = await page.evaluate(pageMeasure, {
            surface: surface.kind, sub: surface.sub, TOUCH_MIN, EPS, CLIP_EPS, HCLIP_EPS, CENTRE_EPS, FONT_MIN,
            KPI_CENTRE_MAX_W, KPI_TILE_VAL_SEL, MANAGE_ONE_COL_MAX_W, wlSelectors, wlFont, lang,
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
          if (!surface.geometryOnly && m.controlCount === 0) {
            failures.push(`[${cellName}] no interactive controls matched (button/a[href]/input/select/[onclick]) — selector rotted or the surface did not render`);
          }
          for (const u of (surface.geometryOnly ? [] : m.undersized)) {
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
          // (h) intra-box horizontal clipping — gated at the supported widths on
          // both surfaces AND at phone-portrait on the management surface (F-B1:
          // the phone is that tab's use case, and its 2-per-row tiles were the
          // reported defect); the dashboard at phone-portrait stays report-only
          // like its (a)/(e) siblings. Non-empty guard GATES everywhere: a cell
          // measuring no overflow-x:hidden|clip box at all is a rotted selector or
          // an unrendered surface, never a pass.
          const hclipGate = vp.overflowGate || surface.kind === 'management' || surface.kind === 'devices';
          if (m.hclipBoxCount === 0) {
            failures.push(`[${cellName}] no overflow-x:hidden|clip boxes measured for the intra-box clipping check — selector rotted or the surface did not render`);
          }
          for (const h of m.hclip) {
            const line = `[${cellName}] horizontal clipping inside a box: ${h.desc} hides ${h.hidden}px of its content (scrollWidth ${h.scrollW} > clientWidth ${h.clientW} + ${HCLIP_EPS}, overflow-x hidden/clip) — unreachable`;
            if (hclipGate) failures.push(line);
            else {
              const rec = hclipReport.get(h.desc) || { hidden: h.hidden, cells: [] };
              if (rec.cells.length < 3) rec.cells.push(cellName);
              hclipReport.set(h.desc, rec);
            }
          }
          // (i) management one column at ≤560 — present only where the rule is
          // in force (phone-portrait on the management surface). Non-empty
          // guard: fewer than MANAGE_TILE_MIN tiles means the grid or its tile
          // class rotted, not a one-column layout.
          if (m.manageTiles) {
            const tileMin = surface.tileMin || MANAGE_TILE_MIN;
            if (m.manageTiles.length < tileMin) {
              failures.push(`[${cellName}] management one-column: only ${m.manageTiles.length} .system-manage-grid > .system-manage-tile tiles measured (floor ${tileMin}) — the grid or its tile class rotted`);
            } else {
              for (const t of m.manageTiles) {
                if (Math.abs(t.off) > EPS) {
                  failures.push(`[${cellName}] management not one column at ≤${MANAGE_ONE_COL_MAX_W}px: ${t.desc} is ${t.w}px wide in a ${t.gridW}px grid (off by ${t.off}px > ${EPS}px)`);
                }
              }
            }
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
          if (!surface.geometryOnly && m.textRunCount === 0) {
            failures.push(`[${cellName}] no text runs measured for the font floor — the surface rendered no visible text (selector rotted or unrendered)`);
          }
          for (const f of (surface.geometryOnly ? [] : m.fontUnder)) {
            if (f.matched.length) for (const i of f.matched) wlFontHits[i]++;
            const rec = subFont.get(f.desc) || { fontPx: f.fontPx, text: f.text, cells: [], known: f.matched.length > 0 };
            if (rec.cells.length < 3) rec.cells.push(cellName);
            subFont.set(f.desc, rec);
          }
          // (j) «Устройства» value fit + no layout jump — GATED at every devices
          // width. Non-vacuity first: all four card kinds, and enough runs that
          // are measured AND populated (a «—» placeholder fits trivially).
          if (m.devFit) {
            const df = m.devFit;
            const missing = DEV_KINDS.filter((k) => !df.kinds.includes(k));
            if (missing.length || df.rows < DEV_ROWS_MIN || df.populated < DEV_ROWS_MIN) {
              failures.push(`[${cellName}] devices value fit: ${df.rows} value runs measured, ${df.populated} populated ` +
                `(floor ${DEV_ROWS_MIN}), card kinds [${df.kinds.join(', ')}]${missing.length ? ` — missing ${missing.join(', ')}` : ''} ` +
                `— the stubbed /api/devices did not render (selector rotted or fixture unread)`);
            }
            if (df.heads < DEV_KINDS.length) {
              failures.push(`[${cellName}] devices header check: only ${df.heads} card headers measured — the cards did not render`);
            }
            if (lang === 'en' && df.enRuns < DEV_ROWS_MIN) {
              failures.push(`[${cellName}] devices EN check: only ${df.enRuns} tile texts measured (floor ${DEV_ROWS_MIN}) — nothing to check`);
            }
            for (const b of df.bad) {
              const what = b.check === 'header' ? 'device card header broken' : b.check === 'i18n' ? 'device tile text untranslated' : 'device value does not fit its tile';
              failures.push(`[${cellName}] ${what}: ${b.tile} «${b.text}» — ${b.why.join('; ')}`);
            }
            // No layout jump: the same cards with «—» placeholders must be exactly
            // as tall — the fit may shrink a value or use the tile's spare line,
            // never grow a tile (a grown tile stretches its whole card row).
            const live = await page.evaluate(devCardHeights);
            await page.evaluate(() => {
              document.querySelectorAll('#dev-grid .dev-kpi-val').forEach((v) => { v.textContent = '—'; });
            });
            await page.evaluate(settleFrames);
            const idle = await page.evaluate(devCardHeights);
            if (live.length !== idle.length || live.length === 0) {
              failures.push(`[${cellName}] devices layout-jump check: ${live.length} cards live vs ${idle.length} with placeholders — nothing to compare`);
            } else {
              live.forEach((c, i) => {
                if (Math.abs(c.h - idle[i].h) > EPS) {
                  failures.push(`[${cellName}] devices layout jump: card ${c.id} is ${rnd1(c.h)}px tall with live values ` +
                    `but ${rnd1(idle[i].h)}px with «—» placeholders — a value grew its tile`);
                }
              });
            }
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
    for (const surface of SURFACES.filter((x) => !x.geometryOnly)) {
      for (const theme of THEMES) {
        const cellName = `contrast__${surface.id}__${theme}`;
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.goto(`${BASE}/index.html`, { waitUntil: 'domcontentloaded' });
        await page.evaluate(pageSetup, {
          surface: surface.kind, sub: surface.sub, theme, variant: 'sa02m-2eth', lang: 'ru',
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

  // ── Report-only intra-box horizontal clipping (dashboard phone-portrait) ──
  // The management surface and every supported width GATE this class above;
  // the dashboard at 360px stays a named report like its overflow/vclip siblings.
  if (hclipReport.size) {
    console.log('\nintra-box horizontal clipping (report-only — dashboard below the design floor; GATED elsewhere):');
    for (const [desc, rec] of hclipReport) {
      console.log(`  · ${desc} — hides ${rec.hidden}px (overflow-x hidden/clip)   e.g. ${rec.cells.join(', ')}`);
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
  console.log('  ' + allContrast.length + ' text/background pairs measured across ' + SURFACES.filter((x) => !x.geometryOnly).length +
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
    `${shots.length} cells (dashboard + management: ${VIEWPORTS.length} viewports × ${THEMES.length} themes × ${VARIANTS.length} variants; ` +
    `devices: ${DEVICES_VIEWPORTS.length} viewports × ${THEMES.length} themes × RU/EN) ` +
    `(geometry + KPI centring + vertical clipping + intra-box horizontal clipping + management one column ≤${MANAGE_ONE_COL_MAX_W}px + devices value fit / no layout jump + ${FONT_MIN}px font floor + ${CONTRAST_MIN}:1 contrast in both themes).`);
  process.exit(0);
}

run().catch((e) => { console.error(e); process.exit(10); });
