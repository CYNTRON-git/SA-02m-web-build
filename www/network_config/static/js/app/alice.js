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
// Binding-modal feedback (add-device), so it shows inside the open dialog.
function aliceSetBindMsg(text, ok) { aliceSetMsgOn('alice-bind-msg', text, ok); }

// Edit-mode state for the bindings modal: id being edited + the last rendered
// device objects by id (source for prefill and id/room_id/type preservation).
let aliceEditId = null;
let aliceDevCache = {};

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
  'devices.types.sensor.climate': 'Климат-датчик',
  'devices.types.thermostat': 'Термостат',
  'devices.types.other': 'Устройство',
};

// Kind key (#alice-dev-kind option value) → the Yandex device/property pin
// (docs/contracts/alice-mqtt-mapping.md). `switch` marks the capability path;
// sensor kinds carry the float-property instance + unit forwarded verbatim.
const ALICE_KINDS = {
  switch: { type: 'devices.types.switch' },
  temperature: { type: 'devices.types.sensor.climate', instance: 'temperature', unit: 'unit.temperature.celsius' },
  humidity: { type: 'devices.types.sensor.climate', instance: 'humidity', unit: 'unit.percent' },
  voltage: { type: 'devices.types.sensor', instance: 'voltage', unit: 'unit.volt' },
  amperage: { type: 'devices.types.sensor', instance: 'amperage', unit: 'unit.ampere' },
  power: { type: 'devices.types.sensor', instance: 'power', unit: 'unit.watt' },
};

// Sentinel option value for a hand-edited config whose float-property instance
// is outside ALICE_KINDS: the select is locked on it and save touches only
// name + topic (instance/unit preserved).
const ALICE_KIND_RAW = '__raw__';

function aliceKindSelect() { return $('alice-dev-kind'); }

// The form owns exactly ONE managed binding per device: the on_off capability
// (switch) or the first devices.properties.float property (sensor).
function aliceManagedProp(dev) {
  const props = (dev && dev.properties) || [];
  for (let i = 0; i < props.length; i++) {
    if (props[i] && props[i].type === 'devices.properties.float') return props[i];
  }
  return null;
}

function aliceManagedCap(dev) {
  const caps = (dev && dev.capabilities) || [];
  for (let i = 0; i < caps.length; i++) {
    if (caps[i] && caps[i].type === 'devices.capabilities.on_off') return caps[i];
  }
  return null;
}

// Detect the form kind of an existing device: on_off capability wins (switch),
// else the first float property's instance; an instance we do not know maps to
// the locked raw sentinel.
function aliceDetectKind(dev) {
  if (aliceManagedCap(dev)) return 'switch';
  const prop = aliceManagedProp(dev);
  if (prop) {
    const inst = prop.parameters && prop.parameters.instance;
    const keys = Object.keys(ALICE_KINDS);
    for (let i = 0; i < keys.length; i++) {
      if (ALICE_KINDS[keys[i]].instance === inst) return keys[i];
    }
    return ALICE_KIND_RAW;
  }
  return 'switch';
}

function aliceMakeManagedItem(kind, topic) {
  const spec = ALICE_KINDS[kind];
  if (!spec || kind === 'switch') {
    return {
      type: 'devices.capabilities.on_off',
      mqtt: topic,
      retrievable: true,
      reportable: true,
      parameters: { instance: 'on' },
    };
  }
  return {
    type: 'devices.properties.float',
    mqtt: topic,
    retrievable: true,
    reportable: true,
    parameters: { instance: spec.instance, unit: spec.unit },
  };
}

// Reset the kind select to a plain enabled state (drop any raw option).
function aliceResetKindSelect() {
  const sel = aliceKindSelect();
  if (!sel) return;
  sel.disabled = false;
  for (let i = sel.options.length - 1; i >= 0; i--) {
    if (sel.options[i].value === ALICE_KIND_RAW) sel.remove(i);
  }
  sel.value = 'switch';
}

// Prefill the kind select for edit mode; a raw (unknown-instance) kind gets a
// locked option labelled with the stored instance so the operator sees what is
// bound without being able to silently retype it.
function alicePrefillKind(kind, dev) {
  const sel = aliceKindSelect();
  if (!sel) return;
  aliceResetKindSelect();
  if (kind === ALICE_KIND_RAW) {
    const prop = aliceManagedProp(dev);
    const inst = (prop && prop.parameters && prop.parameters.instance) || 'custom';
    const opt = document.createElement('option');
    opt.value = ALICE_KIND_RAW;
    opt.textContent = String(inst);
    sel.appendChild(opt);
    sel.value = ALICE_KIND_RAW;
    sel.disabled = true;
    return;
  }
  sel.value = kind;
}

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

  const devices = (d.devices && d.devices.devices) || [];
  const rooms = (d.devices && d.devices.rooms) || [];
  const list = $('alice-device-list');
  if (list) {
    aliceDevCache = {};
    devices.forEach(function (dev) {
      if (dev && dev.id) aliceDevCache[dev.id] = dev;
    });
    // The device being edited disappeared (deleted elsewhere / re-poll) —
    // drop the stale edit mode instead of saving over a ghost id.
    if (aliceEditId && !aliceDevCache[aliceEditId]) aliceCancelEdit();
    if (!devices.length) {
      list.innerHTML = '<p class="field-hint">' + escHtml(uiT('Устройства ещё не добавлены')) + '</p>';
    } else {
      list.innerHTML = devices.map(function (dev) {
        return '<div class="alice-dev-row" data-id="' + escHtml(dev.id || '') + '"><span class="mono text-sm">' +
          escHtml(dev.name || dev.id) + '</span> <span class="text-sm text-sec">' +
          escHtml(aliceDeviceTypeLabel(dev.type)) + '</span>' +
          '<span class="alice-dev-actions">' +
          '<button type="button" class="btn btn-sm" data-act="edit">' + escHtml(uiT('Изменить')) + '</button> ' +
          '<button type="button" class="btn btn-sm btn-danger" data-act="del">' + escHtml(uiT('Удалить')) + '</button>' +
          '</span></div>';
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
  const kindSel = aliceKindSelect();
  const kind = (kindSel && kindSel.value) || 'switch';
  let device;
  const editing = aliceEditId && aliceDevCache[aliceEditId];
  if (editing) {
    // Edit: send the ORIGINAL object with id — the backend replaces in place;
    // id/room_id and any extra (hand-edited) items survive. The form owns
    // exactly ONE managed binding (see aliceManagedCap/aliceManagedProp).
    device = JSON.parse(JSON.stringify(aliceDevCache[aliceEditId]));
    device.name = name;
    const prevKind = aliceDetectKind(device);
    if (kind === ALICE_KIND_RAW) {
      // Locked unknown-instance sensor: retarget the topic only; the stored
      // instance/unit are preserved untouched.
      const rawProp = aliceManagedProp(device);
      if (rawProp) rawProp.mqtt = topic;
    } else if (kind === prevKind) {
      // Kind unchanged: update the managed item's topic in place (a sensor
      // keeps its stored instance/unit; a switch with no on_off yet gains one
      // — same semantics as before this selector existed).
      if (kind === 'switch') {
        const onoff = aliceManagedCap(device);
        if (onoff) {
          onoff.mqtt = topic;
        } else {
          device.capabilities = device.capabilities || [];
          device.capabilities.push(aliceMakeManagedItem('switch', topic));
        }
      } else {
        const prop = aliceManagedProp(device);
        if (prop) prop.mqtt = topic;
        else {
          device.properties = device.properties || [];
          device.properties.push(aliceMakeManagedItem(kind, topic));
        }
      }
    } else {
      // Kind CHANGED: remove the old managed item, insert the new kind's,
      // and move the Yandex device type — never leave both bindings behind.
      const caps = device.capabilities || [];
      const props = device.properties || [];
      if (prevKind === 'switch') {
        const onoff = aliceManagedCap(device);
        if (onoff) caps.splice(caps.indexOf(onoff), 1);
      } else {
        const prop = aliceManagedProp(device);
        if (prop) props.splice(props.indexOf(prop), 1);
      }
      const item = aliceMakeManagedItem(kind, topic);
      if (kind === 'switch') caps.push(item);
      else props.push(item);
      device.capabilities = caps;
      device.properties = props;
      device.type = (ALICE_KINDS[kind] || {}).type || device.type;
    }
  } else {
    const spec = ALICE_KINDS[kind] || ALICE_KINDS.switch;
    device = {
      name: name,
      type: spec.type,
      capabilities: [],
      properties: [],
    };
    if (kind !== 'switch' && spec.instance) {
      device.properties.push(aliceMakeManagedItem(kind, topic));
    } else {
      device.capabilities.push(aliceMakeManagedItem('switch', topic));
    }
  }
  try {
    const d = await aliceApi({ action: 'upsert_device', device: device });
    if (!d.ok) {
      aliceSetBindMsg(d.message || d.error || uiT('Ошибка'), false);
    } else {
      aliceSetBindMsg(uiT('Устройство сохранено'), true);
      aliceCancelEdit();
    }
    await aliceRefresh();
  } catch (e) {
    aliceSetBindMsg(uiT('Ошибка запроса API Алисы'), false);
  }
}

// ── Edit / delete on the device rows (delegated from #alice-device-list) ────
function aliceListClick(e) {
  const btn = e.target && e.target.closest ? e.target.closest('button[data-act]') : null;
  if (!btn) return;
  const row = btn.closest('.alice-dev-row');
  const id = row && row.getAttribute('data-id');
  if (!id) return;
  if (btn.getAttribute('data-act') === 'edit') aliceBeginEdit(id);
  else if (btn.getAttribute('data-act') === 'del') aliceDeleteDevice(id);
}

function aliceBeginEdit(id) {
  const dev = aliceDevCache[id];
  if (!dev) return;
  aliceEditId = id;
  if ($('alice-dev-name')) $('alice-dev-name').value = dev.name || '';
  const kind = aliceDetectKind(dev);
  alicePrefillKind(kind, dev);
  const sel = $('alice-topic-select');
  let topic = '';
  if (kind === 'switch') {
    const onoff = aliceManagedCap(dev);
    topic = (onoff && onoff.mqtt) || '';
  } else {
    const prop = aliceManagedProp(dev);
    topic = (prop && prop.mqtt) || '';
  }
  if (sel && topic) {
    // The bound topic may no longer be in the live topic list — keep it
    // selectable so an edit of the name alone does not silently retarget.
    let found = false;
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === topic) { found = true; break; }
    }
    if (!found) {
      const opt = document.createElement('option');
      opt.value = topic;
      opt.textContent = topic;
      sel.appendChild(opt);
    }
    sel.value = topic;
  }
  const save = $('alice-dev-save');
  if (save) save.textContent = uiT('Сохранить');
  const cancel = $('alice-dev-cancel');
  if (cancel) cancel.hidden = false;
  aliceSetBindMsg('', true);
}

function aliceCancelEdit() {
  aliceEditId = null;
  if ($('alice-dev-name')) $('alice-dev-name').value = '';
  aliceResetKindSelect();
  const save = $('alice-dev-save');
  if (save) save.textContent = uiT('Добавить');
  const cancel = $('alice-dev-cancel');
  if (cancel) cancel.hidden = true;
}

async function aliceDeleteDevice(id) {
  const dev = aliceDevCache[id];
  const label = (dev && dev.name) || id;
  if (!window.confirm(uiT('Удалить устройство') + ' «' + label + '»?')) return;
  try {
    const d = await aliceApi({ action: 'delete_device', id: id });
    if (!d.ok) {
      aliceSetBindMsg(d.message || d.error || uiT('Ошибка'), false);
    } else {
      aliceSetBindMsg(uiT('Устройство удалено'), true);
      if (aliceEditId === id) aliceCancelEdit();
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
  aliceCancelEdit();
}

// Close only on a click on the overlay backdrop itself, not its dialog contents.
function aliceModalBackdrop(e) {
  if (e.target && e.target.id === 'alice-modal') aliceCloseModal();
}

let _alicePoll = null;
let _aliceFastPoll = null;

// After an action that changes state on the device (cert issued → client
// restart, enable/disable), the 5 s cadence makes the card look stuck for
// several seconds. Poll every second for a short window instead, and stop
// early once the state settles.
function aliceFastPoll(windowMs) {
  const until = Date.now() + (windowMs || 20000);
  if (_aliceFastPoll) clearInterval(_aliceFastPoll);
  _aliceFastPoll = setInterval(function () {
    if (Date.now() > until) {
      clearInterval(_aliceFastPoll);
      _aliceFastPoll = null;
      return;
    }
    aliceRefresh();
  }, 1000);
}

function aliceInit() {
  if (!$('alice-card')) return;
  const list = $('alice-device-list');
  if (list) list.addEventListener('click', aliceListClick);
  aliceRefresh();
  aliceLoadTopics();
  if (_alicePoll) clearInterval(_alicePoll);
  _alicePoll = setInterval(aliceRefresh, 5000);
}

// Only functions invoked from HTML onclick handlers need a global handle;
// aliceStartLink/CompleteLink/Unlink/Refresh are called internally (via
// aliceLinkAction / the poll timer) and are intentionally not exported.
window.aliceToggleClient = aliceToggleClient;
window.aliceLinkAction = aliceLinkAction;
window.aliceAddDevice = aliceAddDevice;
window.aliceCancelEdit = aliceCancelEdit;
window.aliceOpenModal = aliceOpenModal;
window.aliceCloseModal = aliceCloseModal;
window.aliceModalBackdrop = aliceModalBackdrop;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', aliceInit);
} else {
  aliceInit();
}

})();
