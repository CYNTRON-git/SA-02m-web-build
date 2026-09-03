#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it evaluates the shipped flasher.js functions and asserts their return values / DOM effects, pinning no source line by text; commenting a line out inside the code under test changes the result it measures, which is exactly what the RED/GREEN ratchet in the header records.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-carel-window — standalone Node test for the Carel (c.pCOmini /
   uAria) config window in flasher.js (release 1.0.6.31).
   ───────────────────────────────────────────────────────────────────────────
   What it pins, and why each pin exists:

   A) Kind dispatch. `deviceConfigKindFromSignature` must answer 'carel' for
      every token of the shared map's _SIG_CARELS list BEFORE the MP/MR hints
      are tried, otherwise the row is neither double-clickable nor openable —
      and, worse, a Carel PLC that answers holding 0 with [0,0] reads as an
      MR-02m in a bootloader and gets offered firmware
      (docs/contracts/carel-ahu.md §1).

   B) Tabs. The five window tabs, and — load-bearing — the I/O tab id must
      equal device_config.CAREL_IO_TAB: the daemon reads the expensive io_*
      block ONLY when the window sends exactly that name in active_tab. A typo
      there leaves the four tables permanently empty with no error anywhere.

   C) Action parity. The JS action list must equal device_config.CAREL_ACTIONS
      (read from the Python source here, not re-typed): an action outside the
      daemon's allow-list is refused at the far end of a leased port session.

   D) Markup ⇄ patch id contract. Every id the live patch writes must be
      emitted by the renderer of the tab it belongs to, per family (crst has
      Sys_Mode/Ma18/fan %, uAria has the fan step/Gs04) — the classic
      "widget silently stops updating" defect the null-safe helpers swallow.

   E) Live patch on a dirty field. The window polls once a second; the patch
      must not wipe an input the operator is typing into (focused) or has
      edited (dataset.carelDirty), and must update the I/O rows IN PLACE —
      a rebuild resets the scroll position of the four I/O tables.

   F) i18n. Every Cyrillic string the four tab renderers emit as text, plus
      the Cyrillic literals of the confirm/toast paths, must have a DICT entry
      in i18n.js. The i18n-dict-contract row sweeps uiT() literals and HTML
      markup only — a Russian string rendered from a flasher.js template
      string is outside its grammar, so this test carries that half for the
      Carel window. Fixture device text is deliberately Latin, so every
      Cyrillic string found in the output is authored by flasher.js.

   RED/GREEN ratchet (both observed, 2026-09-03; the mutations are recorded in
   the commit body):
     * deleting the `if (signatureLooksLikeCarel(n)) return 'carel';` line from
       deviceConfigKindFromSignature → section A fails (window never opens);
     * making `carelFieldHolds` return false → section E's dirty/focused
       assertions fail (a live poll wipes the operator's input).

   Following the js-unit precedent (test-flasher-signature-hints.mjs): every
   function under test is extracted from the SHIPPED flasher.js by
   brace-matching and evaluated in a minimal sandbox, so the test exercises
   the shipped source and cannot drift from what ships. The extractor matches
   `function NAME(` exactly (a prefix match would extract
   carelFamilyFromSignature when asked for carelFamily).
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const FLASHER_JS = join(ROOT, 'www', 'network_config', 'static', 'js', 'flasher.js');
const I18N_JS = join(ROOT, 'www', 'network_config', 'static', 'js', 'i18n.js');
const DEVICE_CONFIG_PY = join(ROOT, 'opt', 'sa02m-flasher', 'sa02m_flasher', 'device_config.py');

const src = readFileSync(FLASHER_JS, 'utf8');

/** Extract `function NAME(...) { ... }` by brace-matching (exact name match). */
function extractFn(src, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const m = re.exec(src);
  if (!m) throw new Error('function ' + name + '() not found in flasher.js');
  const start = m.index;
  const open = src.indexOf('{', start);
  if (open < 0) throw new Error('no body brace for ' + name + '()');
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name + '()');
}

/** Extract a top-level `const NAME = [...]` / `{...}` / '...' statement. */
function extractConst(src, name) {
  const re = new RegExp('const\\s+' + name + '\\s*=');
  const m = re.exec(src);
  if (!m) throw new Error('const ' + name + ' not found in flasher.js');
  const start = m.index;
  const eq = src.indexOf('=', start);
  let openIdx = eq + 1;
  while (/\s/.test(src[openIdx])) openIdx++;
  const openCh = src[openIdx];
  if (openCh === "'" || openCh === '"') {
    const semi = src.indexOf(';', openIdx);
    return src.slice(start, semi + 1);
  }
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

/* ── minimal DOM the patch can drive ──────────────────────────────────────── */

class El {
  constructor(tag, opts) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.textContent = '';
    this.className = '';
    this.innerHTML = null;      // null = never rebuilt (the in-place assertion)
    this.children = [];
    this.dataset = {};
    Object.assign(this, opts || {});
    const self = this;
    this.classList = {
      toggle(name, on) {
        const set = new Set(String(self.className).split(/\s+/).filter(Boolean));
        if (on) set.add(name); else set.delete(name);
        self.className = Array.from(set).join(' ');
      },
      contains(name) {
        return String(self.className).split(/\s+/).indexOf(name) >= 0;
      },
    };
  }
  get firstChild() { return this.children[0] || null; }
  querySelector(sel) {
    const want = String(sel).toUpperCase();
    return this.children.find(c => c.tagName === want) || null;
  }
  querySelectorAll(sel) {
    const want = String(sel).toUpperCase();
    return this.children.filter(c => c.tagName === want);
  }
}

function listRow(headText, valueText, valueClass) {
  const span = new El('span', { textContent: headText });
  const strong = new El('strong', { textContent: valueText, className: valueClass || 'do-state-value' });
  return new El('div', { children: [span, strong] });
}

const DOM = new Map();
const documentStub = { activeElement: null };
const stateStub = { configTab: 'plant', configBodyKey: '' };
const windowStub = { sa02mI18n: { lang: 'ru', t: s => s } };
function configModalEl(id) { return DOM.get(id) || null; }

const parts = [
  extractConst(src, 'WB_RELAY_SIG_PREFIXES'),
  extractConst(src, 'WB_MAO4_SIG_PREFIXES'),
  extractConst(src, 'CAREL_SIGNATURE_TOKENS'),
  extractConst(src, 'CAREL_ACTIONS'),
  extractConst(src, 'CAREL_FAMILY_CRST'),
  extractConst(src, 'CAREL_FAMILY_UARIA'),
  extractConst(src, 'CAREL_IO_TAB'),
  extractConst(src, 'CAREL_IO_COLUMNS'),
  extractConst(src, 'CAREL_IO_KEYS'),
  extractConst(src, 'CAREL_SYS_MODES'),
  extractConst(src, 'CAREL_PLANT_STATES'),
  extractConst(src, 'CAREL_VARIANT_LABELS'),
  extractFn(src, 'stripBootloaderSignatureSuffix'),
  extractFn(src, 'isMpModuleSignatureForFirmwareHint'),
  extractFn(src, 'signatureLooksLikeCarel'),
  extractFn(src, 'deviceConfigKindFromSignature'),
  extractFn(src, 'isDeviceConfigSupported'),
  extractFn(src, 'deviceConfigTitle'),
  extractFn(src, 'carelFamilyFromSignature'),
  extractFn(src, 'carelModelLabel'),
  extractFn(src, 'carelVariantLabel'),
  extractFn(src, 'carelFamily'),
  extractFn(src, 'carelIsUaria'),
  extractFn(src, 'carelBlock'),
  extractFn(src, 'formatFloat'),
  extractFn(src, 'escapeHtml'),
  extractFn(src, 'carelValueText'),
  extractFn(src, 'carelInputValue'),
  extractFn(src, 'carelOnOffText'),
  extractFn(src, 'carelPlantStateView'),
  extractFn(src, 'carelAlarmLine'),
  extractFn(src, 'mergeCarelSnapshot'),
  extractFn(src, 'carelIoRowHead'),
  extractFn(src, 'carelIoRowValue'),
  extractFn(src, 'carelIoRowHtml'),
  extractFn(src, 'renderCarelInfoTab'),
  extractFn(src, 'renderCarelNetworkTab'),
  extractFn(src, 'renderCarelCrstSettings'),
  extractFn(src, 'renderCarelUariaSettings'),
  extractFn(src, 'renderCarelPlantTab'),
  extractFn(src, 'renderCarelIoTab'),
  extractFn(src, 'renderCarelAlarmsTab'),
  extractFn(src, 'carelFieldHolds'),
  extractFn(src, 'carelSetText'),
  extractFn(src, 'carelSetField'),
  extractFn(src, 'carelPatchList'),
  extractFn(src, 'carelPatchAlarms'),
  extractFn(src, 'patchCarelLiveReadouts'),
  extractFn(src, 'carelSetpointPlan'),
  extractFn(src, 'configTabsForSnapshot'),
  extractFn(src, 'configBodyRenderKey'),
  extractFn(src, 'configBodyIsPatchable'),
  `return {
     CAREL_ACTIONS, CAREL_IO_TAB, CAREL_SYS_MODES,
     deviceConfigKindFromSignature, isDeviceConfigSupported, deviceConfigTitle,
     carelFamilyFromSignature, carelValueText, carelOnOffText, carelAlarmLine,
     carelPlantStateView, mergeCarelSnapshot, carelSetpointPlan, carelInputValue,
     renderCarelInfoTab, renderCarelNetworkTab, renderCarelPlantTab,
     renderCarelIoTab, renderCarelAlarmsTab,
     patchCarelLiveReadouts, configTabsForSnapshot,
     configBodyRenderKey, configBodyIsPatchable,
   };`,
];

const api = new Function('window', 'document', 'state', 'configModalEl', parts.join('\n'))(
  windowStub, documentStub, stateStub, configModalEl);

let failures = 0;
function check(cond, msg) {
  if (cond) console.log('  ok   - ' + msg);
  else { failures++; console.error('  FAIL - ' + msg); }
}
function eq(got, want, msg) { check(got === want, msg + ' (got ' + JSON.stringify(got) + ')'); }

/* ── fixtures: the bench values of 2026-09-03 (plan carel-1.0.6.31) ───────── */

const CRST_SNAP = {
  kind: 'carel',
  family: 'crst',
  info: {
    address: 1, signature: 'CRSTDrAHAQ', app_version: '2.03.00.46',
    carel_variant: 'E', variant_label: 'Enhanced', model: 'c.pCOmini',
    line: { baudrate: 19200, parity: 'N', stopbits: 1 },
  },
  network: { address: 1, baudrate: 19200, parity: 'N', stopbits: 1, writable: false },
  carel: {
    fam: 'crst', answered: true, plant_state: 'run', unit_status_text: 'Rabota',
    unit_status_algo: 'v2', alarm_reset_coil: 66,
    oat: 0.0, sat: 26.5, rwt: 75.1, valve: 100.0, disp_sp: 27.2,
    unit: 1, mode: 1, sp_w: 27.2, sp_s: 20.5, fan_sa: 80.0, fan_ea: 80.0,
    bms_run: true, ma18: true, pump: true, alarms: [],
    io_u: [{ tag: 'B1', text: 'Supply air', unit: 'C', value: 26.5 }],
    io_no: [{ tag: 'NO1', text: 'Pump relay', on: true }],
    io_di: [{ tag: 'DI1', text: 'Fire alarm', on: false }],
    io_ao: [{ tag: 'Y1', text: 'Heat valve', unit: '%', value: 100 }],
  },
};

const UARIA_SNAP = {
  kind: 'carel',
  family: 'uaria',
  info: {
    address: 2, signature: 'CRSTDm_AHU', app_version: '1.0.61',
    carel_variant: '', variant_label: '', model: 'uAria',
    line: { baudrate: 19200, parity: 'N', stopbits: 1 },
  },
  network: { address: 2, baudrate: 19200, parity: 'N', stopbits: 1, writable: false },
  carel: {
    fam: 'uaria', answered: true, plant_state: 'run', unit_status_text: 'Vklyucheno',
    unit_status_algo: 'uaria', alarm_reset_coil: 37,
    oat: 0.0, sat: 27.22, rwt: 43.59, valve: 0.0, fan: 0.0, unit: 1,
    sp_w: 22.0, sp_s: 22.0, season_code: 2, fan_min: 22.0, fan_sp: 7, fan_calc: 76.6,
    uaria_run: true, gs04: true, uaria_local: true, pump: true, alarms: [],
    io_u: [], io_no: [], io_di: [], io_ao: [],
  },
};

function clone(o) { return JSON.parse(JSON.stringify(o)); }

/* ── A. kind dispatch: the row opens the window ───────────────────────────── */

console.log('A. deviceConfigKindFromSignature');
const CAREL_SIGS = [
  'CRSTDrAHAQ', 'CRKRFAHAQ', 'CRSTDm_AHU', 'CRSTDM', 'uARIA', 'UARIA',
  'c.pCOmini', 'C.PCOMINI', 'CPCOmini', 'c.pCO',
];
for (const sig of CAREL_SIGS) {
  eq(api.deviceConfigKindFromSignature(sig), 'carel', `deviceConfigKindFromSignature('${sig}') === 'carel'`);
  check(api.isDeviceConfigSupported({ signature: sig }) === true,
    `isDeviceConfigSupported('${sig}') — row is double-clickable, window opens`);
}
const NON_CAREL = [['6AI6AO', 'mr'], ['12AI', 'mr'], ['Sens.', 'dtv'], ['CE02M3', 'ce'], ['MR2M-01', '']];
for (const [sig, want] of NON_CAREL) {
  eq(api.deviceConfigKindFromSignature(sig), want,
    `deviceConfigKindFromSignature('${sig}') === '${want}' (Carel branch must not swallow it)`);
}
eq(api.deviceConfigKindFromSignature(''), '', "blank signature stays unsupported");
eq(api.deviceConfigKindFromSignature('—'), '', "'—' stays unsupported");
eq(api.deviceConfigTitle('carel', 'CRSTDrAHAQ'), 'Приточная установка Carel', 'window title for kind carel');
eq(api.carelFamilyFromSignature('CRSTDm_AHU'), 'uaria', 'CRSTDm_AHU is the uAria family');
eq(api.carelFamilyFromSignature('CRSTDrAHAQ'), 'crst', 'CRSTDrAHAQ is the crst family');

/* ── B. tabs, and the I/O tab id the daemon keys the expensive read on ────── */

console.log('B. configTabsForSnapshot');
const tabs = api.configTabsForSnapshot(CRST_SNAP);
const tabIds = tabs.map(t => t.id);
eq(tabIds.join(','), ['info', 'network', 'plant', api.CAREL_IO_TAB, 'alarms'].join(','),
  'five Carel tabs in order');
eq(tabs.map(t => t.label).join(','), 'Сведения,Сеть,Установка,Входы/выходы,Тревоги', 'Carel tab labels');

const pySrc = readFileSync(DEVICE_CONFIG_PY, 'utf8');
const pyIoTab = /CAREL_IO_TAB\s*=\s*"([^"]+)"/.exec(pySrc);
check(!!pyIoTab, 'device_config.py declares CAREL_IO_TAB');
eq(api.CAREL_IO_TAB, pyIoTab && pyIoTab[1],
  'JS I/O tab id === device_config.CAREL_IO_TAB (else the daemon never reads io_*)');

const alarmTab = api.configTabsForSnapshot(
  Object.assign({}, CRST_SNAP, { carel: Object.assign({}, CRST_SNAP.carel, { alarms: [{ code: 'E03', text: 'x' }] }) })
).find(t => t.id === 'alarms');
eq(alarmTab.suffix, ' - 1', 'the alarms tab carries the active-alarm count');

/* ── C. action allow-list parity with the daemon ──────────────────────────── */

console.log('C. CAREL_ACTIONS parity');
const pyActionsBlock = /CAREL_ACTIONS:\s*Tuple\[str,\s*\.\.\.\]\s*=\s*\(([\s\S]*?)\)/.exec(pySrc);
check(!!pyActionsBlock, 'device_config.py declares CAREL_ACTIONS');
const pyActions = (pyActionsBlock ? pyActionsBlock[1] : '')
  .split(',').map(s => s.trim().replace(/^"|"$/g, '')).filter(Boolean);
check(pyActions.length === 10, `daemon exposes 10 actions (got ${pyActions.length})`);
eq(api.CAREL_ACTIONS.join(','), pyActions.join(','),
  'JS CAREL_ACTIONS === device_config.CAREL_ACTIONS, same order');

const setpointActions = api.carelSetpointPlan(false, () => true, () => 1).map(x => x.action)
  .concat(api.carelSetpointPlan(true, () => true, () => 1).map(x => x.action));
for (const act of setpointActions) {
  check(api.CAREL_ACTIONS.indexOf(act) >= 0, `setpoint plan action '${act}' is in the allow-list`);
}
eq(api.carelSetpointPlan(false, () => true, () => 1).map(x => x.action).join(','),
  'sp_winter,sp_summer,fan_supply,fan_exhaust', 'crst setpoint plan');
eq(api.carelSetpointPlan(true, () => true, () => 1).map(x => x.action).join(','),
  'sp_winter,sp_summer,fan_step', 'uAria setpoint plan (no fan %, no Sys_Mode)');
eq(api.carelSetpointPlan(false, () => false, () => 1).length, 0,
  'an untouched form writes nothing (no needless session on a shared line)');
eq(api.carelSetpointPlan(false, id => id === 'cfg-carel-sp-w', () => null).length, 0,
  'a dirty field with an unparsable value is not written');

/* ── D. markup ⇄ patch id contract ────────────────────────────────────────── */

console.log('D. rendered ids');
function idsIn(html) {
  const out = new Set();
  const re = /id="([^"]+)"/g;
  let m;
  while ((m = re.exec(html))) out.add(m[1]);
  return out;
}
const crstPlantHtml = api.renderCarelPlantTab(CRST_SNAP);
const uariaPlantHtml = api.renderCarelPlantTab(UARIA_SNAP);
const crstIds = idsIn(crstPlantHtml);
const uariaIds = idsIn(uariaPlantHtml);
const COMMON_IDS = ['cfg-carel-state-badge', 'cfg-carel-status', 'cfg-carel-sat', 'cfg-carel-rwt',
  'cfg-carel-oat', 'cfg-carel-valve', 'cfg-carel-pump', 'cfg-carel-run',
  'cfg-carel-sp-w', 'cfg-carel-sp-s', 'cfg-carel-start-btn', 'cfg-carel-stop-btn',
  'cfg-carel-apply-btn'];
for (const id of COMMON_IDS) {
  check(crstIds.has(id), `crst plant tab emits #${id}`);
  check(uariaIds.has(id), `uAria plant tab emits #${id}`);
}
for (const id of ['cfg-carel-fan-sa', 'cfg-carel-fan-ea', 'cfg-carel-sys-mode', 'cfg-carel-ma18']) {
  check(crstIds.has(id), `crst plant tab emits #${id}`);
  check(!uariaIds.has(id), `uAria plant tab does NOT emit #${id} (crst-only control)`);
}
for (const id of ['cfg-carel-fan-step', 'cfg-carel-gs04', 'cfg-carel-fan-calc', 'cfg-carel-fan-act',
  'cfg-carel-local', 'cfg-carel-season']) {
  check(uariaIds.has(id), `uAria plant tab emits #${id}`);
  check(!crstIds.has(id), `crst plant tab does NOT emit #${id} (uAria-only control)`);
}
check(api.CAREL_SYS_MODES.length === 6 && crstPlantHtml.includes('value="5"'),
  'Sys_Mode select carries all six modes (0..5)');
check(!crstPlantHtml.includes('cfg-carel-io-') && !uariaPlantHtml.includes('cfg-carel-io-'),
  'the plant tab does not claim the I/O ids');

const ioIds = idsIn(api.renderCarelIoTab(CRST_SNAP));
for (const id of ['cfg-carel-io-u', 'cfg-carel-io-no', 'cfg-carel-io-di', 'cfg-carel-io-ao']) {
  check(ioIds.has(id), `I/O tab emits #${id} (the four snapshot tables)`);
}
const ioHtml = api.renderCarelIoTab(CRST_SNAP);
check(ioHtml.includes('B1 · Supply air') && ioHtml.includes('26.5 C'),
  'an analog I/O row prints tag, text and the scaled value');
check(ioHtml.includes('do-state-on'), 'an active discrete row is marked (do-state-on)');

const alarmsIds = idsIn(api.renderCarelAlarmsTab(CRST_SNAP));
check(alarmsIds.has('cfg-carel-alarms') && alarmsIds.has('cfg-carel-alarm-reset-btn'),
  'alarms tab emits the list and the reset button');
check(api.renderCarelAlarmsTab(CRST_SNAP).includes('Нет активных тревог'),
  'an empty alarm list says «Нет активных тревог»');
const twoAlarms = clone(CRST_SNAP);
twoAlarms.carel.alarms = [
  { code: 'E03', text: 'Datchik pomeshcheniya', text_en: 'E03 room probe' },
  { code: 'A12', text: 'Filtr', text_en: 'A12 filter' },
];
const alarmsHtml = api.renderCarelAlarmsTab(twoAlarms);
check(alarmsHtml.includes('E03 — Datchik pomeshcheniya') && alarmsHtml.includes('A12 — Filtr'),
  'each alarm renders as «code — text»');
check(!alarmsHtml.includes('Нет активных тревог'), 'the empty-list line is gone when alarms exist');

const infoIds = idsIn(api.renderCarelInfoTab(CRST_SNAP));
check(infoIds.size === 0, 'the info tab is fully static (identity never patched live)');
const infoHtml = api.renderCarelInfoTab(CRST_SNAP);
check(infoHtml.includes('c.pCOmini') && infoHtml.includes('CRSTDrAHAQ') &&
  infoHtml.includes('2.03.00.46') && infoHtml.includes('Enhanced') && infoHtml.includes('>1<'),
  'info tab shows model, signature, firmware, board variant and address');
const netHtml = api.renderCarelNetworkTab(CRST_SNAP);
check(!netHtml.includes('<input') && !netHtml.includes('<select'),
  'the network tab is read-only — no input writes the line parameters');
check(netHtml.includes('Hd01–Hd03') && netHtml.includes('Sv01–Sv05'),
  'the network tab names the controller keypad menus');

/* ── E. live patch: dirty fields held, rows updated in place ──────────────── */

console.log('E. patchCarelLiveReadouts');
function mountCrst() {
  DOM.clear();
  documentStub.activeElement = null;
  for (const id of ['cfg-carel-status', 'cfg-carel-sat', 'cfg-carel-rwt', 'cfg-carel-oat',
    'cfg-carel-valve', 'cfg-carel-pump', 'cfg-carel-run']) {
    DOM.set(id, new El('dd', { textContent: 'stale' }));
  }
  DOM.set('cfg-carel-state-badge', new El('span', { textContent: 'stale', className: 'badge badge-unk' }));
  for (const id of ['cfg-carel-sp-w', 'cfg-carel-sp-s', 'cfg-carel-fan-sa', 'cfg-carel-fan-ea']) {
    DOM.set(id, new El('input', { type: 'number', value: '0.0' }));
  }
  DOM.set('cfg-carel-sys-mode', new El('select', { type: 'select-one', value: '0' }));
  DOM.set('cfg-carel-ma18', new El('input', { type: 'checkbox', checked: false }));
  DOM.set('cfg-carel-io-u', new El('div', { children: [listRow('stale', 'stale')] }));
  DOM.set('cfg-carel-io-no', new El('div', { children: [listRow('stale', 'stale')] }));
  DOM.set('cfg-carel-io-di', new El('div', { children: [listRow('stale', 'stale')] }));
  DOM.set('cfg-carel-io-ao', new El('div', { children: [listRow('stale', 'stale')] }));
  DOM.set('cfg-carel-alarms', new El('div', { children: [listRow('stale', '')] }));
}

mountCrst();
api.patchCarelLiveReadouts(CRST_SNAP);
eq(DOM.get('cfg-carel-sat').textContent, '26.5 °C', 'supply temperature patched');
eq(DOM.get('cfg-carel-rwt').textContent, '75.1 °C', 'return-water temperature patched');
eq(DOM.get('cfg-carel-valve').textContent, '100.0 %', 'valve percentage patched');
eq(DOM.get('cfg-carel-pump').textContent, 'Вкл', 'pump state patched');
eq(DOM.get('cfg-carel-run').textContent, 'Вкл', 'BMS run state patched');
eq(DOM.get('cfg-carel-status').textContent, 'Rabota', 'unit status text patched');
eq(DOM.get('cfg-carel-state-badge').className, 'badge badge-ok', 'plant_state run → green chip');
eq(DOM.get('cfg-carel-state-badge').textContent, 'Работает', 'plant_state run → «Работает»');
eq(DOM.get('cfg-carel-sp-w').value, '27.2', 'a clean setpoint input takes the live value');
eq(DOM.get('cfg-carel-sys-mode').value, '1', 'a clean select takes the live Sys_Mode');
eq(DOM.get('cfg-carel-ma18').checked, true, 'a clean checkbox takes the live Ma18');

// The two states the operator must never lose: focused, and edited-then-blurred.
mountCrst();
const focused = DOM.get('cfg-carel-sp-w');
focused.value = '30.5';
documentStub.activeElement = focused;
api.patchCarelLiveReadouts(CRST_SNAP);
eq(focused.value, '30.5', 'a FOCUSED setpoint input is NOT overwritten by the 1 s poll');
eq(DOM.get('cfg-carel-sp-s').value, '20.5', 'the other inputs still take the live value');

mountCrst();
const dirty = DOM.get('cfg-carel-sp-s');
dirty.value = '18.0';
dirty.dataset.carelDirty = '1';
api.patchCarelLiveReadouts(CRST_SNAP);
eq(dirty.value, '18.0', 'an EDITED (dirty) setpoint input is NOT overwritten by the 1 s poll');
eq(DOM.get('cfg-carel-sp-w').value, '27.2', 'a clean sibling input is still patched');

mountCrst();
const dirtyBox = DOM.get('cfg-carel-ma18');
dirtyBox.dataset.carelDirty = '1';
api.patchCarelLiveReadouts(CRST_SNAP);
eq(dirtyBox.checked, false, 'a dirty checkbox keeps the operator value');

// I/O rows update in place: the same nodes, innerHTML never reassigned.
mountCrst();
const uHost = DOM.get('cfg-carel-io-u');
const uRow = uHost.children[0];
const noHost = DOM.get('cfg-carel-io-no');
const noRow = noHost.children[0];
api.patchCarelLiveReadouts(CRST_SNAP);
check(uHost.children[0] === uRow, 'the analog I/O row NODE is reused (scroll position survives)');
check(uHost.innerHTML === null, 'the analog I/O list was not rebuilt via innerHTML');
eq(uRow.querySelector('span').textContent, 'B1 · Supply air', 'the I/O row caption is patched in place');
eq(uRow.querySelector('strong').textContent, '26.5 C', 'the I/O row value is patched in place');
eq(noRow.querySelector('strong').textContent, 'Вкл', 'a discrete I/O row prints Вкл');
check(noRow.querySelector('strong').classList.contains('do-state-on'),
  'an active discrete row gets the do-state-on class');
const alarmHost = DOM.get('cfg-carel-alarms');
eq(alarmHost.children[0].querySelector('span').textContent, 'Нет активных тревог',
  'the alarm list falls back to «Нет активных тревог»');

// A row that goes inactive loses the marker (the patch must clear, not only set).
mountCrst();
const offSnap = clone(CRST_SNAP);
offSnap.carel.io_no = [{ tag: 'NO1', text: 'Pump relay', on: false }];
DOM.get('cfg-carel-io-no').children[0].querySelector('strong').className = 'do-state-value do-state-on';
api.patchCarelLiveReadouts(offSnap);
check(!DOM.get('cfg-carel-io-no').children[0].querySelector('strong').classList.contains('do-state-on'),
  'a discrete row that switched off loses do-state-on');

// uAria: its own fields, and the crst-only ids simply absent (null-safe).
function mountUaria() {
  DOM.clear();
  documentStub.activeElement = null;
  for (const id of ['cfg-carel-status', 'cfg-carel-sat', 'cfg-carel-rwt', 'cfg-carel-oat',
    'cfg-carel-valve', 'cfg-carel-pump', 'cfg-carel-run', 'cfg-carel-fan-calc',
    'cfg-carel-fan-act', 'cfg-carel-local', 'cfg-carel-season']) {
    DOM.set(id, new El('dd', { textContent: 'stale' }));
  }
  DOM.set('cfg-carel-state-badge', new El('span', { textContent: 'stale', className: 'badge badge-unk' }));
  DOM.set('cfg-carel-sp-w', new El('input', { type: 'number', value: '0' }));
  DOM.set('cfg-carel-sp-s', new El('input', { type: 'number', value: '0' }));
  DOM.set('cfg-carel-fan-step', new El('input', { type: 'number', value: '0' }));
  DOM.set('cfg-carel-gs04', new El('input', { type: 'checkbox', checked: false }));
}
mountUaria();
api.patchCarelLiveReadouts(UARIA_SNAP);
eq(DOM.get('cfg-carel-sat').textContent, '27.2 °C', 'uAria float32 supply temperature patched');
eq(DOM.get('cfg-carel-fan-step').value, '7', 'uAria fan step patched');
eq(DOM.get('cfg-carel-fan-calc').textContent, '76.6 %', 'uAria calculated fan output patched');
eq(DOM.get('cfg-carel-local').textContent, 'Вкл', 'uAria local-terminal state is shown read-only');
eq(DOM.get('cfg-carel-gs04').checked, true, 'uAria Gs04 checkbox patched');
eq(DOM.get('cfg-carel-run').textContent, 'Вкл', 'uAria network-run state patched');

mountCrst();
const unreadSnap = clone(CRST_SNAP);
delete unreadSnap.carel.sp_w;
api.patchCarelLiveReadouts(unreadSnap);
eq(DOM.get('cfg-carel-sp-w').value, '',
  'an unread setpoint clears the input instead of showing a plausible 0.0');

// State words: alarm beats run, and silence is not «stopped».
mountCrst();
const alarmSnap = clone(CRST_SNAP);
alarmSnap.carel.plant_state = 'alarm';
api.patchCarelLiveReadouts(alarmSnap);
eq(DOM.get('cfg-carel-state-badge').className, 'badge badge-err', 'plant_state alarm → red chip');
eq(DOM.get('cfg-carel-state-badge').textContent, 'Авария', 'plant_state alarm → «Авария»');
mountCrst();
const stopSnap = clone(CRST_SNAP);
stopSnap.carel.plant_state = 'stop';
api.patchCarelLiveReadouts(stopSnap);
eq(DOM.get('cfg-carel-state-badge').textContent, 'Остановлена', 'plant_state stop → «Остановлена»');
mountCrst();
const silentSnap = clone(CRST_SNAP);
silentSnap.carel = { answered: false };
api.patchCarelLiveReadouts(silentSnap);
eq(DOM.get('cfg-carel-state-badge').textContent, 'Нет данных',
  'a PLC that did not answer reads «Нет данных», never «Остановлена»');
eq(DOM.get('cfg-carel-sat').textContent, '—', 'a missing value reads as a dash, not 0.0');

// Values that must never print as a plausible reading.
eq(api.carelValueText(Number.NaN, 1, '°C'), '—', 'float32 NaN (no probe) prints a dash, not 0.0 °C');
eq(api.carelValueText(null, 1, '°C'), '—', 'a missing value prints a dash');
eq(api.carelValueText(26.5, 1, '°C'), '26.5 °C', 'a real value prints with its unit');
eq(api.carelInputValue(null, 1), '', 'an unread setpoint leaves the input EMPTY, never «0.0»');
eq(api.carelInputValue(Number.NaN, 1), '', 'an unreadable setpoint leaves the input empty');
eq(api.carelInputValue(27.2, 1), '27.2', 'a read setpoint fills the input');
eq(api.carelOnOffText(null), '—', 'an unread coil prints a dash, not «Выкл»');
eq(api.carelOnOffText(false), 'Выкл', 'a read-off coil prints «Выкл»');
windowStub.sa02mI18n.lang = 'en';
eq(api.carelAlarmLine({ code: 'E03', text: 'ru', text_en: 'E03 room probe' }), 'E03 — E03 room probe',
  'EN alarm text comes from the map (the i18n observer cannot know device strings)');
windowStub.sa02mI18n.lang = 'ru';
eq(api.carelAlarmLine({ code: 'E03', text: 'ru', text_en: 'E03 room probe' }), 'E03 — ru',
  'RU alarm text is the map text');

/* ── E2. the poll patches in place; only a changed skeleton rebuilds ─────── */

console.log('E2. configBodyIsPatchable');
mountCrst();
DOM.set('flasher-config-body', new El('div', { children: [new El('section')] }));
stateStub.configTab = 'plant';
stateStub.configBodyKey = api.configBodyRenderKey(CRST_SNAP);
check(stateStub.configBodyKey !== '', 'a Carel body has a render key');
check(api.configBodyIsPatchable(CRST_SNAP) === true,
  'an unchanged skeleton is patched in place — the 1 s poll never rebuilds the body');
const oneAlarm = clone(CRST_SNAP);
oneAlarm.carel.alarms = [{ code: 'E03', text: 'x' }];
check(api.configBodyIsPatchable(oneAlarm) === false,
  'a changed row count rebuilds (the new alarm row must appear)');
const noIo = clone(CRST_SNAP);
noIo.carel.io_di = [];
check(api.configBodyIsPatchable(noIo) === false, 'a changed I/O row count rebuilds');
stateStub.configTab = 'alarms';
check(api.configBodyIsPatchable(CRST_SNAP) === false, 'switching tabs rebuilds the body');
stateStub.configTab = 'plant';
check(api.configBodyIsPatchable(UARIA_SNAP) === false,
  'a different family rebuilds (crst and uAria have different controls)');
check(api.configBodyIsPatchable({ kind: 'mr', mr: {} }) === false,
  'a non-Carel snapshot keeps the existing MR render path');
DOM.set('flasher-config-body', new El('div'));
check(api.configBodyIsPatchable(CRST_SNAP) === false, 'an empty body is rendered, not patched');
DOM.delete('flasher-config-body');
check(api.configBodyIsPatchable(CRST_SNAP) === false, 'a missing body host is never patched');

/* ── F. the expensive I/O block survives a poll that did not read it ──────── */

console.log('F. mergeCarelSnapshot');
const withoutIo = clone(CRST_SNAP);
delete withoutIo.carel.io_u;
delete withoutIo.carel.io_no;
delete withoutIo.carel.io_di;
delete withoutIo.carel.io_ao;
const mergedSnap = api.mergeCarelSnapshot(CRST_SNAP, withoutIo);
check(Array.isArray(mergedSnap.carel.io_u) && mergedSnap.carel.io_u.length === 1,
  'a reply without io_* keeps the previous I/O columns (tables do not blink empty)');
eq(mergedSnap.carel.sat, 26.5, 'the fresh live values still win');
const withIo = clone(CRST_SNAP);
withIo.carel.io_u = [];
eq(api.mergeCarelSnapshot(CRST_SNAP, withIo).carel.io_u.length, 0,
  'a reply that DID read io_* wins, even when empty');
const foreign = { kind: 'mr', mr: {} };
check(api.mergeCarelSnapshot(CRST_SNAP, foreign) === foreign, 'a non-Carel snapshot passes through');

/* ── G. i18n: every authored Russian string has a DICT entry ──────────────── */

console.log('G. i18n DICT coverage');
const i18nSrc = readFileSync(I18N_JS, 'utf8');
const dictKeys = new Set();
const keyRe = /^\s*'((?:[^'\\]|\\.)*)':\s*'/gm;
let km;
while ((km = keyRe.exec(i18nSrc))) dictKeys.add(km[1].replace(/\\'/g, "'"));
check(dictKeys.size > 500, `i18n.js DICT parsed (${dictKeys.size} keys) — non-vacuity floor`);

function cyrillicTextNodes(html) {
  const out = new Set();
  String(html).split(/<[^>]*>/).forEach(seg => {
    const text = seg.replace(/\s+/g, ' ').trim();
    if (text && /[А-Яа-яЁё]/.test(text)) out.add(text);
  });
  return out;
}
const rendered = new Set();
[api.renderCarelInfoTab(CRST_SNAP), api.renderCarelNetworkTab(CRST_SNAP),
  api.renderCarelPlantTab(CRST_SNAP), api.renderCarelPlantTab(UARIA_SNAP),
  api.renderCarelIoTab(CRST_SNAP), api.renderCarelAlarmsTab(CRST_SNAP)]
  .forEach(html => cyrillicTextNodes(html).forEach(s => rendered.add(s)));
// Cyrillic literals of the confirm/toast paths (the observer never sees a
// native confirm(), so those strings are wrapped in t() and need DICT rows too).
const literalRe = /'([^'\\\n]*[А-Яа-яЁё][^'\\\n]*)'/g;
['carelStartPlant', 'carelStopPlant', 'carelResetAlarms', 'applyCarelSetpoints', 'wireCarelBodyEvents']
  .forEach(name => {
    const body = extractFn(src, name);
    let m;
    literalRe.lastIndex = 0;
    while ((m = literalRe.exec(body))) rendered.add(m[1].replace(/\s+/g, ' ').trim());
  });
check(rendered.size >= 25, `swept ${rendered.size} authored Russian strings — non-vacuity floor (>=25)`);
const missing = Array.from(rendered).filter(s => !dictKeys.has(s));
for (const s of missing) console.error('  FAIL - no i18n DICT entry for: ' + JSON.stringify(s));
failures += missing.length;
if (!missing.length) console.log(`  ok   - all ${rendered.size} authored Russian strings have an EN translation`);
// The tab labels live in configTabsForSnapshot, not in the tab bodies.
for (const label of tabs.map(t => t.label)) {
  check(dictKeys.has(label), `tab label «${label}» has a DICT entry`);
}

if (failures) {
  console.error('js-unit-carel-window: ' + failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('js-unit-carel-window: ok');
process.exit(0);
