#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-flasher-mtd-window — the MTDx62-MB settings window in «Устройства
   RS-485»: static/js/flasher/mtd.js, the bridge relink
   static/js/flasher/bridge-relink.js, and their seam in flasher.js (plan
   mtd-settings-window S4; contract docs/contracts/mtd-presence.md).

   A) Seam. deviceConfigKindFromSignature answers 'mtd' for the model the
      daemon writes into the scan row — mtd_config.MTD_MODEL PARSED from the
      Python source (comment lines ignored) — case-tolerant, before the MP/MR
      hints, and the row is never a firmware target; MR/DTV/CE/Carel/LED are
      unchanged. Tabs, render key and patchability route to the window. The
      close guard (F4): while a link change runs, Close is disabled, the close
      path refuses with a toast, and the beforeunload listener asks.
   B) The window's names equal their homes: MTD_MODEL, and MTD_TEMPLATE ==
      mqtt_bus_scan.MTD_TEMPLATE (the bridge entry the relink moves).
   C) Rendering over a snapshot built from the SHIPPED template (titles,
      ranges, scales) + holding 11 from mtd_config.py: one input per setting
      with the snapshot's min/max/step; a read-only setting is disabled;
      template missing → banner + no writable input; status text, both
      versions, the reporting warning only when on, holding 12/13 shown
      read-only, distance «—» while nobody is present; parity offers N only.
   D) Markup ⇄ patch: every id the live patch writes exists in the rendered
      markup, values land from a new snapshot, a focused or dirty field
      survives the poll.
   E) Settings: an out-of-range / off-grid / junk value is refused before any
      POST; each dirty field is ONE POST /device_config/holding {port, device,
      reg, value: raw word}; a differing read-back warns; nothing dirty → no
      POST; a daemon refusal reaches the banner.
   F) Link change + relink (F2/F3/F4): a successful reply moves exactly the
      matching bridge entry with ONE GET + ONE POST to mqtt_config.cgi
      (restart:false, capabilities dropped, CSRF header) to the line the daemon
      REPORTS (also a half-applied one); zero or two matches and an unreadable
      config post nothing and say so; a refusal by the daemon writes nothing
      to the bridge and keeps the operator's input; the F2 rule offers a new
      baud only when every other entry on the port already uses it, and a baud
      the rule forbids never reaches the daemon; Close is guarded for the
      whole change.
   G) i18n, as the RUNTIME composes it: every text the scenarios above emit
      (toasts, banners, the persistent note), every rendered text node, the
      window's literals, the template titles and the daemon's status texts and
      refusal messages (PARSED from mtd_config.py / device_config.py and shown
      behind the window's prefixes) come out of the shipped i18n.js t() in
      English — the half i18n-dict-contract cannot see.
   Non-vacuous: a missing source fails, and fewer than 80 checks fail.

   Comment-mutation case (comment-mutation-proof): commenting out
   `MTD_MODEL = "MTDx62-MB"` in mtd_config.py turns A RED — the literal is
   read comment-stripped.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const JS = join(ROOT, 'www', 'network_config', 'static', 'js');
const PATHS = {
  mtd: join(JS, 'flasher', 'mtd.js'),
  relink: join(JS, 'flasher', 'bridge-relink.js'),
  flasher: join(JS, 'flasher.js'),
  i18n: join(JS, 'i18n.js'),
  mtdConfig: join(ROOT, 'opt', 'sa02m-flasher', 'sa02m_flasher', 'mtd_config.py'),
  deviceConfig: join(ROOT, 'opt', 'sa02m-flasher', 'sa02m_flasher', 'device_config.py'),
  scan: join(ROOT, 'opt', 'sa02m-modbus-mqtt', 'mqtt_bus_scan.py'),
  template: join(ROOT, 'opt', 'sa02m-modbus-mqtt', 'templates', 'config-mtdx62-mb.json'),
};

let failures = 0;
let checks = 0;
function check(cond, msg) {
  checks++;
  if (cond) console.log('  ok   ' + msg);
  else { failures++; console.log('  FAIL ' + msg); }
}
const SRC = {};
for (const [k, p] of Object.entries(PATHS)) {
  try { SRC[k] = readFileSync(p, 'utf8'); } catch (err) {
    console.log('  FAIL cannot read ' + p + ': ' + err.message);
    process.exit(1);
  }
}
const CYR = /[Ѐ-ӿ]/;
const pyNoComments = (src) => src.split('\n').filter((l) => !/^\s*#/.test(l)).join('\n');

// ── extractors (brace matching over the shipped flasher.js) ─────────────────
function extractFn(src, name) {
  const m = new RegExp('function\\s+' + name + '\\s*\\(').exec(src);
  if (!m) throw new Error('function ' + name + '() not found');
  const open = src.indexOf('{', m.index);
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(m.index, j + 1);
  }
  throw new Error('unbalanced ' + name);
}
function extractConst(src, name) {
  const m = new RegExp('const\\s+' + name + '\\s*=').exec(src);
  if (!m) throw new Error('const ' + name + ' not found');
  let i = src.indexOf('=', m.index) + 1;
  while (/\s/.test(src[i])) i++;
  const open = src[i];
  const close = open === '[' ? ']' : '}';
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === open) depth++;
    else if (src[j] === close && --depth === 0) return src.slice(m.index, src.indexOf(';', j) + 1);
  }
  throw new Error('unbalanced const ' + name);
}

// ── the homes, parsed ───────────────────────────────────────────────────────
const cfgPy = pyNoComments(SRC.mtdConfig);
const MODEL = (cfgPy.match(/^MTD_MODEL\s*=\s*"([^"]+)"/m) || [])[1];
const SCAN_TEMPLATE = (pyNoComments(SRC.scan).match(/^MTD_TEMPLATE\s*=\s*"([^"]+)"/m) || [])[1];
const statusBlock = cfgPy.match(/STATUS_TEXT\s*=\s*\{([\s\S]*?)\n\}/);
const STATUS = statusBlock ? Object.fromEntries([...statusBlock[1].matchAll(/(\d+):\s*"([^"]+)"/g)].map((m) => [Number(m[1]), m[2]])) : {};
const lightM = cfgPy.match(/"title":\s*"([^"]+)"[^}]*"scale":\s*([\d.]+),\s*"min":\s*([\d.]+),\s*"max":\s*([\d.]+)/);
const TEMPLATE = JSON.parse(SRC.template);
const TPL_HOLD = TEMPLATE.device.channels.filter((c) => c.reg_type === 'holding');

// ── load the two modules and the flasher.js seam ────────────────────────────
const windowStub = {};
let fetchImpl = null;
const csrfMark = { 'X-Test-CSRF': 'token' };
new Function('window', 'fetch', 'withCsrfHeaders', SRC.relink)(
  windowStub, (...a) => fetchImpl(...a), (h) => Object.assign({}, h, csrfMark));
new Function('window', SRC.mtd)(windowStub);
const BR = windowStub.sa02mBridgeRelink;
const MW = windowStub.sa02mMtdWindow;

console.log('A. seam');
check(!!MODEL, `MTD_MODEL parsed from mtd_config.py (${MODEL})`);
const seamState = { configLinkBusy: false, configOpen: true, configBodyKey: '', configTab: 'mtd_info' };
const closeBtn = { disabled: false };
const seamToasts = [];
let mtdApi = null;
const seam = new Function('window', 'state', 'configModalEl', 'toast', 'mtdApiRef', [
  extractConst(SRC.flasher, 'WB_RELAY_SIG_PREFIXES'), extractConst(SRC.flasher, 'WB_MAO4_SIG_PREFIXES'),
  extractConst(SRC.flasher, 'CAREL_SIGNATURE_TOKENS'), extractConst(SRC.flasher, 'LED_SIGNATURE_ALIASES'),
  extractConst(SRC.flasher, 'LED_SIGNATURE_PREFIXES'),
  extractFn(SRC.flasher, 'stripBootloaderSignatureSuffix'), extractFn(SRC.flasher, 'isMpModuleSignatureForFirmwareHint'),
  extractFn(SRC.flasher, 'signatureLooksLikeCarel'), extractFn(SRC.flasher, 'signatureLooksLikeLed'),
  extractFn(SRC.flasher, 'deviceConfigKindFromSignature'), extractFn(SRC.flasher, 'configTabsForSnapshot'),
  extractFn(SRC.flasher, 'ensureConfigCloseEnabled'), extractFn(SRC.flasher, 'setConfigLinkBusy'),
  extractFn(SRC.flasher, 'closeConfigModal'),
  'function mtdWindow() { return mtdApiRef(); }',
  'function ledWindow() { throw new Error("led"); }',
  'let _configPollSeq = 0;',
  'function stopConfigPolling() {} function clearAiConfigEditState() {} function _autoRestorePortForConfig() {}',
  'const document = { body: { style: {} } };',
  'return { deviceConfigKindFromSignature, isMpModuleSignatureForFirmwareHint, configTabsForSnapshot, setConfigLinkBusy, closeConfigModal };',
].join('\n'))(windowStub, seamState, (id) => (id === 'flasher-config-close-btn' ? closeBtn : null),
  (m, k) => seamToasts.push([m, k]), () => mtdApi);
check(seam.deviceConfigKindFromSignature(MODEL) === 'mtd', `'${MODEL}' → 'mtd'`);
check(seam.deviceConfigKindFromSignature(' ' + String(MODEL).toLowerCase() + ' ') === 'mtd', 'case and spaces are tolerated');
check(seam.deviceConfigKindFromSignature('MTDx62') === '', 'a partial model is not an MTD');
check(!seam.isMpModuleSignatureForFirmwareHint(MODEL), 'an MTD row is never a firmware target');
for (const [sig, want] of [['6AI6AO', 'mr'], ['SENSOR', 'dtv'], ['CE02M3', 'ce'], ['CRSTDrAHAQ', 'carel'], ['RGBW_WS2812', 'led'], ['', '']]) {
  check(seam.deviceConfigKindFromSignature(sig) === want, `'${sig}' → '${want}' (unchanged)`);
}
const flasherBody = SRC.flasher;
check(/else if \(snap\.kind === 'mtd'\) html = mtdWindow\(\)\.render\(snap, state\.configTab\);/.test(flasherBody), 'renderConfigBody routes an MTD snapshot to the window before the house network tab');
check(/snap\.kind !== 'mtd'\) \|\| !state\.configBodyKey/.test(flasherBody), 'an MTD body is patchable in place');
check(/kind === 'mtd'\) mtdWindow\(\)\.wire\(body\)/.test(flasherBody), 'the MTD body is wired');
check(/state\.scanJobId \|\| state\.flashJobId \|\| state\.configLinkBusy/.test(flasherBody), 'beforeunload asks while a link change runs (F4)');
{
  seam.setConfigLinkBusy(true);
  check(closeBtn.disabled === true, 'link busy → Close disabled');
  seam.closeConfigModal();
  check(seamState.configOpen === true && seamToasts.length === 1 && seamToasts[0][1] === 'warn',
    'link busy → the close path refuses with a warning');
  seam.setConfigLinkBusy(false);
  check(closeBtn.disabled === false, 'link done → Close enabled');
  seam.closeConfigModal();
  check(seamState.configOpen === false, 'link done → the window closes');
}

{
  // The stub painted on open must not move the device off its scanned line:
  // the MTD branch of configDeviceFromSnapshot follows info.line, and the
  // house line profile for an unknown signature is the MR one (115200) —
  // the first snapshot request would then go out at the wrong baud.
  const dev = { address: 20, baudrate: 19200, parity: 'N', stopbits: 1, signature: MODEL, serial: 0, app_version: '1.1.4' };
  const open = new Function('window', 'dev', [
    extractConst(SRC.flasher, 'WB_RELAY_SIG_PREFIXES'), extractConst(SRC.flasher, 'WB_MAO4_SIG_PREFIXES'),
    extractConst(SRC.flasher, 'CAREL_SIGNATURE_TOKENS'), extractConst(SRC.flasher, 'LED_SIGNATURE_ALIASES'),
    extractConst(SRC.flasher, 'LED_SIGNATURE_PREFIXES'),
    extractFn(SRC.flasher, 'stripBootloaderSignatureSuffix'), extractFn(SRC.flasher, 'isMpModuleSignatureForFirmwareHint'),
    extractFn(SRC.flasher, 'signatureLooksLikeCarel'), extractFn(SRC.flasher, 'signatureLooksLikeLed'),
    extractFn(SRC.flasher, 'deviceConfigKindFromSignature'), extractFn(SRC.flasher, 'buildConfigSnapshotStubFromDevice'),
    extractFn(SRC.flasher, 'configDeviceFromSnapshot'),
    "function resolveApplicationLineProfile() { return { baudrate: 115200, parity: 'N', stopbits: 1 }; }",
    'function currentConfigDevice() { return dev; }',
    'function serialHex(v) { return String(v); }',
    'return { buildConfigSnapshotStubFromDevice, configDeviceFromSnapshot };',
  ].join('\n'))(windowStub, dev);
  const stub = open.buildConfigSnapshotStubFromDevice(dev);
  open.configDeviceFromSnapshot(stub);
  check(stub.kind === 'mtd' && dev.baudrate === 19200 && dev.address === 20,
    `the open stub keeps the scanned line (dev ${dev.address} @${dev.baudrate})`);
}

{
  // A8: a dropped tag makes mtdWindow() throw on double-click — both modules
  // load before flasher.js, each with a ?v= and an &r= cache-bust.
  const html = readFileSync(join(ROOT, 'www', 'network_config', 'index.html'), 'utf8');
  const at = (name) => {
    const m = new RegExp('<script src="static/js/' + name.replace(/[./]/g, (c) => '\\' + c) + '\\?v=[0-9.]+&r=[A-Za-z0-9_-]+"></script>').exec(html);
    return m ? m.index : -1;
  };
  const relinkAt = at('flasher/bridge-relink.js');
  const mtdAt = at('flasher/mtd.js');
  const flasherAt = at('flasher.js');
  check(relinkAt >= 0 && mtdAt >= 0 && flasherAt >= 0, 'index.html loads bridge-relink.js, mtd.js and flasher.js with ?v=&r=');
  check(relinkAt < flasherAt && mtdAt < flasherAt, 'both modules load before flasher.js');
}

console.log('B. names == their homes');
check(!!MW && typeof MW.create === 'function' && !!BR && typeof BR.relink === 'function', 'both modules expose their globals');
check(MW.MTD_MODEL === MODEL, 'the window names the daemon model');
check(!!SCAN_TEMPLATE && MW.MTD_TEMPLATE === SCAN_TEMPLATE, `MTD_TEMPLATE == mqtt_bus_scan.MTD_TEMPLATE (${SCAN_TEMPLATE})`);
check(TPL_HOLD.length === 7 && TPL_HOLD.every((c) => c.readonly === false && 'min' in c && 'max' in c), 'template: 7 writable holdings with ranges (fixture source)');
check(Object.keys(STATUS).length === 7, `status texts parsed from mtd_config.py (${Object.keys(STATUS).length})`);
check(!!lightM, 'holding 11 row parsed from mtd_config.py');

// ── fixtures ────────────────────────────────────────────────────────────────
const RAW = [600, 60, 10, 30, 7, 7, 60, 20, 19200, 0, 2, 0, 600, 1];
function fmt(raw, scale) {
  if (Number(scale) === 1) return String(raw);
  const d = Math.max(0, -Math.floor(Math.log10(scale) + 1e-9));
  return (raw * scale).toFixed(d);
}
function settings(opts) {
  const o = opts || {};
  const rows = TPL_HOLD.map((c) => {
    const scale = c.scale || 1;
    const writable = o.templateOk !== false && !(o.readOnly || []).includes(c.address);
    return { reg: c.address, name: c.name, title: c.title, units: c.units || '', scale, step: scale,
      min: writable ? c.min : null, max: writable ? c.max : null, writable, raw: RAW[c.address], value: fmt(RAW[c.address], scale) };
  });
  const scale = Number(lightM[2]);
  rows.push({ reg: 11, name: 'light_threshold', title: lightM[1], units: 'lux', scale, step: scale,
    min: Number(lightM[3]), max: Number(lightM[4]), writable: o.templateOk !== false, raw: 0, value: '0.0' });
  return rows;
}
function snapshot(over) {
  const o = over || {};
  return {
    kind: 'mtd', bus_mode_supported: false, snapshot_detail: 'full',
    info: { address: o.address || 20, serial: 0, signature: MODEL, model: MODEL, app_version: '1.1.4', radar_version: '0.0.0',
      line: { baudrate: o.baud || 19200, parity: 'N', stopbits: 1 } },
    network: { address: o.address || 20, baudrate: o.baud || 19200, parity: 'N', stopbits: 1, writable: true,
      bauds: [1200, 2400, 4800, 9600, 19200, 38400, 57600], parities: ['N'] },
    mtd: {
      answered: o.answered !== false, template_ok: o.templateOk !== false,
      live: { presence: o.presence ?? 1, illuminance_lux: 347.1, target_distance_m: 1.48, device_status: o.status ?? 1,
        device_status_text: STATUS[o.status ?? 1] },
      settings: settings(o), active_reporting: { raw: o.repOn ? 1 : 2, on: !!o.repOn },
      report_interval_s: 600, mac_code: 1, never_written: [7, 8, 9, 10],
    },
  };
}

// ── a host over a Map-backed DOM filled from the rendered markup ────────────
const unesc = (s) => String(s).replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const attr = (a, n) => { const m = new RegExp('\\s' + n + '="([^"]*)"').exec(a); return m ? unesc(m[1]) : null; };
const flag = (a, n) => new RegExp('\\s' + n + '(?=[\\s/>]|$)').test(a);
function mount(html) {
  const dom = new Map();
  const re = /<(\w+)\b([^>]*)>/g;
  let m;
  while ((m = re.exec(html))) {
    const tag = m[1].toLowerCase();
    const a = ' ' + m[2];
    const id = attr(a, 'id');
    if (!id) continue;
    const e = { id, tagName: tag.toUpperCase(), dataset: {}, hidden: flag(a, 'hidden'), disabled: flag(a, 'disabled'),
      className: attr(a, 'class') || '', attrs: {}, value: '', textContent: '',
      setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } };
    for (const k of ['min', 'max', 'step']) if (attr(a, k) != null) e[k] = attr(a, k);
    if (tag === 'select') {
      const inner = html.slice(m.index, html.indexOf('</select>', m.index));
      e.options = [...inner.matchAll(/<option\b([^>]*)>/g)].map((o) => ({ value: attr(' ' + o[1], 'value'), selected: flag(' ' + o[1], 'selected'), disabled: false, hidden: false }));
      const sel = e.options.find((o) => o.selected) || e.options[0];
      e.value = sel ? sel.value : '';
    } else if (tag === 'input') {
      e.value = attr(a, 'value') == null ? '' : attr(a, 'value');
    } else {
      const close = html.indexOf('</' + tag + '>', m.index);
      e.textContent = unesc(html.slice(m.index + m[0].length, close).replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim());
    }
    dom.set(id, e);
  }
  return dom;
}
const ids = (html) => new Set([...String(html).matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]));
const escapeHtml = new Function(extractFn(SRC.flasher, 'escapeHtml') + '; return escapeHtml;')();

function makeHost(snap, opts) {
  const o = opts || {};
  const tr = o.t || ((s) => String(s));
  const rec = { calls: [], toasts: [], banners: [], applied: [], busy: [], link: [], requested: new Set(), linkAtCall: [] };
  const state = { configSnapshot: snap, configBodyKey: 'x', configNetworkDirty: false };
  const devObj = { address: snap.info.address, baudrate: snap.info.line.baudrate, parity: 'N', stopbits: 1, signature: MODEL };
  let linkBusy = false;
  const h = {
    dom: new Map(), active: null, reply: null, state, rec, devObj, port: 'COM3',
    host: {
      escapeHtml,
      t: tr,
      toast: (m, k) => rec.toasts.push([tr(m), k]),
      configModalEl: (id) => { rec.requested.add(id); return h.dom.get(id) || null; },
      configApi: async (path, body) => {
        rec.calls.push([path, JSON.parse(JSON.stringify(body))]);
        rec.linkAtCall.push(linkBusy);
        return typeof h.reply === 'function' ? h.reply(path, body) : h.reply;
      },
      getState: () => state,
      applyConfigSnapshot: (s) => { rec.applied.push(s); state.configSnapshot = s; },
      setConfigBusy: (on) => rec.busy.push(!!on),
      setConfigBanner: (text, kind) => rec.banners.push([tr(text), kind]),
      setLinkBusy: (on) => { linkBusy = !!on; rec.link.push(linkBusy); },
      currentConfigDevice: () => devObj,
      currentPort: () => h.port,
      invalidatePolls: () => {},
      activeElement: () => h.active,
      refreshSnapshot: () => {},
      bridge: BR,
    },
  };
  h.win = MW.create(h.host);
  h.render = (tab) => { const html = h.win.render(state.configSnapshot, tab); h.dom = mount(html); return html; };
  h.linkBusy = () => linkBusy;
  return h;
}

// ── a fake mqtt_config.cgi behind fetch ─────────────────────────────────────
function bridgeConfig(entries) {
  return { mqtt: { broker: '127.0.0.1' }, devices: entries, capabilities: { transports: ['rtu', 'tcp'] } };
}
const MTD_ENTRY = { id: 'mtdx62-mb-COM3-20', type: 'template', template: 'mtdx62-mb', port: '/dev/COM3', address: 20, baudrate: 19200 };
const MR_ENTRY = { id: 'mr02m-COM3-5', type: 'mr02m', port: '/dev/COM3', address: 5, baudrate: 19200 };
const OTHER_PORT = { id: 'dtv-COM1-2', type: 'dtv', port: '/dev/COM1', address: 2, baudrate: 9600 };
const TCP_ENTRY = { id: 'carel-tcp', type: 'carel', transport: 'tcp', host: '10.0.0.5', tcp_port: 502, address: 1 };
function fakeBridge(cfg, opts) {
  const o = opts || {};
  const log = { gets: 0, posts: [], linkAtFetch: [] };
  fetchImpl = async (url, init) => {
    log.linkAtFetch.push(o.linkBusy ? o.linkBusy() : null);
    if (url !== BR.CONFIG_URL) throw new Error('unexpected url ' + url);
    if (!init || !init.method) {
      log.gets++;
      const body = o.getReply !== undefined ? o.getReply : JSON.parse(JSON.stringify(cfg));
      return { ok: true, status: 200, json: async () => body };
    }
    log.posts.push({ init, body: JSON.parse(init.body) });
    const reply = o.postReply || { ok: true };
    return { ok: true, status: 200, json: async () => reply };
  };
  return log;
}

console.log('C. rendering');
{
  const snap = snapshot();
  const h = makeHost(snap);
  const html = h.render('mtd_settings');
  const inputs = [...html.matchAll(/<input id="cfg-mtd-f-(\d+)"[^>]*min="([^"]+)" max="([^"]+)" step="([^"]+)"/g)];
  check(inputs.length === snap.mtd.settings.length && inputs.length === 8, `one ranged input per snapshot setting (${inputs.length})`);
  const byReg = Object.fromEntries(inputs.map((m) => [m[1], m]));
  const tpl0 = TPL_HOLD.find((c) => c.address === 0);
  check(!!byReg[0] && Number(byReg[0][2]) === tpl0.min && Number(byReg[0][3]) === tpl0.max && Number(byReg[0][4]) === tpl0.scale,
    'HR0 input carries the template range and scale');
  check(!!byReg[11] && Number(byReg[11][2]) === Number(lightM[3]) && Number(byReg[11][3]) === Number(lightM[4]) && Number(byReg[11][4]) === Number(lightM[2]),
    `HR11 input carries mtd_config’s ${lightM[3]}…${lightM[4]} step ${lightM[2]}`);
  check(!/cfg-mtd-f-(7|8|9|10|12|13)"/.test(html), 'holding 7..10, 12, 13 are never inputs');
  const ro = makeHost(snapshot({ readOnly: [4] })).render('mtd_settings');
  check(/<input id="cfg-mtd-f-4"[^>]* disabled/.test(ro) && !/<input id="cfg-mtd-f-4"[^>]*min=/.test(ro), 'a setting without a range renders disabled, no range');
  const none = makeHost(snapshot({ templateOk: false })).render('mtd_settings');
  check(/id="cfg-mtd-tpl-missing"/.test(none) && !/<input id="cfg-mtd-f-\d+"[^>]*min=/.test(none) && /id="cfg-mtd-apply-btn" disabled/.test(none),
    'template missing → banner, no writable input, the button disabled');
  const info = h.render('mtd_info');
  check(/id="cfg-mtd-rep-warn" hidden/.test(info), 'reporting warning hidden while off');
  check(!/id="cfg-mtd-rep-warn" hidden/.test(makeHost(snapshot({ repOn: true })).render('mtd_info')), 'reporting warning shown when on');
  check(/id="cfg-mtd-silent"\s+hidden/.test(info) && !/id="cfg-mtd-silent"\s+hidden/.test(makeHost(snapshot({ answered: false })).render('mtd_info')),
    '«Датчик не отвечает» only when the sensor is silent');
  check(/>1\.1\.4</.test(info) && />0\.0\.0</.test(info), 'both firmware versions shown');
  check(new RegExp('cfg-mtd-status-code">1<').test(info) && info.includes('>' + STATUS[1] + '<'), 'status code + the daemon’s meaning shown');
  check(/cfg-mtd-interval">600</.test(info) && /cfg-mtd-mac">1</.test(info) && !/<input[^>]*cfg-mtd-(interval|mac)/.test(info), 'HR12/HR13 shown read-only (F5)');
  check(/cfg-mtd-dist">1\.48</.test(info) && /cfg-mtd-dist">—</.test(makeHost(snapshot({ presence: 0 })).render('mtd_info')), 'distance «—» while nobody is present');
  const net = h.render('mtd_network');
  const parity = net.match(/<select id="cfg-mtd-net-parity"[\s\S]*?<\/select>/)[0];
  check([...parity.matchAll(/<option /g)].length === 1 && /value="N"/.test(parity), 'parity offers N only (F1)');
  check(/<option value="57600"/.test(net) && !/<option value="115200"/.test(net), 'baud list is the sensor’s (≤57600)');
  check(/<select id="cfg-mtd-net-baud"[^>]* disabled/.test(net), 'baud select starts disabled until the bridge rule is known');
  check(h.win.tabs().map((t) => t.id).join(',') === MW.MTD_TABS.join(','), 'tab ids = MTD_TABS');
  mtdApi = h.win;
  check(seam.configTabsForSnapshot(snap).map((t) => t.id).join(',') === MW.MTD_TABS.join(','), 'flasher.js tabs route to the window');
  check(h.win.renderKey(snap, 'mtd_info') === h.win.renderKey(snapshot({ presence: 0, status: 3 }), 'mtd_info'),
    'live values do not change the render key');
  check(h.win.renderKey(snap, 'mtd_settings') !== h.win.renderKey(snapshot({ readOnly: [4] }), 'mtd_settings'),
    'a writability change re-renders');
}

console.log('D. markup ⇄ patch');
for (const tab of MW.MTD_TABS) {
  const h = makeHost(snapshot());
  const html = h.render(tab);
  h.rec.requested.clear();
  h.win.patch(snapshot({ presence: 0, status: 4, repOn: true }));
  const declared = ids(html);
  const written = [...h.rec.requested].filter((id) => h.dom.has(id));
  check(written.length > 0, `${tab}: the patch writes into the rendered markup (${written.length} ids)`);
}
{
  const h = makeHost(snapshot());
  h.render('mtd_info');
  h.win.patch(snapshot({ presence: 0, status: 4, repOn: true, answered: false }));
  check(h.dom.get('cfg-mtd-presence').textContent === 'Нет' && h.dom.get('cfg-mtd-dist').textContent === '—', 'presence and distance patched');
  check(h.dom.get('cfg-mtd-status-text').textContent === STATUS[4], 'status text patched');
  check(h.dom.get('cfg-mtd-rep-warn').hidden === false && h.dom.get('cfg-mtd-silent').hidden === false, 'warnings un-hidden by the patch');
  const s = makeHost(snapshot());
  s.render('mtd_settings');
  const f4 = s.dom.get('cfg-mtd-f-4');
  const f5 = s.dom.get('cfg-mtd-f-5');
  const f6 = s.dom.get('cfg-mtd-f-6');
  f4.value = '9'; s.active = f4;
  f5.value = '3'; f5.dataset.mtdDirty = '1';
  const next = snapshot();
  next.mtd.settings.forEach((f) => { if (f.reg === 4 || f.reg === 5) f.value = '2'; if (f.reg === 6) f.value = '0.99'; });
  s.win.patch(next);
  check(f4.value === '9' && f5.value === '3', 'a focused and a dirty field survive the poll');
  check(f6.value === '0.99', 'an untouched field takes the polled value');
}

console.log('E. settings writes');
async function settingsWrites() {
  for (const [reg, bad] of [[4, '10'], [4, '5.5'], [0, '10.5'], [0, '0.555'], [3, '4'], [11, 'abc'], [11, '420.05'], [1, '']]) {
    const h = makeHost(snapshot());
    h.render('mtd_settings');
    const input = h.dom.get('cfg-mtd-f-' + reg);
    input.value = bad; input.dataset.mtdDirty = '1';
    await h.win.applySettings();
    check(h.rec.calls.length === 0 && input.attrs['aria-invalid'] === 'true', `HR${reg}=${JSON.stringify(bad)} refused before any POST`);
  }
  {
    const h = makeHost(snapshot());
    h.render('mtd_settings');
    h.dom.get('cfg-mtd-f-0').value = '2,35'; h.dom.get('cfg-mtd-f-0').dataset.mtdDirty = '1';
    h.dom.get('cfg-mtd-f-11').value = '10'; h.dom.get('cfg-mtd-f-11').dataset.mtdDirty = '1';
    h.reply = (path, body) => Object.assign(snapshot(), { write: { reg: body.reg, written: body.value, read_back: body.value, applied: true } });
    await h.win.applySettings();
    check(h.rec.calls.length === 2 && h.rec.calls.every((c) => c[0] === '/device_config/holding'), 'two dirty fields → two POSTs to /device_config/holding');
    const [a, b] = h.rec.calls.map((c) => c[1]);
    check(a.reg === 0 && a.value === 235 && b.reg === 11 && b.value === 100 && a.port === 'COM3' && a.device.signature === MODEL,
      'each body {port, device, reg, value} carries the RAW word (2,35 m → 235; 10 lx → 100)');
    check(!h.dom.get('cfg-mtd-f-0').dataset.mtdDirty && h.rec.applied.length === 1, 'dirty marks cleared, the last reply applied once');
    check(h.rec.toasts.some(([, k]) => k === 'success'), 'success toast');
  }
  {
    const h = makeHost(snapshot());
    h.render('mtd_settings');
    h.dom.get('cfg-mtd-f-4').value = '8'; h.dom.get('cfg-mtd-f-4').dataset.mtdDirty = '1';
    h.reply = () => Object.assign(snapshot(), { write: { reg: 4, written: 8, read_back: 5, applied: false } });
    await h.win.applySettings();
    check(h.rec.toasts.some(([m, k]) => k === 'warn' && /сохранил другое значение — 5$/.test(m)), 'a differing read-back warns with the stored value');
  }
  {
    // A6: a read-back that failed is not «a different value — null».
    const h = makeHost(snapshot());
    h.render('mtd_settings');
    h.dom.get('cfg-mtd-f-4').value = '8'; h.dom.get('cfg-mtd-f-4').dataset.mtdDirty = '1';
    h.reply = () => Object.assign(snapshot(), { write: { reg: 4, written: 8, read_back: null, applied: false } });
    await h.win.applySettings();
    check(h.rec.toasts.some(([m, k]) => k === 'warn' && /чтение после записи не удалось/.test(m)) &&
      !h.rec.toasts.some(([m]) => /null/.test(m)), 'a failed read-back is named, never «— null»');
  }
  {
    const h = makeHost(snapshot());
    h.render('mtd_settings');
    await h.win.applySettings();
    check(h.rec.calls.length === 0, 'nothing dirty → no POST');
  }
  {
    const h = makeHost(snapshot());
    h.render('mtd_settings');
    h.dom.get('cfg-mtd-f-4').value = '5'; h.dom.get('cfg-mtd-f-4').dataset.mtdDirty = '1';
    h.reply = () => { throw new Error('Шаблон датчика не найден — запись недоступна'); };
    await h.win.applySettings();
    check(h.rec.banners.some(([m, k]) => k === 'error' && m === 'Запись настройки: Шаблон датчика не найден — запись недоступна'), 'a daemon refusal reaches the banner');
    check(h.rec.busy[h.rec.busy.length - 1] === false, 'the window is released');
  }
}
await settingsWrites();

console.log('F. link change, relink, F2');
function netReply(device, extra) {
  return Object.assign(snapshot({ address: device.address, baud: device.baudrate }),
    { network_apply: Object.assign({ device, applies_after_restart: false, written: [], error: null }, extra || {}) });
}
async function linkChange(h, addr, baud) {
  h.render('mtd_network');
  h.dom.get('cfg-mtd-net-addr').value = String(addr);
  const sel = h.dom.get('cfg-mtd-net-baud');
  sel.value = String(baud);
  await h.win.saveNetwork();
}
async function relinkCases() {
  {
    const h = makeHost(snapshot());
    const log = fakeBridge(bridgeConfig([MTD_ENTRY, MR_ENTRY, OTHER_PORT, TCP_ENTRY]), { linkBusy: () => h.linkBusy() });
    h.reply = () => netReply({ address: 21, baudrate: 19200, parity: 'N', stopbits: 1 });
    await linkChange(h, 21, 19200);
    check(h.rec.calls.length === 1 && h.rec.calls[0][0] === '/device_config/network', 'one POST to /device_config/network');
    check(JSON.stringify(h.rec.calls[0][1].network) === JSON.stringify({ address: 21, baudrate: 19200, parity: 'N' }), 'network body {address, baudrate, parity N}');
    check(log.gets === 1 && log.posts.length === 1, 'exactly one GET + one POST to mqtt_config.cgi');
    const post = log.posts[0] || { body: {}, init: {} };
    const devs = post.body.devices || [];
    const moved = devs.find((d) => d.id === MTD_ENTRY.id) || {};
    check(moved.address === 21 && moved.baudrate === 19200, 'the matching entry moved to address 21');
    check(JSON.stringify(devs.filter((d) => d.id !== MTD_ENTRY.id)) === JSON.stringify([MR_ENTRY, OTHER_PORT, TCP_ENTRY]), 'every other entry is posted back untouched');
    check(post.body.restart === false && !('capabilities' in post.body) && JSON.stringify(post.body.mqtt) === JSON.stringify({ broker: '127.0.0.1' }),
      'restart:false, capabilities dropped, mqtt block kept');
    check(post.init.headers && post.init.headers['X-Test-CSRF'] === 'token' && post.init.method === 'POST', 'the POST carries the CSRF header');
    check(h.rec.linkAtCall[0] === true && log.linkAtFetch.every((b) => b === true), 'Close is guarded during the daemon call and the relink (F4)');
    check(h.rec.link[h.rec.link.length - 1] === false, 'the guard is released after');
    check(h.dom.get('cfg-mtd-link-note').hidden === false && /mtdx62-mb-COM3-20/.test(h.dom.get('cfg-mtd-link-note').textContent), 'the note names the moved entry');
    check(h.rec.toasts.some(([m, k]) => k === 'success' && /MPLC4\/CODESYS/.test(m)), 'success says to check MPLC4/CODESYS programs');
    check(/19200/.test(h.dom.get('cfg-mtd-net-baud-why').textContent) && log.gets === 1,
      'the F2 rule of the new line comes from the relinked config — no second GET');
  }
  {
    const h = makeHost(snapshot({ baud: 9600 }));
    const log = fakeBridge(bridgeConfig([Object.assign({}, MTD_ENTRY, { baudrate: 9600 }), MR_ENTRY]));
    h.reply = () => netReply({ address: 20, baudrate: 9600, parity: 'N', stopbits: 1 }, { error: 'Датчик не принял рег. 8 = 19200; он отвечает на адресе 20, 9600 N' });
    // address unchanged, baud 9600 → 19200 (F2 allows: the other entry is at 19200), the sensor refused HR8
    await linkChange(h, 21, 19200);
    check(h.rec.banners.some(([m, k]) => k === 'error' && m.startsWith('Сеть применена не полностью: ')), 'a half-applied change is an error banner');
    const moved = ((log.posts[0] || { body: { devices: [] } }).body.devices || []).find((d) => d.id === MTD_ENTRY.id) || {};
    check(log.posts.length === 0 && log.gets >= 1, 'the REPORTED line equals the old one → the bridge entry is not touched');
  }
  {
    const h = makeHost(snapshot());
    const log = fakeBridge(bridgeConfig([MTD_ENTRY, MR_ENTRY]));
    h.reply = () => netReply({ address: 21, baudrate: 19200, parity: 'N', stopbits: 1 }, { error: 'Датчик не принял рег. 8 = 9600; он отвечает на адресе 21, 19200 N' });
    await linkChange(h, 21, 19200);
    const moved = ((log.posts[0] || { body: { devices: [] } }).body.devices || []).find((d) => d.id === MTD_ENTRY.id) || {};
    check(log.posts.length === 1 && moved.address === 21 && moved.baudrate === 19200, 'a partial reply relinks to the line the daemon REPORTS');
  }
  for (const [label, entries, status] of [
    ['zero matches', [MR_ENTRY], 'none'],
    ['two matches', [MTD_ENTRY, Object.assign({}, MTD_ENTRY, { id: 'mtdx62-mb-dup' })], 'ambiguous'],
  ]) {
    const h = makeHost(snapshot());
    const log = fakeBridge(bridgeConfig(entries));
    h.reply = () => netReply({ address: 21, baudrate: 19200, parity: 'N', stopbits: 1 });
    await linkChange(h, 21, 19200);
    const note = h.dom.get('cfg-mtd-link-note');
    check(log.posts.length === 0, `${label}: nothing is posted`);
    check(status === 'none' ? /не настроен/.test(note.textContent) && !/is-error/.test(note.className)
      : /mtdx62-mb-COM3-20, mtdx62-mb-dup/.test(note.textContent) && /is-error/.test(note.className),
    `${label}: the note says so${status === 'ambiguous' ? ', naming both ids' : ''}`);
  }
  {
    const h = makeHost(snapshot());
    const log = fakeBridge(null, { getReply: { error: 'unauthorized' } });
    h.reply = () => netReply({ address: 21, baudrate: 19200, parity: 'N', stopbits: 1 });
    await linkChange(h, 21, 19200);
    const note = h.dom.get('cfg-mtd-link-note');
    check(log.posts.length === 0 && /is-error/.test(note.className) && /адресе 21, а запись MQTT не обновлена/.test(note.textContent),
      'an unreadable config posts nothing back and names the real address');
  }
  {
    const h = makeHost(snapshot());
    const log = fakeBridge(bridgeConfig([MTD_ENTRY]), { postReply: { ok: false, error: 'config_apply_failed' } });
    h.reply = () => netReply({ address: 21, baudrate: 19200, parity: 'N', stopbits: 1 });
    await linkChange(h, 21, 19200);
    check(log.posts.length === 1 && /is-error/.test(h.dom.get('cfg-mtd-link-note').className), 'a refused POST is an error note');
    h.render('mtd_network');
    check(h.dom.get('cfg-mtd-link-note').hidden === false, 'the note survives a re-render until the window closes');
  }
  {
    const h = makeHost(snapshot());
    const log = fakeBridge(bridgeConfig([MTD_ENTRY]));
    const refusal = 'На адресе 21, 19200 N уже отвечает другое устройство — параметры связи не изменены. Выберите свободный адрес.';
    h.reply = () => { throw new Error(refusal); };
    h.state.configNetworkDirty = true;
    await linkChange(h, 21, 19200);
    check(h.rec.banners.some(([m, k]) => k === 'error' && m === 'Сохранение сети: ' + refusal), 'a daemon refusal: banner with its text');
    check(log.gets === 0 && log.posts.length === 0 && h.rec.applied.length === 0, 'a daemon refusal: the bridge is not touched, no snapshot applied');
    check(h.state.configNetworkDirty === true && h.dom.get('cfg-mtd-net-addr').value === '21', 'a daemon refusal keeps the operator’s input');
    check(h.rec.link[h.rec.link.length - 1] === false, 'a daemon refusal releases the guard');
  }
  {
    const h = makeHost(snapshot());
    const log = fakeBridge(bridgeConfig([MTD_ENTRY]));
    h.reply = () => netReply({ address: 20, baudrate: 19200, parity: 'N', stopbits: 1 }, { applies_after_restart: true });
    await linkChange(h, 21, 19200);
    check(log.posts.length === 0 && h.rec.banners.some(([m, k]) => k === 'info' && /после перезапуска питания/.test(m)),
      'deferred: banner says so, the bridge entry stays');
  }
  {
    // A1: the address applied at once, the baud waits for a power cycle — the
    // relink follows the line the sensor answers on NOW, and the banner says
    // what applied, what waits and that the MQTT entry must be fixed later.
    const h = makeHost(snapshot());
    const log = fakeBridge(bridgeConfig([MTD_ENTRY]));
    h.reply = () => netReply({ address: 21, baudrate: 19200, parity: 'N', stopbits: 1 }, { applies_after_restart: true });
    await linkChange(h, 21, 9600);
    const moved = ((log.posts[0] || { body: { devices: [] } }).body.devices || []).find((d) => d.id === MTD_ENTRY.id) || {};
    check(log.posts.length === 1 && moved.address === 21 && moved.baudrate === 19200,
      'deferred + moved: the bridge entry follows the line the sensor answers on now (21 @19200)');
    const banner = (h.rec.banners.find(([, k]) => k === 'info') || [''])[0];
    check(/отвечает на адресе 21, 19200 бод/.test(banner) && /перейдёт на адрес 21, 9600 бод/.test(banner) &&
      /вкладке MQTT/.test(banner) && !/запись MQTT не изменена/.test(banner),
      `deferred + moved: the banner names now / after the power cycle / the MQTT fix (${banner})`);
  }
  {
    // A3: the relink uses the port the daemon call used, even if the port
    // select changed while the call ran.
    const h = makeHost(snapshot());
    const log = fakeBridge(bridgeConfig([MTD_ENTRY, Object.assign({}, MTD_ENTRY, { id: 'mtdx62-mb-COM1-20', port: '/dev/COM1' })]));
    h.reply = () => { h.port = 'COM1'; return netReply({ address: 21, baudrate: 19200, parity: 'N', stopbits: 1 }); };
    await linkChange(h, 21, 19200);
    const devs = (log.posts[0] || { body: { devices: [] } }).body.devices || [];
    const com3 = devs.find((d) => d.id === MTD_ENTRY.id) || {};
    const com1 = devs.find((d) => d.id === 'mtdx62-mb-COM1-20') || {};
    check(log.posts.length === 1 && com3.address === 21 && com1.address === 20,
      'a port change mid-flight: the COM3 entry moves, the COM1 one does not');
  }
  {
    // A4: a failed relink is an error toast, never a success one.
    const h = makeHost(snapshot());
    fakeBridge(bridgeConfig([MTD_ENTRY]), { postReply: { ok: false, error: 'config_apply_failed' } });
    h.reply = () => netReply({ address: 21, baudrate: 19200, parity: 'N', stopbits: 1 });
    await linkChange(h, 21, 19200);
    check(h.rec.toasts.some(([, k]) => k === 'error') && !h.rec.toasts.some(([, k]) => k === 'success'),
      'a failed relink: error toast, no success toast');
  }
  // ── F2 ──
  const self = { address: 20, match: BR.templateMatch('mtdx62-mb') };
  const all = [1200, 2400, 4800, 9600, 19200, 38400, 57600];
  const rule = (entries, cur) => BR.allowedBauds(bridgeConfig(entries), 'COM3', self, cur, all);
  check(JSON.stringify(rule([MTD_ENTRY, OTHER_PORT, TCP_ENTRY], 19200).bauds) === JSON.stringify(all), 'F2: alone on its port → every baud');
  check(JSON.stringify(rule([MTD_ENTRY, MR_ENTRY], 9600).bauds.sort((a, b) => a - b)) === JSON.stringify([9600, 19200]), 'F2: others at 19200 → current + 19200 only');
  check(JSON.stringify(rule([MTD_ENTRY, MR_ENTRY, Object.assign({}, MR_ENTRY, { id: 'x', address: 6, baudrate: 9600 })], 19200).bauds) === JSON.stringify([19200]), 'F2: mixed bauds on the port → current only');
  check(JSON.stringify(rule([MTD_ENTRY, Object.assign({}, MR_ENTRY, { baudrate: undefined })], 19200).bauds) === JSON.stringify([19200]), 'F2: an entry without a baud → current only (fail closed)');
  {
    const h = makeHost(snapshot({ baud: 9600 }));
    fakeBridge(bridgeConfig([Object.assign({}, MTD_ENTRY, { baudrate: 9600 }), MR_ENTRY]));
    h.render('mtd_network');
    await h.win.loadBridgeView();
    const sel = h.dom.get('cfg-mtd-net-baud');
    const offered = sel.options.filter((o) => !o.disabled).map((o) => Number(o.value));
    check(JSON.stringify(offered) === JSON.stringify([9600, 19200]) && sel.disabled === false,
      'F2 in the window: the baud select offers only 9600 (current) and 19200');
    check(/19200/.test(h.dom.get('cfg-mtd-net-baud-why').textContent), 'F2 in the window: the reason names the port baud');
  }
  {
    const h = makeHost(snapshot());
    const log = fakeBridge(bridgeConfig([MTD_ENTRY, MR_ENTRY]));
    h.reply = () => netReply({ address: 20, baudrate: 38400, parity: 'N', stopbits: 1 });
    await linkChange(h, 20, 38400);
    check(h.rec.calls.length === 0 && log.posts.length === 0, 'F2: a forbidden baud never reaches the daemon');
    check(h.rec.banners.some(([m, k]) => k === 'error' && /19200/.test(m)), 'F2: the refusal names the port baud');
  }
}
await relinkCases();

console.log('G. i18n (the runtime composes it)');
{
  const sandbox = {
    localStorage: { getItem: () => null, setItem: () => {} },
    document: { readyState: 'complete', addEventListener: () => {}, documentElement: {}, body: null,
      getElementById: () => null, querySelectorAll: () => [], createTreeWalker: () => ({ nextNode: () => null }) },
    MutationObserver: function () { this.observe = () => {}; this.disconnect = () => {}; },
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3, DOCUMENT_NODE: 9 },
    NodeFilter: { SHOW_TEXT: 4, SHOW_ELEMENT: 1 }, requestAnimationFrame: () => {}, console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC.i18n, sandbox);
  const T = sandbox.sa02mI18n;
  T.setLang('en');
  const t = (s) => T.t(s);
  const missing = [];
  let swept = 0;
  const need = (s, where) => { swept++; if (CYR.test(t(s))) missing.push(`${where}: ${s} → ${t(s)}`); };

  // 1. every text node the three tabs render, over every state
  for (const over of [{}, { repOn: true, answered: false, presence: 0 }, { templateOk: false }, { readOnly: [4] }]) {
    const h = makeHost(snapshot(over));
    for (const tab of MW.MTD_TABS) {
      const html = h.win.render(h.state.configSnapshot, tab);
      for (const m of html.matchAll(/>([^<>]*)</g)) if (CYR.test(m[1])) need(unesc(m[1].trim()), 'markup');
    }
  }
  // 2. labels, units, hints, the window's names, the daemon's status texts, the flasher.js seam literal
  const h0 = makeHost(snapshot());
  [h0.win.title(), h0.win.kicker(), ...h0.win.tabs().map((x) => x.label), ...Object.values(MW.MTD_UNITS),
    ...Object.values(MW.MTD_HINTS), ...Object.values(STATUS), ...TPL_HOLD.map((c) => c.title), lightM[1],
    'Дождитесь окончания смены параметров связи'].forEach((s) => need(s, 'label'));
  // 3. every single-literal message call in mtd.js (comment-stripped)
  const jsNoComments = SRC.mtd.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  for (const m of jsNoComments.matchAll(/(?:host\.toast|host\.setConfigBanner|setNote|host\.t)\(\s*'((?:\\.|[^'\\])*)'\s*(\+)?/g)) {
    if (!CYR.test(m[1])) continue;
    // A prefix ('…: ' + tail) is probed with a tail; a literal continued into
    // a sentence ('… ' + n + ', …') is checked whole by the emitted sweep (4.).
    if (m[2] && !/: $/.test(m[1])) continue;
    need(m[2] ? m[1] + 'Timeout' : m[1], 'literal');
  }
  for (const m of jsNoComments.matchAll(/(?:return|text =)\s*'((?:\\.|[^'\\])*[Ѐ-ӿ](?:\\.|[^'\\])*)'\s*;/g)) need(m[1], 'literal');
  for (const m of jsNoComments.matchAll(/error:\s*'((?:\\.|[^'\\])*[Ѐ-ӿ](?:\\.|[^'\\])*)'/g)) need(m[1], 'literal');
  // 4. every message the scenarios emit, through a host whose t() is the runtime (EN)
  const daemonMsgs = [];
  // Each format filled the way the daemon fills it: a line (_line_text) after
  // «на», the range bounds, a Modbus error after «рег. N:», the baud list.
  const sample = (fmt) => fmt
    .replace(/(рег\. %d): %s/, '$1: Таймаут')
    .replace(/%d/g, '7')
    .replace(/(^| )([Нн]и на|[Нн]а) %s/g, '$1$2 адресе 20, 9600 N')
    .replace(/%s…%s/, '0.6…10')
    .replace(/только %s/, 'только 1200, 2400, 4800, 9600, 19200, 38400, 57600')
    .replace(/%s/g, '20');
  for (const src of [cfgPy, pyNoComments(SRC.deviceConfig)]) {
    for (const m of src.matchAll(/raise (?:ValueError|RuntimeError)\(\s*"((?:[^"\\]|\\.)*)"\s*(?:"((?:[^"\\]|\\.)*)")?/g)) {
      const fmt = m[1] + (m[2] || '');
      if (CYR.test(fmt) && (src === cfgPy || /MTD|датчик/i.test(fmt))) daemonMsgs.push(sample(fmt));
    }
  }
  const nv = /_MTD_NOT_VERIFIED = \(\s*"([^"]+)"/.exec(SRC.deviceConfig);
  if (nv) daemonMsgs.push(nv[1]);
  const refusedFmt = /"(Датчик не принял рег\. %d = %d; он отвечает на %s)"/.exec(cfgPy);
  if (refusedFmt) daemonMsgs.push(sample(refusedFmt[1]));
  check(daemonMsgs.length >= 12, `${daemonMsgs.length} daemon messages parsed from mtd_config.py / device_config.py`);
  const emitted = [];
  for (const msg of daemonMsgs) {
    for (const path of ['settings', 'network']) {
      const h = makeHost(snapshot(), { t });
      fakeBridge(bridgeConfig([MTD_ENTRY]));
      h.reply = () => { throw new Error(msg); };
      if (path === 'settings') {
        h.render('mtd_settings');
        h.dom.get('cfg-mtd-f-4').value = '5'; h.dom.get('cfg-mtd-f-4').dataset.mtdDirty = '1';
        await h.win.applySettings();
      } else {
        await linkChange(h, 21, 19200);
      }
      h.rec.toasts.concat(h.rec.banners).forEach(([m]) => emitted.push(m));
    }
  }
  const scen = [
    [bridgeConfig([MTD_ENTRY, MR_ENTRY]), null, {}],
    [bridgeConfig([MR_ENTRY]), null, {}],
    [bridgeConfig([MTD_ENTRY, Object.assign({}, MTD_ENTRY, { id: 'dup' })]), null, {}],
    [null, { error: 'unauthorized' }, {}],
    [bridgeConfig([MTD_ENTRY]), null, { error: refusedFmt ? sample(refusedFmt[1]) : 'x' }],
    [bridgeConfig([MTD_ENTRY]), null, { applies_after_restart: true }],
    [null, { error: 'unauthorized' }, { applies_after_restart: true }],
  ];
  for (const [cfg, getReply, extra] of scen) {
    const h = makeHost(snapshot(), { t });
    fakeBridge(cfg, getReply ? { getReply } : {});
    h.reply = () => netReply({ address: 21, baudrate: 19200, parity: 'N', stopbits: 1 }, extra);
    await linkChange(h, 21, 19200);
    h.rec.toasts.concat(h.rec.banners).forEach(([m]) => emitted.push(m));
    emitted.push(h.dom.get('cfg-mtd-link-note').textContent);
  }
  for (const over of [{ baud: 9600 }]) {
    const h = makeHost(snapshot(over), { t });
    fakeBridge(bridgeConfig([MTD_ENTRY, MR_ENTRY, Object.assign({}, MR_ENTRY, { id: 'y', address: 7, baudrate: 38400 })]));
    await linkChange(h, 20, 19200);
    h.rec.banners.forEach(([m]) => emitted.push(m));
    emitted.push(h.dom.get('cfg-mtd-net-baud-why').textContent);
  }
  {
    const h = makeHost(snapshot(), { t });
    h.render('mtd_settings');
    h.dom.get('cfg-mtd-f-4').value = '8'; h.dom.get('cfg-mtd-f-4').dataset.mtdDirty = '1';
    h.reply = () => Object.assign(snapshot(), { write: { reg: 4, written: 8, read_back: 5, applied: false } });
    await h.win.applySettings();
    h.dom.get('cfg-mtd-f-0').value = '99'; h.dom.get('cfg-mtd-f-0').dataset.mtdDirty = '1';
    await h.win.applySettings();
    h.dom.get('cfg-mtd-f-4').value = '6'; h.dom.get('cfg-mtd-f-4').dataset.mtdDirty = '1';
    h.reply = () => Object.assign(snapshot(), { write: { reg: 4, written: 6, read_back: null, applied: false } });
    await h.win.applySettings();
    h.rec.toasts.forEach(([m]) => emitted.push(m));
  }
  const nonEmpty = emitted.filter((m) => String(m).trim());
  check(nonEmpty.length >= 40, `${nonEmpty.length} emitted messages captured`);
  nonEmpty.forEach((m) => { swept++; if (CYR.test(m)) missing.push('emitted: ' + m); });
  check(swept >= 150, `${swept} strings swept`);
  check(missing.length === 0, `every one comes out English (missing ${missing.length}):\n      ${[...new Set(missing)].join('\n      ')}`);
}

console.log(`\n${checks - failures}/${checks} checks passed`);
if (checks < 80) { console.log('FAIL: fewer than 80 checks ran — the harness stopped measuring'); process.exit(1); }
process.exit(failures === 0 ? 0 : 1);
