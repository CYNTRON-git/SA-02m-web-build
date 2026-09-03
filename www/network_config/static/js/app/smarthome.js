/* SA-02m — Умный дом (вкладка «Управление»): комнаты / устройства / привязки
   + «Управление из облака». Classic script loaded AFTER app/alice.js: it rides
   the Alice card's status poll through window.sa02mAliceOnData (one CGI poll
   for both cards) and calls the same CGI through window.sa02mAliceApi. The
   bindings modal (#sh-modal) moved here from the Alice card in 1.0.6.26. */

(function () {
'use strict';

function uiT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(String(s)) : String(s);
}

function $(id) { return document.getElementById(id); }

// The shared CGI + poll pipeline is alice.js's; read the handles lazily so a
// load-order slip degrades to "no data" rather than a TypeError at parse.
function shApi(body) {
  if (typeof window.sa02mAliceApi !== 'function') return Promise.reject(new Error('alice api missing'));
  return window.sa02mAliceApi(body);
}

function shRefresh() {
  if (typeof window.sa02mAliceRefresh !== 'function') return Promise.resolve(null);
  return window.sa02mAliceRefresh();
}

async function shTopics() {
  const r = await fetch('cgi-bin/sa02m_alice_topics.cgi', {
    method: 'GET',
    credentials: 'same-origin',
  });
  return r.json();
}

// Transient success notices auto-clear; errors stay until the state changes.
const SH_MSG_TTL_MS = 5000;
const _shMsgTimers = {};

function shSetMsgOn(id, text, ok) {
  const msg = $(id);
  if (!msg) return;
  if (_shMsgTimers[id]) { clearTimeout(_shMsgTimers[id]); _shMsgTimers[id] = null; }
  if (!text) {
    msg.hidden = true;
    msg.textContent = '';
    msg.className = 'cloud-msg';
    return;
  }
  msg.hidden = false;
  msg.textContent = text;
  // ok === null → neutral hint (neither success nor error tint).
  msg.className = 'cloud-msg' + (ok === null ? '' : (ok ? ' is-ok' : ' is-err'));
  if (ok === true) {
    _shMsgTimers[id] = setTimeout(function () {
      _shMsgTimers[id] = null;
      const el = $(id);
      // Only clear what is still this notice — a newer message owns itself.
      if (el && !el.hidden && el.textContent === text) shSetMsgOn(id, '', true);
    }, SH_MSG_TTL_MS);
  }
}

// Modal feedback (#sh-bind-msg). The card-level line moved to the «Облако»
// card with the cloud-control button (1.0.6.29).
function shSetBindMsg(text, ok) { shSetMsgOn('sh-bind-msg', text, ok); }

// ── Vocabulary ─────────────────────────────────────────────────────────────
// Yandex device-type ids are machine keys, never a user label — map to a human
// RU string (translated by uiT / DICT), falling back to the last dotted
// segment so a new type never renders its raw key. Ids verified against the
// Yandex device-type reference on 2026-09-02.
const SH_DEV_TYPES = {
  'devices.types.light': 'Освещение',
  'devices.types.socket': 'Розетка',
  'devices.types.ventilation.fan': 'Вентилятор',
  'devices.types.switch': 'Выключатель',
  'devices.types.other': 'Другое',
  'devices.types.sensor': 'Датчик',
  'devices.types.sensor.climate': 'Климат-датчик',
  'devices.types.sensor.motion': 'Датчик движения',
  'devices.types.smart_meter': 'Счётчик',
  'devices.types.smart_meter.electricity': 'Счётчик электроэнергии',
  'devices.types.thermostat': 'Термостат',
  'devices.types.ventilation': 'Вентустановка',
};

// The on/off device types — the «Включить/выключить» optgroup in #sh-dev-type.
// Named explicitly (not derived from the icon map below) so a type added here
// without an icon is still seeded with a `switch` row and shows the icon
// field; it then falls back to the `generic` icon (shSyncTypeUi).
const SH_ONOFF_TYPES = [
  'devices.types.light',
  'devices.types.socket',
  'devices.types.ventilation.fan',
  'devices.types.switch',
  'devices.types.other',
];
// Default tile icon per on/off type (the cloud page draws it); the icon
// select is preselected from this until the operator picks one by hand.
const SH_ICON_BY_TYPE = {
  'devices.types.light': 'bulb',
  'devices.types.socket': 'socket',
  'devices.types.ventilation.fan': 'fan',
  'devices.types.switch': 'relay',
  'devices.types.other': 'generic',
  'devices.types.ventilation': 'fan',
};
const SH_ICONS = ['bulb', 'fan', 'socket', 'relay', 'pump', 'valve', 'siren', 'generic'];

// A ventilation unit is neither a plain switch nor a plain sensor: it is
// switched on/off AND carries a setpoint and several readings, so it gets the
// icon picker (the cloud page draws a tile for it) without joining
// SH_ONOFF_TYPES, whose members seed a single «Переключатель» row.
const SH_COMPOSITE_TYPES = ['devices.types.ventilation'];

function shIsCompositeType(type) {
  return SH_COMPOSITE_TYPES.indexOf(type) !== -1;
}

function shIsOnOffType(type) {
  return SH_ONOFF_TYPES.indexOf(type) !== -1;
}

// Reading kind (row select value) → the Yandex pin
// (docs/contracts/alice-mqtt-mapping.md). `kindOf` picks the item shape:
// cap = on_off capability, float = float property (instance + unit + an
// optional unit conversion), event = event property (instance + events[]).
// `type` is the device type auto-suggested when this kind leads the card.
// `scale` converts what the Modbus→MQTT bridge publishes into the unit Yandex
// names; the conversion itself happens once, in the Python converter.
const SH_KINDS = {
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
  // Ventilation unit (Carel AHU). `range` is a writable setpoint, so its
  // bounds travel with it; `cloudOnly` marks a reading Yandex has no instance
  // for — it reaches the cloud control page and is dropped from everything
  // the Alice profile sends (docs/contracts/alice-mqtt-mapping.md).
  setpoint:    { kindOf: 'range', instance: 'temperature', unit: 'unit.temperature.celsius', range: { min: 0, max: 99, precision: 0.5 }, type: 'devices.types.ventilation' },
  supply_temp: { kindOf: 'float', instance: 'temperature', unit: 'unit.temperature.celsius', scale: 1, type: 'devices.types.ventilation' },
  return_water: { kindOf: 'float', instance: 'return_water_temperature', unit: 'unit.temperature.celsius', scale: 1, cloudOnly: true, type: 'devices.types.ventilation' },
  room_temp:   { kindOf: 'float', instance: 'room_temperature', unit: 'unit.temperature.celsius', scale: 1, cloudOnly: true, type: 'devices.types.ventilation' },
  outdoor_temp: { kindOf: 'float', instance: 'outdoor_temperature', unit: 'unit.temperature.celsius', scale: 1, cloudOnly: true, type: 'devices.types.ventilation' },
  plant_state: { kindOf: 'event', instance: 'plant_state', events: ['run', 'stop', 'alarm'], cloudOnly: true, type: 'devices.types.ventilation' },
  unit_status: { kindOf: 'event', instance: 'unit_status', cloudOnly: true, type: 'devices.types.ventilation' },
  alarm:       { kindOf: 'event', instance: 'alarm', events: ['alarm', 'normal'], cloudOnly: true, type: 'devices.types.ventilation' },
};

// Russian source labels — NOT passed through uiT() when a row is built: the
// i18n engine remembers a node's first text as its original, so an English
// label baked in at build time would stay English after a switch back to RU.
// Emitting the RU source lets the DICT observer own both directions.
const SH_KIND_LABELS = {
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
  setpoint: 'Уставка температуры',
  supply_temp: 'Температура притока',
  return_water: 'Температура обратной воды',
  room_temp: 'Температура в помещении',
  outdoor_temp: 'Температура снаружи',
  plant_state: 'Состояние установки',
  unit_status: 'Статус установки (текст)',
  alarm: 'Авария',
};

// Sentinel option value for a hand-edited binding whose instance is outside
// SH_KINDS: the row's select is locked on it and save touches only the
// topic (instance/unit/scale preserved verbatim).
const SH_KIND_RAW = '__raw__';

// Item types the form can express; anything else on a device is preserved
// untouched through an edit.
const SH_MANAGED_TYPES = [
  'devices.capabilities.on_off',
  'devices.capabilities.range',
  'devices.properties.float',
  'devices.properties.event',
];

function shIsManagedItem(item) {
  return !!item && SH_MANAGED_TYPES.indexOf(item.type) !== -1;
}

function shItemInstance(item) {
  return (item && item.parameters && item.parameters.instance) || '';
}

// The kind key describing a stored item, or the raw sentinel when its instance
// is one we do not offer.
function shKindForItem(item) {
  if (!item) return SH_KIND_RAW;
  if (item.type === 'devices.capabilities.on_off') return 'switch';
  const inst = shItemInstance(item);
  const isRange = item.type === 'devices.capabilities.range';
  const wantEvent = item.type === 'devices.properties.event';
  const keys = Object.keys(SH_KINDS);
  for (let i = 0; i < keys.length; i++) {
    const spec = SH_KINDS[keys[i]];
    const specRange = spec.kindOf === 'range';
    const isEvent = spec.kindOf === 'event';
    if (specRange !== isRange) continue;
    if (spec.instance !== inst || isEvent !== wantEvent) continue;
    // temperature is claimed by two kinds — a plain climate sensor and the
    // unit's supply-air reading. They write the same item, so the first match
    // round-trips an edit unchanged either way.
    return keys[i];
  }
  return SH_KIND_RAW;
}

// Every managed binding of a device, in stored order (capabilities first) —
// one form row each.
function shDetectRows(dev) {
  const rows = [];
  const push = function (item) {
    if (!shIsManagedItem(item)) return;
    rows.push({
      kind: shKindForItem(item),
      topic: (item && item.mqtt) || '',
      inverted: !!(item && item.inverted),
      rawItem: item,
    });
  };
  ((dev && dev.capabilities) || []).forEach(push);
  ((dev && dev.properties) || []).forEach(push);
  return rows;
}

function shMakeManagedItem(kind, topic, inverted) {
  const spec = SH_KINDS[kind];
  if (spec && spec.kindOf === 'range') {
    return {
      type: 'devices.capabilities.range',
      mqtt: topic,
      retrievable: true,
      reportable: true,
      parameters: {
        instance: spec.instance,
        unit: spec.unit,
        range: { min: spec.range.min, max: spec.range.max, precision: spec.range.precision },
      },
    };
  }
  if (!spec || spec.kindOf === 'cap') {
    const cap = {
      type: 'devices.capabilities.on_off',
      mqtt: topic,
      retrievable: true,
      reportable: true,
      parameters: { instance: 'on' },
    };
    // Emitted ONLY when true — an unchecked box writes the item a pre-1.0.6.29
    // version wrote (the `scale` rule; the validator drops a stored `false`).
    if (inverted) cap.inverted = true;
    return cap;
  }
  if (spec.kindOf === 'event') {
    const item = {
      type: 'devices.properties.event',
      mqtt: topic,
      retrievable: true,
      reportable: true,
      parameters: { instance: spec.instance },
    };
    // A free-text status line has no closed value set to declare.
    if (spec.events) {
      item.parameters.events = spec.events.map(function (v) { return { value: v }; });
    }
    if (spec.cloudOnly) item.cloud_only = true;
    return item;
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
  if (spec.cloudOnly) item.cloud_only = true;
  return item;
}

// The (item type, instance) pair a row will write — the address Yandex uses,
// and therefore what may not repeat within one device.
function shRowPair(kind, rawItem) {
  if (kind === SH_KIND_RAW) {
    return (rawItem && rawItem.type ? rawItem.type : '') + '|' + shItemInstance(rawItem);
  }
  const item = shMakeManagedItem(kind, '');
  return item.type + '|' + shItemInstance(item);
}

// ── Reading rows («Показания») ──────────────────────────────────────────────
// #sh-rows is USER-owned: only the functions below touch it. The poll-driven
// render must never read or rebuild it, or a poll landing mid-edit would wipe
// half-filled rows (the renderer-owned-container trap).

// Picker topics, cached once per modal open so a topic refresh never rebuilds
// an existing row or resets the value chosen in it.
let shTopicList = [];
// True once the operator picks a device type by hand — the first row's kind
// stops auto-filling it, so an edited device is never silently retyped.
let shDtypeTouched = false;
// True once the operator picks an icon by hand — a type change stops
// re-suggesting one.
let shIconTouched = false;

function shRowsHost() { return $('sh-rows'); }

function shRowKindOptions(selected) {
  return Object.keys(SH_KINDS).map(function (k) {
    return '<option value="' + escHtml(k) + '"' + (k === selected ? ' selected' : '') + '>' +
      escHtml(SH_KIND_LABELS[k] || k) + '</option>';
  }).join('');
}

function shRowTopicOptions(topic) {
  const list = shTopicList.slice();
  // A bound topic missing from the live list stays selectable — editing the
  // name alone must never silently retarget the binding.
  if (topic && list.indexOf(topic) === -1) list.push(topic);
  return list.map(function (t) {
    return '<option value="' + escHtml(t) + '"' + (t === topic ? ' selected' : '') + '>' +
      escHtml(t) + '</option>';
  }).join('');
}

function shAddRow(kind, topic, rawItem) {
  const host = shRowsHost();
  if (!host) return;
  const locked = kind === SH_KIND_RAW;
  const k = locked ? SH_KIND_RAW : (SH_KINDS[kind] ? kind : 'temperature');
  const row = document.createElement('div');
  row.className = 'sh-bind-row';
  let kindHtml;
  if (locked) {
    // Unknown stored instance: show what is bound, refuse to retype it.
    const inst = shItemInstance(rawItem) || 'custom';
    kindHtml = '<option value="' + escHtml(SH_KIND_RAW) + '" selected>' + escHtml(inst) + '</option>';
  } else {
    kindHtml = shRowKindOptions(k);
  }
  row.innerHTML =
    '<select class="sh-row-kind" aria-label="Вид показания"' + (locked ? ' disabled' : '') + '>' +
    kindHtml + '</select>' +
    '<select class="sh-row-topic" aria-label="MQTT-топик">' +
    shRowTopicOptions(topic || '') + '</select>' +
    '<button type="button" class="btn btn-sm btn-danger sh-row-del" data-act="row-del"' +
    ' aria-label="Удалить показание" title="Удалить показание">✕</button>';
  // JS properties, never data-attributes: the stored item must not be
  // serialised into the markup.
  row._shRawItem = rawItem || null;
  row._shOrigKind = rawItem ? k : null;
  host.appendChild(row);
  shSyncInvertedField();
  return row;
}

// «Инвертировать» is a DEVICE-level control (#sh-inv-field), shown only while
// the device has an on/off binding — a reading has no output to invert. It is
// device level, not row level, because a device can hold at most one on_off
// item (validate_device refuses a second one with the same type+instance), and
// on the row it squeezed the topic select below legibility.
function shSyncInvertedField() {
  const field = $('sh-inv-field');
  if (!field) return;
  field.hidden = !shHasOnOffRow();
}

function shHasOnOffRow() {
  const host = shRowsHost();
  const sels = host ? host.querySelectorAll('.sh-bind-row .sh-row-kind') : [];
  for (let i = 0; i < sels.length; i++) {
    const spec = SH_KINDS[sels[i].value];
    if (spec && spec.kindOf === 'cap') return true;
  }
  return false;
}

function shClearRows() {
  const host = shRowsHost();
  if (host) host.innerHTML = '';
  shSyncInvertedField();
}

// Add mode starts on one empty row; the device type follows it, so the form
// never opens showing «Датчик» beside a «Температура» row.
const SH_DEFAULT_KIND = 'temperature';

function shSeedDefaultRow() {
  shAddRow(SH_DEFAULT_KIND, '', null);
  if (!shDtypeTouched) shSetDtype(SH_KINDS[SH_DEFAULT_KIND].type);
  shSyncTypeUi();
}

// The rows are still the untouched seed (nothing bound, nothing chosen) — the
// only state in which a type change may replace them. "Touched" is a JS flag
// set on any change inside the row, NOT the topic select's value: a select
// with options always auto-picks the first one, so the value alone would
// make every fresh seed look chosen.
function shRowsUntouched() {
  const host = shRowsHost();
  const rows = host ? host.querySelectorAll('.sh-bind-row') : [];
  if (rows.length === 0) return true;
  if (rows.length > 1) return false;
  return !rows[0]._shRawItem && !rows[0]._shTouched;
}

// Choosing an on/off type seeds one `switch` row; choosing a sensor type over
// an untouched `switch` seed puts the default reading back. Bound rows are
// never replaced.
// The readings a ventilation unit publishes, in the order the cloud card
// draws them. Seeded together because binding them one by one is eight manual
// topic picks for a device whose control names are fixed.
const SH_VENT_ROWS = ['switch', 'setpoint', 'supply_temp', 'return_water',
                      'room_temp', 'plant_state', 'unit_status', 'alarm'];

function shSeedRowsForType(type) {
  if (!shRowsUntouched()) return;
  const host = shRowsHost();
  const first = host && host.querySelector('.sh-bind-row .sh-row-kind');
  const current = first ? first.value : '';
  if (shIsCompositeType(type)) {
    // shRowsUntouched() above already guarantees at most one untouched row
    // here, so this only ever replaces a seed, never a bound row.
    shClearRows();
    SH_VENT_ROWS.forEach(function (kind) { shAddRow(kind, '', null); });
    return;
  }
  if (shIsOnOffType(type)) {
    if (current !== 'switch') { shClearRows(); shAddRow('switch', '', null); }
  } else if (current === 'switch') {
    shClearRows();
    shAddRow(SH_DEFAULT_KIND, '', null);
  }
}

function shRowsClick(e) {
  const btn = e.target && e.target.closest ? e.target.closest('button[data-act="row-del"]') : null;
  if (!btn) return;
  const host = shRowsHost();
  const row = btn.closest('.sh-bind-row');
  if (!host || !row) return;
  if (host.querySelectorAll('.sh-bind-row').length <= 1) {
    shSetBindMsg(uiT('Нужно хотя бы одно показание'), false);
    return;
  }
  row.parentNode.removeChild(row);
  // Deleting the on/off row takes the «Инвертировать» field with it.
  shSyncInvertedField();
  shSetBindMsg('', true);
}

function shRowsChange(e) {
  const sel = e.target;
  // Any change inside a row (kind or topic) marks it as the operator's: a
  // later type pick must not replace it (see shRowsUntouched).
  const row = sel && sel.closest ? sel.closest('.sh-bind-row') : null;
  if (row) row._shTouched = true;
  // A kind change may reveal or hide the «Инвертировать» field.
  if (sel && sel.classList && sel.classList.contains('sh-row-kind')) shSyncInvertedField();
  if (!sel || !sel.classList || !sel.classList.contains('sh-row-kind')) return;
  if (shDtypeTouched) return;
  const host = shRowsHost();
  const first = host && host.querySelector('.sh-bind-row .sh-row-kind');
  if (!first || first !== sel) return;
  const spec = SH_KINDS[sel.value];
  const dtype = $('sh-dev-type');
  if (spec && dtype) { shSetDtype(spec.type); shSyncTypeUi(); }
}

// Read the rows back at save time — the DOM is the state, so there is no
// parallel array to fall out of sync with it.
function shCollectRows() {
  const host = shRowsHost();
  const out = [];
  const seen = {};
  const invEl = $('sh-dev-inverted');
  const inverted = !!(invEl && invEl.checked);
  const nodes = host ? host.querySelectorAll('.sh-bind-row') : [];
  for (let i = 0; i < nodes.length; i++) {
    const row = nodes[i];
    const kindSel = row.querySelector('.sh-row-kind');
    const topicSel = row.querySelector('.sh-row-topic');
    const kind = (kindSel && kindSel.value) || '';
    const topic = (topicSel && topicSel.value) || '';
    if (!topic) return { rows: [], error: uiT('Укажите MQTT-топик для каждого показания') };
    const pair = shRowPair(kind, row._shRawItem);
    if (seen[pair]) {
      return { rows: [], error: uiT('Два показания одного вида в одном устройстве — выберите разные') };
    }
    seen[pair] = true;
    out.push({
      kind: kind,
      topic: topic,
      // Device-level flag, applied to the one on/off binding it can describe.
      inverted: inverted && !!(SH_KINDS[kind] && SH_KINDS[kind].kindOf === 'cap'),
      rawItem: row._shRawItem,
      origKind: row._shOrigKind,
    });
  }
  if (!out.length) return { rows: [], error: uiT('Нужно хотя бы одно показание') };
  return { rows: out, error: null };
}

// The item a row writes: an untouched stored item keeps every field it had
// (instance, unit, scale, hand-added keys) and is only retargeted — the
// round-trip guarantee deployed bindings depend on.
function shRowItem(row) {
  if (row.rawItem && row.origKind === row.kind) {
    const item = JSON.parse(JSON.stringify(row.rawItem));
    item.mqtt = row.topic;
    // The checkbox owns the flag on an on/off item: set it when ticked, drop it
    // when not, so unticking really clears it instead of keeping the old value.
    if (item.type === 'devices.capabilities.on_off') {
      if (row.inverted) item.inverted = true;
      else delete item.inverted;
    }
    return item;
  }
  return shMakeManagedItem(row.kind, row.topic, row.inverted);
}

// ── Type / icon ─────────────────────────────────────────────────────────────
function shSetDtype(type) {
  const sel = $('sh-dev-type');
  if (!sel || !type) return;
  for (let i = 0; i < sel.options.length; i++) {
    if (sel.options[i].value === type) { sel.value = type; return; }
  }
  // An unusual stored type gets a locked-in option rather than being rewritten.
  // RU source text (not uiT) so the DICT observer owns both directions.
  const opt = document.createElement('option');
  opt.value = type;
  opt.textContent = SH_DEV_TYPES[type] || String(type);
  sel.appendChild(opt);
  sel.value = type;
}

function shCurrentDtype() {
  const sel = $('sh-dev-type');
  return (sel && sel.value) || 'devices.types.sensor';
}

function shSetIcon(icon) {
  const sel = $('sh-dev-icon');
  if (!sel) return;
  sel.value = SH_ICONS.indexOf(icon) !== -1 ? icon : 'generic';
  shRenderPreview();
}

function shRenderPreview() {
  const host = $('sh-type-preview');
  const sel = $('sh-dev-icon');
  if (!host || !sel) return;
  const icon = SH_ICONS.indexOf(sel.value) !== -1 ? sel.value : 'generic';
  const use = host.querySelector('use');
  if (use) use.setAttribute('href', '#i-' + icon);
}

// The icon field follows the type: shown for on/off types, preselected by
// type until the operator picks one by hand.
function shSyncTypeUi() {
  const type = shCurrentDtype();
  const field = $('sh-icon-field');
  const onOff = shIsOnOffType(type);
  const tiled = onOff || shIsCompositeType(type);
  if (field) field.hidden = !tiled;
  if (tiled && !shIconTouched) shSetIcon(SH_ICON_BY_TYPE[type] || 'generic');
}

function shDtypeChanged() {
  shDtypeTouched = true;
  shSeedRowsForType(shCurrentDtype());
  shSyncTypeUi();
}

// « · 6 показаний» for a multi-reading card, nothing for a single binding —
// so a card carrying several values is legible in the list. Russian counts
// 2–4 differently from 5+; the list is rebuilt on every poll, so uiT() here
// cannot freeze a language (unlike the durable row labels above).
function shReadingCount(dev) {
  const n = (((dev && dev.capabilities) || []).length) + (((dev && dev.properties) || []).length);
  if (n < 2) return '';
  const mod10 = n % 10;
  const mod100 = n % 100;
  const few = mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14);
  return ' · ' + n + ' ' + uiT(few ? 'показания' : 'показаний');
}

function shDeviceTypeLabel(type) {
  if (!type) return '';
  const ru = SH_DEV_TYPES[type];
  if (ru) return uiT(ru);
  const seg = String(type).split('.').pop();
  return seg ? seg.charAt(0).toUpperCase() + seg.slice(1) : '';
}

function shDeviceIcon(dev) {
  if (dev && SH_ICONS.indexOf(dev.icon) !== -1) return dev.icon;
  return SH_ICON_BY_TYPE[dev && dev.type] || 'generic';
}

function shVisibleInAlice(dev) {
  return !dev || dev.alice_visible !== false;
}

// ── Render (poll-driven) ────────────────────────────────────────────────────
// Edit-mode state: id being edited + the last rendered device objects by id
// (source for prefill and id/room_id/type preservation).
let shEditId = null;
let shDevCache = {};
let shRoomCache = {};
let shRoomSig = '';

function shCountsText(rooms, devices) {
  let inAlice = 0;
  devices.forEach(function (d) { if (shVisibleInAlice(d)) inAlice++; });
  return uiT('Комнат') + ': ' + rooms.length + ' · ' + uiT('Устройств') + ': ' + devices.length +
    ' · ' + uiT('в Алисе') + ': ' + inAlice;
}

function shRenderRooms(rooms) {
  shRoomCache = {};
  rooms.forEach(function (r) { if (r && r.id) shRoomCache[r.id] = r; });
  const list = $('sh-room-list');
  if (list) {
    if (!rooms.length) {
      list.innerHTML = '<p class="field-hint">' + escHtml(uiT('Комнаты ещё не добавлены')) + '</p>';
    } else {
      list.innerHTML = rooms.map(function (r) {
        return '<div class="sh-room-row" data-id="' + escHtml(r.id || '') + '">' +
          '<span class="sh-room-name text-sm">' + escHtml(r.name || r.id) + '</span>' +
          '<button type="button" class="btn btn-sm btn-danger" data-act="room-del" aria-label="' +
          escHtml(uiT('Удалить комнату')) + '" title="' + escHtml(uiT('Удалить комнату')) + '">✕</button>' +
          '</div>';
      }).join('');
    }
  }
  // The room select is user-owned while a device is being edited: rebuild its
  // options only when the room SET changed, and keep the chosen value.
  const sel = $('sh-dev-room');
  if (!sel) return;
  const sig = rooms.map(function (r) { return (r.id || '') + '' + (r.name || ''); }).join('');
  if (sig === shRoomSig) return;
  shRoomSig = sig;
  const keep = sel.value;
  // The «Без комнаты» option is static markup (DICT-translated) — keep it.
  while (sel.options.length > 1) sel.remove(1);
  rooms.forEach(function (r) {
    if (!r || !r.id) return;
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = r.name || r.id;
    sel.appendChild(opt);
  });
  sel.value = shRoomCache[keep] ? keep : '';
}

function shRenderDevices(devices, rooms) {
  const list = $('sh-device-list');
  if (!list) return;
  shDevCache = {};
  devices.forEach(function (dev) {
    if (dev && dev.id) shDevCache[dev.id] = dev;
  });
  // The device being edited disappeared (deleted elsewhere / re-poll) — drop
  // the stale edit mode instead of saving over a ghost id. This is the ONE
  // path on which the poll may reset #sh-rows, and only because the rows
  // describe a device that no longer exists.
  if (shEditId && !shDevCache[shEditId]) shCancelEdit();
  if (!devices.length) {
    list.innerHTML = '<p class="field-hint">' + escHtml(uiT('Устройства ещё не добавлены')) + '</p>';
    return;
  }
  list.innerHTML = devices.map(function (dev) {
    const room = shRoomCache[dev.room_id];
    const meta = shDeviceTypeLabel(dev.type) + shReadingCount(dev) +
      (room ? ' · ' + (room.name || room.id) : '');
    const hidden = shVisibleInAlice(dev) ? '' :
      ' <span class="badge badge-unk">' + escHtml(uiT('скрыто из Алисы')) + '</span>';
    return '<div class="sh-dev-row" data-id="' + escHtml(dev.id || '') + '">' +
      '<svg class="sh-icon" aria-hidden="true"><use href="#i-' + escHtml(shDeviceIcon(dev)) + '"></use></svg>' +
      '<span class="mono text-sm">' + escHtml(dev.name || dev.id) + '</span> ' +
      '<span class="text-sm text-sec">' + escHtml(meta) + '</span>' + hidden +
      '<span class="sh-dev-actions">' +
      '<button type="button" class="btn btn-sm" data-act="edit">' + escHtml(uiT('Изменить')) + '</button> ' +
      '<button type="button" class="btn btn-sm btn-danger" data-act="del">' + escHtml(uiT('Удалить')) + '</button>' +
      '</span></div>';
  }).join('');
}

// Last poll payload, kept so a language switch can re-render the counts and
// rows in the new language without waiting for the next tick.
let _shLastData = null;

// Called by alice.js on every status poll (and once on registration with the
// last payload) — the only data path into this file.
function shOnData(d) {
  if (!d || !$('sh-card')) return;
  _shLastData = d;
  const devices = (d.devices && d.devices.devices) || [];
  const rooms = (d.devices && d.devices.rooms) || [];
  shRenderRooms(rooms);
  shRenderDevices(devices, rooms);
  const counts = shCountsText(rooms, devices);
  const card = $('sh-counts');
  if (card) card.textContent = counts;
  const modal = $('sh-modal-counts');
  if (modal) modal.textContent = counts;
}

// ── Actions ────────────────────────────────────────────────────────────────
// Fills the cached picker list ONLY — existing rows are never rebuilt, so a
// refresh cannot reset a topic the operator already chose.
async function shLoadTopics() {
  try {
    const d = await shTopics();
    shTopicList = (d && d.topics) || [];
  } catch (e) {
    /* ignore — an empty list still lets a bound topic show through */
  }
}

async function shAddRoom() {
  const input = $('sh-room-name');
  const name = (input && input.value || '').trim();
  if (!name) {
    shSetBindMsg(uiT('Укажите название комнаты'), false);
    return;
  }
  try {
    const d = await shApi({ action: 'upsert_room', room: { name: name } });
    if (!d.ok) {
      shSetBindMsg(d.message || d.error || uiT('Ошибка'), false);
    } else {
      if (input) input.value = '';
      shSetBindMsg(uiT('Комната добавлена'), true);
    }
    await shRefresh();
  } catch (e) {
    shSetBindMsg(uiT('Ошибка запроса API Алисы'), false);
  }
}

async function shDeleteRoom(id) {
  const room = shRoomCache[id];
  const label = (room && room.name) || id;
  if (!window.confirm(uiT('Удалить комнату') + ' «' + label + '»?')) return;
  try {
    const d = await shApi({ action: 'delete_room', id: id });
    if (!d.ok) {
      shSetBindMsg(d.message || d.error || uiT('Ошибка'), false);
    } else {
      shSetBindMsg(uiT('Комната удалена'), true);
    }
    await shRefresh();
  } catch (e) {
    shSetBindMsg(uiT('Ошибка запроса API Алисы'), false);
  }
}

function shRoomListClick(e) {
  const btn = e.target && e.target.closest ? e.target.closest('button[data-act="room-del"]') : null;
  if (!btn) return;
  const row = btn.closest('.sh-room-row');
  const id = row && row.getAttribute('data-id');
  if (id) shDeleteRoom(id);
}

async function shAddDevice() {
  const name = ($('sh-dev-name') && $('sh-dev-name').value || '').trim();
  if (!name) {
    shSetBindMsg(uiT('Укажите имя и MQTT-топик'), false);
    return;
  }
  const collected = shCollectRows();
  if (collected.error) {
    shSetBindMsg(collected.error, false);
    return;
  }
  const dtype = shCurrentDtype();
  const caps = [];
  const props = [];
  const editing = shEditId && shDevCache[shEditId];
  let device;
  if (editing) {
    // Edit: keep id and every item the form cannot express, in stored order,
    // then re-emit one item per row.
    device = JSON.parse(JSON.stringify(shDevCache[shEditId]));
    (device.capabilities || []).forEach(function (it) {
      if (!shIsManagedItem(it)) caps.push(it);
    });
    (device.properties || []).forEach(function (it) {
      if (!shIsManagedItem(it)) props.push(it);
    });
  } else {
    device = { capabilities: [], properties: [] };
  }
  device.name = name;
  device.type = dtype;
  const roomSel = $('sh-dev-room');
  device.room_id = (roomSel && roomSel.value) || '';
  const exportEl = $('sh-dev-export');
  device.alice_visible = exportEl ? !!exportEl.checked : true;
  // The icon belongs to on/off tiles only; an empty value drops the key.
  const iconSel = $('sh-dev-icon');
  device.icon = shIsOnOffType(dtype) && iconSel ? iconSel.value : '';
  collected.rows.forEach(function (row) {
    const item = shRowItem(row);
    if (item.type.indexOf('devices.capabilities.') === 0) caps.push(item);
    else props.push(item);
  });
  device.capabilities = caps;
  device.properties = props;
  try {
    const d = await shApi({ action: 'upsert_device', device: device });
    if (!d.ok) {
      shSetBindMsg(d.message || d.error || uiT('Ошибка'), false);
    } else {
      shSetBindMsg(uiT('Устройство сохранено'), true);
      shCancelEdit();
    }
    await shRefresh();
  } catch (e) {
    shSetBindMsg(uiT('Ошибка запроса API Алисы'), false);
  }
}

// ── Edit / delete on the device rows (delegated from #sh-device-list) ───────
function shListClick(e) {
  const btn = e.target && e.target.closest ? e.target.closest('button[data-act]') : null;
  if (!btn) return;
  const row = btn.closest('.sh-dev-row');
  const id = row && row.getAttribute('data-id');
  if (!id) return;
  if (btn.getAttribute('data-act') === 'edit') shBeginEdit(id);
  else if (btn.getAttribute('data-act') === 'del') shDeleteDevice(id);
}

function shBeginEdit(id) {
  const dev = shDevCache[id];
  if (!dev) return;
  shEditId = id;
  if ($('sh-dev-name')) $('sh-dev-name').value = dev.name || '';
  // An existing device's type/icon are the operator's, never re-derived.
  shDtypeTouched = true;
  shIconTouched = true;
  shSetDtype(dev.type || 'devices.types.sensor');
  shSetIcon(shDeviceIcon(dev));
  shSyncTypeUi();
  const roomSel = $('sh-dev-room');
  if (roomSel) roomSel.value = shRoomCache[dev.room_id] ? dev.room_id : '';
  const exportEl = $('sh-dev-export');
  if (exportEl) exportEl.checked = shVisibleInAlice(dev);
  shClearRows();
  const rows = shDetectRows(dev);
  if (!rows.length) shAddRow(SH_DEFAULT_KIND, '', null);
  else rows.forEach(function (r) { shAddRow(r.kind, r.topic, r.rawItem); });
  // The stored flag lives on the device's single on/off binding.
  const invEl = $('sh-dev-inverted');
  if (invEl) invEl.checked = rows.some(function (r) { return r.inverted; });
  shSyncInvertedField();
  const save = $('sh-dev-save');
  if (save) save.textContent = uiT('Сохранить');
  const cancel = $('sh-dev-cancel');
  if (cancel) cancel.hidden = false;
  shSetBindMsg('', true);
}

function shCancelEdit() {
  shEditId = null;
  if ($('sh-dev-name')) $('sh-dev-name').value = '';
  const roomSel = $('sh-dev-room');
  if (roomSel) roomSel.value = '';
  const exportEl = $('sh-dev-export');
  if (exportEl) exportEl.checked = true;
  const invEl = $('sh-dev-inverted');
  if (invEl) invEl.checked = false;
  shDtypeTouched = false;
  shIconTouched = false;
  shClearRows();
  shSeedDefaultRow();
  const save = $('sh-dev-save');
  if (save) save.textContent = uiT('Добавить');
  const cancel = $('sh-dev-cancel');
  if (cancel) cancel.hidden = true;
}

async function shDeleteDevice(id) {
  const dev = shDevCache[id];
  const label = (dev && dev.name) || id;
  if (!window.confirm(uiT('Удалить устройство') + ' «' + label + '»?')) return;
  try {
    const d = await shApi({ action: 'delete_device', id: id });
    if (!d.ok) {
      shSetBindMsg(d.message || d.error || uiT('Ошибка'), false);
    } else {
      shSetBindMsg(uiT('Устройство удалено'), true);
      if (shEditId === id) shCancelEdit();
    }
    await shRefresh();
  } catch (e) {
    shSetBindMsg(uiT('Ошибка запроса API Алисы'), false);
  }
}

// ── Modal («Комнаты и устройства») ──────────────────────────────────────────
// Reuses the shared mqtt-modal markup/behaviour, not a new one.
function shModalEsc(e) {
  if (e.key === 'Escape') shCloseModal();
}

async function shOpenModal() {
  const m = $('sh-modal');
  if (!m) return;
  shSetBindMsg('', true);
  m.removeAttribute('hidden');
  document.addEventListener('keydown', shModalEsc);
  shRefresh();
  // Topics FIRST: the seeded row's topic picker would otherwise open empty.
  await shLoadTopics();
  const host = shRowsHost();
  if (host && !host.querySelector('.sh-bind-row')) shSeedDefaultRow();
}

function shCloseModal() {
  const m = $('sh-modal');
  if (m) m.setAttribute('hidden', '');
  document.removeEventListener('keydown', shModalEsc);
  shCancelEdit();
}

// Close only on a click on the overlay backdrop itself, not its dialog contents.
function shModalBackdrop(e) {
  if (e.target && e.target.id === 'sh-modal') shCloseModal();
}

function shInit() {
  if (!$('sh-card')) return;
  const list = $('sh-device-list');
  if (list) list.addEventListener('click', shListClick);
  const roomList = $('sh-room-list');
  if (roomList) roomList.addEventListener('click', shRoomListClick);
  // Delegated from the containers, which are static markup — a rebuilt row
  // keeps working without re-binding anything.
  const rows = shRowsHost();
  if (rows) {
    rows.addEventListener('click', shRowsClick);
    rows.addEventListener('change', shRowsChange);
  }
  const dtype = $('sh-dev-type');
  if (dtype) dtype.addEventListener('change', shDtypeChanged);
  const icon = $('sh-dev-icon');
  if (icon) icon.addEventListener('change', function () { shIconTouched = true; shRenderPreview(); });
  const roomInput = $('sh-room-name');
  if (roomInput) {
    roomInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); shAddRoom(); }
    });
  }
  shRefreshI18n();
  if (typeof window.sa02mAliceOnData === 'function') window.sa02mAliceOnData(shOnData);
}

// Language switch (called by i18n.js updateControl, and once at init). The
// type picker's <optgroup> labels are attributes the DICT walker never visits
// — and a data-i18n on an optgroup would let applyDataI18n() textContent-wipe
// its options — so they are translated here from their RU source
// (data-label). The counts/rows are rebuilt from the last payload so the
// card reads in the new language at once, not on the next 5 s poll.
function shRefreshI18n() {
  const sel = $('sh-dev-type');
  if (sel) {
    sel.querySelectorAll('optgroup[data-label]').forEach(function (og) {
      og.label = uiT(og.dataset.label);
    });
  }
  if (_shLastData) shOnData(_shLastData);
}

// Only functions invoked from HTML onclick handlers need a global handle.
window.refreshSmartHomeI18n = shRefreshI18n;
window.shOpenModal = shOpenModal;
window.shCloseModal = shCloseModal;
window.shModalBackdrop = shModalBackdrop;
window.shAddRow = shAddRow;
window.shAddDevice = shAddDevice;
window.shCancelEdit = shCancelEdit;
window.shAddRoom = shAddRoom;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', shInit);
} else {
  shInit();
}

})();
