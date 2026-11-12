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
const JS_DIR = join(HERE, '..', '..', 'www', 'network_config', 'static', 'js');
const SRC = readFileSync(join(JS_DIR, 'flasher.js'), 'utf8');
const I18N = readFileSync(join(JS_DIR, 'i18n.js'), 'utf8');

function extractFn(name, src = SRC) {
  let start = src.indexOf('async function ' + name + '(');
  if (start < 0) start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('function ' + name + '() not found');
  const open = src.indexOf('{', src.indexOf(')', start));
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name + '()');
}
function extractConst(name, src = SRC) {
  const start = src.indexOf('const ' + name);
  if (start < 0) throw new Error('const ' + name + ' not found');
  let i = src.indexOf('=', start) + 1;
  while (/\s/.test(src[i])) i++;
  const open = src[i], close = open === '[' ? ']' : '}';
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === open) depth++;
    else if (src[j] === close && --depth === 0) return src.slice(start, src.indexOf(';', j) + 1);
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
    'resolveApplicationLineProfile', 'buildFlashTargetFromDevice'].map(n => extractFn(n)),
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
// The real i18n engine (DICT + REGEX + translateText of the SHIPPED i18n.js); lang 'en' translates, 'ru' is the identity.
const i18nParts = [
  extractConst('DICT', I18N), extractConst('REGEX', I18N), extractFn('normalize', I18N), extractFn('translateText', I18N),
  'return translateText;',
];
const translateText = new Function(i18nParts.join('\n'))();
function makeEnd(snapshot, lang = 'ru') {
  const tr = (msg) => (lang === 'en' ? translateText(String(msg)) : String(msg));
  const rec = { toasts: [], scans: 0, ports: 0 };
  const parts = [
    "const state = {flashJobId: 'j1', flashIrreversible: true};",
    extractFn('failedTargetsText'), extractFn('flashFailureText'), extractFn('failureToastText'),
    extractFn('onFlashJobEnd'),
    'return onFlashJobEnd;',
  ];
  const factory = new Function('apiGet', 'clearPersistedJobId', 'hideProgress', 'loadPorts', 'updateGlobalBusyFromPorts',
    'setFlashButtons', 'toast', 'uiT', 'refreshScanAfterFlash', 't',
    parts.join('\n'));
  const end = factory(
    async () => { if (snapshot instanceof Error) throw snapshot; return snapshot; },
    () => {}, () => {}, async () => { rec.ports++; }, () => {}, () => {},
    (msg, type) => rec.toasts.push([tr(msg), type]), tr, async () => { rec.scans++; }, tr);
  return { end, rec };
}
async function run(stateName, snapshot, lang = 'ru') {
  const { end, rec } = makeEnd(snapshot, lang);
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

// The text the daemon puts in job.error for a failed target (jobs.failed_targets_text): single target AND batch.
const DAEMON_TEXT = 'Не прошито: адр.5 SN 0x0E0A7530: Нет ответа устройства адр.5: таймаут чтения серийного номера на линиях 115200 N1, 19200 N1. Прошивка не начата, запись не отправлялась. Проверьте скорость линии модуля и адрес.';
r = await run('error', { state: 'error', error: DAEMON_TEXT, params: {} }, 'ru');
check(r.toasts.length === 1 && r.toasts[0][0] === DAEMON_TEXT && !/RuntimeError/.test(r.toasts[0][0]),
  'RU: the toast is the daemon text - address and serial named, no Python class name');
r = await run('error', { state: 'error', error: DAEMON_TEXT, params: {} }, 'en');
check(r.toasts.length === 1 && r.toasts[0][0].startsWith('Not flashed: addr 5 SN 0x0E0A7530: No answer from the device at address 5')
  && !/[А-Яа-яЁё]/.test(r.toasts[0][0]),
  'EN: the same toast is fully English (the rule translates the parts itself): ' + (r.toasts[0] || [''])[0].slice(0, 90));
r = await run('error', { state: 'error', error: 'Не прошито: адр.5 SN 0x0E0A7530: Серийный номер устройства не совпал с выбранной целью.; адр.6: Серийный номер загрузчика не совпал с номером приложения.', params: {} }, 'en');
check(r.toasts.length === 1 && r.toasts[0][0] === 'Not flashed: addr 5 SN 0x0E0A7530: The device serial number does not match the selected target.; addr 6: The bootloader serial number does not match the application serial number.',
  'EN: a continued batch names every failed target, each reason translated');
r = await run('done', { state: 'done', error: null, params: { errors: [{ address: 5, serial: 0x0E0A7530, error: 'Серийный номер устройства не совпал с выбранной целью.' }] } }, 'en');
check(r.toasts.length === 1 && r.toasts[0][0] === 'Not flashed: addr 5 SN 0x0E0A7530: The device serial number does not match the selected target.',
  'EN: an older daemon (done + params.errors) renders the same target text, translated');
r = await run('error', { state: 'error', error: 'PortBusyError: порт занят', params: {} }, 'ru');
check(r.toasts.length === 1 && r.toasts[0][0].startsWith('Прошивка не выполнена:'), 'a non-target job error keeps the «Прошивка не выполнена:» lead');
check(translateText('Адр.5 не ответил на 115200 N1; пробую линию 19200 N1') === 'Address 5 did not answer on 115200 N1; trying line 19200 N1',
  'the daemon line-fallback log message is translated');
// D36b-02: reg 129 accepted, the bootloader found nowhere - the toast says the module may be in its bootloader (RU and EN).
const BL_LOST = 'Загрузчик не найден на линиях 19200 N1, 115200 N1 после успешной записи рег. 129 (вход в загрузчик): модуль мог остаться в загрузчике. Образ не записывался. По исходникам загрузчика MR-02м он сам возвращается в приложение примерно через 120 с без обращений к нему; можно также выключить и включить питание модуля. Проверьте скорость и чётность линии.';
r = await run('error', { state: 'error', error: 'Не прошито: адр.5 SN 0x0E0A7530: ' + BL_LOST, params: {} }, 'ru');
check(r.toasts.length === 1 && r.toasts[0][0].includes('мог остаться в загрузчике') && !/RuntimeError/.test(r.toasts[0][0]),
  'RU: the lost-bootloader toast names the target and says the module may be left in its bootloader');
r = await run('error', { state: 'error', error: 'Не прошито: адр.5 SN 0x0E0A7530: ' + BL_LOST, params: {} }, 'en');
check(r.toasts.length === 1 && r.toasts[0][0].startsWith('Not flashed: addr 5 SN 0x0E0A7530: The bootloader was not found on lines 19200 N1, 115200 N1')
  && r.toasts[0][0].includes('may have been left in its bootloader') && !/[А-Яа-яЁё]/.test(r.toasts[0][0]),
  'EN: the lost-bootloader toast is fully English: ' + (r.toasts[0] || [''])[0].slice(0, 100));
check(translateText('Загрузчик не ответил на 19200 N1; пробую линию 115200 N1') === 'The bootloader did not answer on 19200 N1; trying line 115200 N1',
  'the bootloader-search log message is translated');
check(translateText('На линии 19200 N1 отвечает не целевой загрузчик: Серийный номер устройства не совпал с выбранной целью.') === 'A bootloader that is not the target answers on line 19200 N1: The device serial number does not match the selected target.',
  'G-2: the log line about a foreign module answering on the application line is translated');
check(translateText('Загрузчик цели не подтверждён: Нет ответа устройства (таймаут чтения серийного номера).') === 'The target bootloader was not confirmed: The device did not answer (serial number read timed out).',
  'G-2: the log line about the unconfirmed target bootloader is translated');

// ── FL-02: the batch is asked to stop at the first failed target ──────────
const flashCall = SRC.slice(SRC.indexOf("apiPost('/flash_batch'"));
const callBody = flashCall.slice(0, flashCall.indexOf('});')).split('\n').map(l => l.replace(/\/\/.*$/, '')).join('\n');
check(/skip_on_error:\s*false/.test(callBody), "FL-02: startFlash sends skip_on_error:false (continue-on-error is not the tab's default)");

if (failures) { console.error(`\ntest-flasher-flash-failure: ${failures} check(s) FAILED`); process.exit(1); }
console.log('\ntest-flasher-flash-failure: all checks passed');
