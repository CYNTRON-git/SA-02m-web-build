#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it runs the shipped app/audio.js against a stub DOM and asserts what its helpers return and what render() writes, pinning no source line by text; a commented-out line changes the result it measures.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-audio-card — the card half of docs/contracts/audio-recorder.md §15.
     - every status state, and every `idle` sub-case (no mic, mic busy, medium
       absent / read-only / full), has its badge; an unknown state is «н/д»,
       no answer is «не отвечает»;
     - «Записать» is allowed ONLY live + idle + mic present + medium ok;
     - every last_status the daemon can publish (read from
       opt/sa02m-audio/sa02m_audio/constants.py, not copied) has a line;
     - the removable-media acknowledgement: a save of `usb`/`sdcard` without
       «Понимаю» never builds a request (ack_required), with it the body
       carries ack_removable:true; per-medium keep_mb bounds and integers are
       checked before any request;
     - a clip id reaches a URL only through the id grammar;
     - render(): server strings land via textContent only (the file contains
       no innerHTML), a removable medium shows the warning note, `recording`
       shows «Остановить», `not_installed` hides the record row and disables
       the toggle, the clip list is rebuilt (no stale row survives).
   The SHIPPED file runs whole in a vm sandbox (stub document, stub fetch).
   RED, measured on a scratch copy (AUDIO_JS): the ack check in saveBody
   deleted (a usb save builds a request), the `medium_state === 'ok'` term of
   recordAllowed deleted (record allowed with no medium), the mic_busy line of
   lastStatusText deleted (one daemon status has no text).
   Run: node scripts/dev/test-audio-card.mjs
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const SRC = process.env.AUDIO_JS || join(ROOT, 'www', 'network_config', 'static', 'js', 'app', 'audio.js');
const CONSTANTS = join(ROOT, 'opt', 'sa02m-audio', 'sa02m_audio', 'constants.py');

let failures = 0;
let passes = 0;
function check(cond, msg) {
  if (cond) { passes++; console.log('  ok   - ' + msg); }
  else { failures++; console.error('  FAIL - ' + msg); }
}

function el(id, tag) {
  return {
    id, tagName: (tag || 'div').toUpperCase(), hidden: false, textContent: '', value: '', checked: false,
    disabled: false, open: false, attrs: {}, listeners: {}, children: [], title: '', className: '',
    type: '', href: '', dataset: {}, max: '',
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); return c; },
    get firstChild() { return this.children[0] || null; },
    play() { return Promise.resolve(); }, pause() {},
  };
}
const IDS = ['audio-card', 'audio-state', 'audio-mic', 'audio-medium', 'audio-count', 'audio-removable-note',
  'audio-record-row', 'audio-seconds', 'audio-btn-record', 'audio-btn-stop', 'audio-elapsed', 'audio-clips',
  'audio-player', 'audio-settings', 'audio-storage', 'audio-storage-warn', 'audio-ack-row', 'audio-ack',
  'audio-default-dur', 'audio-keep-files', 'audio-keep-mb', 'audio-keep-hours', 'audio-btn-save',
  'audio-events', 'audio-event-list', 'audio-msg', 'audio-btn-restart', 'audio-btn-enable'];
const dom = {};
IDS.forEach(id => { dom[id] = el(id); });

const src = readFileSync(SRC, 'utf8');
const sandbox = {
  console,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  Promise, Date, Math, Number, JSON, Array, Object, String, isNaN, encodeURIComponent,
  fetch: () => new Promise(() => {}),
  withCsrfHeaders: h => h,
  document: {
    readyState: 'complete', visibilityState: 'visible',
    getElementById: id => dom[id] || null,
    createElement: tag => el('', tag),
    addEventListener() {},
  },
};
sandbox.window = sandbox;
sandbox.window.confirm = () => true;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: SRC });
const A = sandbox.sa02mAudioCard;
check(!!A && typeof A.stateBadge === 'function', 'the card exposes its helpers (window.sa02mAudioCard)');

// ── badges ──
const live = { ok: true, live: true, installed: true, enabled: true, state: 'idle', mic: 'present', medium_state: 'ok' };
const cases = [
  [{ ok: true, state: 'not_installed' }, 'не установлено', 'unk'],
  [{ ok: true, state: 'disabled' }, 'выключено', 'unk'],
  [{ ok: true, state: 'not_running' }, 'не отвечает', 'err'],
  [{ ok: true, state: 'conf_unreadable' }, 'нет доступа к настройкам', 'err'],
  [{ ok: true, state: 'missing_deps' }, 'нет компонентов', 'err'],
  [{ ok: true, state: 'error' }, 'ошибка', 'err'],
  [{ ok: true, state: 'stopped' }, 'остановлено', 'unk'],
  [{ ok: true, state: 'recording' }, 'идёт запись…', 'ok'],
  [live, 'готово', 'ok'],
  [{ ...live, mic: 'absent' }, 'нет микрофона', 'warn'],
  [{ ...live, mic: 'busy' }, 'микрофон занят', 'warn'],
  [{ ...live, medium_state: 'absent' }, 'нет носителя', 'warn'],
  [{ ...live, medium_state: 'readonly' }, 'только чтение', 'warn'],
  [{ ...live, medium_state: 'full' }, 'мало места', 'warn'],
  [{ ok: true, state: 'made_up' }, 'н/д', 'unk'],
  [null, 'не отвечает', 'err'],
];
cases.forEach(([d, label, kind]) => {
  const got = A.stateBadge(d);
  check(got[0] === label && got[1] === kind, `badge ${d ? d.state + '/' + (d.mic || '') + '/' + (d.medium_state || '') : 'null'} → ${label} (${kind}), got ${got}`);
});

// ── record gating ──
check(A.recordAllowed(live) === true, 'record allowed: live, idle, mic present, medium ok');
[{ ...live, live: false }, { ...live, state: 'recording' }, { ...live, mic: 'busy' }, { ...live, mic: 'absent' },
  { ...live, medium_state: 'absent' }, { ...live, medium_state: 'full' }, { ...live, medium_state: 'readonly' },
  { ...live, ok: false }, null].forEach((d, i) => {
  check(A.recordAllowed(d) === false, `record refused in case ${i} (${d ? JSON.stringify(d).slice(0, 80) : 'null'})`);
});

// ── every daemon last_status has a line (read from the package, not copied) ──
const py = readFileSync(CONSTANTS, 'utf8');
const block = py.slice(py.indexOf('LAST_STATUSES = ('), py.indexOf(')', py.indexOf('LAST_STATUSES = (')));
const names = [...block.matchAll(/\bST_[A-Z_]+/g)].map(m => m[0]);
const values = {};
for (const m of py.matchAll(/^(ST_[A-Z_]+) = "([a-z_]+)"$/gm)) values[m[1]] = m[2];
const statuses = names.map(n => values[n]).filter(Boolean);
check(statuses.length >= 13 && statuses.length === names.length, `LAST_STATUSES read from constants.py (${statuses.length})`);
statuses.forEach(st => check(A.lastStatusText(st).length > 0, `last_status ${st} has a line`));
check(A.lastStatusText('made_up') === '', 'an unknown last_status has no line');

// ── save: acknowledgement and bounds ──
const bounds = { default_duration_s: [2, 30], keep_files: [1, 50], keep_hours: [1, 168],
  keep_mb: { emmc: [1, 256], usb: [1, 4096], sdcard: [1, 4096] } };
const d0 = { bounds };
const form = (over) => ({ storage: 'emmc', default_duration_s: '10', keep_files: '50', keep_mb: '32', keep_hours: '24', ack: false, ...over });
let out = A.saveBody(form({}), d0);
check(out.body && out.body.storage === 'emmc' && out.body.keep_mb === 32 && !('ack_removable' in out.body), 'emmc save: a body, no ack field');
for (const m of ['usb', 'sdcard']) {
  out = A.saveBody(form({ storage: m }), d0);
  check(out.error === 'ack_required' && !out.body, `${m} without «Понимаю» → ack_required, no request body`);
  out = A.saveBody(form({ storage: m, ack: true, keep_mb: '4000' }), d0);
  check(out.body && out.body.ack_removable === true && out.body.keep_mb === 4000, `${m} with «Понимаю» → ack_removable:true`);
}
check(A.saveBody(form({ keep_mb: '300' }), d0).error === 'invalid_value', 'keep_mb 300 on emmc refused before any request');
check(A.saveBody(form({ keep_files: '1.5' }), d0).error === 'invalid_value', 'a non-integer refused');
check(A.saveBody(form({ storage: 'nfs' }), d0).error === 'invalid_value', 'an unknown storage refused');

// ── urls and dates ──
check(A.clipUrl('20261015T120000Z-0a1b2c', true) === 'cgi-bin/sa02m_audio_api.cgi?action=download&id=20261015T120000Z-0a1b2c&inline=1',
  'a valid id → the download URL');
['../x', '20261015T120000Z-0A1B2C', '"><img src=x>', ''].forEach(bad =>
  check(A.clipUrl(bad, false) === '', `id ${JSON.stringify(bad)} → no URL`));
const dt = A.idDate('20261015T120000Z-0a1b2c');
check(dt && dt.toISOString() === '2026-10-15T12:00:00.000Z', 'the id is read as UTC');

// ── render ──
check(!/\binnerHTML\b/.test(src), 'audio.js writes no innerHTML (textContent only)');
const evil = '<img src=x onerror=alert(1)>';
A.render({ ...live, medium: 'usb', conf: { storage: 'usb', default_duration_s: 10, max_duration_s: 30, keep_files: 50, keep_mb: 32, keep_hours: 24 },
  bounds, clips: [{ id: '20261015T120000Z-0a1b2c', medium: 'usb', size: 160044, ts: 1 }, { id: evil, medium: 'usb', size: 1, ts: 2 }],
  events: [{ ts: 1, event: 'refused', status: 'busy', detail: evil }] });
check(dom['audio-removable-note'].hidden === false, 'a removable medium shows the warning note');
check(dom['audio-clips'].children.length === 1, 'a clip whose id breaks the grammar is not rendered');
const row = dom['audio-clips'].children[0];
check(row && row.children.some(c => /≈5\.0/.test(c.textContent)), 'the clip row shows ≈5.0 s for a 160 044-byte clip');
check(dom['audio-event-list'].children[0].textContent.includes(evil), 'an event detail lands as text, verbatim');
check(dom['audio-btn-record'].disabled === false && dom['audio-btn-stop'].hidden === true, 'idle: record enabled, stop hidden');
A.render({ ...live, state: 'recording', medium: 'emmc', clips: [], events: [] });
check(dom['audio-btn-stop'].hidden === false && dom['audio-btn-record'].disabled === true, 'recording: stop shown, record disabled');
check(dom['audio-clips'].children.length === 0 && dom['audio-clips'].hidden === true, 'the clip list is rebuilt (no stale row)');
check(dom['audio-removable-note'].hidden === true, 'eMMC hides the removable note');
A.render({ ok: true, state: 'not_installed', installed: false, enabled: false, live: false });
check(dom['audio-record-row'].hidden === true && dom['audio-btn-enable'].disabled === true, 'not_installed: no record row, toggle disabled');
check(dom['audio-msg'].textContent === 'Нужна полная установка (install.sh --refresh)', 'not_installed: the install hint');

console.log(`js-unit-audio-card: ${passes} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
