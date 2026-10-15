/* SA-02m — card «Запись звука» (вкладка «Управление» → «Система»). Talks only
   to cgi-bin/sa02m_audio_api.cgi (docs/contracts/audio-recorder.md §15).
   Polls every 5 s ONLY while the «Система» sub-tab is open and the page is
   visible (window.audioTabInit/Destroy from app.js systemLoadSub), and every
   1 s while a recording runs (bounded: the clip's own length + 5 s). Every
   server string reaches the DOM through textContent; a clip id reaches a URL
   only after the id grammar check. The removable-media acknowledgement is
   asked here AND enforced by the CGI (`ack_required`). */

(function () {
'use strict';

function uiT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(String(s)) : String(s);
}

function $(id) { return document.getElementById(id); }

var AU_API = 'cgi-bin/sa02m_audio_api.cgi';
var AU_POLL_MS = 5000;
var AU_FAST_MS = 1000;
var AU_GET_TIMEOUT_MS = 10000;
var AU_POST_TIMEOUT_MS = 21000;
// Contract §7: the only id form a URL or a delete may carry.
var AU_ID_RE = /^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$/;
var AU_REMOVABLE = ['usb', 'sdcard'];
var AU_WAV_HEADER = 44;
var AU_BYTES_PER_S = 32000;

var _last = null;
var _busy = false;
var _active = false;
var _poll = null;
var _fast = null;
var _fastUntil = 0;
var _recSeen = 0;
var _seq = 0;
// An action's own message stays this long before the state line may replace it.
var AU_NOTICE_MS = 6000;
var _noticeUntil = 0;

// ── pure helpers (also read by scripts/dev/test-audio-card.mjs) ────────────
function mediumLabel(m) {
  switch (m) {
    case 'emmc': return uiT('внутренняя память');
    case 'usb': return uiT('USB-накопитель');
    case 'sdcard': return uiT('карта microSD');
    default: return '—';
  }
}

// state → [label, badge kind]
function stateBadge(d) {
  if (!d || d.ok !== true || typeof d.state !== 'string') return [uiT('не отвечает'), 'err'];
  switch (d.state) {
    case 'not_installed': return [uiT('не установлено'), 'unk'];
    case 'disabled': return [uiT('выключено'), 'unk'];
    case 'not_running': return [uiT('не отвечает'), 'err'];
    case 'conf_unreadable': return [uiT('нет доступа к настройкам'), 'err'];
    case 'missing_deps': return [uiT('нет компонентов'), 'err'];
    case 'error': return [uiT('ошибка'), 'err'];
    case 'stopped': return [uiT('остановлено'), 'unk'];
    case 'recording': return [uiT('идёт запись…'), 'ok'];
    case 'idle':
      if (d.mic === 'absent') return [uiT('нет микрофона'), 'warn'];
      if (d.mic === 'busy') return [uiT('микрофон занят'), 'warn'];
      if (d.medium_state === 'absent') return [uiT('нет носителя'), 'warn'];
      if (d.medium_state === 'readonly') return [uiT('только чтение'), 'warn'];
      if (d.medium_state === 'full') return [uiT('мало места'), 'warn'];
      return [uiT('готово'), 'ok'];
    default: return [uiT('н/д'), 'unk'];
  }
}

// The standing explanation of the current state (§10), or ''.
function stateHint(d) {
  if (!d || d.ok !== true) return '';
  switch (d.state) {
    case 'not_installed': return uiT('Нужна полная установка (install.sh --refresh)');
    case 'not_running': return uiT('Служба не отвечает — нажмите «Перезапустить»');
    case 'conf_unreadable': return uiT('Нет доступа к настройкам — нажмите «Перезапустить»');
    case 'missing_deps': return uiT('Нет python3-paho-mqtt — нужна полная установка');
    case 'error': return uiT('Ошибка службы — журнал sa02m-audio');
    case 'idle':
      if (d.mic === 'absent') return uiT('Микрофон не подключён');
      if (d.mic === 'busy') return uiT('Микрофон занят другой программой');
      if (d.medium_state === 'absent') return uiT('Носитель (USB/SD) не найден — вставьте его или выберите внутреннюю память');
      if (d.medium_state === 'readonly') return uiT('Носитель только для чтения');
      if (d.medium_state === 'full') return uiT('Мало места на носителе — освободите место');
      return '';
    default: return '';
  }
}

// last_status → the line telling what happened / what to do next.
function lastStatusText(st) {
  switch (st) {
    case 'ok': return uiT('Запись сохранена');
    case 'busy': return uiT('Уже идёт запись');
    case 'disabled': return uiT('Запись звука выключена');
    case 'no_mic': return uiT('Микрофон не подключён');
    case 'mic_busy': return uiT('Микрофон занят другой программой');
    case 'storage_full': return uiT('Мало места на носителе — освободите место');
    case 'medium_absent': return uiT('Носитель (USB/SD) не найден — вставьте его или выберите внутреннюю память');
    case 'medium_readonly': return uiT('Носитель только для чтения');
    case 'medium_lost': return uiT('Носитель извлечён во время записи — запись потеряна');
    case 'rate_limited': return uiT('Слишком часто — подождите');
    case 'bad_command': return uiT('Неверная команда записи');
    case 'interrupted': return uiT('Запись прервана (перезагрузка, остановка службы или остановка раньше 1 с)');
    case 'error': return uiT('Ошибка записи — журнал sa02m-audio');
    default: return '';
  }
}

function errorText(code) {
  switch (code) {
    case 'ack_required': return uiT('Отметьте «Понимаю» — запись на съёмный носитель');
    case 'disabled': return uiT('Сначала включите запись звука');
    case 'not_running': return uiT('Служба не отвечает — нажмите «Перезапустить»');
    case 'not_installed': return uiT('Нужна полная установка (install.sh --refresh)');
    case 'bad_seconds': return uiT('Недопустимая длительность');
    case 'invalid_value': return uiT('Недопустимое значение');
    case 'conf_write_failed': return uiT('Не удалось сохранить настройки');
    case 'conf_unreadable': return uiT('Нет доступа к настройкам');
    case 'publish_failed': return uiT('Брокер MQTT не принял команду');
    case 'audio_api_failed': return uiT('Нет ответа от платы');
    default: return uiT('Не удалось выполнить действие');
  }
}

// May «Записать» be pressed?
function recordAllowed(d) {
  return !!d && d.ok === true && d.live === true && d.state === 'idle' && d.mic === 'present'
    && d.medium_state === 'ok';
}

// "YYYYMMDDTHHMMSSZ-xxxxxx" → a Date (UTC), or null.
function idDate(id) {
  if (typeof id !== 'string' || !AU_ID_RE.test(id)) return null;
  var t = Date.UTC(+id.slice(0, 4), +id.slice(4, 6) - 1, +id.slice(6, 8),
    +id.slice(9, 11), +id.slice(11, 13), +id.slice(13, 15));
  return isNaN(t) ? null : new Date(t);
}

function clipLine(c) {
  var when = idDate(c && c.id);
  var size = c && typeof c.size === 'number' ? c.size : 0;
  var secs = Math.max(0, (size - AU_WAV_HEADER) / AU_BYTES_PER_S);
  return {
    when: when ? when.toLocaleString() : String((c && c.id) || '?'),
    medium: mediumLabel(c && c.medium),
    dur: '≈' + secs.toFixed(1) + ' ' + uiT('с'),
    size: (size / 1024).toFixed(0) + ' ' + uiT('КБ'),
  };
}

function clipUrl(id, inline) {
  if (!AU_ID_RE.test(String(id))) return '';
  return AU_API + '?action=download&id=' + encodeURIComponent(id) + (inline ? '&inline=1' : '');
}

// The settings form → the save body, or {error} without a request.
function saveBody(form, d) {
  var b = d && d.bounds ? d.bounds : {};
  var storage = form.storage;
  if (['emmc', 'usb', 'sdcard'].indexOf(storage) < 0) return { error: 'invalid_value' };
  var body = { action: 'save', storage: storage };
  var keys = ['default_duration_s', 'keep_files', 'keep_mb', 'keep_hours'];
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i];
    var v = Number(form[k]);
    var lim = k === 'keep_mb' ? (b.keep_mb && b.keep_mb[storage]) : b[k];
    if (!Number.isInteger(v)) return { error: 'invalid_value' };
    if (Array.isArray(lim) && (v < lim[0] || v > lim[1])) return { error: 'invalid_value' };
    body[k] = v;
  }
  if (AU_REMOVABLE.indexOf(storage) >= 0) {
    if (form.ack !== true) return { error: 'ack_required' };
    body.ack_removable = true;
  }
  return { body: body };
}

// ── DOM ────────────────────────────────────────────────────────────────────
function setText(id, text) { var el = $(id); if (el) el.textContent = text; }
function setHidden(id, hidden) { var el = $(id); if (el) el.hidden = !!hidden; }

function setBadge(label, kind) {
  var dd = $('audio-state');
  if (!dd) return;
  while (dd.firstChild) dd.removeChild(dd.firstChild);
  var b = document.createElement('span');
  b.className = 'badge badge-' + kind;
  b.textContent = label;
  dd.appendChild(b);
}

// An action's outcome: shown at once and kept for AU_NOTICE_MS.
function actionNotice(text, good) {
  _noticeUntil = Date.now() + AU_NOTICE_MS;
  notice(text, good);
}

function notice(text, good) {
  var el = $('audio-msg');
  if (!el) return;
  el.textContent = text || '';
  el.hidden = !text;
  el.className = 'cloud-msg' + (text ? (good ? ' is-ok' : ' is-err') : '');
}

function renderClips(d) {
  var list = $('audio-clips');
  if (!list) return;
  while (list.firstChild) list.removeChild(list.firstChild);
  var rows = d && Array.isArray(d.clips) ? d.clips : [];
  list.hidden = rows.length === 0;
  var canDelete = !!(d && d.live);
  rows.forEach(function (c) {
    if (!c || !AU_ID_RE.test(String(c.id))) return;
    var line = clipLine(c);
    var li = document.createElement('li');
    var name = document.createElement('span');
    name.className = 'homeconnect-app-name';
    name.textContent = line.when;
    var meta = document.createElement('span');
    meta.className = 'homeconnect-app-meta';
    meta.textContent = [line.medium, line.dur, line.size].join(' · ');
    var play = document.createElement('button');
    play.type = 'button';
    play.className = 'btn btn-sm';
    play.textContent = '▶';
    play.title = uiT('Прослушать');
    play.setAttribute('aria-label', uiT('Прослушать'));
    play.addEventListener('click', function () { audioPlay(c.id); });
    var dl = document.createElement('a');
    dl.className = 'btn btn-sm';
    dl.href = clipUrl(c.id, false);
    dl.setAttribute('download', c.id + '.wav');
    dl.textContent = uiT('Скачать');
    var actions = document.createElement('span');
    actions.className = 'audio-clip-actions';
    actions.appendChild(play);
    actions.appendChild(dl);
    if (canDelete) {
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'btn btn-sm btn-danger';
      del.textContent = uiT('Удалить');
      del.addEventListener('click', function () { audioDelete(c.id); });
      actions.appendChild(del);
    }
    li.appendChild(name);
    li.appendChild(meta);
    li.appendChild(actions);
    list.appendChild(li);
  });
}

function renderEvents(d) {
  var list = $('audio-event-list');
  var box = $('audio-events');
  if (!list) return;
  while (list.firstChild) list.removeChild(list.firstChild);
  var rows = d && Array.isArray(d.events) ? d.events.slice().reverse() : [];
  if (box) box.hidden = rows.length === 0;
  rows.forEach(function (e) {
    if (!e || typeof e.event !== 'string') return;
    var li = document.createElement('li');
    var when = typeof e.ts === 'number' && e.ts > 0 ? new Date(e.ts * 1000).toLocaleString() : '';
    li.textContent = [when, e.event, e.status || '', e.medium || '', e.detail || '']
      .filter(function (s) { return s; }).join(' · ');
    list.appendChild(li);
  });
}

function fillSettings(d) {
  var det = $('audio-settings');
  if (!det) return;
  det.hidden = !(d && d.installed && d.conf);
  if (det.hidden || det.open) return;   // never overwrite what is being edited
  var c = d.conf;
  var sel = $('audio-storage');
  if (sel) sel.value = c.storage;
  var map = { 'audio-default-dur': 'default_duration_s', 'audio-keep-files': 'keep_files',
    'audio-keep-mb': 'keep_mb', 'audio-keep-hours': 'keep_hours' };
  Object.keys(map).forEach(function (id) {
    var el = $(id);
    if (el && typeof c[map[id]] === 'number') el.value = String(c[map[id]]);
  });
  var ack = $('audio-ack');
  if (ack) ack.checked = false;
  onStorageChange();
}

function render(d) {
  if (!$('audio-card')) return;
  _last = d;
  var badge = stateBadge(d);
  setBadge(badge[0], badge[1]);
  if (!d || d.ok !== true) {
    setText('audio-mic', '—');
    setText('audio-medium', '—');
    setText('audio-count', '—');
    setHidden('audio-record-row', true);
    notice('', true);
    return;
  }
  var installed = d.installed === true;
  setText('audio-mic', d.mic === 'present' ? uiT('подключён') : d.mic === 'busy' ? uiT('занят')
    : d.mic === 'absent' ? uiT('не подключён') : '—');
  setText('audio-medium', installed ? mediumLabel(d.medium) : '—');
  setText('audio-count', installed && Array.isArray(d.clips) ? String(d.clips.length) : '—');
  setHidden('audio-removable-note', !(installed && AU_REMOVABLE.indexOf(d.medium) >= 0));

  var recording = d.state === 'recording';
  setHidden('audio-record-row', !(installed && d.enabled));
  var rec = $('audio-btn-record');
  if (rec) rec.disabled = _busy || !recordAllowed(d);
  setHidden('audio-btn-stop', !recording);
  var secs = $('audio-seconds');
  if (secs && d.conf && d.bounds) {
    secs.max = String(d.bounds.default_duration_s ? d.bounds.default_duration_s[1] : 30);
    if (!secs.dataset.touched) secs.value = String(d.conf.default_duration_s);
  }
  if (recording) {
    if (!_recSeen) _recSeen = Date.now();
    setText('audio-elapsed', Math.round((Date.now() - _recSeen) / 1000) + ' ' + uiT('с'));
  } else {
    _recSeen = 0;
    setText('audio-elapsed', '');
  }

  var en = $('audio-btn-enable');
  if (en) {
    var on = !!d.enabled;
    en.dataset.action = on ? 'disable' : 'enable';
    en.className = 'btn btn-sm ' + (on ? 'btn-danger' : 'btn-primary');
    en.textContent = on ? uiT('Выключить') : uiT('Включить');
    en.disabled = _busy || !installed;
  }
  setHidden('audio-btn-restart', !(installed && d.enabled
    && (d.state === 'not_running' || d.state === 'conf_unreadable' || d.state === 'error')));

  renderClips(d);
  renderEvents(d);
  fillSettings(d);

  if (Date.now() < _noticeUntil) return;
  var hint = stateHint(d);
  if (!hint && d.last_status && d.last_status !== 'ok') hint = lastStatusText(d.last_status);
  notice(hint, false);
}

// ── transport ──────────────────────────────────────────────────────────────
function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise(function (_r, rej) {
    setTimeout(function () { rej(new Error('timeout')); }, ms);
  })]);
}

function get() {
  return withTimeout(fetch(AU_API, { method: 'GET', credentials: 'same-origin', cache: 'no-store' })
    .then(function (r) { return r.json(); }), AU_GET_TIMEOUT_MS);
}

function post(body) {
  return withTimeout(fetch(AU_API, {
    method: 'POST',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'same-origin',
    cache: 'no-store',
    body: JSON.stringify(body)
  }).then(function (r) { return r.json(); }), AU_POST_TIMEOUT_MS);
}

function refresh() {
  var seq = ++_seq;
  return get().then(function (d) {
    if (seq !== _seq) return null;
    if (d && d.error === 'unauthorized') return null;
    render(d);
    schedule(d);
    return d;
  }, function () {
    if (seq === _seq) render(null);
    return null;
  });
}

function stopTimers() {
  if (_poll) { clearInterval(_poll); _poll = null; }
  if (_fast) { clearTimeout(_fast); _fast = null; }
}

// 1 s ticks while a recording runs, never past its own length + 5 s.
function schedule(d) {
  if (!_active || document.visibilityState === 'hidden') return;
  var recording = d && d.state === 'recording';
  if (recording) {
    if (!_fastUntil) {
      var len = d.recording && typeof d.recording.seconds === 'number' ? d.recording.seconds : 30;
      _fastUntil = Date.now() + (len + 5) * 1000;
    }
    if (Date.now() < _fastUntil) {
      if (_poll) { clearInterval(_poll); _poll = null; }
      if (_fast) clearTimeout(_fast);
      _fast = setTimeout(refresh, AU_FAST_MS);
      return;
    }
  } else {
    _fastUntil = 0;
  }
  if (_fast) { clearTimeout(_fast); _fast = null; }
  if (!_poll) _poll = setInterval(refresh, AU_POLL_MS);
}

function setBusy(on) {
  _busy = on;
  if (_last) render(_last);
}

function mutate(body, okText) {
  if (_busy) return Promise.resolve(null);
  setBusy(true);
  ++_seq;
  return post(body).then(function (d) {
    setBusy(false);
    if (!d || d.error === 'unauthorized' || d.error_code === 'E_CSRF') return null;
    if (d.status) render(d.status);
    if (d.ok !== true) { actionNotice(errorText(d.error), false); return d; }
    if (d.done === 'failed' || d.done === 'timeout') {
      actionNotice(errorText(d.done_error || ''), false);
    } else if (okText) {
      actionNotice(okText, true);
    }
    setTimeout(refresh, 700);
    return d;
  }, function () {
    setBusy(false);
    actionNotice(uiT('Нет ответа от платы'), false);
    return null;
  });
}

// ── actions (HTML onclick) ─────────────────────────────────────────────────
function audioToggle() {
  var en = $('audio-btn-enable');
  var action = en && en.dataset.action === 'disable' ? 'disable' : 'enable';
  return mutate({ action: action }, uiT('Сохранено'));
}

function audioRestart() {
  return mutate({ action: 'restart' }, uiT('Служба перезапускается'));
}

function audioRecord() {
  var el = $('audio-seconds');
  var secs = el ? Number(el.value) : NaN;
  var max = _last && _last.conf ? _last.conf.max_duration_s : 30;
  if (!Number.isInteger(secs) || secs < 2 || secs > max) { actionNotice(errorText('bad_seconds'), false); return Promise.resolve(null); }
  _fastUntil = Date.now() + (secs + 5) * 1000;
  return mutate({ action: 'record', seconds: secs }, uiT('Запись начата'));
}

function audioStop() {
  return mutate({ action: 'stop' }, null);
}

function audioDelete(id) {
  if (!AU_ID_RE.test(String(id))) return Promise.resolve(null);
  if (!window.confirm(uiT('Удалить запись?'))) return Promise.resolve(null);
  return mutate({ action: 'delete', id: id }, uiT('Запись удалена'));
}

function audioPlay(id) {
  var player = $('audio-player');
  var url = clipUrl(id, true);
  if (!player || !url) return;
  player.hidden = false;
  player.src = url;
  var p = player.play && player.play();
  if (p && p.catch) p.catch(function () { /* the user can press play */ });
}

function onStorageChange() {
  var sel = $('audio-storage');
  var removable = !!sel && AU_REMOVABLE.indexOf(sel.value) >= 0;
  setHidden('audio-storage-warn', !removable);
  setHidden('audio-ack-row', !removable);
}

function audioSave() {
  var form = {
    storage: $('audio-storage') ? $('audio-storage').value : '',
    default_duration_s: $('audio-default-dur') ? $('audio-default-dur').value : '',
    keep_files: $('audio-keep-files') ? $('audio-keep-files').value : '',
    keep_mb: $('audio-keep-mb') ? $('audio-keep-mb').value : '',
    keep_hours: $('audio-keep-hours') ? $('audio-keep-hours').value : '',
    ack: !!($('audio-ack') && $('audio-ack').checked),
  };
  var out = saveBody(form, _last);
  if (out.error) { actionNotice(errorText(out.error), false); return Promise.resolve(null); }
  return mutate(out.body, uiT('Сохранено')).then(function (d) {
    var det = $('audio-settings');
    if (d && d.ok && det) { det.open = false; if (d.status) render(d.status); }
    return d;
  });
}

// ── tab lifecycle (app.js systemLoadSub / systemStopIntegrations) ──────────
function audioTabInit() {
  if (!$('audio-card')) return;
  _active = true;
  if (document.visibilityState === 'hidden') return;
  refresh();
}

function audioTabDestroy() {
  _active = false;
  stopTimers();
  ++_seq;
  var player = $('audio-player');
  if (player && player.pause) player.pause();
}

function onVisibility() {
  if (!_active) return;
  if (document.visibilityState === 'hidden') { stopTimers(); return; }
  refresh();
}

function refreshAudioI18n() {
  if (_active && _last) render(_last);
}

function init() {
  if (!$('audio-card')) return;
  document.addEventListener('visibilitychange', onVisibility);
  var sel = $('audio-storage');
  if (sel) sel.addEventListener('change', onStorageChange);
  var secs = $('audio-seconds');
  if (secs) secs.addEventListener('input', function () { secs.dataset.touched = '1'; });
}

window.audioTabInit = audioTabInit;
window.audioTabDestroy = audioTabDestroy;
window.audioToggle = audioToggle;
window.audioRestart = audioRestart;
window.audioRecord = audioRecord;
window.audioStop = audioStop;
window.audioSave = audioSave;
window.refreshAudioI18n = refreshAudioI18n;
window.sa02mAudioCard = {
  stateBadge: stateBadge, stateHint: stateHint, lastStatusText: lastStatusText,
  errorText: errorText, recordAllowed: recordAllowed, idDate: idDate, clipLine: clipLine,
  clipUrl: clipUrl, saveBody: saveBody, render: render,
};

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
})();
