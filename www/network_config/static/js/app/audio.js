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
// Contract §12: the token grammar the CGI accepts (write-only field).
var AU_TOKEN_RE = /^[A-Za-z0-9._~+\/=-]{1,512}$/;
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
var _inflight = null;
var _controller = null;
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
    case 'url_refused': return uiT('Этот адрес отправки запрещён (локальный или служебный)');
    case 'bad_token': return uiT('Недопустимый токен');
    case 'no_target': return uiT('Сначала выберите ленту в настройках');
    case 'audio_api_failed': return uiT('Нет ответа от платы');
    default: return uiT('Не удалось выполнить действие');
  }
}

// light_music_state (contract §22) → its short line ('' for none / unknown).
var AU_LM_STYLES = ['level', 'equalizer', 'color'];
var AU_LM_TARGET_RE = /^[A-Za-z0-9_.-]{1,64}$/;
function lightMusicText(st) {
  switch (st) {
    case 'off': return uiT('не играет');
    case 'running': return uiT('играет');
    case 'no_mic': return uiT('нет микрофона');
    case 'strip_offline': return uiT('лента недоступна');
    case 'disabled': return uiT('лента не выбрана');
    default: return '';
  }
}

// The switch is offered only with a live daemon and a configured strip.
function lightMusicAllowed(d) {
  return !!d && d.ok === true && d.live === true && !!(d.conf && d.conf.light_music_target);
}

// Same rule as the capture_gain row: a daemon older than 1.0.7.9 (a web-only
// update) has none of these keys in its conf and refuses a save carrying one
// (unknown_field), so the fields hide and the save leaves them out.
var AU_LM_CONF_KEYS = ['light_music_target', 'light_music_style', 'light_music_rate_hz'];
function lightMusicSupported(d) {
  var c = d && d.conf;
  return !!c && typeof c === 'object' && AU_LM_CONF_KEYS.every(function (k) { return k in c; });
}

// The strip / picture / rate inputs carry no id on their `.field` wrappers.
function showLightMusicFields(show) {
  ['audio-lm-target', 'audio-lm-style', 'audio-lm-rate'].forEach(function (id) {
    var el = $(id);
    var field = el && el.closest ? el.closest('.field') : null;
    if (field) field.hidden = !show;
  });
}

// last_upload / a clip's upload state → its short line ('' for none).
function uploadText(st) {
  switch (st) {
    case 'pending': return uiT('отправляется');
    case 'ok': return uiT('отправлено');
    case 'failed': return uiT('не отправлено');
    case 'refused': return uiT('адрес запрещён');
    case 'unavailable': return uiT('отправка недоступна');
    default: return '';
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
  // The sidecar's exact duration when there is one, else ≈ from the size.
  var exact = c && typeof c.duration_ms === 'number';
  var secs = exact ? c.duration_ms / 1000 : Math.max(0, (size - AU_WAV_HEADER) / AU_BYTES_PER_S);
  return {
    when: when ? when.toLocaleString() : String((c && c.id) || '?'),
    medium: mediumLabel(c && c.medium),
    dur: (exact ? '' : '≈') + secs.toFixed(1) + ' ' + uiT('с'),
    size: (size / 1024).toFixed(0) + ' ' + uiT('КБ'),
    peak: c && typeof c.peak_dbfs === 'number' ? c.peak_dbfs + ' ' + uiT('дБFS') : '',
    upload: uploadText(c && c.upload),
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
  if (typeof form.upload_url === 'string') {
    var url = form.upload_url.trim();
    if (url && !/^https?:\/\/[^\s]+$/.test(url)) return { error: 'invalid_value' };
    body.upload_url = url;
  }
  if (typeof form.delete_after_upload === 'boolean') body.delete_after_upload = form.delete_after_upload;
  if (typeof form.light_music_target === 'string') {
    var target = form.light_music_target;
    var leds = d && Array.isArray(d.led_devices) ? d.led_devices : [];
    // Only "" or a strip the board's bridge has; the CGI checks the same.
    if (target && (!AU_LM_TARGET_RE.test(target) || leds.indexOf(target) < 0)) return { error: 'invalid_value' };
    body.light_music_target = target;
  }
  if (typeof form.light_music_style === 'string') {
    if (AU_LM_STYLES.indexOf(form.light_music_style) < 0) return { error: 'invalid_value' };
    body.light_music_style = form.light_music_style;
  }
  if (form.light_music_rate_hz !== undefined) {
    var rate = Number(form.light_music_rate_hz);
    var rlim = Array.isArray(b.light_music_rate_hz) ? b.light_music_rate_hz : [1, 10];
    if (!Number.isInteger(rate) || rate < rlim[0] || rate > rlim[1]) return { error: 'invalid_value' };
    body.light_music_rate_hz = rate;
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
    meta.textContent = [line.medium, line.dur, line.size, line.peak, line.upload]
      .filter(function (x) { return x; }).join(' · ');
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
  // Not an edited field: keep it current while the form is open (language
  // switch, or the token action's own answer).
  if (d && d.installed) {
    setText('audio-token-state', d.token_set ? uiT('Токен задан') : uiT('Токен не задан'));
  }
  showLightMusicFields(lightMusicSupported(d));
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
  var url = $('audio-upload-url');
  if (url && typeof c.upload_url === 'string') url.value = c.upload_url;
  var del = $('audio-delete-after');
  if (del) del.checked = c.delete_after_upload !== false;
  fillTargets(d);
  var style = $('audio-lm-style');
  if (style && AU_LM_STYLES.indexOf(c.light_music_style) >= 0) style.value = c.light_music_style;
  var rate = $('audio-lm-rate');
  if (rate && typeof c.light_music_rate_hz === 'number') rate.value = String(c.light_music_rate_hz);
  onStorageChange();
}

// The strip select: «— нет —» plus the bridge's `type: led` ids, as text.
function fillTargets(d) {
  var sel = $('audio-lm-target');
  if (!sel) return;
  while (sel.firstChild) sel.removeChild(sel.firstChild);
  var ids = [''].concat((Array.isArray(d.led_devices) ? d.led_devices : [])
    .filter(function (id) { return typeof id === 'string' && AU_LM_TARGET_RE.test(id); }));
  var current = d.conf && typeof d.conf.light_music_target === 'string' ? d.conf.light_music_target : '';
  if (current && ids.indexOf(current) < 0 && AU_LM_TARGET_RE.test(current)) ids.push(current);
  ids.forEach(function (id) {
    var opt = document.createElement('option');
    opt.value = id;
    opt.textContent = id || uiT('— нет —');
    sel.appendChild(opt);
  });
  sel.value = current;
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
    setText('audio-lm-state', '—');
    setHidden('audio-record-row', true);
    setHidden('audio-lm-row', true);
    notice('', true);
    return;
  }
  var installed = d.installed === true;
  setText('audio-mic', d.mic === 'present' ? uiT('подключён') : d.mic === 'busy' ? uiT('занят')
    : d.mic === 'absent' ? uiT('не подключён') : '—');
  setText('audio-medium', installed ? mediumLabel(d.medium) : '—');
  setText('audio-count', installed && Array.isArray(d.clips) ? String(d.clips.length) : '—');
  var up = uploadText(d.last_upload);
  if (up && d.last_file) setText('audio-last-upload', uiT('Последняя запись') + ': ' + up);
  else setText('audio-last-upload', '');
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

  setHidden('audio-lm-row', !(installed && d.enabled));
  var lm = $('audio-lm-on');
  if (lm) {
    lm.checked = d.light_music === true;
    lm.disabled = _busy || !lightMusicAllowed(d);
  }
  setText('audio-lm-state', lightMusicText(d.live ? d.light_music_state : '') || '—');

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
  renderGain(d);

  if (Date.now() < _noticeUntil) return;
  var hint = stateHint(d);
  if (!hint && d.last_status && d.last_status !== 'ok') hint = lastStatusText(d.last_status);
  notice(hint, false);
}

// ── transport ──────────────────────────────────────────────────────────────
function get(signal) {
  return fetchWithTimeout(AU_API, { method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: signal }, AU_GET_TIMEOUT_MS)
    .then(function (r) { return r.json(); });
}

function post(body) {
  return fetchWithTimeout(AU_API, {
    method: 'POST',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'same-origin', cache: 'no-store', body: JSON.stringify(body)
  }, AU_POST_TIMEOUT_MS).then(function (r) { return r.json(); });
}

function refresh() {
  if (_inflight) return _inflight;
  var seq = ++_seq;
  var controller = new AbortController();
  _controller = controller;
  var pending = get(controller.signal).then(function (d) {
    if (seq !== _seq) return null;
    if (d && d.error === 'unauthorized') return null;
    render(d);
    schedule(d);
    return d;
  }, function () {
    if (seq === _seq) { render(null); schedule(null); }
    return null;
  }).finally(function () {
    if (_inflight === pending) _inflight = null;
    if (_controller === controller) _controller = null;
  });
  _inflight = pending;
  return pending;
}

function stopTimers() {
  ++_seq;
  if (_controller) _controller.abort();
  _controller = null;
  _inflight = null;
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

function audioLightMusic() {
  var lm = $('audio-lm-on');
  if (!lm) return Promise.resolve(null);
  var on = !!lm.checked;
  if (!lightMusicAllowed(_last)) {
    lm.checked = !on;
    actionNotice(errorText(_last && _last.live ? 'no_target' : 'not_running'), false);
    return Promise.resolve(null);
  }
  return mutate({ action: 'light_music', on: on },
    on ? uiT('Светомузыка включена') : uiT('Светомузыка выключена'));
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
  if ($('audio-upload-url')) form.upload_url = $('audio-upload-url').value;
  if ($('audio-delete-after')) form.delete_after_upload = !!$('audio-delete-after').checked;
  if (lightMusicSupported(_last)) {
    if ($('audio-lm-target')) form.light_music_target = $('audio-lm-target').value;
    if ($('audio-lm-style')) form.light_music_style = $('audio-lm-style').value;
    if ($('audio-lm-rate')) form.light_music_rate_hz = $('audio-lm-rate').value;
  }
  var out = saveBody(form, _last);
  out = gainSaveMerge(out, _last);
  if (out.error) { actionNotice(errorText(out.error), false); return Promise.resolve(null); }
  return mutate(out.body, uiT('Сохранено')).then(function (d) {
    var det = $('audio-settings');
    if (d && d.ok && det) { det.open = false; if (d.status) render(d.status); }
    return d;
  });
}

// The token is write-only: the field is cleared after a save, the card shows
// only «задан / не задан».
function tokenBody(raw) {
  var t = String(raw || '').trim();
  if (!AU_TOKEN_RE.test(t)) return { error: 'bad_token' };
  return { body: { action: 'set_token', token: t } };
}

function audioSaveToken() {
  var el = $('audio-token');
  var out = tokenBody(el ? el.value : '');
  if (out.error) { actionNotice(errorText(out.error), false); return Promise.resolve(null); }
  return mutate(out.body, uiT('Токен сохранён')).then(function (d) {
    if (!(d && d.ok)) return d;
    if (el) el.value = '';
    setText('audio-token-state', d.token_set ? uiT('Токен задан') : uiT('Токен не задан'));
    return d;
  });
}

function audioClearToken() {
  return mutate({ action: 'set_token', token: '' }, uiT('Токен удалён')).then(function (d) {
    if (d && d.ok) setText('audio-token-state', uiT('Токен не задан'));
    return d;
  });
}

// ── mic capture gain (contract §8 `capture_gain`, §9 `mic_gain`) ───────────
// Self-contained block; render() and audioSave() each call in once. The row
// shows only when the daemon's conf carries the key: an older daemon (a
// web-only update) never sees an unknown field in its save body. The device
// range is the daemon's to read (the CGI cannot open the mixer), so the
// client clamps only to a range the daemon reported.
var AU_GAIN_GRAMMAR = 100000;           // config.py static grammar bound, not a device range
var AU_GAIN_DB_RE = /^[+-]?[0-9]{1,3}(\.[0-9]{1,3})?$/;

function gainSupported(d) {
  return !!(d && d.conf && typeof d.conf === 'object' && 'capture_gain' in d.conf);
}

// mic_gain.min/max → [min, max] when both are integers in order, else null.
function gainRange(g) {
  if (!g || !Number.isInteger(g.min) || !Number.isInteger(g.max) || g.min > g.max) return null;
  return [g.min, g.max];
}

// amixer's own dB string → "+18,75 дБ" (RU decimal comma), or ''.
function gainDb(db) {
  if (typeof db !== 'string' || !AU_GAIN_DB_RE.test(db)) return '';
  var s = db.charAt(0) === '+' || db.charAt(0) === '-' ? db : (Number(db) > 0 ? '+' : '') + db;
  var en = window.sa02mI18n && window.sa02mI18n.lang === 'en';
  return (en ? s : s.replace('.', ',')) + ' ' + uiT('дБ');
}

function gainRangeText(r) {
  return r ? r[0] + '…' + r[1] : '';
}

// The one-line hint under the field: the range, or why there is none yet.
function gainHint(d) {
  var r = gainRange(d && d.mic_gain);
  return r ? uiT('Диапазон') + ': ' + gainRangeText(r) : uiT('Диапазон — когда есть микрофон');
}

// mic_gain.state → the one status line (§9); conf alone when the daemon has
// not reported yet.
function gainStateText(d) {
  var g = d && d.mic_gain && typeof d.mic_gain === 'object' ? d.mic_gain : null;
  var r = gainRange(g);
  var want = g && Number.isInteger(g.want) ? g.want : (d && d.conf && Number.isInteger(d.conf.capture_gain) ? d.conf.capture_gain : null);
  switch (g ? g.state : '') {
    case 'off': return uiT('Не задано — уровень не меняется');
    case 'applied': {
      var v = Number.isInteger(g.applied) ? g.applied : want;
      var db = gainDb(g.db);
      return uiT('Сейчас') + ': ' + (v === null ? '—' : v) + (r ? ' ' + uiT('из') + ' ' + gainRangeText(r) : '')
        + (db ? ' (' + db + ')' : '');
    }
    case 'out_of_range':
      return (want === null ? '' : want + ' ') + (r ? uiT('вне') + ' ' + gainRangeText(r) + ' ' : '')
        + '— ' + uiT('не применено');
    case 'no_control': return uiT('Нет регулятора усиления');
    case 'no_card': return uiT('Карта микрофона не определена');
    case 'failed': return uiT('Не удалось задать усиление');
    case 'no_mic': return uiT('Ждёт подключения микрофона');
    default:
      if (d && d.conf && d.conf.capture_gain === null) return uiT('Не задано — уровень не меняется');
      return uiT('Результат пока неизвестен');
  }
}

// The checkbox + field → {value: int|null} or {error}; clamps to the
// daemon-reported range only (`clamped` tells the caller to show it).
function gainValue(on, raw, d) {
  if (!on) return { value: null };
  var s = String(raw == null ? '' : raw).trim();
  if (!/^-?[0-9]{1,6}$/.test(s)) return { error: 'invalid_value' };
  var v = Number(s);
  if (v < -AU_GAIN_GRAMMAR || v > AU_GAIN_GRAMMAR) return { error: 'invalid_value' };
  var r = gainRange(d && d.mic_gain);
  if (r && (v < r[0] || v > r[1])) return { value: v < r[0] ? r[0] : r[1], clamped: true };
  return { value: v };
}

// audioSave hook: adds capture_gain to a built save body, or an error.
function gainSaveMerge(out, d) {
  if (!out || out.error || !out.body || !gainSupported(d)) return out;
  var cb = $('audio-gain-on');
  var inp = $('audio-gain');
  var g = gainValue(!!(cb && cb.checked), inp ? inp.value : '', d);
  if (g.error) return { error: g.error };
  if (g.clamped && inp) inp.value = String(g.value);
  out.body.capture_gain = g.value;
  return out;
}

function gainSyncInput() {
  var cb = $('audio-gain-on');
  var inp = $('audio-gain');
  if (inp) inp.disabled = !(cb && cb.checked);
}

// render hook: visibility, hint and status line always (not edited fields);
// the checkbox and the value only while the form is closed, like fillSettings.
function renderGain(d) {
  var row = $('audio-gain-row');
  if (!row) return;
  var ok = !!(d && d.installed && gainSupported(d));
  row.hidden = !ok;
  if (!ok) return;
  var cb = $('audio-gain-on');
  var inp = $('audio-gain');
  var r = gainRange(d.mic_gain);
  if (inp) {
    inp.min = r ? String(r[0]) : '';
    inp.max = r ? String(r[1]) : '';
  }
  if (cb && !cb.dataset.bound) {
    cb.dataset.bound = '1';
    cb.addEventListener('change', gainSyncInput);
  }
  setText('audio-gain-hint', gainHint(d));
  setText('audio-gain-state', gainStateText(d));
  var det = $('audio-settings');
  if (det && (det.hidden || det.open)) return;   // never overwrite what is being edited
  var cur = d.conf.capture_gain;
  if (cb) cb.checked = Number.isInteger(cur);
  if (inp) inp.value = Number.isInteger(cur) ? String(cur) : '';
  gainSyncInput();
}

window.sa02mAudioGain = {
  gainSupported: gainSupported, gainRange: gainRange, gainDb: gainDb, gainHint: gainHint,
  gainStateText: gainStateText, gainValue: gainValue, gainSaveMerge: gainSaveMerge, renderGain: renderGain,
};

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
  var lm = $('audio-lm-on');
  if (lm) lm.addEventListener('change', audioLightMusic);
}

window.audioTabInit = audioTabInit;
window.audioTabDestroy = audioTabDestroy;
window.audioToggle = audioToggle;
window.audioRestart = audioRestart;
window.audioRecord = audioRecord;
window.audioStop = audioStop;
window.audioSave = audioSave;
window.audioSaveToken = audioSaveToken;
window.audioClearToken = audioClearToken;
window.refreshAudioI18n = refreshAudioI18n;
window.sa02mAudioCard = {
  stateBadge: stateBadge, stateHint: stateHint, lastStatusText: lastStatusText,
  errorText: errorText, recordAllowed: recordAllowed, idDate: idDate, clipLine: clipLine,
  clipUrl: clipUrl, saveBody: saveBody, render: render, uploadText: uploadText, tokenBody: tokenBody,
  lightMusicText: lightMusicText, lightMusicAllowed: lightMusicAllowed, audioLightMusic: audioLightMusic,
};

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
})();
