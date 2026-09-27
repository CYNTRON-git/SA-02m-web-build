/* SA-02m — Apple HomeKit bridge card (вкладка «Управление»). Talks only to
   cgi-bin/sa02m_homekit_api.cgi (JSON per action: docs/contracts/homekit-bridge.md).
   Polls every 10 s ONLY while «Управление» is the active tab and the page is
   visible (window.homekitTabInit/Destroy from app.js switchTab), plus a short
   fast-poll window after an action. The setup code and its QR are fetched only
   on an explicit «Показать код» and live in the DOM only while the bridge is
   running and unpaired. The «Умный дом» modal (app/smarthome.js) reads whether
   the module is installed through window.sa02mHomekitInstalled and the
   `sa02m-homekit-status` document event. */

(function () {
'use strict';

function uiT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(String(s)) : String(s);
}

function $(id) { return document.getElementById(id); }

const HK_API = 'cgi-bin/sa02m_homekit_api.cgi';
const HK_POLL_MS = 10000;
// GET = dispatch only (CGI budget 8 s); a POST may add the privileged nudge
// (11 s) — both stay under nginx's 20 s read timeout, so the browser waits a
// little longer than the CGI can take and never longer.
const HK_GET_TIMEOUT_MS = 10000;
const HK_POST_TIMEOUT_MS = 21000;
const HK_IFACE_RE = /^eth[01]$/;
const HK_QR_QUIET = 4;
const SVG_NS = 'http://www.w3.org/2000/svg';

// state → [RU label, badge kind]. Unknown ⇒ «н/д».
const HOMEKIT_STATE_MAP = {
  not_installed: ['не установлен', 'unk'],
  disabled: ['выключен', 'unk'],
  starting: ['запуск…', 'warn'],
  running: ['работает', 'ok'],
  missing_deps: ['нет компонентов', 'err'],
  no_interface: ['нет сети', 'warn'],
  port_in_use: ['порт занят', 'err'],
  error: ['ошибка', 'err'],
};

// Projection skip reasons (sa02m_homekit/projection.py SKIP_REASONS) → RU.
const HOMEKIT_SKIP_MAP = {
  range_unsupported: 'диапазон не поддерживается',
  capability_unsupported: 'умение не поддерживается',
  needs_threshold: 'нужен порог срабатывания',
  no_homekit_type: 'нет типа в HomeKit',
  unit_unsupported: 'единица не поддерживается',
  nothing_mappable: 'нечего передать',
  hidden: 'не отмечено для HomeKit',
  bridge_full: 'мост заполнен (149)',
};

// `{"ok":false,"error":…}` codes of the dispatch/CGI → RU (never the raw code
// when a phrase exists).
const HOMEKIT_ERROR_MAP = {
  invalid_interface: 'Недопустимый интерфейс',
  invalid_port: 'Недопустимый порт',
  not_installed: 'HomeKit не установлен — нужна полная установка (install.sh)',
  conf_write_failed: 'Не удалось сохранить настройки',
  not_available: 'Код недоступен — мост сопряжён или не запущен',
  homekit_api_failed: 'Нет ответа от платы',
  payload_too_large: 'Ошибка запроса API HomeKit',
  invalid_json: 'Ошибка запроса API HomeKit',
  not_found: 'Ошибка запроса API HomeKit',
  method_not_allowed: 'Ошибка запроса API HomeKit',
};

function hkBadgeHtml(text, kind) {
  const cls = kind === 'ok' ? 'badge-ok' : kind === 'warn' ? 'badge-warn' : kind === 'err' ? 'badge-err' : 'badge-unk';
  return '<span class="badge ' + cls + '">' + escHtml(String(text)) + '</span>';
}

function hkSetBadge(text, kind) {
  const el = $('homekit-state');
  if (el) el.innerHTML = hkBadgeHtml(uiT(text), kind);
}

function hkSetText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

// The standing card line — owned by the renderer, shown for as long as the
// state lasts (a toast would say it once and never again). ok: true/false
// tints it, null is a neutral hint.
function hkSetCardMsg(text, ok) {
  const msg = $('homekit-msg');
  if (!msg) return;
  if (!text) {
    msg.hidden = true;
    msg.textContent = '';
    msg.className = 'cloud-msg';
    return;
  }
  msg.hidden = false;
  msg.textContent = text;
  msg.className = 'cloud-msg' + (ok === null ? '' : (ok ? ' is-ok' : ' is-err'));
}

// Action feedback is ephemeral → the viewport toast.
function hkNotice(text, ok) {
  if (!text) return;
  if (typeof cardNotice === 'function') cardNotice(text, ok);
  else if (typeof toast === 'function') toast(text, ok === false ? 'error' : 'success', 5000);
}

function hkErrorText(d) {
  const code = d && typeof d.error === 'string' ? d.error : '';
  return uiT(HOMEKIT_ERROR_MAP[code] || 'Ошибка запроса API HomeKit');
}

// ── Transport ──────────────────────────────────────────────────────────────
function hkFetch(opt, ms) {
  if (typeof fetchWithTimeout === 'function') return fetchWithTimeout(HK_API, opt, ms);
  return fetch(HK_API, opt);
}

function hkGet() {
  return hkFetch({ method: 'GET', credentials: 'same-origin', cache: 'no-store' }, HK_GET_TIMEOUT_MS)
    .then(function (r) { return r.json(); });
}

function hkPost(body) {
  return hkFetch({
    method: 'POST',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'same-origin',
    cache: 'no-store',
    body: JSON.stringify(body),
  }, HK_POST_TIMEOUT_MS).then(function (r) { return r.json(); });
}

// ── State ──────────────────────────────────────────────────────────────────
let _hkLast = null;          // last rendered status (null = none / no answer)
let _hkLastFailed = false;   // the last poll got no usable answer
let _hkBusy = false;         // an action is in flight — every button disabled
let _hkSeq = 0;              // newest request wins; an older answer never renders
let _hkInflight = null;
let _hkSkipOpen = false;
let _hkCodeShown = false;
let _hkTabActive = false;
let _hkPoll = null;
let _hkFastPoll = null;
let _hkFastPollGen = 0;

// null until the first answer; then true unless the board says not_installed.
window.sa02mHomekitInstalled = null;

function hkPublishInstalled(installed) {
  if (window.sa02mHomekitInstalled === installed) return;
  window.sa02mHomekitInstalled = installed;
  try {
    document.dispatchEvent(new CustomEvent('sa02m-homekit-status', { detail: { installed: installed } }));
  } catch (e) {
    /* CustomEvent unsupported — smarthome.js re-reads the flag on modal open */
  }
}

// ── QR (server-computed matrix → SVG rects, no library) ─────────────────────
function hkValidQr(rows) {
  if (!Array.isArray(rows) || rows.length < 21 || rows.length > 177) return false;
  const n = rows.length;
  return rows.every(function (r) { return typeof r === 'string' && r.length === n && /^[01]+$/.test(r); });
}

// Horizontal runs of dark modules become one rect each (a 25×25 code draws
// ~200 rects instead of ~300). Returns the number of module rects drawn.
function hkDrawQr(svg, rows) {
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const n = rows.length;
  const size = n + 2 * HK_QR_QUIET;
  svg.setAttribute('viewBox', '0 0 ' + size + ' ' + size);
  const bg = document.createElementNS(SVG_NS, 'rect');
  bg.setAttribute('class', 'homekit-qr-bg');
  bg.setAttribute('x', '0');
  bg.setAttribute('y', '0');
  bg.setAttribute('width', String(size));
  bg.setAttribute('height', String(size));
  svg.appendChild(bg);
  let drawn = 0;
  for (let y = 0; y < n; y++) {
    const row = rows[y];
    let x = 0;
    while (x < n) {
      if (row.charAt(x) !== '1') { x++; continue; }
      let end = x;
      while (end < n && row.charAt(end) === '1') end++;
      const m = document.createElementNS(SVG_NS, 'rect');
      m.setAttribute('class', 'homekit-qr-m');
      m.setAttribute('x', String(x + HK_QR_QUIET));
      m.setAttribute('y', String(y + HK_QR_QUIET));
      m.setAttribute('width', String(end - x));
      m.setAttribute('height', '1');
      svg.appendChild(m);
      drawn++;
      x = end;
    }
  }
  return drawn;
}

// Removes the code and the QR from the DOM (not merely hides them).
function hkClearCode() {
  _hkCodeShown = false;
  const svg = $('homekit-qr');
  if (svg) while (svg.firstChild) svg.removeChild(svg.firstChild);
  hkSetText('homekit-code', '');
  const box = $('homekit-code-box');
  if (box) box.hidden = true;
}

// ── Render ─────────────────────────────────────────────────────────────────
// Disabled while an action is pending, and while the board is not answering
// (the controls keep the last known layout, but nothing is sent blind).
function hkSetButtonsDisabled() {
  const d = _hkLast;
  const blocked = _hkBusy || _hkLastFailed || !d;
  const installed = !!(d && d.state !== 'not_installed');
  const en = $('homekit-btn-enable');
  if (en) en.disabled = blocked || !installed;
  const reset = $('homekit-btn-reset');
  if (reset) reset.disabled = blocked;
  const code = $('homekit-btn-code');
  if (code) code.disabled = blocked || (!_hkCodeShown && (!d.setup_available || !!d.pair_setup_locked));
  ['homekit-btn-net', 'homekit-iface-sel', 'homekit-port-in'].forEach(function (id) {
    const el = $(id);
    if (el) el.disabled = blocked || !installed;
  });
}

function hkSetBusy(on) {
  _hkBusy = !!on;
  hkSetButtonsDisabled();
}

function hkIfaceLabel(it) {
  const addr = it && typeof it.address === 'string' && it.address ? it.address : uiT('нет адреса');
  return it.name + ' · ' + addr;
}

// The picker offers the wired ports the board reports (the dispatch knows
// only eth0/eth1 — never a modem); an absent port is listed only when it is
// the configured one, so the stored value stays representable.
function hkFillNet(d) {
  const det = $('homekit-net');
  const sel = $('homekit-iface-sel');
  const port = $('homekit-port-in');
  if (!det) return;
  det.hidden = !d || d.state === 'not_installed';
  if (det.hidden) return;
  if (sel && document.activeElement !== sel) {
    const want = HK_IFACE_RE.test(String(d.interface || '')) ? d.interface : '';
    const list = (Array.isArray(d.interfaces) ? d.interfaces : []).filter(function (it) {
      return it && HK_IFACE_RE.test(String(it.name || '')) && (it.present || it.name === want);
    });
    while (sel.options.length) sel.remove(0);
    list.forEach(function (it) {
      const opt = document.createElement('option');
      opt.value = it.name;
      opt.textContent = hkIfaceLabel(it);
      sel.appendChild(opt);
    });
    if (want) sel.value = want;
  }
  if (port && document.activeElement !== port && !det.open) {
    if (typeof d.port_min === 'number') port.min = String(d.port_min);
    if (typeof d.port_max === 'number') port.max = String(d.port_max);
    port.value = typeof d.port === 'number' ? String(d.port) : '';
  }
}

// `properties.float:voltage` → `voltage`: the instance names the reading; the
// type prefix is protocol detail the integrator does not need.
function hkItemLabel(item) {
  const s = String(item);
  const i = s.lastIndexOf(':');
  return i >= 0 && i < s.length - 1 ? s.slice(i + 1) : s.split('.').pop();
}

function hkRenderSkipped(d) {
  const line = $('homekit-skip-line');
  const list = $('homekit-skip-list');
  const link = $('homekit-skip-link');
  const total = d && typeof d.skipped_total === 'number' ? d.skipped_total : 0;
  const rows = d && Array.isArray(d.skipped) ? d.skipped : [];
  if (line) line.hidden = total <= 0;
  // One text run: the link is inline-flex, so separate spans would lose the
  // spaces between them.
  hkSetText('homekit-skip-text', uiT('не передаётся') + ': ' + total + ' ›');
  if (total <= 0) _hkSkipOpen = false;
  if (link) link.setAttribute('aria-expanded', _hkSkipOpen ? 'true' : 'false');
  if (!list) return;
  list.hidden = !_hkSkipOpen;
  while (list.firstChild) list.removeChild(list.firstChild);
  if (!_hkSkipOpen) return;
  rows.forEach(function (s) {
    if (!s || typeof s !== 'object') return;
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'homekit-skip-name';
    name.textContent = String(s.name || s.device_id || '?') + (s.item ? ' · ' + hkItemLabel(s.item) : '');
    const why = document.createElement('span');
    why.className = 'homekit-skip-why';
    const reason = String(s.reason || '');
    why.textContent = HOMEKIT_SKIP_MAP[reason] ? uiT(HOMEKIT_SKIP_MAP[reason]) : reason;
    li.appendChild(name);
    li.appendChild(document.createTextNode(' — '));
    li.appendChild(why);
    list.appendChild(li);
  });
  if (total > rows.length) {
    const li = document.createElement('li');
    li.className = 'homekit-skip-more';
    li.textContent = uiT('и ещё') + ' ' + (total - rows.length);
    list.appendChild(li);
  }
}

// The standing explanation for the current state (null = none).
function hkStateLine(d) {
  const st = d.state;
  const reason = d.reason || '';
  if (st === 'not_installed') return [uiT('Нужна полная установка (install.sh)'), null];
  if (st === 'missing_deps') return [uiT('Не установлены компоненты HomeKit — нужна полная установка (install.sh)'), false];
  if (st === 'error' && reason === 'status_stale') return [uiT('Мост не отвечает — статус устарел'), false];
  if (st === 'error') return [uiT('Ошибка моста — подробности в журнале sa02m-homekit'), false];
  if (st === 'no_interface') return [uiT('На выбранном интерфейсе нет адреса'), false];
  if (st === 'port_in_use') return [uiT('Порт занят другой программой — выберите другой'), false];
  if (st === 'running' && (d.pair_setup_locked || reason === 'pair_setup_locked') && d.paired === false) {
    return [uiT('Сопряжение заблокировано после 100 неудачных попыток — перезапустите мост'), false];
  }
  if (reason === 'identity_regenerated' || reason === 'state_corrupt_regenerated') {
    return [uiT('Мост создан заново — добавьте его в «Дом» повторно'), null];
  }
  return null;
}

// No usable answer (timeout, 5xx, a non-status body): every value «н/д» and
// ONE standing line — never a toast per poll.
function hkRenderUnavailable() {
  _hkLastFailed = true;
  hkSetBadge('н/д', 'unk');
  hkSetText('homekit-paired', uiT('н/д'));
  hkSetText('homekit-acc', '—');
  hkSetText('homekit-iface', '—');
  hkSetText('homekit-addr', '—');
  _hkSkipOpen = false;
  hkRenderSkipped(null);
  hkSetCardMsg(uiT('Нет ответа от платы'), false);
  if (_hkLast) {
    // Keep the last known layout; hkSetButtonsDisabled refuses every action.
    const setup = $('homekit-setup');
    if (setup) setup.hidden = true;
    hkClearCode();
  } else {
    const en = $('homekit-btn-enable');
    if (en) { en.textContent = uiT('Включить'); en.className = 'btn btn-sm btn-primary'; }
  }
  hkSetButtonsDisabled();
}

function hkRender(d) {
  if (!$('homekit-card')) return;
  if (!d || d.ok !== true || typeof d.state !== 'string') {
    hkRenderUnavailable();
    return;
  }
  _hkLast = d;
  _hkLastFailed = false;
  const st = d.state;
  const installed = st !== 'not_installed';
  hkPublishInstalled(installed);

  const stale = st === 'error' && d.reason === 'status_stale';
  const entry = stale ? ['не отвечает', 'err'] : (HOMEKIT_STATE_MAP[st] || ['н/д', 'unk']);
  hkSetBadge(entry[0], entry[1]);

  const running = st === 'running';
  let paired;
  if (running && d.paired === true) paired = uiT('сопряжено') + ': ' + (typeof d.pairings === 'number' ? d.pairings : '?');
  else if (running && d.paired === false) paired = uiT('не сопряжено');
  else paired = uiT('н/д');
  hkSetText('homekit-paired', paired);
  hkSetText('homekit-acc', running && typeof d.accessories === 'number' ? String(d.accessories) : '—');
  hkSetText('homekit-iface', installed && d.interface ? String(d.interface) : '—');
  hkSetText('homekit-addr', running && d.address ? String(d.address) + ':' + d.port : '—');

  hkRenderSkipped(d);

  // Setup block: only while running ∧ unpaired. Leaving that state removes
  // the code from the DOM at once (a pairing just completed, a stop, a reset).
  const setupOpen = running && d.paired === false;
  const setup = $('homekit-setup');
  if (setup) setup.hidden = !setupOpen;
  if (!setupOpen || !d.setup_available) hkClearCode();
  const codeBtn = $('homekit-btn-code');
  if (codeBtn) codeBtn.textContent = uiT(_hkCodeShown ? 'Скрыть код' : 'Показать код');

  hkFillNet(d);

  const reset = $('homekit-btn-reset');
  if (reset) reset.hidden = !installed || d.paired === false;
  const en = $('homekit-btn-enable');
  if (en) {
    const on = !!d.enabled;
    en.dataset.action = on ? 'disable' : 'enable';
    en.className = 'btn btn-sm ' + (on ? 'btn-danger' : 'btn-primary');
    en.textContent = uiT(on ? 'Выключить' : 'Включить');
  }

  const line = hkStateLine(d);
  if (line) hkSetCardMsg(line[0], line[1]);
  else hkSetCardMsg('', true);
  hkSetButtonsDisabled();
}

// ── Poll ───────────────────────────────────────────────────────────────────
// Returns the rendered status, or null when nothing was rendered (no answer,
// unauthorized, or a newer request overtook this one).
function hkRefresh() {
  if (_hkInflight) return _hkInflight;
  const seq = ++_hkSeq;
  _hkInflight = hkGet()
    .then(function (d) {
      if (seq !== _hkSeq) return null;
      // The session died: the fetch guard / status poll own the login flow.
      if (d && d.error === 'unauthorized') return null;
      hkRender(d);
      return d && d.ok === true ? d : null;
    }, function () {
      if (seq === _hkSeq) hkRender(null);
      return null;
    })
    .finally(function () { _hkInflight = null; });
  return _hkInflight;
}

function hkPageVisible() {
  return document.visibilityState !== 'hidden';
}

function hkStopPolls() {
  if (_hkPoll) { clearInterval(_hkPoll); _hkPoll = null; }
  if (_hkFastPoll) { clearTimeout(_hkFastPoll); _hkFastPoll = null; }
  _hkFastPollGen += 1;   // retire an in-flight fast-poll chain
}

function hkStartPoll() {
  if (!_hkTabActive || !hkPageVisible() || !$('homekit-card')) return;
  if (!_hkPoll && !_hkFastPoll) _hkPoll = setInterval(hkRefresh, HK_POLL_MS);
}

// After an action: 1 s ticks for ≤ 20 s, CHAINED (each tick waits for its own
// answer — the CGI forks python, never stack requests on the ARM target), ending
// early once `settled(d)` holds. The base poll is parked meanwhile.
function hkFastPoll(settled) {
  const until = Date.now() + 20000;
  _hkFastPollGen += 1;
  const gen = _hkFastPollGen;
  if (_hkFastPoll) clearTimeout(_hkFastPoll);
  if (_hkPoll) { clearInterval(_hkPoll); _hkPoll = null; }
  const stop = function () {
    _hkFastPoll = null;
    hkStartPoll();
  };
  const tick = async function () {
    _hkFastPoll = null;
    let done = false;
    try {
      const d = await hkRefresh();
      done = !!d && settled(d);
    } catch (e) {
      /* transport hiccup — keep the window running */
    }
    if (gen !== _hkFastPollGen) return;
    if (done || Date.now() >= until) { stop(); return; }
    _hkFastPoll = setTimeout(tick, 1000);
  };
  _hkFastPoll = setTimeout(tick, 700);
}

function hkSettledState(d) { return d.state !== 'starting'; }

// ── Actions ────────────────────────────────────────────────────────────────
// One mutation at a time; the answer carries the merged status, rendered at
// once. Returns the response (null on transport failure / unauthorized).
async function hkMutate(body, okText) {
  if (_hkBusy) return null;
  hkSetBusy(true);
  ++_hkSeq;   // a poll already in flight must not paint over this answer
  const seq = _hkSeq;
  let d = null;
  try {
    d = await hkPost(body);
  } catch (e) {
    hkNotice(uiT('Нет ответа от платы'), false);
    hkSetBusy(false);
    return null;
  }
  hkSetBusy(false);
  if (!d || d.error === 'unauthorized') return null;
  // E_CSRF is handled (refresh/retry/logout) by the global fetch guard.
  if (d.error_code === 'E_CSRF') return null;
  if (d.ok !== true) {
    hkNotice(hkErrorText(d), false);
    if (d.error === 'not_installed' && seq === _hkSeq) hkRender({ ok: true, state: 'not_installed', enabled: false });
    return d;
  }
  if (d.trigger === 'failed' || d.trigger === 'timeout') {
    hkNotice(uiT('Настройки сохранены, но служба не ответила'), false);
  } else if (okText) {
    hkNotice(okText, true);
  }
  if (d.status && seq === _hkSeq) hkRender(d.status);
  return d;
}

async function homekitToggle() {
  const btn = $('homekit-btn-enable');
  const action = btn && btn.dataset.action === 'disable' ? 'disable' : 'enable';
  if (action === 'disable') hkClearCode();
  const d = await hkMutate({ action: action }, uiT('Сохранено'));
  if (d && d.ok) hkFastPoll(hkSettledState);
}

async function homekitResetPairing() {
  if (!window.confirm(uiT('Все iPhone и iPad потеряют доступ к мосту. Их придётся добавить заново.'))) return;
  hkClearCode();
  const d = await hkMutate({ action: 'reset_pairing' }, uiT('Сопряжение сброшено'));
  if (d && d.ok) hkFastPoll(function (s) { return hkSettledState(s) && s.paired !== true; });
}

async function homekitShowCode() {
  if (_hkCodeShown) {
    hkClearCode();
    if (_hkLast) hkRender(_hkLast);
    return;
  }
  if (_hkBusy) return;
  hkSetBusy(true);
  let d = null;
  try {
    d = await hkPost({ action: 'setup' });
  } catch (e) {
    hkSetBusy(false);
    hkNotice(uiT('Нет ответа от платы'), false);
    return;
  }
  hkSetBusy(false);
  if (!d || d.error === 'unauthorized' || d.error_code === 'E_CSRF') return;
  const svg = $('homekit-qr');
  const box = $('homekit-code-box');
  // The bridge may have been paired between the render and this answer.
  const stillOpen = _hkLast && _hkLast.state === 'running' && _hkLast.paired === false;
  if (d.ok !== true || !d.available || typeof d.code !== 'string' || !hkValidQr(d.qr) || !svg || !box || !stillOpen) {
    hkClearCode();
    hkNotice(hkErrorText(d.ok === true ? { error: 'not_available' } : d), false);
    return;
  }
  if (!hkDrawQr(svg, d.qr)) {
    hkClearCode();
    hkNotice(hkErrorText({ error: 'not_available' }), false);
    return;
  }
  hkSetText('homekit-code', d.code);
  box.hidden = false;
  _hkCodeShown = true;
  hkRender(_hkLast);
}

async function homekitApplyNet() {
  const d0 = _hkLast;
  const sel = $('homekit-iface-sel');
  const portEl = $('homekit-port-in');
  if (!d0 || !sel || !portEl) return;
  const iface = sel.value;
  const portRaw = String(portEl.value || '').trim();
  const port = /^\d{1,5}$/.test(portRaw) ? parseInt(portRaw, 10) : NaN;
  const pmin = typeof d0.port_min === 'number' ? d0.port_min : 1024;
  const pmax = typeof d0.port_max === 'number' ? d0.port_max : 65535;
  const forbidden = Array.isArray(d0.forbidden_ports) ? d0.forbidden_ports : [];
  if (iface && !HK_IFACE_RE.test(iface)) { hkNotice(uiT('Недопустимый интерфейс'), false); return; }
  if (!(port >= pmin && port <= pmax)) { hkNotice(uiT('Недопустимый порт'), false); return; }
  if (forbidden.indexOf(port) !== -1) { hkNotice(uiT('Порт занят службой платы — выберите другой'), false); return; }
  let changed = false;
  let failed = false;
  if (iface && iface !== d0.interface) {
    const r = await hkMutate({ action: 'set_interface', interface: iface }, null);
    changed = true;
    failed = !r || r.ok !== true;
  }
  if (!failed && port !== d0.port) {
    const r = await hkMutate({ action: 'set_port', port: port }, null);
    changed = true;
    failed = !r || r.ok !== true;
  }
  if (!changed) return;
  if (!failed) {
    hkNotice(uiT('Сохранено'), true);
    const det = $('homekit-net');
    if (det) det.open = false;
    hkFastPoll(hkSettledState);
  }
}

function homekitToggleSkipped() {
  _hkSkipOpen = !_hkSkipOpen;
  hkRenderSkipped(_hkLast);
}

// ── Tab lifecycle (app.js switchTab) ────────────────────────────────────────
function homekitTabInit() {
  if (!$('homekit-card')) return;
  _hkTabActive = true;
  if (!hkPageVisible()) return;
  hkRefresh();
  hkStartPoll();
}

// Leaving the tab stops the poll and takes the setup code out of the DOM.
function homekitTabDestroy() {
  _hkTabActive = false;
  hkStopPolls();
  hkClearCode();
  if (_hkLast) hkRender(_hkLast);
}

function hkVisibilityChanged() {
  if (!_hkTabActive) return;
  if (!hkPageVisible()) {
    hkStopPolls();
    return;
  }
  hkRefresh();
  hkStartPoll();
}

// Language switch (i18n.js updateControl): re-render the composed strings.
function refreshHomekitI18n() {
  if (_hkLastFailed || !_hkLast) {
    if (_hkLastFailed) hkRenderUnavailable();
    return;
  }
  hkRender(_hkLast);
}

function hkInit() {
  if (!$('homekit-card')) return;
  document.addEventListener('visibilitychange', hkVisibilityChanged);
  // A deep link straight to #system switched tabs before this file's init ran.
  const pane = $('tab-system');
  if (pane && pane.classList.contains('active')) homekitTabInit();
}

// Only HTML onclick handlers, app.js / i18n.js hooks and smarthome.js need a
// global handle.
window.homekitTabInit = homekitTabInit;
window.homekitTabDestroy = homekitTabDestroy;
window.homekitToggle = homekitToggle;
window.homekitResetPairing = homekitResetPairing;
window.homekitShowCode = homekitShowCode;
window.homekitApplyNet = homekitApplyNet;
window.homekitToggleSkipped = homekitToggleSkipped;
window.homekitRefresh = hkRefresh;
window.refreshHomekitI18n = refreshHomekitI18n;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', hkInit);
} else {
  hkInit();
}

})();
