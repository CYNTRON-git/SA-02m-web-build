/* SA-02m — Яндекс Алиса (вкладка «Управление»): status badges, link/unlink,
   enable. The rooms/devices/bindings modal lives in app/smarthome.js (since
   1.0.6.26); it rides THIS file's 5 s poll through window.sa02mAliceOnData and
   calls the same CGI through window.sa02mAliceApi — one poll for both cards. */

(function () {
'use strict';

function uiT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(String(s)) : String(s);
}

function $(id) { return document.getElementById(id); }

function aliceBadge(text, kind) {
  const cls = kind === 'ok' ? 'badge-ok' : kind === 'warn' ? 'badge-warn' : kind === 'err' ? 'badge-err' : 'badge-unk';
  return '<span class="badge ' + cls + '">' + escHtml(String(text)) + '</span>';
}

// Transient success notices auto-clear; errors and the pending-link notice
// (rendered by aliceSetMsgLink) stay until the state itself changes.
const ALICE_MSG_TTL_MS = 5000;
let _aliceMsgTimer = null;

function aliceClearMsgTimer() {
  if (_aliceMsgTimer) {
    clearTimeout(_aliceMsgTimer);
    _aliceMsgTimer = null;
  }
}

function aliceSetMsgOn(id, text, ok) {
  const msg = $(id);
  if (!msg) return;
  if (id === 'alice-msg') aliceClearMsgTimer();
  if (!text) {
    msg.hidden = true;
    msg.textContent = '';
    msg.className = 'cloud-msg';
    return;
  }
  msg.hidden = false;
  msg.textContent = text;
  msg.className = 'cloud-msg ' + (ok ? 'is-ok' : 'is-err');
  if (id === 'alice-msg' && ok) {
    _aliceMsgTimer = setTimeout(function () {
      _aliceMsgTimer = null;
      const el = $('alice-msg');
      // Only clear what is still this notice — a newer message owns itself.
      if (el && !el.hidden && el.textContent === text) aliceSetMsgOn('alice-msg', '', true);
    }, ALICE_MSG_TTL_MS);
  }
}

// Card-level status/link/enable feedback.
function aliceSetMsg(text, ok) { aliceSetMsgOn('alice-msg', text, ok); }

// Message + a real clickable link (DOM-built, no innerHTML). Needed because
// window.open after an await is eaten by popup blockers — the operator must
// always have the registration link ON the card while a claim is pending.
function aliceSetMsgLink(text, url, label) {
  const msg = $('alice-msg');
  if (!msg) return;
  aliceClearMsgTimer();  // the link notice lives as long as the claim does
  msg.hidden = false;
  msg.className = 'cloud-msg is-ok';
  msg.textContent = '';
  msg.appendChild(document.createTextNode(text + ' '));
  const a = document.createElement('a');
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener';
  a.textContent = label;
  msg.appendChild(a);
}

// Registration URL of the pending claim: server-fed (link.registration_url
// survives F5 and other browsers) with the last start_link response as the
// same-session fallback.
let aliceRegUrl = null;

function aliceSetBadge(el, text, kind) {
  if (!el) return;
  el.innerHTML = aliceBadge(uiT(text), kind);
}

async function aliceApi(body) {
  const opt = body
    ? {
        method: 'POST',
        headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
        credentials: 'same-origin',
        body: JSON.stringify(body),
      }
    : { method: 'GET', credentials: 'same-origin' };
  const r = await fetch('cgi-bin/sa02m_alice_api.cgi', opt);
  return r.json();
}

// Session-local bridge for the 2-step link flow (link → open URL →
// complete_link). Set when the registration URL is opened, cleared once the
// certificate is present (linked) or on unlink.
function aliceGetPending() {
  try {
    return sessionStorage.getItem('alice_link_pending') === '1';
  } catch (e) {
    return false;
  }
}

function aliceSetPending(on) {
  try {
    if (on) sessionStorage.setItem('alice_link_pending', '1');
    else sessionStorage.removeItem('alice_link_pending');
  } catch (e) {
    /* ignore */
  }
}

const ALICE_STATE_MAP = {
  disabled: ['Отключено', 'unk'],
  offline: ['Шлюз недоступен', 'err'],
  connecting: ['Подключение', 'warn'],
  connected: ['Подключено', 'ok'],
  error: ['Ошибка', 'err'],
  missing_deps: ['Нет зависимостей', 'err'],
  missing_cert: ['Нет сертификата', 'warn'],
  unknown: ['Нет данных', 'unk'],
};

// A raw gateway exception (str(URLError): "<urlopen error [SSL:
// CERTIFICATE_VERIFY_FAILED] … self-signed certificate …>") is a TLS-trust
// failure, not a plain reachability failure — surface it as its own friendly
// status, never the raw string.
function aliceCertUntrusted(raw) {
  return /CERTIFICATE|CERT_|\bSSL\b|self[\s-]?signed/i.test(String(raw || ''));
}

// Map any gateway/client condition to a friendly {text, kind} pill — NEVER the
// raw urlopen/SSL exception text (Operator rule, 1.0.5.73). Both the compact
// card pill and the modal message share this mapping.
function aliceFriendlyStatus(d) {
  const enabled = !!d.client_enabled;
  const avail = !!(d.gateway && d.gateway.available);
  const st = (d.status && d.status.state) || (enabled ? 'unknown' : 'disabled');
  if (!enabled) return { text: 'Отключено', kind: 'unk' };
  if (!avail) {
    const probe = d.gateway && d.gateway.probe;
    const raw = (probe && probe.message) || '';
    if (aliceCertUntrusted(raw)) return { text: 'сертификат шлюза не доверенный', kind: 'err' };
    return { text: 'Шлюз недоступен', kind: 'err' };
  }
  const entry = ALICE_STATE_MAP[st] || ALICE_STATE_MAP.unknown;
  return { text: entry[0], kind: entry[1] };
}

// Listeners riding this file's poll (smarthome.js). A listener registered
// after the first poll already returned gets that payload at once, so load
// order between the two classic scripts never leaves a card empty until the
// next tick.
const _aliceDataListeners = [];
let _aliceLastData = null;

// A listener's exception must not take the Alice card down with it, but it
// must not vanish either — it is logged where the console shows it.
function aliceCallListener(cb, d) {
  try {
    cb(d);
  } catch (e) {
    if (window.console && console.error) console.error('sa02mAliceOnData listener failed:', e);
  }
}

function aliceOnData(cb) {
  if (typeof cb !== 'function') return;
  _aliceDataListeners.push(cb);
  if (_aliceLastData) aliceCallListener(cb, _aliceLastData);
}

function aliceNotify(d) {
  _aliceLastData = d;
  _aliceDataListeners.forEach(function (cb) { aliceCallListener(cb, d); });
}

function aliceRender(d) {
  const card = $('alice-card');
  if (!card || !d) return;

  const enabled = !!d.client_enabled;
  const avail = !!(d.gateway && d.gateway.available);
  const st = (d.status && d.status.state) || (enabled ? 'unknown' : 'disabled');
  const entry = ALICE_STATE_MAP[st] || ALICE_STATE_MAP.unknown;

  // Friendly overall status (never raw exception text) — surfaced in #alice-msg
  const friendly = aliceFriendlyStatus(d);

  aliceSetBadge($('alice-svc-state'), enabled ? 'Включен' : 'Выключен', enabled ? 'ok' : 'unk');
  aliceSetBadge($('alice-gw-state'), avail ? 'Доступен' : 'Недоступен', avail ? 'ok' : 'err');
  aliceSetBadge($('alice-conn-state'), entry[0], entry[1]);

  // mtls.cert_present is tri-state: true / false / null-or-absent (the API could
  // not look — the cert dir is root-only and the CGI runs as www-data; or an
  // older backend without the field). Unknown renders «н/д», never a false «Нет».
  const certRaw = d.mtls ? d.mtls.cert_present : undefined;
  const certOk = certRaw === true;
  const certKnown = certRaw === true || certRaw === false;
  aliceSetBadge(
    $('alice-cert-state'),
    certKnown ? (certOk ? 'Есть' : 'Нет') : 'н/д',
    certKnown ? (certOk ? 'ok' : 'warn') : 'unk'
  );

  const btn = $('alice-btn-enable');
  if (btn) {
    btn.disabled = false;
    btn.dataset.action = enabled ? 'disable' : 'enable';
    btn.className = 'btn btn-sm ' + (enabled ? 'btn-danger' : 'btn-primary');
    btn.textContent = uiT(enabled ? 'Выключить клиент' : 'Включить клиент');
  }

  // Single contextual link/unlink action (WB parity): one button whose label +
  // action follow the link state — not linked → «Привязать», link opened but
  // cert not yet issued → «Завершить привязку», linked → «Отвязать». The
  // session-local `link_pending` flag bridges our 2-step flow; it is cleared
  // once the device is linked or on unlink. All three actions hit the
  // gateway, so the button is gated on reachability and the reserved
  // «нет интернета» note is surfaced when !avail (unchanged gate).
  // Linked = a live mTLS session (state connected — the strongest proof the
  // cert is enrolled) OR a positively known cert; linkage is never hostage to
  // an unknown cert flag. Checked BEFORE `pending`, so a stale pending mark
  // cannot show «ожидание завершения» on a connected device.
  const linked = st === 'connected' || certOk;
  if (linked) aliceSetPending(false);
  const srvRegUrl = (d.link && d.link.registration_url) || null;
  if (srvRegUrl) aliceRegUrl = srvRegUrl;
  if (linked) aliceRegUrl = null;
  // Server truth wins: link.pending is False once the claim expired or was
  // abandoned — the card must return to «Привязать», never hang on the
  // session-local mark (Operator hit exactly that deadlock, 2026-08-26).
  // An older backend without the field falls back to the local heuristics.
  const srvPending = d.link && typeof d.link.pending === 'boolean' ? d.link.pending : null;
  if (srvPending === false) { aliceSetPending(false); aliceRegUrl = null; }
  const pending = srvPending !== null
    ? (srvPending && !linked)
    : (aliceGetPending() || (!linked && !!srvRegUrl));
  const linkBtn = $('alice-btn-link');
  const linkRow = $('alice-link-row');
  const statusVal = $('alice-link-status-val');
  let statusText, statusAction, statusLabel, statusDanger;
  if (!enabled) {
    statusText = 'клиент выключен';
  } else if (linked) {
    statusText = 'привязан';
    statusAction = 'unlink';
    statusLabel = 'Отвязать';
    statusDanger = true;
  } else if (pending) {
    statusText = 'ожидание завершения';
    statusAction = 'complete_link';
    statusLabel = 'Завершить привязку';
  } else {
    statusText = 'не привязан';
    statusAction = 'link';
    statusLabel = 'Привязать';
  }
  // Offline surfaces in the Status line itself (no separate «нет интернета» row).
  if (statusVal) statusVal.textContent = avail ? uiT(statusText) : uiT('нет интернета');
  if (linkRow) linkRow.hidden = !enabled;
  if (linkBtn) {
    linkBtn.dataset.action = statusAction || '';
    linkBtn.className = 'btn btn-sm ' + (statusDanger ? 'btn-danger' : 'btn-primary');
    linkBtn.textContent = uiT(statusLabel || 'Привязать');
    linkBtn.disabled = !avail;
  }

  // «Устройств в Алисе: K» — the devices the Yandex profile actually lists
  // (alice_visible absent ⇒ visible). The list itself lives in «Умный дом».
  const devices = (d.devices && d.devices.devices) || [];
  const count = $('alice-sh-count');
  if (count) {
    let k = 0;
    devices.forEach(function (dev) { if (dev && dev.alice_visible !== false) k++; });
    count.textContent = String(k);
  }

  // Surface a gateway/client problem in the card as the FRIENDLY label only —
  // never the raw urlopen/SSL exception string (Operator rule, 1.0.5.73). The
  // mapping lives in aliceFriendlyStatus; a healthy state leaves any action
  // feedback message («Сохранено» etc.) in place.
  if (friendly.kind === 'err') {
    aliceSetMsg(uiT(friendly.text), false);
  } else if (enabled && pending && aliceRegUrl) {
    // Keep the reopen link visible for the whole pending window — a closed
    // tab or a blocked popup must not strand the operator.
    aliceSetMsgLink(
      uiT('Откройте ссылку привязки, затем «Завершить привязку»:'),
      aliceRegUrl,
      uiT('Открыть ссылку привязки')
    );
  } else if (!enabled) {
    aliceSetMsg('', true);
  }

  aliceNotify(d);
}

// Returns the rendered status (or null) so a caller can tell whether the
// device state has settled — see aliceFastPoll.
async function aliceRefresh() {
  try {
    const d = await aliceApi(null);
    if (d && d.error === 'unauthorized') return null;
    aliceRender(d);
    return d;
  } catch (e) {
    aliceSetMsg(uiT('Ошибка запроса API Алисы'), false);
    return null;
  }
}

async function aliceToggleClient() {
  const btn = $('alice-btn-enable');
  const action = btn && btn.dataset.action === 'disable' ? 'disable' : 'enable';
  aliceSetMsg(uiT('Сохранение'), true);
  try {
    const d = await aliceApi({ action: action });
    if (!d.ok) {
      aliceSetMsg(d.message || d.error || uiT('Ошибка'), false);
    } else {
      // The unit start/stop + first gateway connect take a few seconds —
      // poll fast so the badges do not look frozen.
      aliceSetMsg(uiT('Сохранено'), true);
      aliceFastPoll();
    }
    await aliceRefresh();
  } catch (e) {
    aliceSetMsg(uiT('Ошибка запроса API Алисы'), false);
  }
}

async function aliceStartLink() {
  aliceSetMsg(uiT('Запрос привязки'), true);
  try {
    const d = await aliceApi({ action: 'link' });
    if (!d.ok) {
      // Honest failure — never show fake success
      aliceSetMsg(d.message || d.error || uiT('Шлюз недоступен'), false);
    } else if (d.enrollment && d.enrollment.registration_url) {
      aliceSetPending(true);
      aliceRegUrl = d.enrollment.registration_url;
      // The anchor is the reliable path — window.open after an await is
      // routinely eaten by popup blockers; keep it as a best-effort bonus.
      aliceSetMsgLink(
        uiT('Откройте ссылку привязки, затем «Завершить привязку»:'),
        aliceRegUrl,
        uiT('Открыть ссылку привязки')
      );
      window.open(aliceRegUrl, '_blank', 'noopener');
    } else {
      aliceSetMsg(uiT('Шлюз ответил без ссылки привязки'), false);
    }
    await aliceRefresh();
  } catch (e) {
    aliceSetMsg(uiT('Ошибка запроса API Алисы'), false);
  }
}

async function aliceCompleteLink() {
  aliceSetMsg(uiT('Выпуск сертификата'), true);
  try {
    const d = await aliceApi({ action: 'complete_link' });
    if (!d.ok) {
      // The raw gateway error alone (e.g. «HTTP Error 400») strands the
      // operator — name the recovery path in the same message.
      aliceSetMsg(
        (d.message || d.error || uiT('Не удалось завершить привязку')) +
        ' — ' + uiT('если ссылка устарела, нажмите «Привязать» заново'),
        false
      );
    } else {
      // Never relay the backend's English operator-note («…enable client and
      // restart…») — the CGI already restarts the client for us.
      aliceSetMsg(uiT('Сертификат установлен, подключаем…'), true);
      aliceFastPoll();
    }
    await aliceRefresh();
  } catch (e) {
    aliceSetMsg(uiT('Ошибка запроса API Алисы'), false);
  }
}

async function aliceUnlink() {
  aliceSetMsg(uiT('Отвязка'), true);
  try {
    const d = await aliceApi({ action: 'unlink' });
    if (!d.ok) {
      aliceSetMsg(d.message || d.error || uiT('Отвязка не выполнена'), false);
    } else {
      aliceSetPending(false);
      aliceSetMsg(d.message || uiT('Отвязано'), true);
    }
    await aliceRefresh();
  } catch (e) {
    aliceSetMsg(uiT('Ошибка запроса API Алисы'), false);
  }
}

// Single entry point for the contextual link button: dispatch by the action
// the current state stamped onto the button (see aliceRender).
async function aliceLinkAction() {
  const btn = $('alice-btn-link');
  const action = btn && btn.dataset.action;
  if (action === 'unlink') return aliceUnlink();
  if (action === 'complete_link') return aliceCompleteLink();
  return aliceStartLink();
}

let _alicePoll = null;
let _aliceFastPoll = null;
let _aliceFastPollGen = 0;

// After an action that changes state on the device (cert issued → client
// restart, enable/disable), the 5 s cadence makes the card look stuck for
// several seconds. Poll faster for a short window instead.
//
// CHAINED, never setInterval: the status endpoint probes the gateway with a
// 5 s timeout inside its own CGI, so a fixed-rate 1 Hz timer would stack
// overlapping requests on a slow uplink — exactly the post-enrollment case —
// and hold several of the 8 shared fcgiwrap workers (web-code-rigor
// ## Architecture: no per-request network work piled onto a polled endpoint).
// Each tick waits for its own refresh to settle, and the window ends early
// once the state stops being transitional.
const ALICE_POLL_MS = 5000;
const ALICE_SETTLED_STATES = ['connected', 'disabled', 'error'];

function aliceFastPollSettled(d) {
  if (!d) return false;
  const st = (d.status && d.status.state) || '';
  return ALICE_SETTLED_STATES.indexOf(st) !== -1;
}

function aliceFastPoll() {
  const until = Date.now() + 20000;
  // A second call landing while a tick is awaiting must not start a second
  // chain: the generation counter makes the older chain retire on its return.
  _aliceFastPollGen += 1;
  const gen = _aliceFastPollGen;
  if (_aliceFastPoll) clearTimeout(_aliceFastPoll);
  // The base 5 s poll would race the window and hit the same probing
  // endpoint — park it and restore it when the window ends.
  if (_alicePoll) {
    clearInterval(_alicePoll);
    _alicePoll = null;
  }
  const stop = function () {
    _aliceFastPoll = null;
    if (!_alicePoll) _alicePoll = setInterval(aliceRefresh, ALICE_POLL_MS);
  };
  const tick = async function () {
    _aliceFastPoll = null;
    let settled = false;
    try {
      settled = aliceFastPollSettled(await aliceRefresh());
    } catch (e) {
      /* transport hiccup — keep the window running */
    }
    // A newer window took over while this tick was in flight — retire this
    // chain and leave the pollers to the newer one.
    if (gen !== _aliceFastPollGen) return;
    if (settled || Date.now() >= until) {
      stop();
      return;
    }
    _aliceFastPoll = setTimeout(tick, 1000);
  };
  _aliceFastPoll = setTimeout(tick, 700);
}

function aliceInit() {
  if (!$('alice-card')) return;
  aliceRefresh();
  if (_alicePoll) clearInterval(_alicePoll);
  _alicePoll = setInterval(aliceRefresh, ALICE_POLL_MS);
}

// Only functions invoked from HTML onclick handlers need a global handle;
// aliceStartLink/CompleteLink/Unlink are called internally (via
// aliceLinkAction) and are intentionally not exported.
window.aliceToggleClient = aliceToggleClient;
window.aliceLinkAction = aliceLinkAction;
// The shared pipeline for app/smarthome.js: the same CGI call, the same
// refresh, and a data hook on this file's poll (no second CGI poll).
window.sa02mAliceApi = aliceApi;
window.sa02mAliceRefresh = aliceRefresh;
window.sa02mAliceOnData = aliceOnData;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', aliceInit);
} else {
  aliceInit();
}

})();
