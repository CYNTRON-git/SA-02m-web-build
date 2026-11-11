#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-flasher-flash-failure — flasher.js after the bench FL findings
   (1.0.7.36, 2026-11-11, .ai-dev/audit/20261111-FL.md):

     FL-01  an MR on a 19200 line was flashed at 115200 and never answered:
            the target's application line is the line the SCAN found the device
            on; the 115200 profile is only the fallback when the scan gave none.
     FL-02  a failed flash ended `done` and the tab showed the green
            «Прошивка завершена»: the tab now reads the job's failure text
            (job.error, or params.errors of an older daemon) and shows an error
            naming the target; a clean job is still a success; a batch stops at
            the first failed target (`skip_on_error: false` is sent).

   The functions under test are extracted from the SHIPPED flasher.js by
   brace-matching (same method as test-flasher-signature-hints.mjs) and run in a
   sandbox with recording stubs for the DOM/network helpers.
   Run: node scripts/dev/test-flasher-flash-failure.mjs
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'flasher.js'), 'utf8');

function extractFn(name) {
  let start = SRC.indexOf('async function ' + name + '(');
  if (start < 0) start = SRC.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('function ' + name + '() not found in flasher.js');
  const open = SRC.indexOf('{', SRC.indexOf(')', start));
  let depth = 0;
  for (let j = open; j < SRC.length; j++) {
    if (SRC[j] === '{') depth++;
    else if (SRC[j] === '}' && --depth === 0) return SRC.slice(start, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name + '()');
}
function extractConst(name) {
  const start = SRC.indexOf('const ' + name);
  if (start < 0) throw new Error('const ' + name + ' not found in flasher.js');
  let i = SRC.indexOf('=', start) + 1;
  while (/\s/.test(SRC[i])) i++;
  const open = SRC[i], close = open === '[' ? ']' : '}';
  let depth = 0;
  for (let j = i; j < SRC.length; j++) {
    if (SRC[j] === open) depth++;
    else if (SRC[j] === close && --depth === 0) return SRC.slice(start, SRC.indexOf(';', j) + 1);
  }
  throw new Error('unbalanced brackets extracting const ' + name);
}

let failures = 0;
function check(cond, msg) {
  if (cond) console.log('  ok   - ' + msg);
  else { failures++; console.error('  FAIL - ' + msg); }
}

// ── FL-01: the application line of a flash target ─────────────────────────
const lineParts = [
  "const state = {devices: [], latestByDevice: {}};",
  extractConst('LINE_PROFILE_MP_MR'), extractConst('LINE_PROFILE_WB_APP'),
  extractConst('WB_RELAY_SIG_PREFIXES'), extractConst('WB_MAO4_SIG_PREFIXES'),
  extractConst('SIGNATURE_IO_HINTS'), extractConst('FAMILY_TO_PRODUCT'),
  ...['normalizeProductToken', 'normalizeModuleSignature', 'stripBootloaderSignatureSuffix', 'firmwareIdentityMissing', 'isMpModuleSignatureForFirmwareHint',
    'isWirenboardModuleSignature', 'resolveDeviceFlashRoute', 'lineProfileFromScan',
    'resolveApplicationLineProfile', 'buildFlashTargetFromDevice'].map(extractFn),
  'return { resolveApplicationLineProfile, buildFlashTargetFromDevice };',
];
const { resolveApplicationLineProfile, buildFlashTargetFromDevice } = new Function(lineParts.join('\n'))();

const line = (p) => `${p.baudrate} ${p.parity}${p.stopbits}`;
const bench = { address: 5, serial: 0x0E0A7530, signature: '14DI', in_bootloader: false,
  baudrate: 19200, parity: 'N', stopbits: 1 };
check(line(resolveApplicationLineProfile('14DI', bench, false)) === '19200 N1',
  'FL-01: an MR found by the scan at 19200 N1 is flashed on 19200 N1, not the 115200 constant');
check(line(resolveApplicationLineProfile('14DI', { signature: '14DI' }, false)) === '115200 N1',
  'no scan line -> the 115200 N1 profile (unchanged fallback)');
check(line(resolveApplicationLineProfile('14DI', { ...bench, baudrate: 115200 }, false)) === '115200 N1',
  'an MR scanned at 115200 stays on 115200 N1');
check(line(resolveApplicationLineProfile('14DI', { ...bench, app_line_baud: 9600, app_line_parity: 'E', app_line_stopbits: 2 }, false)) === '9600 E2',
  'an explicit app_line_* of the target still wins over the scan');
check(line(resolveApplicationLineProfile('mr6c_v2', { baudrate: 38400, parity: 'E', stopbits: 1 }, true)) === '38400 E1',
  'Wiren Board .wbfw keeps using the scan line');
const target = buildFlashTargetFromDevice(bench);
check(target.baudrate === 19200 && target.app_line_baud === 19200 && target.app_line_parity === 'N' && target.app_line_stopbits === 1,
  'the flash target carries the scan line in baudrate AND app_line_* (the daemon reads app_line_*)');

// ── FL-02: the end of a flash job ─────────────────────────────────────────
function makeEnd(snapshot) {
  const rec = { toasts: [], scans: 0, ports: 0 };
  const parts = [
    "const state = {flashJobId: 'j1', flashIrreversible: true};",
    extractFn('flashFailureText'), extractFn('onFlashJobEnd'),
    'return onFlashJobEnd;',
  ];
  const factory = new Function('apiGet', 'clearPersistedJobId', 'hideProgress', 'loadPorts', 'updateGlobalBusyFromPorts',
    'setFlashButtons', 'toast', 'uiT', 'refreshScanAfterFlash',
    parts.join('\n'));
  const end = factory(
    async () => { if (snapshot instanceof Error) throw snapshot; return snapshot; },
    () => {}, () => {}, async () => { rec.ports++; }, () => {}, () => {},
    (msg, type) => rec.toasts.push([msg, type]), (s) => s, async () => { rec.scans++; });
  return { end, rec };
}
async function run(stateName, snapshot) {
  const { end, rec } = makeEnd(snapshot);
  await end(stateName, 'j1');
  return rec;
}
const greenDone = (rec) => rec.toasts.some(([m, t]) => m === 'Прошивка завершена' && t === 'success');

let r = await run('done', { state: 'done', error: null, params: {} });
check(greenDone(r) && r.scans === 1, 'a clean job still shows «Прошивка завершена» and rescans');

r = await run('error', { state: 'error', error: 'Не прошито: адр.5 SN 0x0E0A7530: Нет ответа устройства (x).', params: {} });
check(!greenDone(r) && r.toasts.length === 1 && r.toasts[0][1] === 'error'
  && r.toasts[0][0].includes('адр.5') && r.toasts[0][0].includes('SN 0x0E0A7530'),
  'FL-02: an `error` job shows an error toast that names the failed target (never the green one)');

r = await run('done', { state: 'done', error: null, params: { errors: [{ address: 5, serial: 1, error: 'boom' }] } });
check(!greenDone(r) && r.toasts.length === 1 && r.toasts[0][1] === 'error' && r.toasts[0][0].includes('адр.5') && r.scans === 0,
  'FL-02: a `done` job of an older daemon carrying params.errors is an error too (no green toast, no success rescan)');

r = await run('error', new Error('HTTP 404'));
check(r.toasts.length === 1 && r.toasts[0][1] === 'error' && !greenDone(r),
  'an unreadable snapshot after an error still ends in the generic error toast');

r = await run('cancelled', { state: 'cancelled', error: null, params: {} });
check(r.toasts.length === 1 && r.toasts[0][1] === 'warn' && !greenDone(r), 'a cancelled job is a warning');

// ── FL-02: the batch is asked to stop at the first failed target ──────────
const flashCall = SRC.slice(SRC.indexOf("apiPost('/flash_batch'"));
const callBody = flashCall.slice(0, flashCall.indexOf('});')).split('\n').map(l => l.replace(/\/\/.*$/, '')).join('\n');
check(/skip_on_error:\s*false/.test(callBody), "FL-02: startFlash sends skip_on_error:false (continue-on-error is not the tab's default)");

if (failures) { console.error(`\ntest-flasher-flash-failure: ${failures} check(s) FAILED`); process.exit(1); }
console.log('\ntest-flasher-flash-failure: all checks passed');
