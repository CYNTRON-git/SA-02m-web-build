/* SA-02m — Яндекс Алиса (вкладка «Управление») */

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

function aliceSetMsgOn(id, text, ok) {
  const msg = $(id);
  if (!msg) return;
  if (!text) {
    msg.hidden = true;
    msg.textContent = '';
    msg.className = 'cloud-msg';
    return;
  }
  msg.hidden = false;
  msg.textContent = text;
  msg.className = 'cloud-msg ' + (ok ? 'is-ok' : 'is-err');
}

// Card-level status/link/enable feedback.
function aliceSetMsg(text, ok) { aliceSetMsgOn('alice-msg', text, ok); }
// Binding-modal feedback (add-device), so it shows inside the open dialog.
function aliceSetBindMsg(text, ok) { aliceSetMsgOn('alice-bind-msg', text, ok); }

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

async function aliceTopics() {
  const r = await fetch('cgi-bin/sa02m_alice_topics.cgi', {
    method: 'GET',
    credentials: 'same-origin',
  });
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

// Yandex device-type ids (`devices.types.switch`, …) are machine keys, never a
// user label — map to a human RU string (translated by uiT / DICT), falling
// back to the last dotted segment so a new type never renders its raw key.
const ALICE_DEV_TYPES = {
  'devices.types.switch': 'Выключатель',
  'devices.types.socket': 'Розетка',
  'devices.types.light': 'Освещение',
  'devices.types.sensor': 'Датчик',
  'devices.types.thermostat': 'Термостат',
  'devices.types.other': 'Устройство',
};

function aliceDeviceTypeLabel(type) {
  if (!type) return '';
  const ru = ALICE_DEV_TYPES[type];
  if (ru) return uiT(ru);
  const seg = String(type).split('.').pop();
  return seg ? seg.charAt(0).toUpperCase() + seg.slice(1) : '';
}

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

  const certOk = !!(d.mtls && d.mtls.cert_present);
  aliceSetBadge($('alice-cert-state'), certOk ? 'Есть' : 'Нет', certOk ? 'ok' : 'warn');

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
  // once the certificate appears (linked) or on unlink. All three actions hit
  // the gateway, so the button is gated on reachability and the reserved
  // «нет интернета» note is surfaced when !avail (unchanged gate).
  const linked = certOk;
  if (linked) aliceSetPending(false);
  const pending = aliceGetPending();
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

  const devices = (d.devices && d.devices.devices) || [];
  const rooms = (d.devices && d.devices.rooms) || [];
  const list = $('alice-device-list');
  if (list) {
    if (!devices.length) {
      list.innerHTML = '<p class="field-hint">' + escHtml(uiT('Устройства ещё не добавлены')) + '</p>';
    } else {
      list.innerHTML = devices.map(function (dev) {
        return '<div class="alice-dev-row"><span class="mono text-sm">' +
          escHtml(dev.name || dev.id) + '</span> <span class="text-sm text-sec">' +
          escHtml(aliceDeviceTypeLabel(dev.type)) + '</span></div>';
      }).join('');
    }
  }
  const meta = $('alice-counts');
  if (meta) {
    meta.textContent = uiT('Комнат') + ': ' + rooms.length + ' · ' + uiT('Устройств') + ': ' + devices.length;
  }

  // Surface a gateway/client problem in the modal as the FRIENDLY label only —
  // never the raw urlopen/SSL exception string (Operator rule, 1.0.5.73). The
  // mapping lives in aliceFriendlyStatus; a healthy state leaves any action
  // feedback message («Сохранено» etc.) in place.
  if (friendly.kind === 'err') {
    aliceSetMsg(uiT(friendly.text), false);
  } else if (!enabled) {
    aliceSetMsg('', true);
  }
}

async function aliceRefresh() {
  try {
    const d = await aliceApi(null);
    if (d && d.error === 'unauthorized') return;
    aliceRender(d);
  } catch (e) {
    aliceSetMsg(uiT('Ошибка запроса API Алисы'), false);
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
      aliceSetMsg(d.message || uiT('Сохранено'), true);
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
      aliceSetMsg(uiT('Откройте ссылку привязки, затем «Завершить привязку»'), true);
      window.open(d.enrollment.registration_url, '_blank', 'noopener');
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
      aliceSetMsg(d.message || d.error || uiT('Не удалось завершить привязку'), false);
    } else {
      aliceSetMsg(d.message || uiT('Сертификат установлен'), true);
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

async function aliceLoadTopics() {
  const sel = $('alice-topic-select');
  if (!sel) return;
  try {
    const d = await aliceTopics();
    const topics = (d && d.topics) || [];
    sel.innerHTML = topics.map(function (t) {
      return '<option value="' + escHtml(t) + '">' + escHtml(t) + '</option>';
    }).join('');
  } catch (e) {
    /* ignore */
  }
}

async function aliceAddDevice() {
  const name = ($('alice-dev-name') && $('alice-dev-name').value || '').trim();
  const topic = $('alice-topic-select') && $('alice-topic-select').value;
  if (!name || !topic) {
    aliceSetBindMsg(uiT('Укажите имя и MQTT-топик'), false);
    return;
  }
  const device = {
    name: name,
    type: 'devices.types.switch',
    capabilities: [{
      type: 'devices.capabilities.on_off',
      mqtt: topic,
      retrievable: true,
      reportable: true,
      parameters: { instance: 'on' },
    }],
    properties: [],
  };
  try {
    const d = await aliceApi({ action: 'upsert_device', device: device });
    if (!d.ok) {
      aliceSetBindMsg(d.message || d.error || uiT('Ошибка'), false);
    } else {
      aliceSetBindMsg(uiT('Устройство сохранено'), true);
      if ($('alice-dev-name')) $('alice-dev-name').value = '';
    }
    await aliceRefresh();
  } catch (e) {
    aliceSetBindMsg(uiT('Ошибка запроса API Алисы'), false);
  }
}

// ── Bindings modal («Управление привязками») — holds ONLY the device/command
// binding UI (counts + list + add-device); status/link/enable stay on the card.
// Reuses the shared mqtt-modal markup/behaviour, not a new one.
function aliceModalEsc(e) {
  if (e.key === 'Escape') aliceCloseModal();
}

function aliceOpenModal() {
  const m = $('alice-modal');
  if (!m) return;
  aliceSetBindMsg('', true);
  m.removeAttribute('hidden');
  document.addEventListener('keydown', aliceModalEsc);
  aliceRefresh();
  aliceLoadTopics();
}

function aliceCloseModal() {
  const m = $('alice-modal');
  if (m) m.setAttribute('hidden', '');
  document.removeEventListener('keydown', aliceModalEsc);
}

// Close only on a click on the overlay backdrop itself, not its dialog contents.
function aliceModalBackdrop(e) {
  if (e.target && e.target.id === 'alice-modal') aliceCloseModal();
}

let _alicePoll = null;

function aliceInit() {
  if (!$('alice-card')) return;
  aliceRefresh();
  aliceLoadTopics();
  if (_alicePoll) clearInterval(_alicePoll);
  _alicePoll = setInterval(aliceRefresh, 5000);
}

window.aliceToggleClient = aliceToggleClient;
window.aliceStartLink = aliceStartLink;
window.aliceCompleteLink = aliceCompleteLink;
window.aliceUnlink = aliceUnlink;
window.aliceLinkAction = aliceLinkAction;
window.aliceAddDevice = aliceAddDevice;
window.aliceRefresh = aliceRefresh;
window.aliceOpenModal = aliceOpenModal;
window.aliceCloseModal = aliceCloseModal;
window.aliceModalBackdrop = aliceModalBackdrop;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', aliceInit);
} else {
  aliceInit();
}

})();
