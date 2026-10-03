#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it imports the shipped pure module and asserts its outputs against oracles parsed from their homes, pinning no source line by text; a commented-out line changes the results it measures.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-mqtt-tcp-dialog — the add-device dialog's Modbus TCP decisions.

   Under test: www/network_config/static/js/mqtt/tcp-dialog.js, the pure ES
   module mqtt.js imports (1.0.6.67, backlog A3). It is imported directly
   (`await import(pathToFileURL(...))`) — no vm, no brace extraction, no DOM:
   the module touches no document/window by design, and this test pins that.

   ORACLES ARE PARSED FROM THEIR HOMES, never re-typed here:
     * the refusal codes — `REASONS` in opt/sa02m-modbus-mqtt/bridge_bus.py
       (the board's validator). The wording table must have EXACTLY those keys:
       a new server code without wording, or wording for a dropped code, FAILS;
     * the probe verdict enum — the `verdict` table of
       docs/contracts/bridge-modbus-tcp.md §10. Every verdict needs a view;
     * the «Тип устройства» option list — the `#mqtt-add-type` <select> in
       index.html, so type narrowing is judged over what the dialog really has.
   NON-VACUOUS: fewer than 11 REASONS codes, fewer than 8 verdicts, or fewer
   than 6 dialog options parsed FAILS (a parser that stopped matching is not a
   pass).

   WHAT IT DOES NOT COVER: the DOM glue in mqtt.js (row toggling, the probe
   button, clearing on edit) — that layer is the Reviewer's headless recipe and
   sh-modal-layout-smoke. The EN wording itself is i18n-dict-contract's (the
   module names its translator `uiT` so that gate sweeps every literal).

   PROVEN RED (1.0.6.67): with the module absent the import FAILS; dropping the
   `carel` exclusion in typeOptionAllowed FAILS the RS-485 narrowing case;
   deleting the `timeout_invalid` wording FAILS the REASONS parity case; mapping
   unit_silent to state 'ok' FAILS the «only a live unit reads ok» case.

   Run: node scripts/dev/test-mqtt-tcp-dialog.mjs
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const MODULE = process.env.MQTT_TCP_DIALOG_SRC
  || join(ROOT, 'www', 'network_config', 'static', 'js', 'mqtt', 'tcp-dialog.js');
const BRIDGE_BUS = join(ROOT, 'opt', 'sa02m-modbus-mqtt', 'bridge_bus.py');
const CONTRACT = join(ROOT, 'docs', 'contracts', 'bridge-modbus-tcp.md');
const INDEX_HTML = join(ROOT, 'www', 'network_config', 'index.html');
const MQTT_JS = join(ROOT, 'www', 'network_config', 'static', 'js', 'mqtt.js');

let fails = 0;
let passes = 0;
const ok = m => { passes++; console.log(`mqtt-tcp-dialog: ok    ${m}`); };
const bad = m => { fails++; console.log(`mqtt-tcp-dialog: FAIL  ${m}`); };
const check = (cond, m, detail) => (cond ? ok(m) : bad(detail ? `${m} — ${detail}` : m));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

for (const f of [MODULE, BRIDGE_BUS, CONTRACT, INDEX_HTML, MQTT_JS]) {
  if (!existsSync(f)) {
    console.log(`mqtt-tcp-dialog: FAIL  not found: ${f}`);
    process.exit(1);
  }
}
let M;
try {
  M = await import(pathToFileURL(MODULE).href);
} catch (e) {
  console.log(`mqtt-tcp-dialog: FAIL  the module does not import: ${e.message}`);
  process.exit(1);
}

// A recording translator: every wording must pass through uiT (else it would
// never be translated), and the marker makes a bypass visible in the text.
const seen = new Set();
const uiT = s => { seen.add(s); return `‹${s}›`; };

// ── oracles ────────────────────────────────────────────────────────────────
const busSrc = readFileSync(BRIDGE_BUS, 'utf8');
const reasonsBlock = (busSrc.match(/^REASONS\s*=\s*\(([\s\S]*?)^\)/m) || [])[1] || '';
const REASONS = [...reasonsBlock.matchAll(/"([a-z0-9_]+)"/g)].map(m => m[1]);

const contract = readFileSync(CONTRACT, 'utf8');
const sec10 = (contract.match(/^## 10\.[\s\S]*?(?=^## )/m) || [''])[0];
const verdictTable = (sec10.match(/^\| `verdict` \|.*\n\|[-| ]+\|\n((?:\|.*\n)+)/m) || [])[1] || '';
const VERDICTS = [...verdictTable.matchAll(/^\| `([a-z_]+)` \|/gm)].map(m => m[1]);

const html = readFileSync(INDEX_HTML, 'utf8');
const typeSelect = (html.match(/<select id="mqtt-add-type"[\s\S]*?<\/select>/) || [''])[0];
const OPTIONS = [...typeSelect.matchAll(/<option value="([^"]+)"/g)].map(m => m[1]);

check(REASONS.length >= 11, `oracle: ${REASONS.length} refusal codes parsed from bridge_bus.REASONS (floor 11)`);
check(VERDICTS.length >= 8, `oracle: ${VERDICTS.length} verdicts parsed from contract §10 (floor 8)`, VERDICTS.join(','));
check(OPTIONS.length >= 6 && OPTIONS.includes('carel') && OPTIONS.includes('template'),
  `oracle: ${OPTIONS.length} «Тип устройства» options parsed from index.html (floor 6, incl. template + carel)`);

// ── purity + one home ──────────────────────────────────────────────────────
const modSrc = readFileSync(MODULE, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
check(!/\b(document|window|localStorage|fetch)\b/.test(modSrc),
  'the module is pure: no document / window / localStorage / fetch outside comments');
const mqttSrc = readFileSync(MQTT_JS, 'utf8');
check(!/const TCP_REFUSAL_TEXT\b/.test(mqttSrc) && /from '\.\/mqtt\/tcp-dialog\.js\?v=/.test(mqttSrc),
  'one home: mqtt.js imports the module (with ?v=) and no longer carries its own TCP_REFUSAL_TEXT');

// ── 1. device id ───────────────────────────────────────────────────────────
check(M.makeTcpDeviceId('template', 'mp02-ahu', '192.168.1.20', 1) === 'mp02-ahu-tcp-192_168_1_20-1',
  'id: template → <template>-tcp-<a_b_c_d>-<addr>');
check(M.makeTcpDeviceId('carel', 'mp02-ahu', '192.168.1.50', 7) === 'carel-tcp-192_168_1_50-7',
  'id: Carel → carel-tcp-… whatever template is picked (the «Устройства»/Alice prefix)');
check(M.makeTcpDeviceId('spodes', 'mp02-ahu', '192.168.1.50', 1) === 'spodes-tcp-192_168_1_50-1',
  'id: SPODES → spodes-tcp-… (not the picked template prefix)');
check(M.makeTcpDeviceId('template', 'tmpl', '  10.0.0.5 ', 255) === 'tmpl-tcp-10_0_0_5-255',
  'id: host trimmed, every dot → underscore');
check(M.makeTcpDeviceId('template', 'tmpl', '', 1) === 'tmpl-tcp--1',
  'id: an empty host still yields a stable id (the save refuses it, not the id)');

// ── 2. type narrowing over the dialog's real option list ───────────────────
const tcpTypes = ['template', 'carel'];
const overTcp = OPTIONS.filter(v => M.typeOptionAllowed(v, true, tcpTypes));
const overRtu = OPTIONS.filter(v => M.typeOptionAllowed(v, false, tcpTypes));
check(same(overTcp, ['template', 'carel']), 'types over TCP: exactly the advertised tcp_types', overTcp.join(','));
check(same(overRtu, OPTIONS.filter(v => v !== 'carel')), 'types over RS-485: everything but Carel', overRtu.join(','));
check(OPTIONS.every(v => !M.typeOptionAllowed(v, true, null)), 'types over TCP with no capabilities: none');

// ── 3. the row matrix ──────────────────────────────────────────────────────
const rowsWant = {
  'rtu/mr02m': { transport: true, port: true, host: false, tcpPort: false, carelFamily: false, template: false, probe: false },
  'rtu/template': { transport: true, port: true, host: false, tcpPort: false, carelFamily: false, template: true, probe: false },
  'rtu/carel': { transport: true, port: true, host: false, tcpPort: false, carelFamily: false, template: false, probe: false },
  'tcp/mr02m': { transport: true, port: false, host: true, tcpPort: true, carelFamily: false, template: false, probe: true },
  'tcp/template': { transport: true, port: false, host: true, tcpPort: true, carelFamily: false, template: true, probe: true },
  'tcp/carel': { transport: true, port: false, host: true, tcpPort: true, carelFamily: true, template: false, probe: true },
};
for (const [k, want] of Object.entries(rowsWant)) {
  const [tr, type] = k.split('/');
  const got = M.dialogRows(tr === 'tcp', type, true);
  check(same(got, want), `rows ${k}`, JSON.stringify(got));
}
check(same(M.dialogRows(true, 'template', false),
  { transport: false, port: true, host: false, tcpPort: false, carelFamily: false, template: true, probe: false }),
  'rows: no capabilities → RS-485 layout, no transport row, no probe row, even if tcp was left selected');
check(M.addrMax(true) === 255 && M.addrMax(false) === 247, 'address max: 255 over TCP, 247 on RS-485');

// ── 4. the one entry shape (save and probe) ────────────────────────────────
const tpl = M.buildTcpEntry({ id: 'mp02-ahu-tcp-192_168_1_20-1', type: 'template', template: 'mp02-ahu',
  family: 'crst', host: ' 192.168.1.20 ', tcpPort: '502', address: 1, name: '' });
check(same(Object.keys(tpl), ['id', 'type', 'template', 'transport', 'host', 'tcp_port', 'address', 'name', 'poll_s']),
  'entry (template): YAML key order, no family, no port/baudrate', Object.keys(tpl).join(','));
check(tpl.tcp_port === 502 && tpl.address === 1 && tpl.host === '192.168.1.20' && tpl.poll_s === 2
  && tpl.name === tpl.id && tpl.transport === 'tcp',
  'entry (template): port is a number, host trimmed, poll_s 2, default name = id', JSON.stringify(tpl));
const car = M.buildTcpEntry({ id: 'carel-tcp-192_168_1_50-7', type: 'carel', template: 'mp02-ahu',
  family: 'uaria', host: '192.168.1.50', tcpPort: '', address: 7, name: '' });
check(same(Object.keys(car), ['id', 'type', 'family', 'transport', 'host', 'tcp_port', 'address', 'name', 'poll_s'])
  && car.tcp_port === 502 && car.name === 'Carel uAria (192.168.1.50:502 addr=7)',
  'entry (Carel): family, no template, empty port → 502, default name names family and endpoint', JSON.stringify(car));
check(M.buildTcpEntry({ id: 'x', type: 'carel', family: 'crst', host: 'h', tcpPort: 8502, address: 2, name: 'Щит' }).name === 'Щит',
  'entry: a typed name wins over the default');

// ── 5. refusal wording = the bridge's codes, exactly ───────────────────────
const keys = Object.keys(M.TCP_REFUSAL_TEXT).sort();
check(same(keys, [...REASONS].sort()), 'refusal wording keys == bridge_bus.REASONS',
  `missing: ${REASONS.filter(r => !keys.includes(r)).join(',') || '-'}; extra: ${keys.filter(k => !REASONS.includes(k)).join(',') || '-'}`);
for (const code of REASONS) {
  seen.clear();
  const text = M.saveRefusalText({ ok: false, error: 'invalid_device', reason: code }, uiT);
  if (!(text.startsWith('‹') && seen.size === 1)) bad(`refusal ${code}: wording missing or not through uiT («${text}»)`);
}
ok(`refusal: every one of ${REASONS.length} codes has wording, through uiT`);
check(M.saveRefusalText({ ok: false, error: 'invalid_device', reason: 'host_forbidden', id: 'd1' }, uiT).endsWith(' (d1)'),
  'refusal: a named entry id is appended');
check(M.saveRefusalText({ ok: false, error: 'invalid_device', reason: 'no_such_code' }, uiT) === ''
  && M.saveRefusalText({ ok: false, error: 'network' }, uiT) === '' && M.saveRefusalText(null, uiT) === '',
  'refusal: an unknown code / non-refusal / no reply → "" (caller shows its generic error)');
check(M.saveRefusalText({ ok: false, error: 'transport_validator_unavailable' }, uiT).startsWith('‹'),
  'refusal: validator unavailable has its own wording');

// ── 6. probe verdicts: every contract verdict has a view; only a live unit is ok ──
const reply = v => ({ ok: true, verdict: v, host: '192.168.1.20', tcp_port: 5020, address: 200, exception: null, elapsed_ms: 3 });
const missingViews = VERDICTS.filter(v => !Object.prototype.hasOwnProperty.call(M.PROBE_VERDICT_VIEW, v));
const extraViews = Object.keys(M.PROBE_VERDICT_VIEW).filter(v => !VERDICTS.includes(v));
check(!missingViews.length && !extraViews.length, 'verdict views == contract §10 verdict enum',
  `missing: ${missingViews.join(',') || '-'}; extra: ${extraViews.join(',') || '-'}`);
for (const v of VERDICTS) {
  const view = M.probeResultView(reply(v), uiT);
  const live = v === 'device_ok' || v === 'device_exception';
  if ((view.state === 'ok') !== live) bad(`verdict ${v}: state '${view.state}' — only device_ok/device_exception may read ok`);
  if (!view.text || !view.text.includes('‹')) bad(`verdict ${v}: text not through uiT («${view.text}»)`);
}
ok('verdicts: only device_ok / device_exception read ok; every text goes through uiT');
check(M.probeResultView(reply('device_ok'), uiT).text.includes('200'), 'device_ok names the address');
check(M.probeResultView(reply('unit_silent'), uiT).text.includes(' 200 '), 'unit_silent names the silent address');
check(M.probeResultView(reply('tcp_refused'), uiT).text.endsWith(' 5020'), 'tcp_refused names the port');
check(M.probeResultView(reply('from_a_newer_board'), uiT).state === 'err'
  && M.probeResultView(reply('toString'), uiT).state === 'err',
  'an unknown verdict (incl. an Object.prototype name) is never ok');

// ── 7. probe errors ────────────────────────────────────────────────────────
const refused = { ok: false, error: 'invalid_device', reason: 'host_forbidden' };
check(same(M.probeResultView(refused, uiT), { state: 'warn', text: M.saveRefusalText(refused, uiT) }),
  'invalid_device: the SAME words as a refused save');
check(M.probeResultView({ ok: false, error: 'probe_busy' }, uiT).state === 'warn', 'probe_busy: a warning, not a failure');
check(M.probeResultView({ ok: false, error: 'probe_unavailable' }, uiT).state === 'unavailable'
  && M.probeResultView({ ok: false, error: 'HTTP 404' }, uiT).state === 'unavailable',
  'probe_unavailable and an old board (nginx 404): «недоступна на этой плате»');
for (const e of ['probe_timeout', 'network', 'csrf', 'unauthorized', 'body_too_large', 'probe_failed']) {
  const v = M.probeResultView({ ok: false, error: e }, uiT);
  if (v.state !== 'err' || !v.text.endsWith(`: ${e}`)) bad(`error ${e}: expected a named failure, got ${JSON.stringify(v)}`);
}
ok('errors: timeout / network / csrf / auth / body / probe_failed → a named failure');
check(M.probeResultView(null, uiT).state === 'err' && M.probeResultView('x', uiT).state === 'err',
  'no reply / a non-object reply → a named failure');

// ── 8. A7: timeout disagreement on one endpoint ────────────────────────────
check(same(M.tcpTimeoutConflicts([]), []), 'timeout conflicts: none on an empty list');
check(same(M.tcpTimeoutConflicts([
  { id: 'a', transport: 'tcp', host: '10.0.0.1', tcp_port: 502 },
  { id: 'b', transport: 'tcp', host: '10.0.0.1', tcp_port: 502, tcp_timeout_s: 1.0 },
  { id: 'c', transport: 'tcp', host: '10.0.0.2', tcp_port: 502, tcp_timeout_s: 3 },
  { id: 'd', port: '/dev/COM1', tcp_timeout_s: 9 },
]), []), 'timeout conflicts: default vs explicit 1.0 agree; other endpoints and RS-485 entries are not compared');
check(same(M.tcpTimeoutConflicts([
  { id: 'a', transport: 'tcp', host: '10.0.0.1' },
  { id: 'b', transport: 'tcp', host: '10.0.0.1', tcp_port: '502', tcp_timeout_s: 2.5 },
  { id: 'c', transport: 'tcp', host: '10.0.0.1', tcp_port: 503, tcp_timeout_s: 2.5 },
]), [{ endpoint: '10.0.0.1:502', timeouts: [1, 2.5], ids: ['a', 'b'] }]),
  'timeout conflicts: one endpoint, two timeouts → one conflict naming both and the ids (absent port = 502)');

console.log('');
if (fails === 0) {
  console.log(`mqtt-tcp-dialog: ALL OK - ${passes} case(s) green`);
  process.exit(0);
}
console.log(`mqtt-tcp-dialog: ${fails} FAILURE(S)`);
process.exit(1);
