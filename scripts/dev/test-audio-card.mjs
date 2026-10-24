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
     - 1.0.7.10 light music: every audio-lm-* / audio-gain-* id of index.html
       is read by audio.js and back; a line for every LIGHT_MUSIC_STATES value
       and the picture options == LIGHT_MUSIC_STYLES (constants.py, read);
       the switch only for ok + live + a configured strip, else disabled; one
       POST {"action":"light_music","on":<bool>}, or none and the switch put
       back; a stale state is not shown when not live; the strip select holds
       only grammar-valid bridge ids as text; strip / picture / rate checked
       before a save (rate bounds read from constants.py).
     - 1.0.7.10 mic gain: a line for every GAIN_STATES value; the value clamped
       only to a daemon-reported range, junk refused; a daemon conf without
       capture_gain never gets the field and hides the row; an open form is
       not overwritten by a poll.
     - older daemon (www-only update over a pre-1.0.7.9 package): a conf
       without every light-music key → the card's save carries no
       light_music_* field and the strip / picture / rate fields hide; a
       1.0.7.10 conf → all three sent, fields shown.
     - null-safe: without the 1.0.7.10 nodes render, the switch and the save
       merge do not throw.
   The SHIPPED file runs whole in a vm sandbox (stub document, stub fetch).
   RED, measured on a scratch copy (AUDIO_JS): the ack check in saveBody
   deleted (a usb save builds a request), the `medium_state === 'ok'` term of
   recordAllowed deleted (record allowed with no medium), the mic_busy line of
   lastStatusText deleted (one daemon status has no text). 1.0.7.10 (14
   mutations, each RED): the target term of lightMusicAllowed, the id filter of
   fillTargets, the bridge-list check and the style check of saveBody, the
   gainSupported guard of gainSaveMerge, the `d.live ?` of the state line, the
   strip_offline line, the switch revert, the `if (lm)` guard, a renamed
   audio-gain-hint, a string `on`, the gain clamp, the row's enabled gate, the
   open-form guard of renderGain; AUDIO_HTML with the audio-lm-state id and
   one style option renamed. Older-daemon guard (4 mutations, each RED): the
   lightMusicSupported gate of audioSave forced true, the fillSettings call
   hiding the fields deleted, every→some in lightMusicSupported, the field
   hidden flag forced false.
   Run: node scripts/dev/test-audio-card.mjs   (AUDIO_JS / AUDIO_HTML judge other copies)
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
    // The enclosing `.field` wrapper of a form input (one stub per element).
    closest(sel) { return sel === '.field' ? (this._field = this._field || { hidden: false }) : null; },
    play() { return Promise.resolve(); }, pause() {},
  };
}
const IDS = ['audio-card', 'audio-state', 'audio-mic', 'audio-medium', 'audio-count', 'audio-removable-note',
  'audio-record-row', 'audio-seconds', 'audio-btn-record', 'audio-btn-stop', 'audio-elapsed', 'audio-clips',
  'audio-player', 'audio-settings', 'audio-storage', 'audio-storage-warn', 'audio-ack-row', 'audio-ack',
  'audio-default-dur', 'audio-keep-files', 'audio-keep-mb', 'audio-keep-hours', 'audio-btn-save',
  'audio-events', 'audio-event-list', 'audio-msg', 'audio-btn-restart', 'audio-btn-enable',
  'audio-last-upload', 'audio-upload-url', 'audio-delete-after', 'audio-token', 'audio-token-state'];
// 1.0.7.10: light music (state line, switch row, strip / picture / rate) and
// the mic capture gain row.
const LM_GAIN_IDS = ['audio-lm-state', 'audio-lm-row', 'audio-lm-on', 'audio-lm-target', 'audio-lm-style',
  'audio-lm-rate', 'audio-gain-row', 'audio-gain-on', 'audio-gain', 'audio-gain-hint', 'audio-gain-state'];
const dom = {};
IDS.concat(LM_GAIN_IDS).forEach(id => { dom[id] = el(id); });
// Ids listed here read as absent (null) — an index.html without those nodes.
const absent = new Set();
// Every POST the card sends (post() → fetchWithTimeout from app/status.js);
// answered with a plain ok so mutate() settles and releases _busy.
const posts = [];

const src = readFileSync(SRC, 'utf8');
const sandbox = {
  console,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  Promise, Date, Math, Number, JSON, Array, Object, String, isNaN, encodeURIComponent,
  fetch: () => new Promise(() => {}),
  fetchWithTimeout: (url, opts) => {
    posts.push({ url, opts });
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    return Promise.resolve({ json: () => ({ ok: true, action: body.action, done: 'ok' }) });
  },
  withCsrfHeaders: h => h,
  document: {
    readyState: 'complete', visibilityState: 'visible',
    getElementById: id => (absent.has(id) ? null : dom[id] || null),
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

// ── upload (slice C): states, settings, token, per-clip sidecar fields ──
const upBlock = py.slice(py.indexOf('UPLOAD_STATES = ('), py.indexOf(')', py.indexOf('UPLOAD_STATES = (')));
const upNames = [...upBlock.matchAll(/\bUP_[A-Z_]+/g)].map(m => m[0]);
const upValues = {};
for (const m of py.matchAll(/^(UP_[A-Z_]+) = "([a-z_]+)"$/gm)) upValues[m[1]] = m[2];
const upStates = upNames.map(n => upValues[n]).filter(Boolean);
check(upStates.length === 6, `UPLOAD_STATES read from constants.py (${upStates.length})`);
upStates.filter(st => st !== 'off').forEach(st => check(A.uploadText(st).length > 0, `upload state ${st} has a line`));
check(A.uploadText('off') === '' && A.uploadText('made_up') === '', 'off / unknown upload state: no line');
out = A.saveBody(form({ upload_url: ' https://192.168.1.10/up ', delete_after_upload: false }), d0);
check(out.body && out.body.upload_url === 'https://192.168.1.10/up' && out.body.delete_after_upload === false,
  'save carries upload_url (trimmed) and delete_after_upload');
check(A.saveBody(form({ upload_url: '' }), d0).body.upload_url === '', 'an empty URL switches the upload off');
['ftp://x/y', 'javascript:alert(1)', 'http://a b'].forEach(u =>
  check(A.saveBody(form({ upload_url: u }), d0).error === 'invalid_value', `URL ${JSON.stringify(u)} refused before a request`));
check(A.tokenBody(' tok-1.A~ ').body.token === 'tok-1.A~', 'a token is trimmed into a set_token body');
['', 'has space', 'x'.repeat(513), 'ключ'].forEach(t =>
  check(A.tokenBody(t).error === 'bad_token', `token ${JSON.stringify(t.slice(0, 12))} refused before a request`));
A.render({ ...live, medium: 'emmc', conf: { storage: 'emmc', default_duration_s: 10, max_duration_s: 30, keep_files: 50,
  keep_mb: 32, keep_hours: 24, upload_url: 'https://h/up', delete_after_upload: true }, bounds, token_set: true,
  last_file: '20261015T120000Z-0a1b2c', last_upload: 'failed',
  clips: [{ id: '20261015T120000Z-0a1b2c', medium: 'emmc', size: 160044, ts: 1, duration_ms: 4980, peak_dbfs: -12, upload: 'failed' }],
  events: [] });
const meta = dom['audio-clips'].children[0].children[1].textContent;
check(/4\.98? с|5\.0 с/.test(meta) && !/≈/.test(meta), `the sidecar's exact duration replaces the estimate (${meta})`);
check(meta.includes('-12 дБFS') && meta.includes('не отправлено'), 'the row shows the peak and the upload state');
check(dom['audio-last-upload'].textContent.includes('не отправлено'), 'the card states the last clip\'s upload');
check(dom['audio-token-state'].textContent === 'Токен задан', 'the token is shown only as set / not set');

// ── 1.0.7.10: light music + mic gain ──
// A constants.py tuple of NAME constants → their string values (read, not copied).
function tupleValues(name) {
  const start = py.indexOf(name + ' = (');
  if (start < 0) return [];
  const blk = py.slice(start, py.indexOf(')', start));
  const vals = {};
  for (const m of py.matchAll(/^([A-Z_]+) = "([a-z_]+)"$/gm)) vals[m[1]] = m[2];
  return [...blk.slice(blk.indexOf('(')).matchAll(/\b[A-Z][A-Z_]+\b/g)].map(m => vals[m[0]]).filter(Boolean);
}
function constInt(name) {
  const m = py.match(new RegExp('^' + name + ' = (-?[0-9]+)$', 'm'));
  return m ? Number(m[1]) : NaN;
}

// id contract, card-local: every audio-lm-* / audio-gain-* id the markup
// declares is one audio.js reads, and the other way round.
const HTML = readFileSync(process.env.AUDIO_HTML || join(ROOT, 'www', 'network_config', 'index.html'), 'utf8');
const htmlIds = new Set([...HTML.matchAll(/\bid="(audio-(?:lm|gain)[a-z-]*)"/g)].map(m => m[1]));
const jsIds = new Set([...src.matchAll(/(?:\$|setText|setHidden)\('(audio-(?:lm|gain)[a-z-]*)'/g)].map(m => m[1]));
check(htmlIds.size >= LM_GAIN_IDS.length && jsIds.size >= LM_GAIN_IDS.length,
  `id sweep non-vacuous (index.html ${htmlIds.size}, audio.js ${jsIds.size}, expected >= ${LM_GAIN_IDS.length})`);
const onlyHtml = [...htmlIds].filter(id => !jsIds.has(id));
const onlyJs = [...jsIds].filter(id => !htmlIds.has(id));
check(onlyHtml.length === 0 && onlyJs.length === 0,
  `light-music / gain ids: index.html == audio.js (only in HTML: ${onlyHtml.join(' ') || '-'}; only in JS: ${onlyJs.join(' ') || '-'})`);
check(LM_GAIN_IDS.every(id => htmlIds.has(id)), 'the stub DOM ids are exactly the shipped ones');
const htmlStyles = [...(HTML.match(/<select id="audio-lm-style">([\s\S]*?)<\/select>/) || ['', ''])[1]
  .matchAll(/<option value="([a-z_]+)"/g)].map(m => m[1]).sort();

// light_music_state: every state the daemon publishes has its own line.
const lmStates = tupleValues('LIGHT_MUSIC_STATES');
check(lmStates.length === 5, `LIGHT_MUSIC_STATES read from constants.py (${lmStates.join(' ')})`);
const lmLines = lmStates.map(st => A.lightMusicText(st));
lmStates.forEach((st, i) => check(lmLines[i].length > 0, `light_music_state ${st} has a line (${lmLines[i]})`));
check(new Set(lmLines).size === lmLines.length, 'every light_music_state line is distinct');
check(A.lightMusicText('made_up') === '' && A.lightMusicText('') === '', 'an unknown / empty light_music_state: no line');
check(A.errorText('no_target') !== A.errorText('made_up'), `no_target has its own message (${A.errorText('no_target')})`);

// Picture styles: the card, the markup and the daemon agree.
const lmStyles = tupleValues('LIGHT_MUSIC_STYLES').sort();
check(lmStyles.length === 3 && JSON.stringify(lmStyles) === JSON.stringify(htmlStyles),
  `audio-lm-style options == LIGHT_MUSIC_STYLES (${htmlStyles.join(' ')} / ${lmStyles.join(' ')})`);

// The switch is offered only with a live daemon and a configured strip.
const lmLive = { ...live, conf: { light_music_target: 'strip_1' }, led_devices: ['strip_1'] };
check(A.lightMusicAllowed(lmLive) === true, 'light music allowed: ok, live, a strip configured');
[{ ...lmLive, live: false }, { ...lmLive, ok: false }, { ...lmLive, conf: { light_music_target: '' } },
  { ...lmLive, conf: {} }, { ...lmLive, conf: undefined }, null].forEach((d, i) =>
  check(A.lightMusicAllowed(d) === false, `light music refused in case ${i} (${d ? JSON.stringify(d.conf) + ' live=' + d.live + ' ok=' + d.ok : 'null'})`));

// save: strip / picture / rate checked before any request.
const lmBounds = { ...bounds, light_music_rate_hz: [constInt('LIGHT_MUSIC_RATE_MIN'), constInt('LIGHT_MUSIC_RATE_MAX')] };
check(lmBounds.light_music_rate_hz.every(Number.isInteger), `rate bounds read from constants.py (${lmBounds.light_music_rate_hz})`);
const dLm = { bounds: lmBounds, led_devices: ['strip_1', 'strip.2'] };
out = A.saveBody(form({ light_music_target: '', light_music_style: 'level', light_music_rate_hz: '8' }), dLm);
check(out.body && out.body.light_music_target === '' && out.body.light_music_style === 'level' && out.body.light_music_rate_hz === 8,
  'save: no strip ("") + level + 8 Hz → in the body, rate as an integer');
check(A.saveBody(form({ light_music_target: 'strip.2' }), dLm).body.light_music_target === 'strip.2', 'save: a strip the bridge has');
check(A.saveBody(form({ light_music_target: 'strip_9' }), dLm).error === 'invalid_value', 'save: a strip the bridge does not have refused');
check(A.saveBody(form({ light_music_target: 'a b' }), { ...dLm, led_devices: ['a b'] }).error === 'invalid_value',
  'save: a strip id outside the grammar refused even when listed');
lmStyles.forEach(st => check(A.saveBody(form({ light_music_style: st }), dLm).body.light_music_style === st, `save: picture ${st} accepted`));
check(A.saveBody(form({ light_music_style: 'rainbow' }), dLm).error === 'invalid_value', 'save: an unknown picture refused');
const [rMin, rMax] = lmBounds.light_music_rate_hz;
[[rMin, true], [rMax, true], [rMin - 1, false], [rMax + 1, false], ['2.5', false], ['', false]].forEach(([v, okv]) => {
  const r = A.saveBody(form({ light_music_rate_hz: String(v) }), dLm);
  check(okv ? r.body && r.body.light_music_rate_hz === Number(v) : r.error === 'invalid_value', `save: rate ${JSON.stringify(String(v))} ${okv ? 'accepted' : 'refused'}`);
});
check(!('light_music_target' in A.saveBody(form({}), dLm).body), 'save: no light-music field in the form → none in the body');

// render: the state line, the switch row and the switch.
const lmConf = { storage: 'emmc', default_duration_s: 10, max_duration_s: 30, keep_files: 50, keep_mb: 32, keep_hours: 24,
  light_music_target: 'strip_1', light_music_style: 'equalizer', light_music_rate_hz: 6 };
const lmFull = { ...live, medium: 'emmc', conf: lmConf, bounds: lmBounds, clips: [], events: [],
  led_devices: ['strip_1', '<img src=x>', 'strip.2', 7], light_music: true, light_music_state: 'running' };
dom['audio-settings'].open = false;
A.render(lmFull);
check(dom['audio-lm-row'].hidden === false, 'enabled + installed: the light-music row is shown');
check(dom['audio-lm-on'].checked === true && dom['audio-lm-on'].disabled === false, 'light_music true + strip set: switch on and usable');
check(dom['audio-lm-state'].textContent === A.lightMusicText('running'), `the state line reads the daemon's state (${dom['audio-lm-state'].textContent})`);
const opts = dom['audio-lm-target'].children;
check(opts.map(o => o.value).join('|') === '|strip_1|strip.2',
  `the strip select: «— нет —» + the bridge's valid ids only (${opts.map(o => o.value).join('|')})`);
check(opts[0] && opts[0].textContent === '— нет —' && opts.slice(1).every(o => o.textContent === o.value),
  'strip options carry text only (textContent), «— нет —» for none');
check(dom['audio-lm-target'].value === 'strip_1' && dom['audio-lm-style'].value === 'equalizer' && dom['audio-lm-rate'].value === '6',
  'the settings form shows the conf strip / picture / rate');
A.render({ ...lmFull, led_devices: ['strip.2'], conf: { ...lmConf, light_music_target: 'strip_gone' } });
check(dom['audio-lm-target'].children.map(o => o.value).join('|') === '|strip.2|strip_gone' && dom['audio-lm-target'].value === 'strip_gone',
  'a configured strip the bridge no longer lists stays selectable (not silently cleared)');
A.render({ ...lmFull, light_music: false, conf: { ...lmConf, light_music_target: '' } });
check(dom['audio-lm-on'].checked === false && dom['audio-lm-on'].disabled === true, 'no strip configured: switch off and disabled');
A.render({ ...lmFull, live: false, state: 'not_running', light_music_state: 'running' });
check(dom['audio-lm-on'].disabled === true && dom['audio-lm-state'].textContent === '—',
  'not live: switch disabled, a stale light_music_state is not shown («—»)');
A.render({ ...lmFull, light_music_state: 'made_up' });
check(dom['audio-lm-state'].textContent === '—', 'an unknown light_music_state reads «—»');
A.render({ ...lmFull, enabled: false });
check(dom['audio-lm-row'].hidden === true, 'recorder disabled: the light-music row is hidden');
A.render(null);
check(dom['audio-lm-row'].hidden === true && dom['audio-lm-state'].textContent === '—', 'no answer: row hidden, state «—»');

// the switch: one light_music POST with a boolean, or nothing and the switch put back.
check((dom['audio-lm-on'].listeners.change || []).includes(A.audioLightMusic), 'init binds the switch to audioLightMusic');
A.render(lmFull);
posts.length = 0;
dom['audio-lm-on'].checked = false;
await A.audioLightMusic();
const sent = posts.map(p => JSON.parse(p.opts.body));
check(posts.length === 1 && posts[0].opts.method === 'POST' && JSON.stringify(sent[0]) === '{"action":"light_music","on":false}',
  `switch off → exactly one POST {"action":"light_music","on":false} (${JSON.stringify(sent)})`);
check(dom['audio-msg'].textContent === 'Светомузыка выключена', `the action's own message (${dom['audio-msg'].textContent})`);
A.render(lmFull);
posts.length = 0;
dom['audio-lm-on'].checked = true;
await A.audioLightMusic();
check(posts.length === 1 && JSON.parse(posts[0].opts.body).on === true, 'switch on → on:true');
A.render({ ...lmFull, light_music: false, conf: { ...lmConf, light_music_target: '' } });
posts.length = 0;
dom['audio-lm-on'].checked = true;
await A.audioLightMusic();
check(posts.length === 0 && dom['audio-lm-on'].checked === false, 'no strip: no request, the switch is put back');
check(dom['audio-msg'].textContent === A.errorText('no_target'), 'no strip: «Сначала выберите ленту в настройках»');
A.render({ ...lmFull, live: false, state: 'not_running' });
posts.length = 0;
dom['audio-lm-on'].checked = false;
await A.audioLightMusic();
check(posts.length === 0 && dom['audio-lm-on'].checked === true, 'daemon not live: no request, the switch is put back');

// mic capture gain (window.sa02mAudioGain).
const G = sandbox.sa02mAudioGain;
check(!!G && typeof G.gainValue === 'function', 'the gain row exposes its helpers (window.sa02mAudioGain)');
const gStates = tupleValues('GAIN_STATES');
check(gStates.length === 7, `GAIN_STATES read from constants.py (${gStates.join(' ')})`);
const gUnknown = G.gainStateText({ conf: { capture_gain: 5 } });
const gLines = gStates.map(st => G.gainStateText({ conf: { capture_gain: 5 }, mic_gain: { state: st, want: 5, applied: 5, min: 0, max: 100, db: '18.75' } }));
gStates.forEach((st, i) => check(gLines[i].length > 0 && gLines[i] !== gUnknown, `mic_gain.state ${st} has its own line (${gLines[i]})`));
check(gLines[gStates.indexOf('applied')] === 'Сейчас: 5 из 0…100 (+18,75 дБ)', `applied: value, range, dB with a RU comma (${gLines[gStates.indexOf('applied')]})`);
check(G.gainStateText({ conf: { capture_gain: null } }) === 'Не задано — уровень не меняется', 'no report yet + gain unset: «Не задано»');
check(G.gainDb('-3.00') === '-3,00 дБ' && G.gainDb('0.00') === '0,00 дБ' && G.gainDb('x') === '' && G.gainDb(18) === '', 'gainDb: sign kept, junk → ""');
check(G.gainRange({ min: 0, max: 100 }).join() === '0,100' && G.gainRange({ min: 5, max: 1 }) === null && G.gainRange({ min: 0.5, max: 2 }) === null,
  'gainRange: integers in order only');
const gR = { mic_gain: { state: 'applied', min: 0, max: 100 } };
check(G.gainValue(false, '42', gR).value === null, 'gain switched off → capture_gain null');
check(G.gainValue(true, ' 42 ', gR).value === 42, 'gain 42 → 42');
const gHi = G.gainValue(true, '150', gR);
check(gHi.value === 100 && gHi.clamped === true, 'gain above the daemon-reported range → clamped to max, flagged');
check(G.gainValue(true, '-5', gR).value === 0, 'gain below the range → clamped to min');
check(G.gainValue(true, '150', {}).value === 150, 'no reported range → no clamp (the device range is the daemon\'s)');
['', '1.5', 'abc', '1e3', '100001', '-100001', '1234567'].forEach(v =>
  check(G.gainValue(true, v, {}).error === 'invalid_value', `gain ${JSON.stringify(v)} refused before a request`));
const oldD = { conf: { storage: 'emmc' } };
const built = () => ({ body: { action: 'save', storage: 'emmc' } });
dom['audio-gain-on'].checked = true; dom['audio-gain'].value = '30';
check(!('capture_gain' in G.gainSaveMerge(built(), oldD).body), 'a daemon without capture_gain in its conf never gets the field');
const newD = { conf: { storage: 'emmc', capture_gain: null }, mic_gain: { state: 'applied', min: 0, max: 20 } };
let gm = G.gainSaveMerge(built(), newD);
check(gm.body.capture_gain === 20 && dom['audio-gain'].value === '20', 'save: 30 over a 0…20 range → capture_gain 20, the field shows 20');
dom['audio-gain-on'].checked = false;
check(G.gainSaveMerge(built(), newD).body.capture_gain === null, 'save: gain switched off → capture_gain null');
dom['audio-gain-on'].checked = true; dom['audio-gain'].value = 'x';
check(G.gainSaveMerge(built(), newD).error === 'invalid_value', 'save: a bad gain → invalid_value, no body');
check(G.gainSaveMerge({ error: 'ack_required' }, newD).error === 'ack_required', 'an earlier save error passes through');
dom['audio-settings'].open = false;
A.render({ ...lmFull, conf: { ...lmConf } });
check(dom['audio-gain-row'].hidden === true, 'a daemon conf without capture_gain: the gain row is hidden');
A.render({ ...lmFull, conf: { ...lmConf, capture_gain: 12 }, mic_gain: { state: 'applied', want: 12, applied: 12, min: 0, max: 31, db: '6.00' } });
check(dom['audio-gain-row'].hidden === false && dom['audio-gain-on'].checked === true && dom['audio-gain'].value === '12'
  && dom['audio-gain'].disabled === false, 'capture_gain 12: row shown, switch on, field 12 and editable');
check(dom['audio-gain'].min === '0' && dom['audio-gain'].max === '31' && dom['audio-gain-hint'].textContent === 'Диапазон: 0…31',
  `the field is bounded by the reported range (${dom['audio-gain-hint'].textContent})`);
A.render({ ...lmFull, conf: { ...lmConf, capture_gain: null } });
check(dom['audio-gain-on'].checked === false && dom['audio-gain'].value === '' && dom['audio-gain'].disabled === true
  && dom['audio-gain-hint'].textContent === 'Диапазон — когда есть микрофон', 'capture_gain null: switch off, field empty and disabled, no range yet');
dom['audio-settings'].open = true;
dom['audio-gain'].value = '7';
A.render({ ...lmFull, conf: { ...lmConf, capture_gain: 12 } });
check(dom['audio-gain'].value === '7', 'an open settings form is never overwritten by a poll');
dom['audio-settings'].open = false;
A.render({ ok: true, state: 'not_installed', installed: false, enabled: false, live: false, conf: { capture_gain: 3 } });
check(dom['audio-gain-row'].hidden === true, 'not installed: the gain row is hidden');

// An older daemon (a www-only update over a pre-1.0.7.9 package): its conf has
// no light-music keys and its save dispatch refuses an unknown field, so the
// card's own save must carry none and the strip / picture / rate fields hide.
// Positive control first: a 1.0.7.10 daemon gets all three.
const LM_FIELDS = ['audio-lm-target', 'audio-lm-style', 'audio-lm-rate'];
const lmKeysOf = body => Object.keys(body).filter(k => k.startsWith('light_music'));
async function saveVia(d, set) {
  dom['audio-settings'].open = false;
  A.render(d);
  dom['audio-lm-target'].value = set.target;
  dom['audio-lm-style'].value = set.style;
  dom['audio-lm-rate'].value = set.rate;
  posts.length = 0;
  await sandbox.audioSave();
  return posts.map(p => JSON.parse(p.opts.body));
}
let saves = await saveVia(lmFull, { target: 'strip_1', style: 'color', rate: '4' });
check(saves.length === 1 && saves[0].action === 'save' && lmKeysOf(saves[0]).sort().join() === 'light_music_rate_hz,light_music_style,light_music_target',
  `a daemon with the light-music keys: the save carries all three (${JSON.stringify(saves)})`);
check(LM_FIELDS.every(id => dom[id].closest('.field').hidden === false), 'a daemon with the light-music keys: the strip / picture / rate fields are shown');
const oldConf = { storage: 'emmc', default_duration_s: 10, max_duration_s: 30, keep_files: 50, keep_mb: 32, keep_hours: 24,
  upload_url: '', delete_after_upload: true };
saves = await saveVia({ ...live, medium: 'emmc', conf: oldConf, bounds, clips: [], events: [] }, { target: '', style: 'level', rate: '6' });
check(saves.length === 1 && saves[0].action === 'save' && lmKeysOf(saves[0]).length === 0,
  `an older daemon (no light-music keys in its conf): the save carries no light-music field (${JSON.stringify(saves)})`);
check(LM_FIELDS.every(id => dom[id].closest('.field').hidden === true), 'an older daemon: the strip / picture / rate fields are hidden');
const partConf = { ...lmConf };
delete partConf.light_music_rate_hz;
saves = await saveVia({ ...lmFull, conf: partConf }, { target: 'strip_1', style: 'level', rate: '6' });
check(saves.length === 1 && lmKeysOf(saves[0]).length === 0, 'a conf missing any one light-music key: no light-music field is sent');

// null-safe: an index.html without the 1.0.7.10 nodes (the new bundle against
// an older page) renders, saves and switches without throwing.
LM_GAIN_IDS.forEach(id => absent.add(id));
let threw = null;
try {
  A.render({ ...lmFull, conf: { ...lmConf, capture_gain: 4 }, mic_gain: { state: 'applied', min: 0, max: 9 } });
  A.render(null);
  await A.audioLightMusic();
  G.renderGain({ installed: true, conf: { capture_gain: 4 } });
  const m = G.gainSaveMerge(built(), newD);
  if (!m.body) throw new Error('gainSaveMerge without the row lost the body: ' + JSON.stringify(m));
} catch (e) { threw = e; }
check(threw === null, `no light-music / gain nodes: render, the switch and the save merge do not throw (${threw && threw.message})`);
absent.clear();

console.log(`js-unit-audio-card: ${passes} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
