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
  'devices.types.sensor.motion': 'Датчик движения',
  'devices.types.smart_meter': 'Счётчик',
  'devices.types.smart_meter.electricity': 'Счётчик электроэнергии',
  'devices.types.thermostat': 'Термостат',
  'devices.types.other': 'Устройство',
};

// Reading kind (row select value) → the Yandex pin
// (docs/contracts/alice-mqtt-mapping.md). `kindOf` picks the item shape:
// cap = on_off capability, float = float property (instance + unit + an
// optional unit conversion), event = event property (instance + events[]).
// `type` is the device type auto-suggested when this kind leads the card.
// `scale` converts what the Modbus→MQTT bridge publishes into the unit Yandex
// names; the conversion itself happens once, in the Python converter.
const ALICE_KINDS = {
  switch:      { kindOf: 'cap',   type: 'devices.types.switch' },
  temperature: { kindOf: 'float', instance: 'temperature', unit: 'unit.temperature.celsius', scale: 1,       type: 'devices.types.sensor.climate' },
  humidity:    { kindOf: 'float', instance: 'humidity',    unit: 'unit.percent',             scale: 1,       type: 'devices.types.sensor.climate' },
  pressure:    { kindOf: 'float', instance: 'pressure',    unit: 'unit.pressure.mmhg',       scale: 7.50062, type: 'devices.types.sensor.climate' },
  co2:         { kindOf: 'float', instance: 'co2_level',   unit: 'unit.ppm',                 scale: 1,       type: 'devices.types.sensor.climate' },
  tvoc:        { kindOf: 'float', instance: 'tvoc',        unit: 'unit.density.mcg_m3',      scale: 1000,    type: 'devices.types.sensor.climate' },
  voltage:     { kindOf: 'float', instance: 'voltage',     unit: 'unit.volt',                scale: 1,       type: 'devices.types.sensor' },
  amperage:    { kindOf: 'float', instance: 'amperage',    unit: 'unit.ampere',              scale: 1,       type: 'devices.types.sensor' },
  power:       { kindOf: 'float', instance: 'power',       unit: 'unit.watt',                scale: 1,       type: 'devices.types.sensor' },
  motion:      { kindOf: 'event', instance: 'motion',      events: ['detected', 'not_detected'], type: 'devices.types.sensor.motion' },
};

// Russian source labels — NOT passed through uiT() when a row is built: the
// i18n engine remembers a node's first text as its original, so an English
// label baked in at build time would stay English after a switch back to RU.
// Emitting the RU source lets the DICT observer own both directions.
const ALICE_KIND_LABELS = {
  switch: 'Переключатель',
  temperature: 'Температура',
  humidity: 'Влажность',
  pressure: 'Давление',
  co2: 'Углекислый газ',
  tvoc: 'Летучие вещества',
  voltage: 'Напряжение',
  amperage: 'Ток',
  power: 'Мощность',
  motion: 'Движение',
};

// Sentinel option value for a hand-edited binding whose instance is outside
// ALICE_KINDS: the row's select is locked on it and save touches only the
// topic (instance/unit/scale preserved verbatim).
const ALICE_KIND_RAW = '__raw__';

// Item types the form can express; anything else on a device is preserved
// untouched through an edit.
const ALICE_MANAGED_TYPES = [
  'devices.capabilities.on_off',
  'devices.properties.float',
  'devices.properties.event',
];

function aliceIsManagedItem(item) {
  return !!item && ALICE_MANAGED_TYPES.indexOf(item.type) !== -1;
}

function aliceItemInstance(item) {
  return (item && item.parameters && item.parameters.instance) || '';
}

// The kind key describing a stored item, or the raw sentinel when its instance
// is one we do not offer.
function aliceKindForItem(item) {
  if (!item) return ALICE_KIND_RAW;
  if (item.type === 'devices.capabilities.on_off') return 'switch';
  const inst = aliceItemInstance(item);
  const wantEvent = item.type === 'devices.properties.event';
  const keys = Object.keys(ALICE_KINDS);
  for (let i = 0; i < keys.length; i++) {
    const spec = ALICE_KINDS[keys[i]];
    const isEvent = spec.kindOf === 'event';
    if (spec.instance === inst && isEvent === wantEvent) return keys[i];
  }
  return ALICE_KIND_RAW;
}

// Every managed binding of a device, in stored order (capabilities first) —
// one form row each.
function aliceDetectRows(dev) {
  const rows = [];
  const push = function (item) {
    if (!aliceIsManagedItem(item)) return;
    rows.push({ kind: aliceKindForItem(item), topic: (item && item.mqtt) || '', rawItem: item });
  };
  ((dev && dev.capabilities) || []).forEach(push);
  ((dev && dev.properties) || []).forEach(push);
  return rows;
}

function aliceMakeManagedItem(kind, topic) {
  const spec = ALICE_KINDS[kind];
  if (!spec || spec.kindOf === 'cap') {
    return {
      type: 'devices.capabilities.on_off',
      mqtt: topic,
      retrievable: true,
      reportable: true,
      parameters: { instance: 'on' },
    };
  }
  if (spec.kindOf === 'event') {
    return {
      type: 'devices.properties.event',
      mqtt: topic,
      retrievable: true,
      reportable: true,
      parameters: {
        instance: spec.instance,
        events: spec.events.map(function (v) { return { value: v }; }),
      },
    };
  }
  const item = {
    type: 'devices.properties.float',
    mqtt: topic,
    retrievable: true,
    reportable: true,
    parameters: { instance: spec.instance, unit: spec.unit },
  };
  // Emitted ONLY when it converts something: every kind that existed before
  // this version writes a byte-identical item to the one it wrote yesterday.
  if (spec.scale && spec.scale !== 1) item.scale = spec.scale;
  return item;
}

// The (item type, instance) pair a row will write — the address Yandex uses,
// and therefore what may not repeat within one device.
function aliceRowPair(kind, rawItem) {
  if (kind === ALICE_KIND_RAW) {
    return (rawItem && rawItem.type ? rawItem.type : '') + '|' + aliceItemInstance(rawItem);
  }
  const item = aliceMakeManagedItem(kind, '');
  return item.type + '|' + aliceItemInstance(item);
}

// ── Reading rows («Показания») ──────────────────────────────────────────────
// #alice-rows is USER-owned: only the functions below touch it. aliceRender —
// which the 5 s poll drives — must never read or rebuild it, or a poll landing
// mid-edit would wipe half-filled rows (the renderer-owned-container trap).

// Picker topics, cached once per modal open so a topic refresh never rebuilds
// an existing row or resets the value chosen in it.
let aliceTopicList = [];
// True once the operator picks a device type by hand — the first row's kind
// stops auto-filling it, so an edited device is never silently retyped.
let aliceDtypeTouched = false;

function aliceRowsHost() { return $('alice-rows'); }

function aliceRowKindOptions(selected) {
  return Object.keys(ALICE_KINDS).map(function (k) {
    return '<option value="' + escHtml(k) + '"' + (k === selected ? ' selected' : '') + '>' +
      escHtml(ALICE_KIND_LABELS[k] || k) + '</option>';
  }).join('');
}

function aliceRowTopicOptions(topic) {
  const list = aliceTopicList.slice();
  // A bound topic missing from the live list stays selectable — editing the
  // name alone must never silently retarget the binding.
  if (topic && list.indexOf(topic) === -1) list.push(topic);
  return list.map(function (t) {
    return '<option value="' + escHtml(t) + '"' + (t === topic ? ' selected' : '') + '>' +
      escHtml(t) + '</option>';
  }).join('');
}

function aliceAddRow(kind, topic, rawItem) {
  const host = aliceRowsHost();
  if (!host) return;
  const locked = kind === ALICE_KIND_RAW;
  const k = locked ? ALICE_KIND_RAW : (ALICE_KINDS[kind] ? kind : 'temperature');
  const row = document.createElement('div');
  row.className = 'alice-bind-row';
  let kindHtml;
  if (locked) {
    // Unknown stored instance: show what is bound, refuse to retype it.
    const inst = aliceItemInstance(rawItem) || 'custom';
    kindHtml = '<option value="' + escHtml(ALICE_KIND_RAW) + '" selected>' + escHtml(inst) + '</option>';
  } else {
    kindHtml = aliceRowKindOptions(k);
  }
  row.innerHTML =
    '<select class="alice-row-kind" aria-label="Вид показания"' + (locked ? ' disabled' : '') + '>' +
    kindHtml + '</select>' +
    '<select class="alice-row-topic" aria-label="MQTT-топик">' +
    aliceRowTopicOptions(topic || '') + '</select>' +
    '<button type="button" class="btn btn-sm btn-danger alice-row-del" data-act="row-del"' +
    ' aria-label="Удалить показание" title="Удалить показание">✕</button>';
  // JS properties, never data-attributes: the stored item must not be
  // serialised into the markup.
  row._aliceRawItem = rawItem || null;
  row._aliceOrigKind = rawItem ? k : null;
  host.appendChild(row);
  return row;
}

function aliceClearRows() {
  const host = aliceRowsHost();
  if (host) host.innerHTML = '';
}

// Add mode starts on one empty row; the device type follows it, so the form
// never opens showing «Датчик» beside a «Температура» row.
const ALICE_DEFAULT_KIND = 'temperature';

function aliceSeedDefaultRow() {
  aliceAddRow(ALICE_DEFAULT_KIND, '', null);
  if (!aliceDtypeTouched) aliceSetDtype(ALICE_KINDS[ALICE_DEFAULT_KIND].type);
}

function aliceRowsClick(e) {
  const btn = e.target && e.target.closest ? e.target.closest('button[data-act="row-del"]') : null;
  if (!btn) return;
  const host = aliceRowsHost();
  const row = btn.closest('.alice-bind-row');
  if (!host || !row) return;
  if (host.querySelectorAll('.alice-bind-row').length <= 1) {
    aliceSetBindMsg(uiT('Нужно хотя бы одно показание'), false);
    return;
  }
  row.parentNode.removeChild(row);
  aliceSetBindMsg('', true);
}

function aliceRowsChange(e) {
  const sel = e.target;
  if (!sel || !sel.classList || !sel.classList.contains('alice-row-kind')) return;
  if (aliceDtypeTouched) return;
  const host = aliceRowsHost();
  const first = host && host.querySelector('.alice-bind-row .alice-row-kind');
  if (!first || first !== sel) return;
  const spec = ALICE_KINDS[sel.value];
  const dtype = $('alice-dev-dtype');
  if (spec && dtype) aliceSetDtype(spec.type);
}

// Read the rows back at save time — the DOM is the state, so there is no
// parallel array to fall out of sync with it.
function aliceCollectRows() {
  const host = aliceRowsHost();
  const out = [];
  const seen = {};
  const nodes = host ? host.querySelectorAll('.alice-bind-row') : [];
  for (let i = 0; i < nodes.length; i++) {
    const row = nodes[i];
    const kindSel = row.querySelector('.alice-row-kind');
    const topicSel = row.querySelector('.alice-row-topic');
    const kind = (kindSel && kindSel.value) || '';
    const topic = (topicSel && topicSel.value) || '';
    if (!topic) return { rows: [], error: uiT('Укажите MQTT-топик для каждого показания') };
    const pair = aliceRowPair(kind, row._aliceRawItem);
    if (seen[pair]) {
      return { rows: [], error: uiT('Два показания одного вида в одном устройстве — выберите разные') };
    }
    seen[pair] = true;
    out.push({ kind: kind, topic: topic, rawItem: row._aliceRawItem, origKind: row._aliceOrigKind });
  }
  if (!out.length) return { rows: [], error: uiT('Нужно хотя бы одно показание') };
  return { rows: out, error: null };
}

// The item a row writes: an untouched stored item keeps every field it had
// (instance, unit, scale, hand-added keys) and is only retargeted — the
// round-trip guarantee deployed bindings depend on.
function aliceRowItem(row) {
  if (row.rawItem && row.origKind === row.kind) {
    const item = JSON.parse(JSON.stringify(row.rawItem));
    item.mqtt = row.topic;
    return item;
  }
  return aliceMakeManagedItem(row.kind, row.topic);
}

function aliceSetDtype(type) {
  const sel = $('alice-dev-dtype');
  if (!sel || !type) return;
  for (let i = 0; i < sel.options.length; i++) {
    if (sel.options[i].value === type) { sel.value = type; return; }
  }
  // An unusual stored type gets a locked-in option rather than being rewritten.
  // RU source text (not uiT) so the DICT observer owns both directions.
  const opt = document.createElement('option');
  opt.value = type;
  opt.textContent = ALICE_DEV_TYPES[type] || String(type);
  sel.appendChild(opt);
  sel.value = type;
}

// « · 6 показаний» for a multi-reading card, nothing for a single binding —
// so a card carrying several values is legible in the list. Russian counts
// 2–4 differently from 5+; the list is rebuilt on every poll, so uiT() here
// cannot freeze a language (unlike the durable row labels above).
function aliceReadingCount(dev) {
  const n = (((dev && dev.capabilities) || []).length) + (((dev && dev.properties) || []).length);
  if (n < 2) return '';
  const mod10 = n % 10;
  const mod100 = n % 100;
  const few = mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14);
  return ' · ' + n + ' ' + uiT(few ? 'показания' : 'показаний');
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
    // drop the stale edit mode instead of saving over a ghost id. This is the
    // ONE path on which the poll may reset #alice-rows, and only because the
    // rows describe a device that no longer exists; the render never otherwise
    // reads or rebuilds them (they are user-owned).
    if (aliceEditId && !aliceDevCache[aliceEditId]) aliceCancelEdit();
    if (!devices.length) {
      list.innerHTML = '<p class="field-hint">' + escHtml(uiT('Устройства ещё не добавлены')) + '</p>';
    } else {
      list.innerHTML = devices.map(function (dev) {
        return '<div class="alice-dev-row" data-id="' + escHtml(dev.id || '') + '"><span class="mono text-sm">' +
          escHtml(dev.name || dev.id) + '</span> <span class="text-sm text-sec">' +
          escHtml(aliceDeviceTypeLabel(dev.type) + aliceReadingCount(dev)) + '</span>' +
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

// Fills the cached picker list ONLY — existing rows are never rebuilt, so a
// refresh cannot reset a topic the operator already chose.
async function aliceLoadTopics() {
  try {
    const d = await aliceTopics();
    aliceTopicList = (d && d.topics) || [];
  } catch (e) {
    /* ignore — an empty list still lets a bound topic show through */
  }
}

async function aliceAddDevice() {
  const name = ($('alice-dev-name') && $('alice-dev-name').value || '').trim();
  if (!name) {
    aliceSetBindMsg(uiT('Укажите имя и MQTT-топик'), false);
    return;
  }
  const collected = aliceCollectRows();
  if (collected.error) {
    aliceSetBindMsg(collected.error, false);
    return;
  }
  const dtype = ($('alice-dev-dtype') && $('alice-dev-dtype').value) || 'devices.types.sensor';
  const caps = [];
  const props = [];
  const editing = aliceEditId && aliceDevCache[aliceEditId];
  let device;
  if (editing) {
    // Edit: keep id/room_id and every item the form cannot express, in stored
    // order, then re-emit one item per row.
    device = JSON.parse(JSON.stringify(aliceDevCache[aliceEditId]));
    (device.capabilities || []).forEach(function (it) {
      if (!aliceIsManagedItem(it)) caps.push(it);
    });
    (device.properties || []).forEach(function (it) {
      if (!aliceIsManagedItem(it)) props.push(it);
    });
  } else {
    device = { capabilities: [], properties: [] };
  }
  device.name = name;
  device.type = dtype;
  collected.rows.forEach(function (row) {
    const item = aliceRowItem(row);
    if (item.type.indexOf('devices.capabilities.') === 0) caps.push(item);
    else props.push(item);
  });
  device.capabilities = caps;
  device.properties = props;
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
  // An existing device's type is the operator's, never re-derived from a row.
  aliceDtypeTouched = true;
  aliceSetDtype(dev.type || 'devices.types.sensor');
  aliceClearRows();
  const rows = aliceDetectRows(dev);
  if (!rows.length) aliceAddRow(ALICE_DEFAULT_KIND, '', null);
  else rows.forEach(function (r) { aliceAddRow(r.kind, r.topic, r.rawItem); });
  const save = $('alice-dev-save');
  if (save) save.textContent = uiT('Сохранить');
  const cancel = $('alice-dev-cancel');
  if (cancel) cancel.hidden = false;
  aliceSetBindMsg('', true);
}

function aliceCancelEdit() {
  aliceEditId = null;
  if ($('alice-dev-name')) $('alice-dev-name').value = '';
  aliceDtypeTouched = false;
  aliceClearRows();
  aliceSeedDefaultRow();
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

async function aliceOpenModal() {
  const m = $('alice-modal');
  if (!m) return;
  aliceSetBindMsg('', true);
  m.removeAttribute('hidden');
  document.addEventListener('keydown', aliceModalEsc);
  aliceRefresh();
  // Topics FIRST: the seeded row's topic picker would otherwise open empty.
  await aliceLoadTopics();
  const host = aliceRowsHost();
  if (host && !host.querySelector('.alice-bind-row')) aliceSeedDefaultRow();
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
  const list = $('alice-device-list');
  if (list) list.addEventListener('click', aliceListClick);
  // Delegated from the container, which itself is static markup — a rebuilt
  // row keeps working without re-binding anything.
  const rows = aliceRowsHost();
  if (rows) {
    rows.addEventListener('click', aliceRowsClick);
    rows.addEventListener('change', aliceRowsChange);
  }
  const dtype = $('alice-dev-dtype');
  if (dtype) dtype.addEventListener('change', function () { aliceDtypeTouched = true; });
  aliceRefresh();
  aliceLoadTopics();
  if (_alicePoll) clearInterval(_alicePoll);
  _alicePoll = setInterval(aliceRefresh, ALICE_POLL_MS);
}

// Only functions invoked from HTML onclick handlers need a global handle;
// aliceStartLink/CompleteLink/Unlink/Refresh are called internally (via
// aliceLinkAction / the poll timer) and are intentionally not exported.
window.aliceToggleClient = aliceToggleClient;
window.aliceLinkAction = aliceLinkAction;
window.aliceAddDevice = aliceAddDevice;
window.aliceAddRow = aliceAddRow;
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
