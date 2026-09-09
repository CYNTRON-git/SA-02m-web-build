#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it evaluates the shipped flasher/led.js and the flasher.js seam functions and asserts their return values / DOM effects, pinning no source line by text; commenting a line out inside the code under test changes the result it measures, which is exactly what the RED/GREEN ratchet in the header records.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-led-window — standalone Node test for the LED strip (RGBW_WS2812,
   type 120) config window: static/js/flasher/led.js + its seam in flasher.js
   (release 1.0.6.40, plan led-window-1.0.6.40 §5).
   ───────────────────────────────────────────────────────────────────────────
   What it pins, and why each pin exists:

   A) Kind dispatch. `deviceConfigKindFromSignature` answers 'led' for every
      token of the shared map's LED_SIGNATURE_ALIASES (read from
      led_mb2ws_map.py, not re-typed) and for a PREFIX of the two long ones only
      — «LED» as a 3-char prefix would steal the Wiren Board signature «ledGe»
      (the lesson recorded in that file). Carel / MR / DTV / CE are unchanged.

   B) Tabs. info · network · the four led_* ids, which must EQUAL
      led_poll.LED_TABS (parsed from the Python source): the daemon gates the
      expensive reads on exactly those names in active_tab. No EXFX tab (plan
      §9 F2). The scene tab carries the play state as its suffix; a `panel`
      poll of the scene tab sends an EMPTY active_tab (base block only, F6).

   C) Action parity. The JS action list equals device_config.LED_ACTIONS
      (parsed), every `ledCommand('…')` literal is in it, and each parameter
      collector emits exactly the field names of the contract (led-mb2ws.md
      §3) — a stray key is refused at the far end of a leased port session.
      ledCommand itself: one POST to /device_config/led_write, the poll
      generation bumped, the reply applied, an error to banner + toast.

   D) Markup ⇄ patch, and hide-don't-grey. Every tab renders the ids the live
      patch writes (values land after a patch over a stale mount), and a
      control the firmware ignores for the device's effect is NOT rendered:
      cards by scene.visibility, PWM channels by mode_visible, the channel-2
      block by the line mode, Scale never. A silent strip or an old daemon
      renders NO input at all.

   E) Live patch. A focused or edited (dataset.ledDirty) field is never
      overwritten by the 1 s poll; telemetry and the play badge are; a
      sentinel renders blank, never 0; only a changed skeleton rebuilds
      (renderKey).

   F) Merge. A reply without the expensive blocks keeps the previous ones; a
      reply that carried them wins; an old daemon is named once.

   G) i18n both ways. Every Cyrillic label the window can show — LED_T values,
      the authored literals, every text node the renderers emit over the
      fixtures, the tab labels — has a DICT row in i18n.js (the half
      i18n-dict-contract cannot see: it sweeps uiT() and markup, and this file
      uses neither). The other way: every "rgbw_…" key the daemon can emit
      (led_mb2ws_map.py, led_mb2ws.py, led_poll.py; the %02d/%d patterns
      expanded) is a LED_T key — a key with no Russian label would show raw.

   RED/GREEN ratchet — observed on 2026-09-08 (the lines are recorded in the
   plan's progress note and the commit body):
     * the whole seam absent (no flasher/led.js, no 'led' branch): every
       section fails;
     * deleting `if (signatureLooksLikeLed(n)) return 'led';` → A fails;
     * renaming `fx_param` in ledSceneParams → C fails;
     * making renderSceneTab ignore visibility.clock → D fails (clock card
       rendered for effect 0);
     * making fieldHolds return false → E fails;
     * dropping one DICT row / one LED_T key → G fails.

   Idiom: flasher.js functions are brace-extracted from the SHIPPED source
   (test-carel-window.mjs precedent); led.js is loaded whole with
   `new Function('window', src)` and driven through `create(host)` against a
   Map-backed DOM that a tiny HTML mounter fills from the rendered markup.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const JS_DIR = join(ROOT, 'www', 'network_config', 'static', 'js');
const LED_JS = join(JS_DIR, 'flasher', 'led.js');
const FLASHER_JS = join(JS_DIR, 'flasher.js');
const I18N_JS = join(JS_DIR, 'i18n.js');
const DEVICE_CONFIG_PY = join(ROOT, 'opt', 'sa02m-flasher', 'sa02m_flasher', 'device_config.py');
const LED_POLL_PY = join(ROOT, 'opt', 'sa02m-flasher', 'sa02m_flasher', 'led_poll.py');
const LED_MAP_PY = join(ROOT, 'opt', 'sa02m-led', 'sa02m_led', 'led_mb2ws_map.py');
const LED_MB2WS_PY = join(ROOT, 'opt', 'sa02m-led', 'sa02m_led', 'led_mb2ws.py');

let failures = 0;
function check(cond, msg) {
  if (cond) console.log('  ok   - ' + msg);
  else { failures++; console.error('  FAIL - ' + msg); }
}
function eq(got, want, msg) { check(got === want, msg + ' (got ' + JSON.stringify(got) + ')'); }
function read(path) {
  try { return readFileSync(path, 'utf8'); } catch (err) { failures++; console.error('  FAIL - cannot read ' + path + ': ' + err.message); return ''; }
}

const ledSrc = read(LED_JS);
const flasherSrc = read(FLASHER_JS);
const i18nSrc = read(I18N_JS);
const deviceConfigSrc = read(DEVICE_CONFIG_PY);
const ledPollSrc = read(LED_POLL_PY);
const ledMapSrc = read(LED_MAP_PY);
const ledMb2wsSrc = read(LED_MB2WS_PY);
if (!ledSrc || !flasherSrc || !i18nSrc || !deviceConfigSrc || !ledPollSrc || !ledMapSrc || !ledMb2wsSrc) {
  console.error('js-unit-led-window: a source under test is missing — nothing to measure');
  process.exit(1);
}

/* ── extractors (exact-name brace matching) ───────────────────────────────── */

function extractFn(src, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const m = re.exec(src);
  if (!m) throw new Error('function ' + name + '() not found');
  const start = m.index;
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name + '()');
}

function extractConst(src, name) {
  const re = new RegExp('const\\s+' + name + '\\s*=');
  const m = re.exec(src);
  if (!m) throw new Error('const ' + name + ' not found');
  const start = m.index;
  const eq0 = src.indexOf('=', start);
  let openIdx = eq0 + 1;
  while (/\s/.test(src[openIdx])) openIdx++;
  const openCh = src[openIdx];
  if (openCh === "'" || openCh === '"') return src.slice(start, src.indexOf(';', openIdx) + 1);
  const closeCh = openCh === '[' ? ']' : openCh === '{' ? '}' : null;
  if (!closeCh) throw new Error('const ' + name + ' is not an array/object/string literal');
  let depth = 0;
  for (let j = openIdx; j < src.length; j++) {
    if (src[j] === openCh) depth++;
    else if (src[j] === closeCh && --depth === 0) {
      const semi = src.indexOf(';', j);
      return src.slice(start, (semi >= 0 ? semi : j) + 1) + (semi >= 0 ? '' : ';');
    }
  }
  throw new Error('unbalanced brackets extracting const ' + name);
}

/* ── the Python homes, parsed (never re-typed) ────────────────────────────── */

function pyTuple(src, name) {
  const m = new RegExp(name + '(?::\\s*Tuple\\[str,\\s*\\.\\.\\.\\])?\\s*=\\s*\\(([\\s\\S]*?)\\)').exec(src);
  if (!m) return null;
  return m[1].split(',').map(s => s.trim().replace(/^"|"$/g, '')).filter(Boolean);
}
const PY_ALIASES = pyTuple(ledMapSrc, 'LED_SIGNATURE_ALIASES');
const PY_PREFIXES = pyTuple(ledMapSrc, 'LED_SIGNATURE_PREFIXES');
const PY_ACTIONS = pyTuple(deviceConfigSrc, 'LED_ACTIONS');
const PY_TAB_CONSTS = {};
for (const m of ledPollSrc.matchAll(/^(LED_TAB_[A-Z]+)\s*=\s*"([^"]+)"/gm)) PY_TAB_CONSTS[m[1]] = m[2];
const pyTabsTuple = /LED_TABS\s*=\s*\(([^)]*)\)/.exec(ledPollSrc);
const PY_TABS = pyTabsTuple ? pyTabsTuple[1].split(',').map(s => s.trim()).filter(Boolean).map(n => PY_TAB_CONSTS[n]) : null;
check(Array.isArray(PY_ALIASES) && PY_ALIASES.length === 4, 'led_mb2ws_map.py declares the 4 signature aliases');
check(Array.isArray(PY_PREFIXES) && PY_PREFIXES.length === 2, 'led_mb2ws_map.py declares the 2 prefix aliases');
check(Array.isArray(PY_ACTIONS) && PY_ACTIONS.length === 12, `device_config.py declares 12 LED actions (got ${PY_ACTIONS && PY_ACTIONS.length})`);
check(Array.isArray(PY_TABS) && PY_TABS.length === 4 && PY_TABS.every(Boolean), 'led_poll.py declares LED_TABS (4 ids)');

/* ── minimal DOM ──────────────────────────────────────────────────────────── */

class El {
  constructor(tag, opts) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.textContent = '';
    this.className = '';
    this.innerHTML = null;
    this.children = [];
    this.dataset = {};
    this.value = '';
    this.checked = false;
    this.hidden = false;
    this.disabled = false;
    Object.assign(this, opts || {});
  }
}
const DOM = new Map();
let activeEl = null;

const unescapeHtml = s => String(s).replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const attr = (attrs, name) => { const m = new RegExp('\\s' + name + '="([^"]*)"').exec(attrs); return m ? unescapeHtml(m[1]) : null; };
const hasFlag = (attrs, name) => new RegExp('\\s' + name + '(?=[\\s/>]|$)').test(attrs);

/** Fill DOM from rendered markup: every id-carrying tag becomes an El with its
    type / value / checked / hidden; a select takes its selected option's value
    (or the first); text-bearing tags take their inner text. */
function mount(html) {
  DOM.clear();
  activeEl = null;
  const tagRe = /<(\w+)\b([^>]*)>/g;
  let m;
  while ((m = tagRe.exec(html))) {
    const tag = m[1].toLowerCase();
    const attrs = ' ' + m[2];
    const id = attr(attrs, 'id');
    if (!id) continue;
    const e = new El(tag);
    e.id = id;
    e.type = attr(attrs, 'type') || (tag === 'select' ? 'select-one' : tag === 'input' ? 'text' : '');
    e.hidden = hasFlag(attrs, 'hidden');
    e.className = attr(attrs, 'class') || '';
    if (tag === 'select') {
      const close = html.indexOf('</select>', m.index);
      const inner = html.slice(m.index, close);
      const opts = [...inner.matchAll(/<option\b([^>]*)>/g)].map(o => ({ value: attr(' ' + o[1], 'value'), selected: hasFlag(' ' + o[1], 'selected') }));
      const sel = opts.find(o => o.selected) || opts[0];
      e.value = sel ? sel.value : '';
      e.options = opts;
    } else if (tag === 'input') {
      e.value = attr(attrs, 'value') == null ? '' : attr(attrs, 'value');
      e.checked = hasFlag(attrs, 'checked');
    } else {
      const close = html.indexOf('</' + tag + '>', m.index);
      const inner = html.slice(m.index + m[0].length, close < 0 ? m.index + m[0].length : close);
      e.textContent = inner.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
      e.children = [...inner.matchAll(/<div>([\s\S]*?)<\/div>/g)].map(d => new El('div', { textContent: unescapeHtml(d[1]) }));
    }
    DOM.set(id, e);
  }
  return DOM;
}
function idsIn(html) {
  const out = new Set();
  for (const m of String(html).matchAll(/\sid="([^"]+)"/g)) out.add(m[1]);
  return out;
}

/* ── load flasher/led.js and the flasher.js seam ──────────────────────────── */

const windowStub = { sa02mI18n: { lang: 'ru', t: s => s } };
new Function('window', ledSrc)(windowStub);
const LW = windowStub.sa02mLedWindow;
check(!!LW && typeof LW.create === 'function', 'flasher/led.js exposes window.sa02mLedWindow.create()');
if (!LW) process.exit(1);

const escapeHtml = new Function(extractFn(flasherSrc, 'escapeHtml') + '; return escapeHtml;')();
function clampInt(value, min, max, fallback) {
  const parsed = parseInt(value, 10);
  const num = Number.isFinite(parsed) ? parsed : fallback;
  return Math.max(min, Math.min(max, num));
}

const stateStub = { configTab: 'led_pwm', configBodyKey: '', configSnapshot: null, configBusy: false };
const calls = { api: [], toasts: [], banners: [], applied: [], invalidated: 0, refreshed: 0 };
let apiReply = null;
let apiThrows = null;
const requestedIds = new Set();
const host = {
  escapeHtml,
  t: s => s,
  toast: (m, kind) => calls.toasts.push([m, kind]),
  configModalEl: id => { requestedIds.add(id); return DOM.get(id) || null; },
  configApi: async (path, body) => { calls.api.push([path, body]); if (apiThrows) throw apiThrows; return apiReply; },
  getState: () => stateStub,
  applyConfigSnapshot: (s) => calls.applied.push(s),
  setConfigBusy: b => { stateStub.configBusy = !!b; },
  setConfigBanner: (t, kind) => calls.banners.push([t, kind]),
  clampInt,
  currentConfigDevice: () => ({ address: 13, signature: 'RGBW_WS2812' }),
  currentPort: () => 'COM3',
  invalidatePolls: () => { calls.invalidated++; },
  activeElement: () => activeEl,
  refreshSnapshot: () => { calls.refreshed++; },
};
const api = LW.create(host);

const seamParts = () => [
  extractConst(flasherSrc, 'WB_RELAY_SIG_PREFIXES'),
  extractConst(flasherSrc, 'WB_MAO4_SIG_PREFIXES'),
  extractConst(flasherSrc, 'CAREL_SIGNATURE_TOKENS'),
  extractConst(flasherSrc, 'CAREL_FAMILY_CRST'),
  extractConst(flasherSrc, 'CAREL_FAMILY_UARIA'),
  extractConst(flasherSrc, 'CAREL_IO_TAB'),
  extractConst(flasherSrc, 'CAREL_IO_KEYS'),
  extractConst(flasherSrc, 'LED_SIGNATURE_ALIASES'),
  extractConst(flasherSrc, 'LED_SIGNATURE_PREFIXES'),
  extractFn(flasherSrc, 'stripBootloaderSignatureSuffix'),
  extractFn(flasherSrc, 'isMpModuleSignatureForFirmwareHint'),
  extractFn(flasherSrc, 'signatureLooksLikeCarel'),
  extractFn(flasherSrc, 'signatureLooksLikeLed'),
  extractFn(flasherSrc, 'deviceConfigKindFromSignature'),
  extractFn(flasherSrc, 'isDeviceConfigSupported'),
  extractFn(flasherSrc, 'deviceConfigTitle'),
  extractFn(flasherSrc, 'carelFamilyFromSignature'),
  extractFn(flasherSrc, 'carelFamily'),
  extractFn(flasherSrc, 'carelBlock'),
  extractFn(flasherSrc, 'configTabsForSnapshot'),
  extractFn(flasherSrc, 'configBodyRenderKey'),
  extractFn(flasherSrc, 'configBodyIsPatchable'),
  extractFn(flasherSrc, 'configPollActiveTab'),
  'function ledWindow() { return ledApi; }',
  `return { LED_SIGNATURE_ALIASES, LED_SIGNATURE_PREFIXES, deviceConfigKindFromSignature,
     isDeviceConfigSupported, deviceConfigTitle, configTabsForSnapshot, configBodyRenderKey,
     configBodyIsPatchable, configPollActiveTab };`,
];
let seam = null;
try {
  seam = new Function('window', 'state', 'configModalEl', 'ledApi', seamParts().join('\n'))(windowStub, stateStub, host.configModalEl, api);
} catch (err) {
  failures++;
  console.error('  FAIL - flasher.js seam cannot be extracted: ' + err.message);
}

/* ── fixtures (Latin device text on purpose — every Cyrillic in the output is authored) ── */

const CHOICES = {
  led_types: [[0, 'WS2812'], [1, 'WS2812 (timing 1)'], [2, 'SM16703'], [3, 'WS2811'], [5, 'WS2815'], [6, 'SK6812 RGBW'], [7, 'APA102 / SK9822']],
  led_type_apa102: 7,
  byte_orders: [[0, 'RGB'], [1, 'RBG'], [2, 'GRB'], [3, 'GBR'], [4, 'BRG'], [5, 'BGR'], [6, 'RGBW'], [7, 'RBGW'], [8, 'GRBW'], [9, 'GBRW'], [10, 'BRGW'], [11, 'BGRW']],
  pwm_modes: [[0, 'rgbw_pwm_mode_rgbw'], [1, 'rgbw_pwm_mode_wwww'], [2, 'rgbw_pwm_mode_cct_cct'], [3, 'rgbw_pwm_mode_cct_ww'], [4, 'rgbw_pwm_mode_2w_2w'], [5, 'rgbw_pwm_mode_4w'], [6, 'rgbw_pwm_mode_2w_ww'], [7, 'rgbw_pwm_mode_2w_cct'], [8, 'rgbw_pwm_mode_cct_2w'], [9, 'rgbw_pwm_mode_ww_cct'], [10, 'rgbw_pwm_mode_ww_2w'], [11, 'rgbw_pwm_mode_2cct']],
  line_ui: [['single_ws', 'rgbw_line_single'], ['dual_ws', 'rgbw_line_dual'], ['apa102', 'rgbw_line_apa102']],
  scene_ui: [['pool', 'rgbw_scene_pool'], ['fx', 'rgbw_scene_fx'], ['flash', 'rgbw_scene_flash']],
  tile_modes: [[0, 'rgbw_tile_mode_span'], [1, 'rgbw_tile_mode_replicate'], [2, 'rgbw_tile_mode_mirror']],
  tile_counts: [[0, 'rgbw_tile_count_auto'], [1, 'rgbw_tile_count_1'], [2, 'rgbw_tile_count_2'], [3, 'rgbw_tile_count_3'], [4, 'rgbw_tile_count_4']],
  matrix_types: [['rgbw_mx_8x8', 8, 8], ['rgbw_mx_8x16', 8, 16], ['rgbw_mx_16x8', 16, 8], ['rgbw_mx_16x16', 16, 16], ['rgbw_mx_8x32', 8, 32], ['rgbw_mx_32x8', 32, 8], ['rgbw_mx_16x32', 16, 32], ['rgbw_mx_32x16', 32, 16], ['rgbw_mx_32x32', 32, 32], ['rgbw_mx_64x16', 64, 16]],
  ch2_modes: [[0, 'rgbw_ch2_off'], [1, 'rgbw_ch2_sync'], [2, 'rgbw_ch2_independent'], [3, 'rgbw_ch2_mirror'], [4, 'rgbw_ch2_continuation']],
  text_lines: [[1, 'rgbw_text_lines_single'], [2, 'rgbw_text_lines_double']],
  wx_lines: [[1, 'rgbw_wx_lines_single'], [2, 'rgbw_wx_lines_double']],
  text_max_chars: 128,
  text_2x_hint_args: [13, 64, 5],
  aux_colors: [[0, 'rgbw_fx_aux_color_default'], [1, 'rgbw_fx_aux_color_red'], [2, 'rgbw_fx_aux_color_green'], [3, 'rgbw_fx_aux_color_blue'], [4, 'rgbw_fx_aux_color_white'], [5, 'rgbw_fx_aux_color_yellow'], [6, 'rgbw_fx_aux_color_orange']],
  spy_ports: [[0, 'rgbw_spy_port_off'], [1, 'rgbw_spy_port_a'], [2, 'rgbw_spy_port_b']],
  spy_modes: [[0, 'rgbw_spy_mode_off'], [1, 'rgbw_spy_mode_spy'], [2, 'rgbw_spy_mode_master']],
  spy_parity: [[0, 'rgbw_spy_parity_none'], [1, 'rgbw_spy_parity_odd'], [2, 'rgbw_spy_parity_even']],
  spy_baud: [[12, '1200'], [96, '9600'], [192, '19200'], [1152, '115200']],
  spy_fc: [[3, 'rgbw_spy_fc_03'], [4, 'rgbw_spy_fc_04'], [6, 'rgbw_spy_fc_06'], [16, 'rgbw_spy_fc_16']],
  spy_types: [[0, 'rgbw_spy_type_int16'], [1, 'rgbw_spy_type_uint16'], [2, 'rgbw_spy_type_int32_ab'], [3, 'rgbw_spy_type_uint32_ab'], [4, 'rgbw_spy_type_int32_cdab'], [5, 'rgbw_spy_type_uint32_cdab'], [6, 'rgbw_spy_type_float_ab'], [7, 'rgbw_spy_type_float_cdab'], [8, 'rgbw_spy_type_float_badc'], [9, 'rgbw_spy_type_float_dcba'], [10, 'rgbw_spy_type_time_hh'], [11, 'rgbw_spy_type_time_mm'], [12, 'rgbw_spy_type_time_ss']],
  spy_units: ['', '°C', '%', 'V', 'A', 'W', 'Hz', 'bar'],
  wx_spy_fields: ['rgbw_wx_spy_hh', 'rgbw_wx_spy_mm', 'rgbw_wx_spy_ss', 'rgbw_wx_spy_date', 'rgbw_wx_spy_temp', 'rgbw_wx_spy_hum', 'rgbw_wx_spy_press'],
  di_modes: [[0, 'rgbw_di_mode_btn'], [1, 'rgbw_di_mode_sw']],
  pixel_formats: [[0, 'rgbw_strip_format_rgb'], [1, 'rgbw_strip_format_rgbw']],
  fx_count: 82, pwm_permille_max: 1000, led_count0_max: 1024, led_count1_max: 512,
};
const FX_GROUPS = [
  ['base', 'rgbw_fx_grp_base', 0, 2], ['color', 'rgbw_fx_grp_color', 3, 12], ['water', 'rgbw_fx_grp_water', 13, 25],
  ['fire', 'rgbw_fx_grp_fire', 26, 29], ['sky', 'rgbw_fx_grp_sky', 30, 36], ['phys', 'rgbw_fx_grp_phys', 37, 54],
  ['nature', 'rgbw_fx_grp_nature', 55, 61], ['text', 'rgbw_fx_grp_text', 62, 65], ['sign', 'rgbw_fx_grp_sign', 66, 75],
  ['special', 'rgbw_fx_grp_special', 76, 81],
].map(g => ({ id: g[0], key: g[1], first: g[2], last: g[3] }));
const groupOf = id => (FX_GROUPS.find(g => id >= g.first && id <= g.last) || FX_GROUPS[0]).id;
const VIS_NONE = { fx_aux_low: false, fx_aux_flag: false, fx_aux_color: false, fx_aux_pool_len: false, fx_density: false, text_window: false, text_lines: false, text_colors: false, clock: false, weather: false, mb_spy: false, wx_temp_color: false, scale: false };
const VIS = {
  0: {},
  3: { fx_density: true },
  51: { fx_aux_low: true, fx_aux_flag: true, fx_aux_pool_len: true },
  62: { fx_aux_low: true, fx_aux_color: true, text_window: true, text_lines: true, text_colors: true },
  63: { fx_aux_color: true, clock: true },
  64: { fx_aux_color: true, clock: true, weather: true, text_colors: true, wx_temp_color: true },
  81: { mb_spy: true, text_colors: true },
};
const STYLE_CHOICES = Array.from({ length: 18 }, (_x, i) => [i, 'rgbw_fx_aux_style_' + String(i).padStart(2, '0')]);
const ESCORT_CHOICES = [[0, 'rgbw_fx_aux_color_warm_white'], [1, 'rgbw_fx_aux_color_red'], [2, 'rgbw_fx_aux_color_green'], [3, 'rgbw_fx_aux_color_blue'], [4, 'rgbw_fx_aux_color_white'], [5, 'rgbw_fx_aux_color_yellow'], [6, 'rgbw_fx_aux_color_orange']];
function auxSpec(fx) {
  const color = { high_kind: 'color', high_label_key: 'rgbw_scene_fx_aux_color', high_min: 0, high_max: 6 };
  const unused = { high_kind: 'unused', high_label_key: 'rgbw_scene_fx_aux_color', high_min: 0, high_max: 6 };
  if (fx === 62) return Object.assign({ low_kind: 'style', low_label_key: 'rgbw_scene_fx_aux_style', low_choices: STYLE_CHOICES, low_min: 0, low_max: 17, flag_label_key: '' }, color);
  if (fx === 51) return { low_kind: 'escort', low_label_key: 'rgbw_scene_fx_aux_escort_color', low_choices: ESCORT_CHOICES, low_min: 0, low_max: 6, flag_label_key: 'rgbw_scene_fx_aux_reverse', high_kind: 'length', high_label_key: 'rgbw_scene_fx_aux_pool_len', high_min: 0, high_max: 255 };
  const base = { low_kind: 'variant', low_label_key: 'rgbw_scene_fx_aux_variant', low_choices: [], low_min: 0, low_max: 255, flag_label_key: '' };
  return Object.assign(base, [13, 18, 40, 44, 62, 63, 64, 66, 67, 75].indexOf(fx) >= 0 ? color : unused);
}
const MODE_DATA = {
  tod_hours: 7, tod_minutes: 30, fx_aux: 0x0102, fx_density: 128, wx_day: 4, wx_month: 9, wx_temp_c: -11.5,
  wx_hum_pct: 61.5, wx_press_mmhg: 748.0, wx_year: 2026, text_colors: [63488, 0, 0, 0], wx_temp_color: 2016,
  fx_aux_fields: { low: 2, flag: false, high: 1 },
  text_colors_hex: ['#FF0000', '#000000', '#000000', '#000000'], wx_temp_color_hex: '#00FF00',
};
const SPY = {
  port: 1, mode: 1, tap: true, baud: 96, parity: 0, stopbits: 1, poll_ms: 1000, timeout_ms: 500, stale_ms: 10000, status: 0,
  slots: [
    { slot: 1, uid: 7, fc: 4, reg: 12, type: 0, decimals: 1, unit: 'C', lo: -200, hi: 600, live: 231 },
    { slot: 2, uid: 0, fc: 3, reg: 0, type: 0, decimals: 0, unit: '', lo: 0, hi: 0, live: null },
    { slot: 3, uid: 0, fc: 3, reg: 0, type: 0, decimals: 0, unit: '', lo: 0, hi: 0, live: null },
    { slot: 4, uid: 0, fc: 3, reg: 0, type: 0, decimals: 0, unit: '', lo: 0, hi: 0, live: null },
  ],
  weather: Array.from({ length: 7 }, (_x, k) => ({ field: k, uid: 0, fc: 3, reg: 0 })),
};
function snapFor(o) {
  const opts = Object.assign({ fx: 62, src: 'fx', pwmMode: 0, line: 'single_ws', play: 1, tab: 'led_scene', geometry: [16, 16], withChoices: true, answered: true, detail: 'full' }, o || {});
  const fx = opts.fx;
  const led = {
    answered: opts.answered, active_tab: opts.tab,
    strip: {
      led_count0: 60, led_count1: 0, led_type: 0, pixel_format: 0, auto_refresh: 1, byte_order: 2, line_mode: 0,
      line_ui: opts.line, gamma: 128, startup_mode: 0, ch2_mode: 0, locked: true,
      matrix_width: opts.geometry[0], matrix_height: opts.geometry[1], matrix_layout: 0x0201, matrix_tile_mode: 0, matrix_tile_count: 2,
      matrix_geometry: `${opts.geometry[0]}×${opts.geometry[1]}`,
      matrix_wiring: { progressive: true, origin_bottom: false, mirror_x: false, swap_xy: false },
    },
    scene: {
      render_source: opts.src === 'fx' ? 1 : opts.src === 'flash' ? 2 : 0, scene_ui: opts.src, fx_id_raw: fx, fx_id: fx, fx_group: groupOf(fx),
      fx_speed: 128, fx_param: 200, flash_slot: 3, play_ctrl: opts.play, options: 1, loop: true,
      visibility: Object.assign({}, VIS_NONE, VIS[fx] || {}), fx_groups: FX_GROUPS, aux_spec: auxSpec(fx),
      aux_hint_keys: fx === 62 ? ['rgbw_fx_aux_style_hint_two_line', 'rgbw_fx_aux_style_hint_wide'] : [],
    },
  };
  if (opts.withChoices) led.choices = CHOICES;
  if (opts.tab === 'led_pwm') {
    const visible = opts.pwmMode === 5 ? [true, false, false, false] : [true, true, true, true];
    const labels = opts.pwmMode === 5 ? ['rgbw_pwm_ch_4w', 'rgbw_pwm_ch_4w_par', 'rgbw_pwm_ch_4w_par', 'rgbw_pwm_ch_4w_par'] : ['rgbw_pwm_ch_r', 'rgbw_pwm_ch_g', 'rgbw_pwm_ch_b', 'rgbw_pwm_ch_w'];
    led.pwm = { levels: [250, 500, 750, 1000], color_hex: '#408000', mode: opts.pwmMode, mode_labels: labels, mode_visible: visible, currents_ma: [120, 240, 360, 480], ntc_raw: 315, vled_raw: 1198, ntc_c: 31.5, vled_v: 11.98 };
  }
  if (opts.tab === 'led_di') {
    led.di = { channels: [1, 2, 3, 4].map(n => ({ channel: n, mode: n === 1 ? 1 : 0, debounce_ms: n === 1 ? 50 : 0 })), encoders: [] };
  }
  if (opts.tab === 'led_scene') {
    led.mode_data = JSON.parse(JSON.stringify(MODE_DATA));
    led.text_lines_raw = 2; led.text_lines = 2; led.text = 'CYNTRON';
    if (fx === 81) led.spy = JSON.parse(JSON.stringify(SPY));
  }
  return { kind: 'led', family: 'led', snapshot_detail: opts.detail, active_tab: opts.tab, info: { address: 13, serial: 0x12345, signature: 'RGBW_WS2812', app_version: '1.0.3.0', line: { baudrate: 19200, parity: 'N', stopbits: 1 } }, network: { address: 13, baudrate: 19200, parity: 'N', stopbits: 1, fast_modbus: false }, led };
}
function clone(o) { return JSON.parse(JSON.stringify(o)); }
function useSnap(snap) { stateStub.configSnapshot = snap; stateStub.configTab = snap.active_tab || 'info'; return snap; }

/* ── A. kind dispatch ─────────────────────────────────────────────────────── */

console.log('A. deviceConfigKindFromSignature');
if (seam) {
  eq(seam.LED_SIGNATURE_ALIASES.join(','), PY_ALIASES.join(','), 'JS LED_SIGNATURE_ALIASES === led_mb2ws_map.LED_SIGNATURE_ALIASES');
  eq(seam.LED_SIGNATURE_PREFIXES.join(','), PY_PREFIXES.join(','), 'JS LED_SIGNATURE_PREFIXES === led_mb2ws_map.LED_SIGNATURE_PREFIXES');
  for (const sig of PY_ALIASES) {
    eq(seam.deviceConfigKindFromSignature(sig), 'led', `deviceConfigKindFromSignature('${sig}') === 'led'`);
    check(seam.isDeviceConfigSupported({ signature: sig }) === true, `isDeviceConfigSupported('${sig}') — the row is double-clickable`);
  }
  eq(seam.deviceConfigKindFromSignature('RGBW_WS2812_v2'), 'led', "a firmware suffix on a long alias still reads as 'led' (prefix rule)");
  eq(seam.deviceConfigKindFromSignature('rgbw_ws2812'), 'led', 'case is tolerated');
  for (const sig of ['ledGe', 'LEDX', 'WB-MLED3', 'LEDGER', 'MYLEDBOX']) {
    check(seam.deviceConfigKindFromSignature(sig) !== 'led', `'${sig}' is NOT a strip (a 3-char LED prefix would steal it)`);
  }
  for (const [sig, want] of [['CRSTDrAHAQ', 'carel'], ['uARIA', 'carel'], ['6AI6AO', 'mr'], ['12AI', 'mr'], ['Sens.', 'dtv'], ['CE02M3', 'ce'], ['MR2M-01', ''], ['', '']]) {
    eq(seam.deviceConfigKindFromSignature(sig), want, `deviceConfigKindFromSignature('${sig}') === '${want}' (unchanged)`);
  }
  eq(seam.deviceConfigTitle('led', 'RGBW_WS2812'), 'Светодиодная лента LED', 'window title for kind led');
  eq(api.title(), 'Светодиодная лента LED', 'led.js title() agrees with the seam');
  eq(api.kicker(), 'Настройка светодиодной ленты', 'led.js kicker()');
}

/* ── B. tabs and the poll shape ───────────────────────────────────────────── */

console.log('B. tabs');
eq(LW.LED_TABS.join(','), PY_TABS.join(','), 'led.js LED_TABS === led_poll.LED_TABS (same ids, same order)');
const sceneSnap = useSnap(snapFor({ fx: 62 }));
const tabIds = api.tabs(sceneSnap).map(t => t.id);
eq(tabIds.join(','), ['info', 'network'].concat(PY_TABS).join(','), 'info · network · the four led_* tabs');
eq(api.tabs(sceneSnap).map(t => t.label).join(','), 'Сведения,Сеть,RGBW каналы,Входы DI,Адресная лента,Сцена', 'tab labels');
check(!api.tabs(sceneSnap).some(t => /exfx/i.test(t.id) || /EXFX/.test(t.label)), 'no EXFX tab (descoped — hidden, never greyed)');
if (seam) eq(seam.configTabsForSnapshot(sceneSnap).map(t => t.id).join(','), tabIds.join(','), 'flasher.js configTabsForSnapshot routes the led kind to led.js');
const sceneTab = api.tabs(sceneSnap).find(t => t.id === 'led_scene');
eq(sceneTab.suffix, ' - Пуск', 'scene tab suffix shows the play state');
check(sceneTab.live === true, 'a playing strip lights the scene tab (is-live)');
const stopped = api.tabs(snapFor({ play: 0 })).find(t => t.id === 'led_scene');
eq(stopped.suffix, ' - Стоп', 'a stopped strip reads Стоп');
check(!stopped.live, 'a stopped strip is not live');
eq(api.pollTab('led_scene', 'panel'), '', "a panel poll of the scene tab sends an EMPTY active_tab (base block only — F6)");
eq(api.pollTab('led_scene', 'full'), 'led_scene', 'a full read of the scene tab names it');
eq(api.pollTab('led_pwm', 'panel'), 'led_pwm', 'the PWM tab keeps its tab on the panel poll');
if (seam) {
  stateStub.configTab = 'led_scene';
  eq(seam.configPollActiveTab('panel'), '', 'flasher.js configPollActiveTab applies the rule for the led kind');
  eq(seam.configPollActiveTab('full'), 'led_scene', 'flasher.js configPollActiveTab keeps the tab on a full read');
  stateStub.configSnapshot = { kind: 'mr', mr: {} };
  eq(seam.configPollActiveTab('panel'), 'led_scene', 'a non-led snapshot keeps the plain tab');
  useSnap(sceneSnap);
}

/* ── C. actions and parameter collectors ──────────────────────────────────── */

console.log('C. LED_ACTIONS parity and the collectors');
eq(LW.LED_ACTIONS.join(','), PY_ACTIONS.join(','), 'led.js LED_ACTIONS === device_config.LED_ACTIONS, same order');
const usedActions = new Set([...ledSrc.matchAll(/ledCommand\('([a-z_]+)'/g)].map(m => m[1]));
for (const a of usedActions) check(PY_ACTIONS.indexOf(a) >= 0, `ledCommand('${a}') is in the daemon's allow-list`);
for (const a of PY_ACTIONS) check(usedActions.has(a), `action '${a}' is reachable from the window`);
const sortedKeys = o => Object.keys(o).sort().join(',');

mount(api.renderSceneTab(useSnap(snapFor({ fx: 62 }))));
let sp = api.ledSceneParams(stateStub.configSnapshot);
eq(sortedKeys(sp), 'flash_slot,fx_aux,fx_id,fx_param,fx_speed,loop,source', 'scene params carry exactly the contract fields (fx 62)');
eq(sortedKeys(sp.fx_aux), 'flag,high,low', 'fx_aux is {low, flag, high}');
eq(sp.source + '/' + sp.fx_id + '/' + sp.fx_speed + '/' + sp.fx_param, 'fx/62/128/200', 'scene values read from the mounted controls');
eq(sp.fx_aux.low + '/' + sp.fx_aux.high, '2/1', 'aux fields read from the style select and colour select');
mount(api.renderSceneTab(useSnap(snapFor({ fx: 3 }))));
sp = api.ledSceneParams(stateStub.configSnapshot);
eq(sortedKeys(sp), 'flash_slot,fx_density,fx_id,fx_param,fx_speed,loop,source', 'a density mode adds fx_density and no fx_aux (fx 3)');
eq(sp.fx_density, 128, 'fx_density read from the slider');
mount(api.renderSceneTab(useSnap(snapFor({ src: 'flash', fx: 0 }))));
sp = api.ledSceneParams(stateStub.configSnapshot);
eq(sp.source + '/' + sp.flash_slot + '/' + sp.loop, 'flash/3/true', 'flash source reads the slot and the loop box');

mount(api.renderStripTab(useSnap(snapFor({ tab: 'led_strip', line: 'single_ws' }))));
let stp = api.ledStripParams();
eq(sortedKeys(stp), 'auto_refresh,byte_order,gamma,led_count0,led_type,line,matrix_height,matrix_width,mirror_x,origin_bottom,pixel_format,progressive,swap_xy,tile_count,tile_mode', 'strip params carry exactly the contract fields (single line)');
eq([stp.line, stp.led_type, stp.byte_order, stp.led_count0, stp.auto_refresh, stp.gamma, stp.matrix_width, stp.matrix_height, stp.tile_count, stp.progressive, stp.swap_xy].join('/'), 'single_ws/0/2/60/1/128/16/16/2/true/false', 'strip values read from the mounted controls');
mount(api.renderStripTab(useSnap(snapFor({ tab: 'led_strip', line: 'dual_ws' }))));
stp = api.ledStripParams();
check(sortedKeys(stp).indexOf('ch2_mode') >= 0 && sortedKeys(stp).indexOf('led_count1') >= 0, 'the dual line adds led_count1 + ch2_mode');
mount(api.renderStripTab(useSnap(snapFor({ tab: 'led_strip', line: 'apa102' }))));
stp = api.ledStripParams();
check(!('led_type' in stp) && !('pixel_format' in stp) && !('led_count1' in stp), 'APA102 omits led_type / pixel_format / channel 2 (the daemon forces them)');

mount(api.renderSceneTab(useSnap(snapFor({ fx: 81 }))));
const spy = api.ledSpyParams(stateStub.configSnapshot);
eq(sortedKeys(spy), 'baud,mode,parity,poll_ms,port,slots,stale_ms,stopbits,tap,timeout_ms,weather', 'spy params carry the WHOLE card (the daemon rewrites 640..646 with defaults otherwise)');
eq(spy.slots.length + '/' + spy.weather.length, '4/7', 'four slots, seven weather binds');
eq(sortedKeys(spy.slots[0]), 'decimals,fc,hi,lo,reg,type,uid,unit', 'slot fields');
eq(sortedKeys(spy.weather[0]), 'fc,reg,uid', 'weather bind fields');
eq([spy.port, spy.mode, spy.tap, spy.baud, spy.slots[0].uid, spy.slots[0].fc, spy.slots[0].unit, spy.slots[0].lo, spy.slots[0].hi].join('/'), '1/1/true/96/7/4/C/-200/600', 'spy values read from the mounted controls');
mount(api.renderSceneTab(useSnap(snapFor({ fx: 62 }))));
eq(sortedKeys(api.ledTextParams()), 'lines,text', 'text params: text + lines');
eq(api.ledTextParams().text + '/' + api.ledTextParams().lines, 'CYNTRON/2', 'text values');
mount(api.renderSceneTab(useSnap(snapFor({ fx: 64 }))));
let wx = api.ledWeatherParams();
eq(sortedKeys(wx), 'day,humidity_pct,month,pressure_mmhg,temp_c,temp_color,year', 'weather params carry the contract fields');
eq([wx.day, wx.month, wx.temp_c, wx.humidity_pct, wx.pressure_mmhg, wx.year, wx.temp_color].join('/'), '4/9/-11.5/61.5/748/2026/#00FF00', 'weather values');
DOM.get('cfg-led-wx-temp').value = '';
wx = api.ledWeatherParams();
check(!('temp_c' in wx), 'a blank weather field is OMITTED (the daemon writes its sentinel, never 0)');
let clock = api.ledClockParams();
eq(clock && clock.hours + ':' + clock.minutes, '7:30', 'clock params from both fields');
DOM.get('cfg-led-clock-mm').value = '';
eq(api.ledClockParams(), null, 'a half-filled clock is refused in the browser (never seeds midnight)');

// ledCommand: the one write path.
console.log('C2. ledCommand');
useSnap(snapFor({ tab: 'led_pwm' }));
mount(api.renderPwmTab(stateStub.configSnapshot));
calls.api.length = 0; calls.applied.length = 0; calls.banners.length = 0; calls.toasts.length = 0; calls.invalidated = 0;
apiReply = snapFor({ tab: 'led_pwm' });
await api.ledCommand('pwm', { mode: 0 }, null, 'ok');
eq(calls.api.length, 1, 'one POST per command');
eq(calls.api[0] && calls.api[0][0], '/device_config/led_write', 'the command endpoint');
const body = (calls.api[0] || [])[1] || {};
eq([body.port, body.device && body.device.address, body.action, body.params && body.params.mode, body.active_tab].join('/'), 'COM3/13/pwm/0/led_pwm', 'request body: port, device, action, params, active_tab');
eq(calls.invalidated, 1, 'the poll generation is bumped (an in-flight poll cannot repaint the pre-write state)');
eq(calls.applied.length, 1, 'the reply snapshot is applied like a poll');
eq(stateStub.configBusy, false, 'busy flag released after the reply');
check(calls.toasts.some(t => t[0] === 'ok' && t[1] === 'success'), 'success toast');
calls.api.length = 0;
await api.ledCommand('nope', {}, null, '');
eq(calls.api.length, 0, 'an action outside the allow-list is never sent');
apiThrows = new Error('HTTP 409: линия занята');
calls.banners.length = 0; calls.toasts.length = 0;
await api.ledCommand('stop', {}, null, 'x');
check(calls.banners.some(b => b[1] === 'error' && /Команда ленты: /.test(b[0]) && /409/.test(b[0])), 'a refused command lands in the banner as an error');
check(calls.toasts.some(t => t[1] === 'error'), '…and in a toast');
eq(stateStub.configBusy, false, 'busy flag released after a failure too');
apiThrows = null;

/* ── D. markup ⇄ patch, hide-don't-grey ───────────────────────────────────── */

console.log('D. rendered ids and gating');
function patchedValues(html, snap, expectations) {
  mount(html);
  for (const id of DOM.keys()) {
    const e = DOM.get(id);
    if (e.tagName === 'INPUT' && (e.type === 'checkbox' || e.type === 'radio')) e.checked = !e.checked;
    else if (e.tagName === 'INPUT' || e.tagName === 'SELECT') e.value = 'stale';
    else e.textContent = 'stale';
  }
  requestedIds.clear();
  api.patch(snap);
  const rendered = idsIn(html);
  let hit = 0;
  for (const id of requestedIds) if (rendered.has(id)) hit++;
  check(hit >= 6, `patch touched ${hit} rendered ids of the ${stateStub.configTab} tab (non-vacuity floor >= 6)`);
  for (const [id, want] of Object.entries(expectations)) {
    const e = DOM.get(id);
    const got = e ? (e.type === 'checkbox' || e.type === 'radio' ? e.checked : (e.tagName === 'INPUT' || e.tagName === 'SELECT' ? e.value : e.textContent)) : undefined;
    eq(got, want, `#${id} patched to ${JSON.stringify(want)}`);
  }
}
const pwmSnap = useSnap(snapFor({ tab: 'led_pwm', pwmMode: 0 }));
const pwmHtml = api.renderPwmTab(pwmSnap);
const pwmIds = idsIn(pwmHtml);
for (const id of ['cfg-led-pwm-mode', 'cfg-led-pwm-color', 'cfg-led-pwm-en-1', 'cfg-led-pwm-lvl-4', 'cfg-led-pwm-cur-1', 'cfg-led-ntc', 'cfg-led-vled']) check(pwmIds.has(id), `PWM tab emits #${id}`);
patchedValues(pwmHtml, pwmSnap, { 'cfg-led-pwm-mode': '0', 'cfg-led-pwm-lvl-1': '250', 'cfg-led-pwm-en-1': true, 'cfg-led-pwm-cur-1': '120', 'cfg-led-ntc': '31.5', 'cfg-led-vled': '11.98', 'cfg-led-pwm-color': '#408000' });
const pwm5 = idsIn(api.renderPwmTab(useSnap(snapFor({ tab: 'led_pwm', pwmMode: 5 }))));
check(pwm5.has('cfg-led-pwm-en-1') && !pwm5.has('cfg-led-pwm-en-2') && !pwm5.has('cfg-led-pwm-lvl-4'), 'PWM mode 5 (4×W) hides channels 2–4 (parallel slaves), never greys them');
check(!pwm5.has('cfg-led-pwm-color'), 'the colour wheel is RGB + W only');
const pwmNoBlock = api.renderPwmTab(useSnap(Object.assign(snapFor({ tab: 'led_pwm' }), {})));
check(pwmNoBlock.includes('cfg-led-pwm-mode'), 'sanity: the PWM block renders its controls');
const noPwm = snapFor({ tab: 'led_pwm' }); delete noPwm.led.pwm;
check(!api.renderPwmTab(useSnap(noPwm)).includes('<select') && api.renderPwmTab(noPwm).includes('Нет данных'), 'an unread PWM block renders «Нет данных» with no controls');

const diSnap = useSnap(snapFor({ tab: 'led_di' }));
const diHtml = api.renderDiTab(diSnap);
for (const id of ['cfg-led-di-mode-1', 'cfg-led-di-deb-1', 'cfg-led-di-mode-4', 'cfg-led-di-deb-4']) check(idsIn(diHtml).has(id), `DI tab emits #${id}`);
patchedValues(diHtml, diSnap, { 'cfg-led-di-mode-1': '1', 'cfg-led-di-deb-1': '50', 'cfg-led-di-mode-2': '0' });

const stripSnap = useSnap(snapFor({ tab: 'led_strip', line: 'single_ws' }));
const stripHtml = api.renderStripTab(stripSnap);
const stripIds = idsIn(stripHtml);
for (const id of ['cfg-led-line', 'cfg-led-ledtype', 'cfg-led-pixfmt', 'cfg-led-order', 'cfg-led-count0', 'cfg-led-autorefresh', 'cfg-led-gamma', 'cfg-led-mxtype', 'cfg-led-mx-prog', 'cfg-led-mx-bottom', 'cfg-led-mx-mirror', 'cfg-led-mx-swap', 'cfg-led-tilecount', 'cfg-led-tilemode', 'cfg-led-strip-apply-btn', 'cfg-led-count1', 'cfg-led-ch2mode']) check(stripIds.has(id), `strip tab emits #${id}`);
check(/data-led-show="dual" hidden/.test(stripHtml), 'the channel-2 card is HIDDEN on a single line');
check(!/data-led-show="ws" hidden/.test(stripHtml), 'LED type / pixel format are shown on a WS line');
check(!/<option value="7"/.test(stripHtml.slice(stripHtml.indexOf('cfg-led-ledtype'), stripHtml.indexOf('cfg-led-pixfmt'))), 'the LED-type combo hides APA102 (chosen through the line selector)');
patchedValues(stripHtml, stripSnap, { 'cfg-led-line': 'single_ws', 'cfg-led-ledtype': '0', 'cfg-led-count0': '60', 'cfg-led-gamma': '128', 'cfg-led-mxtype': '16,16', 'cfg-led-mx-prog': true, 'cfg-led-mx-swap': false, 'cfg-led-tilecount': '2', 'cfg-led-tilemode': '0', 'cfg-led-autorefresh': true });
const dualHtml = api.renderStripTab(useSnap(snapFor({ tab: 'led_strip', line: 'dual_ws' })));
check(/data-led-show="dual">/.test(dualHtml) && !/data-led-show="dual" hidden/.test(dualHtml), 'DUAL shows the channel-2 block');
const apaHtml = api.renderStripTab(useSnap(snapFor({ tab: 'led_strip', line: 'apa102' })));
check(/data-led-show="ws" hidden/.test(apaHtml), 'APA102 hides LED type and pixel format');
const oddHtml = api.renderStripTab(useSnap(snapFor({ tab: 'led_strip', geometry: [7, 9] })));
check(/<option value="7,9" selected>7×9<\/option>/.test(oddHtml), 'an unlisted geometry is SHOWN as read (7×9), never rewritten to a listed type');

const fxHtml = {};
for (const fx of [0, 3, 51, 62, 63, 64, 81]) fxHtml[fx] = api.renderSceneTab(useSnap(snapFor({ fx })));
const has = (fx, id) => idsIn(fxHtml[fx]).has(id);
check(!has(0, 'cfg-led-text') && !has(0, 'cfg-led-clock-hh') && !has(0, 'cfg-led-wx-day') && !has(0, 'cfg-led-spy-port') && !has(0, 'cfg-led-tc1') && !has(0, 'cfg-led-aux-low') && !has(0, 'cfg-led-density'), 'fx 0 (Static): no cards, no aux, no density');
check(has(0, 'cfg-led-fx') && has(0, 'cfg-led-speed') && has(0, 'cfg-led-bri') && has(0, 'cfg-led-play-btn') && has(0, 'cfg-led-stop-btn') && has(0, 'cfg-led-refresh-btn') && has(0, 'cfg-led-play-badge'), 'fx 0: effect select, speed, brightness, Play/Stop/Refresh, the play badge');
check(has(3, 'cfg-led-density') && !has(3, 'cfg-led-aux-low'), 'fx 3 (Rainbow): density slider, no aux row');
check(has(51, 'cfg-led-aux-low') && has(51, 'cfg-led-aux-flag') && /<input id="cfg-led-aux-high"/.test(fxHtml[51]), 'fx 51 (Escort): colour select + reverse flag + pool LENGTH number (not a colour combo)');
check(/<select id="cfg-led-aux-low"/.test(fxHtml[62]) && (fxHtml[62].match(/<option value="\d+"[^>]*>[^<]*<\/option>/g) || []).length >= 18 && /<select id="cfg-led-aux-high"/.test(fxHtml[62]), 'fx 62 (Matrix text): 18 text styles + colour override combo');
check(has(62, 'cfg-led-text') && has(62, 'cfg-led-text-count') && has(62, 'cfg-led-textlines') && has(62, 'cfg-led-text-apply-btn') && has(62, 'cfg-led-tc1') && has(62, 'cfg-led-bg2'), 'fx 62: marquee card + text colours');
check(!has(62, 'cfg-led-clock-hh') && !has(62, 'cfg-led-wx-day') && !has(62, 'cfg-led-spy-port') && !has(62, 'cfg-led-wx-tempcolor'), 'fx 62: no clock, weather or spy card');
check(has(63, 'cfg-led-clock-hh') && has(63, 'cfg-led-clock-mm') && has(63, 'cfg-led-clock-apply-btn') && has(63, 'cfg-led-clock-pc-btn') && has(63, 'cfg-led-clock-unset-btn'), 'fx 63 (Clock): the clock card with its three buttons');
check(!has(63, 'cfg-led-text') && !has(63, 'cfg-led-wx-day') && !has(63, 'cfg-led-tc1'), 'fx 63: no text, weather or colours');
check(has(64, 'cfg-led-clock-hh') && has(64, 'cfg-led-wx-day') && has(64, 'cfg-led-wx-year') && has(64, 'cfg-led-wx-lines') && has(64, 'cfg-led-wx-apply-btn') && has(64, 'cfg-led-tc1') && has(64, 'cfg-led-wx-tempcolor'), 'fx 64 (Weather): clock + weather + colours + temperature colour');
check(!has(64, 'cfg-led-text') && !has(64, 'cfg-led-spy-port'), 'fx 64: no marquee input, no spy card');
check(has(81, 'cfg-led-spy-port') && has(81, 'cfg-led-spy-s1-uid') && has(81, 'cfg-led-spy-s4-live') && has(81, 'cfg-led-wxspy-6-reg') && has(81, 'cfg-led-spy-apply-btn') && has(81, 'cfg-led-spy-refresh-btn') && has(81, 'cfg-led-spy-lines') && has(81, 'cfg-led-tc1'), 'fx 81 (Line indicator): the spy card (slots, weather binds, apply/refresh) + colours');
check(!has(81, 'cfg-led-text') && !has(81, 'cfg-led-clock-hh'), 'fx 81: no marquee, no clock');
check(fxHtml[81].includes('flasher-config-scroll-x'), 'the slots table sits in a horizontal scroller (narrow viewports)');
const flashHtml = api.renderSceneTab(useSnap(snapFor({ src: 'flash', fx: 0 })));
check(idsIn(flashHtml).has('cfg-led-slot') && idsIn(flashHtml).has('cfg-led-loop') && idsIn(flashHtml).has('cfg-led-loadflash-btn') && !idsIn(flashHtml).has('cfg-led-fx') && !idsIn(flashHtml).has('cfg-led-speed'), 'flash source: slot + loop + Load Flash only');
const poolHtml = api.renderSceneTab(useSnap(snapFor({ src: 'pool', fx: 0 })));
check(!idsIn(poolHtml).has('cfg-led-fx') && !idsIn(poolHtml).has('cfg-led-slot') && !idsIn(poolHtml).has('cfg-led-text'), 'pool source: no effect, flash or card controls');
for (const fx of [0, 3, 51, 62, 63, 64, 81]) check(!/scale/i.test(fxHtml[fx]), `fx ${fx}: Scale (460) is never rendered`);
check(!/scale/i.test(flashHtml) && !/scale/i.test(poolHtml), 'Scale is never rendered for the flash / pool sources either');
const spyMissing = snapFor({ fx: 81 }); delete spyMissing.led.spy;
check(api.renderSceneTab(useSnap(spyMissing)).includes('Нет данных') && !idsIn(api.renderSceneTab(spyMissing)).has('cfg-led-spy-port'), 'the spy card waits for its block («Нет данных»), no inputs');

const silent = snapFor({ answered: false });
silent.led = { answered: false, choices: CHOICES };
for (const [name, fn] of [['pwm', api.renderPwmTab], ['di', api.renderDiTab], ['strip', api.renderStripTab], ['scene', api.renderSceneTab]]) {
  const h = fn(useSnap(Object.assign(silent, { active_tab: 'led_' + name })));
  check(!/<input|<select|<button/.test(h) && h.includes('Нет данных'), `a silent strip renders «Нет данных» on the ${name} tab — no inputs`);
}
const oldDaemon = snapFor({ tab: 'led_strip', withChoices: false });
const oldHtml = api.renderStripTab(useSnap(oldDaemon));
check(!/<input|<select/.test(oldHtml) && /sa02m-flasher/.test(oldHtml), 'an old daemon (no choice lists) renders no inputs and names the package to update');

// Scene: values land after a patch over a stale mount.
const s62 = useSnap(snapFor({ fx: 62 }));
patchedValues(api.renderSceneTab(s62), s62, { 'cfg-led-fx': '62', 'cfg-led-speed': '128', 'cfg-led-bri': '200', 'cfg-led-aux-low': '2', 'cfg-led-aux-high': '1', 'cfg-led-src-fx': true, 'cfg-led-src-pool': false, 'cfg-led-fxgrp-text': true, 'cfg-led-play-badge': 'Пуск', 'cfg-led-text': 'CYNTRON', 'cfg-led-text-count': '7 / 128', 'cfg-led-textlines': '2', 'cfg-led-tc1': '#FF0000' });
eq(DOM.get('cfg-led-play-badge').className, 'badge badge-ok', 'play badge class follows play_ctrl');
eq(DOM.get('cfg-led-speed-val').textContent, '128', 'the slider readout follows the patched value');
const s64 = useSnap(snapFor({ fx: 64 }));
patchedValues(api.renderSceneTab(s64), s64, { 'cfg-led-clock-hh': '7', 'cfg-led-clock-mm': '30', 'cfg-led-wx-day': '4', 'cfg-led-wx-temp': '-11.5', 'cfg-led-wx-year': '2026', 'cfg-led-wx-tempcolor': '#00FF00', 'cfg-led-wx-lines': '2' });
const s81 = useSnap(snapFor({ fx: 81 }));
patchedValues(api.renderSceneTab(s81), s81, { 'cfg-led-spy-port': '1', 'cfg-led-spy-tap': true, 'cfg-led-spy-s1-uid': '7', 'cfg-led-spy-s1-live': '231', 'cfg-led-spy-s2-live': '—', 'cfg-led-wxspy-0-uid': '0', 'cfg-led-spy-status': '0x0000', 'cfg-led-spy-lines': '2' });

/* ── E. live patch: hold, sentinels, render key ───────────────────────────── */

console.log('E. patch holds the operator\'s field');
const pwmE = useSnap(snapFor({ tab: 'led_pwm' }));
mount(api.renderPwmTab(pwmE));
const focused = DOM.get('cfg-led-pwm-lvl-1'); focused.value = '999'; activeEl = focused;
const dirty = DOM.get('cfg-led-pwm-lvl-2'); dirty.value = '111'; dirty.dataset.ledDirty = '1';
const dirtyBox = DOM.get('cfg-led-pwm-en-3'); dirtyBox.checked = false; dirtyBox.dataset.ledDirty = '1';
DOM.get('cfg-led-pwm-lvl-4').value = 'stale';
api.patch(pwmE);
eq(focused.value, '999', 'a FOCUSED level input is NOT overwritten by the 1 s poll');
eq(dirty.value, '111', 'an EDITED (dirty) level input is NOT overwritten');
eq(dirtyBox.checked, false, 'a dirty checkbox keeps the operator value');
eq(DOM.get('cfg-led-pwm-lvl-4').value, '1000', 'a clean sibling still takes the live value');
check(api.fieldHolds(focused, focused) && api.fieldHolds(dirty, null) && !api.fieldHolds(DOM.get('cfg-led-pwm-lvl-4'), null), 'fieldHolds: focused / dirty hold, clean does not');
activeEl = null;
const unset = snapFor({ fx: 63 }); unset.led.mode_data.tod_hours = null; unset.led.mode_data.tod_minutes = null;
const unsetHtml = api.renderSceneTab(useSnap(unset));
check(/id="cfg-led-clock-hh"[^>]*value=""/.test(unsetHtml), 'an unset clock renders a BLANK hour field (sentinel), never 0');
mount(unsetHtml); DOM.get('cfg-led-clock-hh').value = '5';
api.patch(unset);
eq(DOM.get('cfg-led-clock-hh').value, '', 'a patch with the sentinel clears the field to blank, never 0');

console.log('E2. renderKey / configBodyIsPatchable');
const base = useSnap(snapFor({ fx: 62 }));
const k = api.renderKey(base, 'led_scene');
check(k !== '', 'a led body has a render key');
eq(api.renderKey(clone(base), 'led_scene'), k, 'the same snapshot keeps the key (patch in place)');
const tele = clone(base); tele.led.scene.fx_speed = 5; tele.led.mode_data.wx_temp_c = 1;
eq(api.renderKey(tele, 'led_scene'), k, 'a value change alone keeps the key');
check(api.renderKey(base, 'led_pwm') !== k, 'a tab change rebuilds');
const fxc = clone(base); fxc.led.scene.fx_id = 63; check(api.renderKey(fxc, 'led_scene') !== k, 'an effect change rebuilds (its cards differ)');
const srcc = clone(base); srcc.led.scene.scene_ui = 'flash'; check(api.renderKey(srcc, 'led_scene') !== k, 'a render-source change rebuilds');
const spyc = clone(base); spyc.led.spy = clone(SPY); check(api.renderKey(spyc, 'led_scene') !== k, 'the spy block arriving rebuilds');
const pwmA = snapFor({ tab: 'led_pwm', pwmMode: 0 }); const pwmB = snapFor({ tab: 'led_pwm', pwmMode: 5 });
check(api.renderKey(pwmA, 'led_pwm') !== api.renderKey(pwmB, 'led_pwm'), 'a PWM mode change rebuilds (channel rows differ)');
const lineA = snapFor({ tab: 'led_strip', line: 'single_ws' }); const lineB = snapFor({ tab: 'led_strip', line: 'dual_ws' });
check(api.renderKey(lineA, 'led_strip') !== api.renderKey(lineB, 'led_strip'), 'a line-mode change rebuilds');
if (seam) {
  useSnap(base);
  mount(api.renderSceneTab(base));
  DOM.set('flasher-config-body', new El('div', { firstChild: new El('section') }));
  stateStub.configBodyKey = seam.configBodyRenderKey(base);
  check(stateStub.configBodyKey === k, 'flasher.js configBodyRenderKey routes the led kind to led.js');
  check(seam.configBodyIsPatchable(base) === true, 'flasher.js patches an unchanged led body in place');
  check(seam.configBodyIsPatchable(fxc) === false, 'flasher.js rebuilds on an effect change');
  check(seam.configBodyIsPatchable({ kind: 'mr', mr: {} }) === false, 'a non-led snapshot keeps its own path');
  DOM.delete('flasher-config-body');
  check(seam.configBodyIsPatchable(base) === false, 'a missing body host is never patched');
}

/* ── F. merge ─────────────────────────────────────────────────────────────── */

console.log('F. merge');
calls.banners.length = 0;
const full = snapFor({ fx: 81 });
const panel = snapFor({ fx: 81, withChoices: false, detail: 'panel' });
delete panel.led.mode_data; delete panel.led.text; delete panel.led.text_lines; delete panel.led.spy;
const merged = api.merge(full, panel);
check(merged.led.choices === CHOICES && merged.led.mode_data && merged.led.text === 'CYNTRON' && merged.led.spy, 'a panel reply keeps the previous choices / mode_data / text / spy');
eq(merged.led.scene.fx_id, 81, 'the fresh base block still wins');
const withMd = snapFor({ fx: 81, withChoices: false, detail: 'panel' }); withMd.led.mode_data.fx_density = 7;
eq(api.merge(full, withMd).led.mode_data.fx_density, 7, 'a reply that DID carry mode_data wins');
const foreign = { kind: 'mr', mr: {} };
check(api.merge(full, foreign) === foreign, 'a non-led snapshot passes through');
eq(calls.banners.length, 0, 'no banner for a healthy daemon');
const old = snapFor({ tab: 'led_strip', withChoices: false });
const m1 = api.merge(null, old);
check(m1.led.stale_daemon === true && calls.banners.length === 1 && /sa02m-flasher/.test(calls.banners[0][0]), 'an old daemon (full reply, no choices) is named ONCE in the banner');
api.merge(m1, old);
eq(calls.banners.length, 1, '…and not again on the next reply');
calls.banners.length = 0;
const stub = { kind: 'led', snapshot_detail: 'stub', led: {} };
api.merge(null, stub);
eq(calls.banners.length, 0, 'the pre-first-read stub raises no banner');

/* ── G. i18n both ways ────────────────────────────────────────────────────── */

console.log('G. i18n');
const dictKeys = new Set();
const keyRe = /^\s*'((?:[^'\\]|\\.)*)':\s*'/gm;
let km;
while ((km = keyRe.exec(i18nSrc))) dictKeys.add(km[1].replace(/\\'/g, "'"));
check(dictKeys.size > 500, `i18n.js DICT parsed (${dictKeys.size} keys) — non-vacuity floor`);
const cyr = /[А-Яа-яЁё]/;
const swept = new Set();
for (const [key, ru] of Object.entries(LW.LED_T)) {
  if (!cyr.test(ru)) continue;
  if (/%d/.test(ru)) continue; // formatted at render time; swept as a text node below
  swept.add(ru);
}
const ledTCount = swept.size;
check(ledTCount >= 250, `${ledTCount} Cyrillic LED_T labels swept`);
function cyrillicTextNodes(html) {
  const out = new Set();
  String(html).split(/<[^>]*>/).forEach(seg => {
    const text = unescapeHtml(seg).replace(/\s+/g, ' ').trim();
    if (text && cyr.test(text)) out.add(text);
  });
  return out;
}
const renders = [
  api.renderPwmTab(snapFor({ tab: 'led_pwm', pwmMode: 0 })), api.renderPwmTab(snapFor({ tab: 'led_pwm', pwmMode: 5 })),
  api.renderDiTab(snapFor({ tab: 'led_di' })),
  api.renderStripTab(snapFor({ tab: 'led_strip', line: 'single_ws' })), api.renderStripTab(snapFor({ tab: 'led_strip', line: 'dual_ws' })),
  api.renderStripTab(snapFor({ tab: 'led_strip', geometry: [7, 9] })),
  ...[0, 3, 51, 62, 63, 64, 81].map(fx => fxHtml[fx]), flashHtml, poolHtml,
  api.renderSceneTab(snapFor({ fx: 62, play: 0 })), api.renderSceneTab(snapFor({ fx: 62, play: 2 })),
  api.renderPwmTab(silent), api.renderStripTab(oldDaemon), api.renderPwmTab(noPwm), api.renderSceneTab(spyMissing),
];
renders.forEach(h => cyrillicTextNodes(h).forEach(s => swept.add(s)));
// Authored literals outside LED_T (toasts, banner prefixes, badge words).
const litRe = /'([^'\\\n]*[А-Яа-яЁё][^'\\\n]*)'/g;
const ledTBlock = extractConst(ledSrc, 'LED_T');
const outsideLedT = ledSrc.replace(ledTBlock, '');
let lm;
while ((lm = litRe.exec(outsideLedT))) {
  const s = lm[1].replace(/\s+/g, ' ').trim();
  if (s) swept.add(s);
}
api.tabs(sceneSnap).forEach(t => { swept.add(t.label); if (t.suffix) swept.add(t.suffix.replace(/\s+/g, ' ').trim()); });
swept.add(api.tabs(snapFor({ play: 0 })).find(t => t.id === 'led_scene').suffix.trim());
check(swept.size >= 150, `swept ${swept.size} authored Russian strings — non-vacuity floor (>=150)`);
const missing = [...swept].filter(s => !dictKeys.has(s));
for (const s of missing) console.error('  FAIL - no i18n DICT entry for: ' + JSON.stringify(s));
failures += missing.length;
if (!missing.length) console.log(`  ok   - all ${swept.size} authored Russian strings have an EN translation`);

// The other way: every key the daemon can emit has a Russian label.
const pyKeys = new Set();
for (const src of [ledMapSrc, ledMb2wsSrc, ledPollSrc]) {
  for (const m of src.matchAll(/"(rgbw_[a-z0-9_%]+)"/g)) {
    const key = m[1];
    if (key === 'rgbw_fx_%02d') for (let i = 0; i < 82; i++) pyKeys.add('rgbw_fx_' + String(i).padStart(2, '0'));
    else if (key === 'rgbw_fx_aux_style_%02d') for (let i = 0; i < 18; i++) pyKeys.add('rgbw_fx_aux_style_' + String(i).padStart(2, '0'));
    else if (key === 'rgbw_tile_count_%d') for (let i = 1; i <= 4; i++) pyKeys.add('rgbw_tile_count_' + i);
    else if (!/%/.test(key)) pyKeys.add(key);
  }
}
check(pyKeys.size >= 200, `${pyKeys.size} daemon-emittable rgbw_ keys parsed (non-vacuity floor >= 200)`);
const unlabelled = [...pyKeys].filter(k => !Object.prototype.hasOwnProperty.call(LW.LED_T, k));
for (const k of unlabelled) console.error('  FAIL - daemon key with no LED_T label: ' + k);
failures += unlabelled.length;
if (!unlabelled.length) console.log(`  ok   - every daemon-emittable key (${pyKeys.size}) has a LED_T label`);

if (failures) {
  console.error('js-unit-led-window: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('js-unit-led-window: ok');
process.exit(0);
