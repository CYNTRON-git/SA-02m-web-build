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

// Two channels, by lifetime. ACTION feedback («Сохранено», a pairing error, a
// gateway request failure) is ephemeral → the viewport toast (cardNotice, 5 s),
// so it never grows .ctrl-card. A STANDING state explanation — the «Причина»
// line of a stood-down board, the friendly gateway/client status — is owned by
// aliceRender and lives ON the card (#alice-msg) for as long as the state does:
// a toast shows it once and then never again (1.0.6.38 regression, audit C1;
// the card line is what docs/contracts/alice-mqtt-mapping.md and
// cloud-card-smoke read). The toast path therefore never touches #alice-msg.
let _aliceLastPollNotice = '';

// The standing card line. `ok` true/false tints it, null is a neutral hint.
function aliceSetCardMsg(text, ok) {
  const msg = $('alice-msg');
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

function aliceHideCardMsg() { aliceSetCardMsg('', true); }

function aliceNotice(text, ok) {
  if (!text) return;
  if (typeof cardNotice === 'function') cardNotice(text, ok);
  else if (typeof toast === 'function') toast(text, ok === false ? 'error' : (ok === true ? 'success' : 'info'), 5000);
}

function alicePollNoticeOnce(text, ok) {
  const key = String(ok) + '\0' + String(text || '');
  if (key === _aliceLastPollNotice) return;
  _aliceLastPollNotice = key;
  aliceNotice(text, ok);
}

// Card-level status/link/enable feedback — viewport toast, not an in-card banner.
function aliceSetMsg(text, ok) {
  _aliceLastPollNotice = '';
  aliceNotice(text, ok);
}

// Compact reopen link lives in the existing status row (#alice-reg-link) so a
// blocked popup still has a target without growing the card. The toast carries
// the same wording for 5 s.
function aliceSetRegLink(url, label) {
  const a = $('alice-reg-link');
  if (!a) return;
  if (!url) {
    a.hidden = true;
    a.removeAttribute('href');
    a.textContent = '';
    return;
  }
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener';
  a.textContent = label || uiT('Открыть ссылку привязки');
  a.hidden = false;
}

function aliceSetMsgLink(text, url, label) {
  aliceSetRegLink(url, label);
  aliceNotice(text, true);
}

// Registration URL of the pending claim: server-fed (link.registration_url
// survives F5 and other browsers) with the last start_link response as the
// same-session fallback.
let aliceRegUrl = null;

function aliceSetBadge(el, text, kind) {
  if (!el) return;
  el.innerHTML = aliceBadge(uiT(text), kind);
}

async function aliceApi(body, signal) {
  const opt = body
    ? {
        method: 'POST',
        headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
        credentials: 'same-origin',
        body: JSON.stringify(body),
      }
    : { method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: signal };
  if (body) {
    // A sent command is never aborted here. Retire only older status reads.
    aliceRetireReads();
    _aliceMutations += 1;
  }
  try {
    const r = await fetch('cgi-bin/sa02m_alice_api.cgi', opt);
    return await r.json();
  } finally {
    if (body) {
      _aliceMutations -= 1;
      if (!_aliceMutations) aliceRefresh();
    }
  }
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
  // Distinct from missing_cert on purpose: the board WAS bound and the cloud
  // unbound it, so the card must say why the certificate is gone instead of
  // reading like a board that was never bound. Same wording as the «Облако»
  // card's state of the same name — one vocabulary for one situation.
  unlinked: ['Отвязано в облаке', 'warn'],
  // Unlinked, but the binding files could NOT be erased — the client keeps
  // retrying. Never «привязан», never «отвязано».
  unlink_failed: ['Ошибка отвязки', 'err'],
  unknown: ['Нет данных', 'unk'],
};

// Client status state / reason code → human RU (DICT-translated), the twin of
// cloud.js's CLOUD_REASON_MAP. Shown in the card's message line as
// «Причина: …» for the two stand-down states and nothing else.
const ALICE_REASON_MAP = {
  unlinked: 'устройство отвязано в облаке',
  wipe_failed: 'не удалось стереть файлы привязки в /var/lib/sa02m-alice — подробности в журнале клиента; попытка повторяется',
  // The local «Отвязать» could not erase the binding AND could not hand the
  // retry to the client (the same read-only filesystem, most likely) — nothing
  // is retrying, so the card must ask for the action instead of implying one.
  wipe_failed_not_recorded: 'не удалось стереть файлы привязки и передать повтор клиенту — нажмите «Отвязать» ещё раз',
};
const ALICE_STAND_DOWN_STATES = ['unlinked', 'unlink_failed'];

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
  // No evidence yet (the backend's cached probe is still being taken —
  // docs/contracts/alice-mqtt-mapping.md §Gateway reachability): a missing
  // answer is not a negative one, so the pill stays neutral.
  if (!avail && d.gateway && d.gateway.state === 'checking') return { text: 'Проверка шлюза…', kind: 'unk' };
  if (!avail) {
    const probe = d.gateway && d.gateway.probe;
    const raw = (probe && probe.message) || '';
    if (aliceCertUntrusted(raw)) return { text: 'сертификат шлюза не доверенный', kind: 'err' };
    return { text: 'Шлюз недоступен', kind: 'err' };
  }
  const entry = ALICE_STATE_MAP[st] || ALICE_STATE_MAP.unknown;
  return { text: entry[0], kind: entry[1] };
}

// The «Шлюз» badge: same three-way reading as the pill (reachable /
// unreachable / checking); an older backend without `state` is two-way.
function aliceGatewayBadge(d) {
  const gw = d.gateway || {};
  if (gw.available) return { text: 'Доступен', kind: 'ok' };
  if (gw.state === 'checking') return { text: 'Проверяется', kind: 'unk' };
  return { text: 'Недоступен', kind: 'err' };
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

  // Friendly overall status (never raw exception text) — toast, not #alice-msg
  const friendly = aliceFriendlyStatus(d);

  aliceSetBadge($('alice-svc-state'), enabled ? 'Включен' : 'Выключен', enabled ? 'ok' : 'unk');
  const gwBadge = aliceGatewayBadge(d);
  aliceSetBadge($('alice-gw-state'), gwBadge.text, gwBadge.kind);
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
  // Gateway probe fail is «Шлюз недоступен» on the badge — not «нет интернета».
  // The box can still have a default route (skill unlink wiped certs, TLS
  // blip, hub 5xx). Only a dedicated offline note may use that phrase.
  if (statusVal) statusVal.textContent = uiT(statusText);
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

  // The card line is the render's: a STANDING explanation stays on the card for
  // as long as the state does (contract §state, cloud-card-smoke), never a
  // once-per-session toast. The «Причина» line for the two stand-down states
  // is checked BEFORE the generic error branch, which would otherwise replace
  // the explanation with a bare «Ошибка отвязки» (or «Шлюз недоступен» on a
  // board that is unlinked AND offline) and leave the operator no reason at
  // all. Unlike the «Облако» card it carries no timestamp: the Alice status
  // file's `unlinked_at` is the durable marker's, and formatting it would
  // duplicate cloud.js's relative-time helper across bundles. The friendly
  // status is the raw-exception-free label only (Operator rule, 1.0.5.73).
  // The reopen link sits in the status row (#alice-reg-link).
  if (ALICE_STAND_DOWN_STATES.indexOf(st) !== -1) {
    const raw = (d.status && d.status.reason) || '';
    const why = ALICE_REASON_MAP[raw] || ALICE_REASON_MAP[st] || '';
    aliceSetRegLink(null);
    aliceSetCardMsg(uiT('Причина') + ': ' + (why ? uiT(why) : String(raw)), false);
  } else if (friendly.kind === 'err') {
    aliceSetRegLink(null);
    aliceSetCardMsg(uiT(friendly.text), false);
  } else if (enabled && pending && aliceRegUrl) {
    aliceSetRegLink(aliceRegUrl, uiT('Открыть ссылку привязки'));
    aliceHideCardMsg();
  } else {
    aliceSetRegLink(null);
    aliceHideCardMsg();
    if (!enabled) _aliceLastPollNotice = '';
  }

  aliceNotify(d);
}

// Returns the rendered status (or null) so a caller can tell whether the
// device state has settled — see aliceFastPoll.
function aliceRefresh() {
  if (!aliceReaderActive() || _aliceMutations) return Promise.resolve(null);
  if (_aliceRead) return _aliceRead.promise;
  if (_alicePoll) { clearTimeout(_alicePoll); _alicePoll = null; }
  const read = {gen: _aliceReadGen, ctrl: new AbortController(), timer: null, cancel: null, promise: null, timedOut: false};
  const deadline = new Promise(function (_resolve, reject) {
    read.cancel = function () { reject(new DOMException('Retired Alice read', 'AbortError')); };
    read.timer = setTimeout(function () {
      read.timedOut = true;
      read.ctrl.abort();
      reject(new Error('Alice read timed out'));
    }, ALICE_READ_TIMEOUT_MS);
  });
  _aliceRead = read;
  read.promise = Promise.race([aliceApi(null, read.ctrl.signal), deadline]).then(function (d) {
    if (read.gen !== _aliceReadGen || _aliceRead !== read || !aliceReaderActive()) return null;
    if (d && d.error === 'unauthorized') return null;
    aliceRender(d);
    if (aliceFastPollSettled(d)) _aliceFastUntil = 0;
    return d;
  }).catch(function (e) {
    if (read.gen === _aliceReadGen && aliceReaderActive() && (read.timedOut || e.name !== 'AbortError')) {
      alicePollNoticeOnce(uiT('Ошибка запроса API Алисы'), false);
    }
    return null;
  }).finally(function () {
    clearTimeout(read.timer);
    if (_aliceRead !== read) return;
    _aliceRead = null;
    if (read.gen === _aliceReadGen) aliceScheduleNext();
  });
  return read.promise;
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
      // Prefer the mapped Russian phrase over the backend's English `message`:
      // these two codes are the wipe-failure outcomes and the operator has to
      // act on the difference between them.
      const why = ALICE_REASON_MAP[d.error];
      aliceSetMsg(why ? uiT(why) : (d.message || d.error || uiT('Отвязка не выполнена')), false);
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
let _aliceRead = null;
let _aliceReadGen = 0;
let _aliceMutations = 0;
let _aliceFastUntil = 0;
let _aliceTabActive = false;
let _aliceModalActive = false;
let _alicePageDeparted = false;

// Alice, cloud controls, Smart Home and HomeKit share one bounded GET. Both
// ordinary and post-action refresh schedule their next request on completion.
const ALICE_POLL_MS = 5000;
const ALICE_READ_TIMEOUT_MS = 8000;
const ALICE_SETTLED_STATES = ['connected', 'disabled', 'error'];

function aliceReaderActive() { return (_aliceTabActive || _aliceModalActive) && !_alicePageDeparted && !document.hidden; }

function aliceRetireReads() {
  _aliceReadGen += 1;
  _aliceFastUntil = 0;
  if (_alicePoll) { clearTimeout(_alicePoll); _alicePoll = null; }
  const read = _aliceRead;
  _aliceRead = null;
  if (read) {
    clearTimeout(read.timer);
    read.cancel();
    read.ctrl.abort();
  }
}

function aliceScheduleNext(delay) {
  if (!aliceReaderActive() || _aliceRead || _aliceMutations) return;
  if (_alicePoll) clearTimeout(_alicePoll);
  const fast = performance.now() < _aliceFastUntil;
  _alicePoll = setTimeout(function () {
    _alicePoll = null;
    aliceRefresh();
  }, delay === undefined ? (fast ? 1000 : ALICE_POLL_MS) : delay);
}

function aliceFastPollSettled(d) {
  if (!d) return false;
  const st = (d.status && d.status.state) || '';
  return ALICE_SETTLED_STATES.indexOf(st) !== -1;
}

function aliceFastPoll() {
  if (!aliceReaderActive()) return;
  _aliceFastUntil = performance.now() + 20000;
  aliceScheduleNext(700);
}

// The 5 s poll runs while an integration sub-tab or its bindings modal is open.
// Starting it with the page held an fcgiwrap worker on every other tab.
function aliceResumePolling() {
  if (aliceReaderActive() && !_aliceRead && !_alicePoll) aliceRefresh();
}

window.aliceTabInit = function () {
  if (!$('alice-card')) return;
  _aliceTabActive = true;
  aliceResumePolling();
};

window.aliceTabDestroy = function () {
  _aliceTabActive = false;
  if (!aliceReaderActive()) aliceRetireReads();
};

// The existing shared modal is also a visible consumer, including when opened
// before the host sub-tab. Closing either owner cannot retire the other one.
window.sa02mAliceModalActive = function (active) {
  _aliceModalActive = active === true;
  if (aliceReaderActive()) aliceResumePolling();
  else aliceRetireReads();
};

let _aliceDocumentVisible = !document.hidden;
document.addEventListener('visibilitychange', function () {
  const visible = !document.hidden;
  if (visible === _aliceDocumentVisible) return;
  _aliceDocumentVisible = visible;
  if (!visible) aliceRetireReads();
  else aliceResumePolling();
});
window.addEventListener('pagehide', function () {
  _alicePageDeparted = true;
  aliceRetireReads();
});
window.addEventListener('pageshow', function (event) {
  if (event.persisted) {
    _alicePageDeparted = false;
    aliceResumePolling();
  }
});

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

})();
