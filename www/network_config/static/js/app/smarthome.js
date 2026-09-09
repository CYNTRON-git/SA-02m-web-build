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

// The STRUCTURED inventory (1.0.6.38): the picker groups by COM port →
// module → DI/DO/AI/AO, which a flat topic list cannot express. The CGI still
// answers the flat shape without the parameter, so an older cached page keeps
// working against a new board.
// Bounded (audit C7): the CGI's own budget is 15 s under nginx's 20 s, and
// shOpenModal awaits this before seeding the first binding row — an unbounded
// hang left the dialog with no row and no error. One request in flight at a
// time: a poll landing while the modal opens must not fan out into a second
// CGI fork on the shared ARM target.
const SH_TOPICS_TIMEOUT_MS = 8000;
let _shTopicsInflight = null;
function shTopics() {
  if (_shTopicsInflight) return _shTopicsInflight;
  _shTopicsInflight = fetchWithTimeout('cgi-bin/sa02m_alice_topics.cgi?format=inventory', {
    method: 'GET',
    credentials: 'same-origin',
  }, SH_TOPICS_TIMEOUT_MS)
    .then(function (r) { return r.json(); })
    .finally(function () { _shTopicsInflight = null; });
  return _shTopicsInflight;
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
// Official Yandex Smart Home type enum (picker + labels). One home with
// opt/sa02m-alice/sa02m_alice/config/device_types.py — the unit test
// pins the key sets equal. Source:
// https://yandex.ru/dev/dialogs/smart-home/doc/ru/concepts/device-types
// (2026-09-04). RU string (translated by uiT / DICT), falling back to the
// last dotted segment so a new type never renders its raw key.
const SH_DEV_TYPES = {
  'devices.types.sensor': 'Датчик',
  'devices.types.sensor.button': 'Умная кнопка',
  'devices.types.sensor.climate': 'Климат-датчик',
  'devices.types.sensor.gas': 'Датчик газа',
  'devices.types.sensor.illumination': 'Датчик освещённости',
  'devices.types.sensor.motion': 'Датчик движения',
  'devices.types.sensor.open': 'Датчик открытия двери',
  'devices.types.sensor.smoke': 'Датчик дыма',
  'devices.types.sensor.vibration': 'Датчик вибрации',
  'devices.types.sensor.water_leak': 'Датчик протечки воды',
  'devices.types.smart_meter': 'Счётчик',
  'devices.types.smart_meter.cold_water': 'Счётчик холодной воды',
  'devices.types.smart_meter.electricity': 'Счётчик электроэнергии',
  'devices.types.smart_meter.gas': 'Счётчик газа',
  'devices.types.smart_meter.heat': 'Счётчик тепла',
  'devices.types.smart_meter.hot_water': 'Счётчик горячей воды',
  'devices.types.camera': 'Видеокамера',
  'devices.types.media_device': 'Медиаустройство',
  'devices.types.media_device.receiver': 'Ресивер',
  'devices.types.media_device.tv': 'Телевизор',
  'devices.types.media_device.tv_box': 'ТВ-приставка',
  'devices.types.cooking': 'Кухонная техника',
  'devices.types.cooking.coffee_maker': 'Кофеварка',
  'devices.types.cooking.kettle': 'Чайник',
  'devices.types.cooking.multicooker': 'Мультиварка',
  'devices.types.dishwasher': 'Посудомоечная машина',
  'devices.types.iron': 'Утюг',
  'devices.types.vacuum_cleaner': 'Робот-пылесос',
  'devices.types.washing_machine': 'Стиральная машина',
  'devices.types.pet_drinking_fountain': 'Поилка',
  'devices.types.pet_feeder': 'Кормушка',
  'devices.types.humidifier': 'Увлажнитель воздуха',
  'devices.types.purifier': 'Очиститель воздуха',
  'devices.types.thermostat': 'Термостат',
  'devices.types.thermostat.ac': 'Кондиционер',
  'devices.types.ventilation': 'Вентустановка',
  'devices.types.ventilation.fan': 'Вентилятор',
  'devices.types.light': 'Освещение',
  'devices.types.light.ceiling': 'Люстра',
  'devices.types.light.dimmable': 'Диммер',
  'devices.types.light.garland': 'Гирлянда',
  'devices.types.light.lamp': 'Настольная лампа',
  'devices.types.light.sconce': 'Бра',
  'devices.types.light.strip': 'Диодная лента',
  'devices.types.light.torchere': 'Торшер',
  'devices.types.socket': 'Розетка',
  'devices.types.switch': 'Выключатель',
  'devices.types.switch.relay': 'Реле',
  'devices.types.openable': 'Открываемое',
  'devices.types.openable.curtain': 'Шторы',
  'devices.types.openable.valve': 'Шаровой кран',
  'devices.types.openable.door_lock': 'Замок',
  'devices.types.other': 'Другое',
};

// Official category groups for #sh-dev-type (same order as device_types.py).
const SH_DEV_TYPE_GROUPS = [
  {label: 'Датчики', types: [
    'devices.types.sensor', 'devices.types.sensor.button',
    'devices.types.sensor.climate', 'devices.types.sensor.gas',
    'devices.types.sensor.illumination', 'devices.types.sensor.motion',
    'devices.types.sensor.open', 'devices.types.sensor.smoke',
    'devices.types.sensor.vibration', 'devices.types.sensor.water_leak',
  ]},
  {label: 'Счётчики', types: [
    'devices.types.smart_meter', 'devices.types.smart_meter.cold_water',
    'devices.types.smart_meter.electricity', 'devices.types.smart_meter.gas',
    'devices.types.smart_meter.heat', 'devices.types.smart_meter.hot_water',
  ]},
  {label: 'Медиаустройства', types: [
    'devices.types.camera', 'devices.types.media_device',
    'devices.types.media_device.receiver', 'devices.types.media_device.tv',
    'devices.types.media_device.tv_box',
  ]},
  {label: 'Кухонная техника', types: [
    'devices.types.cooking', 'devices.types.cooking.coffee_maker',
    'devices.types.cooking.kettle', 'devices.types.cooking.multicooker',
    'devices.types.dishwasher',
  ]},
  {label: 'Бытовая техника', types: [
    'devices.types.iron', 'devices.types.vacuum_cleaner',
    'devices.types.washing_machine',
  ]},
  {label: 'Устройства для животных', types: [
    'devices.types.pet_drinking_fountain', 'devices.types.pet_feeder',
  ]},
  {label: 'Климатическая техника', types: [
    'devices.types.humidifier', 'devices.types.purifier',
    'devices.types.thermostat', 'devices.types.thermostat.ac',
    'devices.types.ventilation', 'devices.types.ventilation.fan',
  ]},
  {label: 'Электрооборудование', types: [
    'devices.types.light', 'devices.types.light.ceiling',
    'devices.types.light.dimmable', 'devices.types.light.garland',
    'devices.types.light.lamp', 'devices.types.light.sconce',
    'devices.types.light.strip', 'devices.types.light.torchere',
    'devices.types.socket', 'devices.types.switch',
    'devices.types.switch.relay',
  ]},
  {label: 'Открытие/закрытие', types: [
    'devices.types.openable', 'devices.types.openable.curtain',
    'devices.types.openable.valve', 'devices.types.openable.door_lock',
  ]},
  {label: 'Остальные устройства', types: ['devices.types.other']},
];

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
  if (SH_ONOFF_TYPES.indexOf(type) !== -1) return true;
  // Light subtypes and the relay switch share the on/off seed + icon field.
  if (String(type).indexOf('devices.types.light.') === 0) return true;
  return type === 'devices.types.switch.relay';
}

function shIconForType(type) {
  if (SH_ICON_BY_TYPE[type]) return SH_ICON_BY_TYPE[type];
  if (String(type).indexOf('devices.types.light') === 0) return 'bulb';
  if (type === 'devices.types.switch.relay') return 'relay';
  return 'generic';
}

function shFillTypeSelect() {
  const sel = $('sh-dev-type');
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = '';
  SH_DEV_TYPE_GROUPS.forEach(function (g) {
    const og = document.createElement('optgroup');
    og.dataset.label = g.label;
    og.label = uiT(g.label);
    g.types.forEach(function (type) {
      const opt = document.createElement('option');
      opt.value = type;
      opt.textContent = uiT(SH_DEV_TYPES[type] || type);
      if (type === 'devices.types.sensor') opt.selected = true;
      og.appendChild(opt);
    });
    sel.appendChild(og);
  });
  if (keep) shSetDtype(keep);
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

// Picker inventory, cached once per modal open so a refresh never rebuilds an
// existing row or resets the value chosen in it.
let shTopicList = [];
// The inventory's devices, and `topic → {devTitle, devShort, chLabel}` for the
// row buttons. `shInvOk` false means the board could not answer: the picker
// then falls back to hand entry and keeps whatever is already bound
// (fail-closed — a picker outage may not silently blank a binding).
let shInvDevices = [];
let shInvOk = false;
let shTopicMeta = {};
// True once the operator picks a device type by hand — the first row's kind
// stops auto-filling it, so an edited device is never silently retyped.
let shDtypeTouched = false;
// True once the operator picks an icon by hand — a type change stops
// re-suggesting one.
let shIconTouched = false;

function shRowsHost() { return $('sh-rows'); }

function shRowKindOptions(selected) {
  return Object.keys(SH_KINDS).map(function (k) {
    return '<option value="' + escAttr(k) + '"' + (k === selected ? ' selected' : '') + '>' +
      escHtml(SH_KIND_LABELS[k] || k) + '</option>';
  }).join('');
}

// ── Binding target: the button and its label ────────────────────────────────
// The topic lives in a hidden input, so shCollectRows()/validation read the
// row exactly as they did when it was a <select>. Until 1.0.6.38 the row WAS
// a select of raw paths (`/devices/mr02m-COM3-10/controls/do_3`): every option
// shared the 25-character prefix, nothing said which module or channel that
// was, and a module whose yaml carried no `channels` block offered one option
// for its fourteen channels.

// A bound topic missing from the inventory keeps its own label: editing the
// name alone must never silently retarget — or lose — the binding.
function shBindLabel(topic) {
  if (!topic) return uiT('Выбрать канал…');
  const meta = shTopicMeta[topic];
  if (!meta) return topic + ' · ' + uiT('неизвестный топик');
  const parts = [meta.devTitle];
  if (meta.devShort) parts.push(meta.devShort);
  parts.push(meta.chLabel);
  return parts.join(' · ');
}

function shBindButtonHtml(topic) {
  const known = !topic || !!shTopicMeta[topic];
  return '<input type="hidden" class="sh-row-topic" value="' + escAttr(topic || '') + '">' +
    '<button type="button" class="btn btn-sm sh-row-bind' + (topic ? '' : ' is-empty') +
    (known ? '' : ' is-unknown') + '" data-act="pick"' +
    ' aria-label="' + escAttr(uiT('Выбрать канал')) + '"' +
    ' title="' + escAttr(topic || uiT('Выбрать канал')) + '">' +
    escHtml(shBindLabel(topic)) + '</button>';
}

// Refresh a row's button after its hidden input changed (pick / inventory
// arriving after the row was built).
function shSyncBindButton(row) {
  if (!row) return;
  const input = row.querySelector('.sh-row-topic');
  const btn = row.querySelector('.sh-row-bind');
  if (!input || !btn) return;
  const topic = input.value || '';
  btn.textContent = shBindLabel(topic);
  btn.title = topic || uiT('Выбрать канал');
  btn.classList.toggle('is-empty', !topic);
  btn.classList.toggle('is-unknown', !!topic && !shTopicMeta[topic]);
}

function shSyncAllBindButtons() {
  const host = shRowsHost();
  if (!host) return;
  host.querySelectorAll('.sh-bind-row').forEach(shSyncBindButton);
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
    kindHtml = '<option value="' + escAttr(SH_KIND_RAW) + '" selected>' + escHtml(inst) + '</option>';
  } else {
    kindHtml = shRowKindOptions(k);
  }
  row.innerHTML =
    '<select class="sh-row-kind" aria-label="Вид показания"' + (locked ? ' disabled' : '') + '>' +
    kindHtml + '</select>' +
    shBindButtonHtml(topic || '') +
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
// draws them. Outdoor / room stay off the seed: those MQTT controls exist
// with retained 0.0 + meta/error=r when the analogue input is unfitted, and
// binding them paints «0,0 °C улица». Add the kind by hand when the control
// is live and in the Carel point list.
const SH_VENT_ROWS = ['switch', 'setpoint', 'supply_temp', 'return_water',
                      'plant_state', 'unit_status', 'alarm'];

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
  const act = e.target && e.target.closest ? e.target.closest('button[data-act]') : null;
  if (act && act.getAttribute('data-act') === 'pick') {
    shPickOpen(act.closest('.sh-bind-row'));
    return;
  }
  const btn = act && act.getAttribute('data-act') === 'row-del' ? act : null;
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

function shCarelControl(topic) {
  const m = /^\/devices\/carel-[^/]+\/controls\/([^/]+)$/.exec(String(topic || ''));
  return m ? m[1] : '';
}

function shLedControl(topic) {
  const m = /^\/devices\/led-[^/]+\/controls\/([^/]+)$/.exec(String(topic || ''));
  return m ? m[1] : '';
}

const SH_CAREL_KIND = {
  unit_on: 'switch',
  setpoint: 'setpoint',
  supply_temp: 'supply_temp',
  return_water_temp: 'return_water',
  room_temp: 'room_temp',
  outdoor_temp: 'outdoor_temp',
  plant_state: 'plant_state',
  unit_status: 'unit_status',
  unit_status_text: 'unit_status',
  alarm: 'alarm',
};

function shApplyCarelTopic(row, topic) {
  const ctrl = shCarelControl(topic);
  if (!ctrl) return;
  const kind = SH_CAREL_KIND[ctrl];
  const kindSel = row && row.querySelector('.sh-row-kind');
  if (kindSel && kind && SH_KINDS[kind] && !kindSel.disabled) kindSel.value = kind;
  if (!shDtypeTouched) {
    shSetDtype('devices.types.ventilation');
    shSyncTypeUi();
  }
}

function shApplyLedTopic(row, topic) {
  const ctrl = shLedControl(topic);
  if (!ctrl) return;
  const kindSel = row && row.querySelector('.sh-row-kind');
  if (ctrl === 'power' && kindSel && SH_KINDS.switch && !kindSel.disabled) kindSel.value = 'switch';
  if (!shDtypeTouched) {
    shSetDtype('devices.types.light');
    shSyncTypeUi();
  }
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

// ── Channel picker (#sh-pick-modal) ─────────────────────────────────────────
// A dialog with a search field and a one-at-a-time device accordion (the MQTT
// tab's pattern), not a longer <select>: the operator picks a MODULE first and
// then a channel inside it, which is how the hardware is wired and labelled.

// Channel groups, in render order. `other` holds the named controls of the
// non-modular families (a DTV's sensors, a Carel unit's points) — real
// channels with no DI/DO/AI/AO number.
const SH_PICK_GROUPS = [
  { key: 'di', short: 'DI', label: 'Дискретные входы' },
  { key: 'do', short: 'DO', label: 'Дискретные выходы' },
  { key: 'ai', short: 'AI', label: 'Аналоговые входы' },
  { key: 'ao', short: 'AO', label: 'Аналоговые выходы' },
  { key: 'other', short: '', label: 'Показания и команды' },
  { key: 'diag', short: '', label: 'Диагностика' },
];
// Chips filter by group; «Все» is the default and stays the default even when
// a reading kind has a preference. A kind only ever HINTS: the operator
// legitimately binds an AI to «Температура» and a DI to «Движение», and one
// board has both, so a kind may not cut the list (contract §Bindings).
const SH_PICK_CHIPS = [
  { key: '', label: 'Все' },
  { key: 'do', label: 'DO' },
  { key: 'di', label: 'DI' },
  { key: 'ai', label: 'AI' },
  { key: 'ao', label: 'AO' },
];

// Which groups a reading kind is usually bound to — the groups shown first,
// with a «рекомендуется» note. Nothing is hidden.
function shPreferredGroups(kind) {
  const spec = SH_KINDS[kind];
  if (!spec) return [];
  if (spec.kindOf === 'cap') return ['do', 'di'];
  if (spec.kindOf === 'range') return ['ao', 'other'];
  if (spec.kindOf === 'event') return ['di', 'other'];
  return ['ai', 'other'];
}

let shPickRow = null;      // the row being (re)bound
let shPickOpenDev = '';    // the one expanded device id
let shPickChip = '';
let shPickQuery = '';

function shPickDevTitle(dev) {
  const stored = (dev.name && String(dev.name).trim()) || '';
  if (dev.type === 'mr02m') {
    const sig = dev.model_ru || dev.model || '';
    return uiT('МР-02м') + (sig ? ' ' + sig : '');
  }
  if (dev.type === 'dtv') return uiT('ДТВ-RS-485');
  if (dev.type === 'ce02m3') return uiT('СЭ-02м-3');
  if (dev.type === 'carel') return stored || uiT('Вентустановка Carel');
  if (dev.type === 'controller') return uiT('Контроллер SA-02m');
  return stored || dev.id;
}

// «COM3:10» — the compact form for the row button.
function shPickDevShort(dev) {
  if (!dev.port) return '';
  return dev.port + (dev.address != null && dev.address !== '' ? ':' + dev.address : '');
}

// «COM3 · адрес 10» — the card's second line.
function shPickDevSub(dev) {
  const parts = [];
  if (dev.port) parts.push(dev.port);
  if (dev.address != null && dev.address !== '') parts.push(uiT('адрес') + ' ' + dev.address);
  if (dev.type === 'controller') parts.push(uiT('встроенные выходы'));
  return parts.join(' · ');
}

function shPickChannels(dev, group) {
  return ((dev.channels || {})[group]) || [];
}

// «6 DO · 8 DI» — what the module is, before it is expanded.
function shPickDevBadges(dev) {
  return SH_PICK_GROUPS.filter(function (g) { return g.short; }).map(function (g) {
    const n = shPickChannels(dev, g.key).length;
    return n ? n + ' ' + g.short : '';
  }).filter(Boolean);
}

// Every topic already bound, with the device names holding it. The same DO in
// two Alice devices is legitimate (a relay in two scenes), so this marks and
// never blocks.
function shOccupancy() {
  const map = {};
  const add = function (topic, name) {
    if (!topic) return;
    if (!map[topic]) map[topic] = [];
    if (map[topic].indexOf(name) === -1) map[topic].push(name);
  };
  Object.keys(shDevCache).forEach(function (id) {
    const dev = shDevCache[id];
    const name = (dev && dev.name) || id;
    shDetectRows(dev).forEach(function (r) { add(r.topic, name); });
  });
  return map;
}

function shPickChipsHtml() {
  return SH_PICK_CHIPS.map(function (c) {
    return '<button type="button" class="sh-pick-chip' +
      (c.key === shPickChip ? ' is-on' : '') + '" data-act="chip" data-chip="' +
      escAttr(c.key) + '">' + escHtml(uiT(c.label)) + '</button>';
  }).join('');
}

// `do_3` → «DO3». A named control (`alarm_led`, `supply_temp`) and a DI
// sub-counter (`di_2_short`) have no terminal number of their own — their
// title already names the channel they belong to.
function shChTagLabel(tag) {
  const m = /^(di|do|ai|ao)_(\d+)$/.exec(String(tag || ''));
  return m ? m[1].toUpperCase() + m[2] : '';
}

function shPickChannelHtml(dev, ch, occupied, prefer) {
  const busy = occupied[ch.topic];
  const cls = 'sh-pick-ch' + (busy ? ' is-busy' : '') + (prefer ? ' is-pref' : '');
  const subs = (ch.sub || []).length;
  const tag = shChTagLabel(ch.tag);
  const title = String(ch.title || ch.tag || '');
  // The bridge's default title for a channel IS its tag («DO3»), so printing
  // both would read «DO3  DO3»; the title earns its column only when it says
  // something the tag column does not. A named control («power», «unit_on»,
  // a DI counter) has NO tag column — only `<kind>_<n>` gets one — so there
  // the title is the only label the row has, and dropping it left the operator
  // an anonymous row (bench 1.135: the LED driver's whole command group).
  const named = !!title && title !== tag && !(tag && title === ch.tag);
  return '<div class="sh-pick-ch-wrap"><div class="sh-pick-ch-line">' +
    '<button type="button" class="' + cls + '" data-act="ch" data-topic="' +
    escAttr(ch.topic) + '" title="' + escAttr(ch.topic) + '">' +
    (tag ? '<span class="sh-pick-ch-tag">' + escHtml(tag) + '</span>' : '') +
    '<span class="sh-pick-ch-title">' + (named ? escHtml(title) : '') + '</span>' +
    '<span class="sh-pick-ch-rw">' + escHtml(ch.rw === 'rw' ? uiT('чт/зп') : uiT('чтение')) + '</span>' +
    (ch.enabled === false ? '<span class="sh-pick-ch-off">' + escHtml(uiT('отключён в MQTT')) + '</span>' : '') +
    (busy ? '<span class="sh-pick-ch-busy">' + escHtml(uiT('занят') + ': ' + busy.join(', ')) + '</span>' : '') +
    '</button>' +
    (subs ? '<button type="button" class="sh-pick-subs-toggle" data-act="subs"' +
      ' aria-expanded="false">' + escHtml(uiT('счётчики')) + ' (' + subs + ')</button>' : '') +
    '</div>' +
    (subs ? '<div class="sh-pick-subs" hidden>' +
      ch.sub.map(function (s) {
        return shPickChannelHtml(dev, s, occupied, false);
      }).join('') + '</div>' : '') +
    '</div>';
}

// The group order is FIXED (DI → DO → AI → AO → named → diagnostics): it is
// the order of the module's own terminal block, so the operator reads the
// dialog the way the wires are numbered. The reading kind only marks its
// groups «рекомендуется» — it never reorders and never hides.
function shPickGroupsHtml(dev, occupied, prefer) {
  return SH_PICK_GROUPS.map(function (g) {
    if (shPickChip && g.key !== shPickChip) return '';
    const chans = shPickChannels(dev, g.key);
    if (!chans.length) return '';
    const isPref = prefer.indexOf(g.key) !== -1;
    // Diagnostics are bindable but never what the operator came for.
    const collapsed = g.key === 'diag' && !shPickChip;
    return '<div class="sh-pick-group' + (collapsed ? ' is-collapsed' : '') + '">' +
      '<button type="button" class="sh-pick-group-head" data-act="group"' +
      ' aria-expanded="' + (collapsed ? 'false' : 'true') + '">' +
      '<span class="sh-pick-arrow">' + (collapsed ? '▸' : '▾') + '</span>' +
      escHtml(uiT(g.label)) +
      (isPref ? ' <span class="sh-pick-pref">' + escHtml(uiT('рекомендуется')) + '</span>' : '') +
      '</button>' +
      '<div class="sh-pick-group-body"' + (collapsed ? ' hidden' : '') + '>' +
      chans.map(function (ch) {
        return shPickChannelHtml(dev, ch, occupied, isPref);
      }).join('') + '</div></div>';
  }).join('');
}

// The yaml says one module, the module itself answered another (inventory
// `model_source: detected`). The picker follows the module — and says so,
// because the yaml is what the MQTT tab shows.
function shPickMismatchHtml(dev) {
  if (dev.model_source !== 'detected' || !dev.yaml_model || dev.yaml_model === dev.model) return '';
  return '<span class="sh-pick-mismatch" title="' +
    escAttr(uiT('Тип модуля определён по опросу; в YAML указан другой')) + '">' +
    escHtml('YAML: ' + dev.yaml_model + ' · ' + uiT('обнаружено') + ': ' + dev.model) + '</span>';
}

function shPickDevHtml(dev, occupied, prefer) {
  const open = dev.id === shPickOpenDev;
  const badges = shPickDevBadges(dev);
  return '<div class="sh-pick-dev' + (open ? ' is-open' : '') + '" data-id="' + escAttr(dev.id) + '">' +
    '<button type="button" class="sh-pick-dev-head" data-act="dev" aria-expanded="' +
    (open ? 'true' : 'false') + '">' +
    '<span class="sh-pick-arrow">' + (open ? '▾' : '▸') + '</span>' +
    '<span class="sh-pick-dev-title">' + escHtml(shPickDevTitle(dev)) + '</span>' +
    '<span class="sh-pick-dev-sub">' + escHtml(shPickDevSub(dev)) + '</span>' +
    badges.map(function (b) {
      return '<span class="badge sh-pick-badge">' + escHtml(b) + '</span>';
    }).join('') + shPickMismatchHtml(dev) + '</button>' +
    '<div class="sh-pick-dev-body"' + (open ? '' : ' hidden') + '>' +
    (open ? shPickGroupsHtml(dev, occupied, prefer) : '') + '</div></div>';
}

// Search matches the words on the card AND on the channel — module name,
// model, COM, address, channel tag, channel title (as in Home Assistant, a
// query flattens the tree into results).
function shPickMatches(dev, ch, query) {
  const hay = [
    dev.id, dev.name, dev.model, dev.model_ru, dev.port,
    dev.address == null ? '' : String(dev.address),
    shPickDevTitle(dev), ch.tag, ch.title,
  ].join(' ').toLowerCase();
  return query.split(/\s+/).every(function (word) {
    return !word || hay.indexOf(word) !== -1;
  });
}

function shPickSearchHtml(occupied, prefer) {
  const query = shPickQuery.toLowerCase();
  const out = [];
  shInvDevices.forEach(function (dev) {
    SH_PICK_GROUPS.forEach(function (g) {
      if (shPickChip && g.key !== shPickChip) return;
      shPickChannels(dev, g.key).forEach(function (ch) {
        const flat = [ch].concat(ch.sub || []);
        flat.forEach(function (item) {
          if (!shPickMatches(dev, item, query)) return;
          const busy = occupied[item.topic];
          out.push('<button type="button" class="sh-pick-hit' + (busy ? ' is-busy' : '') +
            (prefer.indexOf(g.key) !== -1 ? ' is-pref' : '') +
            '" data-act="ch" data-topic="' + escAttr(item.topic) + '" title="' +
            escAttr(item.topic) + '">' +
            '<span class="sh-pick-hit-dev">' + escHtml(shPickDevTitle(dev)) +
            (shPickDevShort(dev) ? ' · ' + escHtml(shPickDevShort(dev)) : '') + '</span>' +
            '<span class="sh-pick-hit-ch">' + escHtml(item.title || item.tag) + '</span>' +
            (busy ? '<span class="sh-pick-ch-busy">' + escHtml(uiT('занят') + ': ' + busy.join(', ')) +
              '</span>' : '') + '</button>');
        });
      });
    });
  });
  if (!out.length) {
    return '<p class="field-hint">' + escHtml(uiT('Ничего не найдено')) + '</p>';
  }
  return out.join('');
}

function shPickRender() {
  const list = $('sh-pick-list');
  const chips = $('sh-pick-chips');
  if (chips) chips.innerHTML = shPickChipsHtml();
  if (!list) return;
  const manual = $('sh-pick-manual');
  if (!shInvOk) {
    // Fail-closed: the board could not answer, so the picker offers hand
    // entry and the bound topic stays exactly as it is.
    list.innerHTML = '<p class="field-hint">' +
      escHtml(uiT('Список каналов недоступен — введите топик вручную')) + '</p>';
    if (manual) manual.hidden = false;
    return;
  }
  if (manual) manual.hidden = true;
  if (!shInvDevices.length) {
    list.innerHTML = '<p class="field-hint">' +
      escHtml(uiT('Устройства не настроены — добавьте модуль во вкладке «Каналы MQTT»')) + '</p>';
    return;
  }
  const occupied = shOccupancy();
  const prefer = shPreferredGroups(shPickKind());
  list.innerHTML = shPickQuery.trim()
    ? shPickSearchHtml(occupied, prefer)
    : shInvDevices.map(function (dev) {
        return shPickDevHtml(dev, occupied, prefer);
      }).join('');
}

function shPickKind() {
  const sel = shPickRow && shPickRow.querySelector('.sh-row-kind');
  return (sel && sel.value) || '';
}

function shPickCurrentTopic() {
  const input = shPickRow && shPickRow.querySelector('.sh-row-topic');
  return (input && input.value) || '';
}

function shPickOpen(row) {
  if (!row) return;
  const m = $('sh-pick-modal');
  if (!m) return;
  shPickRow = row;
  shPickQuery = '';
  shPickChip = '';
  const current = shPickCurrentTopic();
  const meta = shTopicMeta[current];
  // Open on the device the row is already bound to — the common case is
  // "same module, other channel".
  shPickOpenDev = meta ? meta.deviceId : (shInvDevices.length === 1 ? shInvDevices[0].id : '');
  const search = $('sh-pick-search');
  if (search) search.value = '';
  const manualIn = $('sh-pick-manual-in');
  if (manualIn) manualIn.value = current;
  m.removeAttribute('hidden');
  shPickRender();
  if (search) search.focus();
}

function shPickClose() {
  const m = $('sh-pick-modal');
  if (m) m.setAttribute('hidden', '');
  shPickRow = null;
}

function shPickIsOpen() {
  const m = $('sh-pick-modal');
  return !!m && !m.hasAttribute('hidden');
}

function shPickBackdrop(e) {
  if (e.target && e.target.id === 'sh-pick-modal') shPickClose();
}

// Writing the topic is the ONLY thing a pick changes: instance, unit, scale
// and every hand-added key of a stored item travel through untouched
// (shRowItem's round-trip guarantee).
function shApplyPickedTopic(row, topic) {
  const input = row && row.querySelector('.sh-row-topic');
  if (!input) return;
  input.value = topic;
  row._shTouched = true;
  shSyncBindButton(row);
  shApplyCarelTopic(row, topic);
  shApplyLedTopic(row, topic);
  shSyncInvertedField();
}

function shPickApply(topic) {
  if (!topic || !shPickRow) return;
  shApplyPickedTopic(shPickRow, topic);
  shPickClose();
}

function shPickManualApply() {
  const input = $('sh-pick-manual-in');
  const topic = (input && input.value || '').trim();
  if (!topic) return;
  shPickApply(topic);
}

function shPickListClick(e) {
  const btn = e.target && e.target.closest ? e.target.closest('button[data-act]') : null;
  if (!btn) return;
  const act = btn.getAttribute('data-act');
  if (act === 'ch') {
    shPickApply(btn.getAttribute('data-topic') || '');
    return;
  }
  if (act === 'dev') {
    const card = btn.closest('.sh-pick-dev');
    const id = card && card.getAttribute('data-id');
    // One device at a time: on a phone that is "tapped it → only its
    // channels", and the way back is one tap, not a second screen.
    shPickOpenDev = (id === shPickOpenDev) ? '' : (id || '');
    shPickRender();
    return;
  }
  if (act === 'group' || act === 'subs') {
    const host = act === 'group'
      ? btn.closest('.sh-pick-group')
      : btn.closest('.sh-pick-ch-wrap');
    const body = host && host.querySelector(
      act === 'group' ? ':scope > .sh-pick-group-body' : ':scope > .sh-pick-subs');
    if (!body) return;
    const show = body.hidden;
    body.hidden = !show;
    btn.setAttribute('aria-expanded', show ? 'true' : 'false');
    if (act === 'group') {
      const arrow = btn.querySelector('.sh-pick-arrow');
      if (arrow) arrow.textContent = show ? '▾' : '▸';
      host.classList.toggle('is-collapsed', !show);
    }
  }
}

function shPickChipsClick(e) {
  const btn = e.target && e.target.closest ? e.target.closest('button[data-act="chip"]') : null;
  if (!btn) return;
  shPickChip = btn.getAttribute('data-chip') || '';
  shPickRender();
}

function shPickSearchInput() {
  const input = $('sh-pick-search');
  shPickQuery = (input && input.value) || '';
  shPickRender();
}

function shPickSearchKey(e) {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const first = document.querySelector('#sh-pick-list button[data-act="ch"]');
  if (first) shPickApply(first.getAttribute('data-topic') || '');
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
  if (tiled && !shIconTouched) shSetIcon(shIconForType(type));
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
  return shIconForType(dev && dev.type);
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

function shCountsText(rooms, devices, sceneDevices) {
  let inAlice = 0;
  devices.forEach(function (d) { if (shVisibleInAlice(d)) inAlice++; });
  let text = uiT('Комнат') + ': ' + rooms.length + ' · ' + uiT('Устройств') + ': ' + devices.length +
    ' · ' + uiT('в Алисе') + ': ' + inAlice;
  // Only when there is at least one: the counts line must stay on one line
  // at the card's width, and a «: 0» tells the operator nothing.
  const scenes = (sceneDevices || []).length;
  if (scenes) text += ' · ' + uiT('Сценариев в Алисе') + ': ' + scenes;
  return text;
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
        return '<div class="sh-room-row" data-id="' + escAttr(r.id || '') + '">' +
          '<span class="sh-room-name text-sm">' + escHtml(r.name || r.id) + '</span>' +
          '<button type="button" class="btn btn-sm btn-danger" data-act="room-del" aria-label="' +
          escAttr(uiT('Удалить комнату')) + '" title="' + escAttr(uiT('Удалить комнату')) + '">✕</button>' +
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

// Read-only row for a scene the cloud editor marked «в Алису» (1.0.6.41).
// NOT entered into shDevCache and carrying no data-id: the edit/delete
// handlers resolve a row by that attribute, and a scene is owned by the
// cloud scenario editor, not by this card.
function shSceneRowHtml(scene) {
  const room = shRoomCache[scene.room_id];
  const meta = uiT('сценарий') + (room ? ' · ' + (room.name || room.id) : '');
  return '<div class="sh-dev-row sh-dev-scene">' +
    // Same icon the board's own type map gives `devices.types.switch` —
    // which is exactly what the scene is published to Alice as.
    '<svg class="sh-icon" aria-hidden="true"><use href="#i-' +
    escAttr(shIconForType('devices.types.switch')) + '"></use></svg>' +
    '<span class="mono text-sm">' + escHtml(scene.name || scene.scene_id || '') + '</span> ' +
    '<span class="text-sm text-sec">' + escHtml(meta) + '</span>' +
    ' <span class="badge badge-unk">' + escHtml(uiT('сценарий')) + '</span>' +
    '</div>';
}

function shRenderDevices(devices, rooms, sceneDevices) {
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
  const scenes = sceneDevices || [];
  if (!devices.length && !scenes.length) {
    list.innerHTML = '<p class="field-hint">' + escHtml(uiT('Устройства ещё не добавлены')) + '</p>';
    return;
  }
  const sceneHtml = scenes.map(shSceneRowHtml).join('');
  list.innerHTML = devices.map(function (dev) {
    const room = shRoomCache[dev.room_id];
    const meta = shDeviceTypeLabel(dev.type) + shReadingCount(dev) +
      (room ? ' · ' + (room.name || room.id) : '');
    const hidden = shVisibleInAlice(dev) ? '' :
      ' <span class="badge badge-unk">' + escHtml(uiT('скрыто из Алисы')) + '</span>';
    return '<div class="sh-dev-row" data-id="' + escAttr(dev.id || '') + '">' +
      '<svg class="sh-icon" aria-hidden="true"><use href="#i-' + escAttr(shDeviceIcon(dev)) + '"></use></svg>' +
      '<span class="mono text-sm">' + escHtml(dev.name || dev.id) + '</span> ' +
      '<span class="text-sm text-sec">' + escHtml(meta) + '</span>' + hidden +
      '<span class="sh-dev-actions">' +
      '<button type="button" class="btn btn-sm" data-act="edit">' + escHtml(uiT('Изменить')) + '</button> ' +
      '<button type="button" class="btn btn-sm btn-danger" data-act="del">' + escHtml(uiT('Удалить')) + '</button>' +
      '</span></div>';
  }).join('') + sceneHtml;
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
  // Absent on an older CGI (or on a board with no scenario engine) — the
  // rows and the count then simply do not appear, never a throw.
  const sceneDevices = d.scene_devices || [];
  shRenderRooms(rooms);
  shRenderDevices(devices, rooms, sceneDevices);
  const counts = shCountsText(rooms, devices, sceneDevices);
  const card = $('sh-counts');
  if (card) card.textContent = counts;
  const modal = $('sh-modal-counts');
  if (modal) modal.textContent = counts;
}

// ── Actions ────────────────────────────────────────────────────────────────
// Fills the cached picker list ONLY — existing rows are never rebuilt, so a
// refresh cannot reset a topic the operator already chose.
function shBuildTopicMeta(devices) {
  const meta = {};
  devices.forEach(function (dev) {
    const devTitle = shPickDevTitle(dev);
    const devShort = shPickDevShort(dev);
    SH_PICK_GROUPS.forEach(function (g) {
      shPickChannels(dev, g.key).forEach(function (ch) {
        [ch].concat(ch.sub || []).forEach(function (item) {
          meta[item.topic] = {
            deviceId: dev.id,
            devTitle: devTitle,
            devShort: devShort,
            chLabel: item.title || item.tag,
            group: g.key,
          };
        });
      });
    });
  });
  return meta;
}

async function shLoadTopics() {
  try {
    const d = await shTopics();
    if (d && d.ok && Array.isArray(d.devices)) {
      shInvDevices = d.devices;
      shInvOk = true;
      shTopicMeta = shBuildTopicMeta(shInvDevices);
      shTopicList = Object.keys(shTopicMeta).sort();
    } else if (d && Array.isArray(d.topics)) {
      // A flat answer — a board whose CGI predates `?format=inventory`. There
      // is no structure to group by, so the picker offers hand entry with the
      // known topics as suggestions rather than an empty tree.
      shInvDevices = [];
      shInvOk = false;
      shTopicMeta = {};
      shTopicList = d.topics;
    } else {
      shInvOk = false;
      shInvDevices = [];
    }
  } catch (e) {
    // Fail-closed: whatever is bound stays bound and stays visible.
    shInvOk = false;
    shInvDevices = [];
  }
  shSyncManualSuggestions();
  shSyncAllBindButtons();
}

// Hand-entry suggestions (fail-closed mode only) — a datalist, so the field
// stays free text.
function shSyncManualSuggestions() {
  const list = $('sh-pick-manual-list');
  if (!list) return;
  list.innerHTML = shTopicList.map(function (t) {
    return '<option value="' + escAttr(t) + '"></option>';
  }).join('');
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

// Alice `_NAME_RE`: 1–64 letters/digits/spaces/-./+ (JS \w is ASCII, so \p{L}).
var SH_NAME_RE = /^[\p{L}\p{N}_ \-./+]{1,64}$/u;
var shTitleEditing = false;

function shTitleRow() { return document.querySelector('#sh-modal .sh-title-row'); }
function shPenEl() { return $('sh-title-pen'); }
function shNinEl() { return $('sh-title-in'); }

function shSyncPen() {
  var p = shPenEl();
  if (!p) return;
  var k = shTitleEditing ? 'Сохранить название' : 'Переименовать';
  p.setAttribute('aria-label', uiT(k));
  p.title = uiT(k);
}

function shSetModalTitle(name, editing) {
  var t = $('sh-modal-title');
  var pen = shPenEl();
  if (!t) return;
  if (editing && name) {
    t.removeAttribute('data-i18n');
    t.textContent = name;
    if (pen) pen.hidden = false;
  } else {
    t.setAttribute('data-i18n', 'Комнаты и устройства');
    t.textContent = uiT('Комнаты и устройства');
    if (pen) pen.hidden = true;
    shCancelTitleEdit();
  }
  shSyncPen();
}

function shStartTitleEdit() {
  var nin = shNinEl(), row = shTitleRow(), t = $('sh-modal-title');
  if (!shEditId || !nin || !row || !t) return;
  shTitleEditing = true;
  nin.hidden = false;
  nin.value = t.textContent || '';
  nin.removeAttribute('aria-invalid');
  nin.setAttribute('tabindex', '0');
  row.setAttribute('data-edit', '');
  shSyncPen();
  nin.focus();
  nin.select();
}

function shCancelTitleEdit() {
  var nin = shNinEl(), row = shTitleRow();
  shTitleEditing = false;
  if (row) row.removeAttribute('data-edit');
  if (nin) {
    nin.hidden = true;
    nin.removeAttribute('aria-invalid');
    nin.setAttribute('tabindex', '-1');
  }
  shSyncPen();
}

async function shCommitTitleEdit() {
  var nin = shNinEl();
  if (!shEditId || !nin || !shTitleEditing) return;
  var name = (nin.value || '').replace(/^\s+|\s+$/g, '');
  if (!name || !SH_NAME_RE.test(name)) {
    nin.setAttribute('aria-invalid', 'true');
    shSetBindMsg(uiT('Недопустимое название'), false);
    nin.focus();
    return;
  }
  try {
    var d = await shApi({ action: 'rename_device', id: shEditId, name: name });
    if (!d.ok) {
      shSetBindMsg(d.message || d.error || uiT('Недопустимое название'), false);
      return;
    }
    if ($('sh-dev-name')) $('sh-dev-name').value = name;
    if (shDevCache[shEditId]) shDevCache[shEditId].name = name;
    shCancelTitleEdit();
    shSetModalTitle(name, true);
    shSetBindMsg('', true);
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
  shSetModalTitle(dev.name || '', true);
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
  shSetModalTitle('', false);
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
  if (e.key !== 'Escape') return;
  // Innermost layer first: the picker sits on top of #sh-modal, so Esc there
  // closes only the picker (checked here rather than in a second document
  // listener — listener order would decide it otherwise).
  if (shPickIsOpen()) { e.preventDefault(); e.stopPropagation(); shPickClose(); return; }
  if (shTitleEditing) { e.preventDefault(); e.stopPropagation(); shCancelTitleEdit(); return; }
  shCloseModal();
}

async function shOpenModal() {
  const m = $('sh-modal');
  if (!m) return;
  shSetBindMsg('', true);
  m.removeAttribute('hidden');
  document.addEventListener('keydown', shModalEsc);
  shRefresh();
  // Topics FIRST: the seeded row's topic picker would otherwise open empty.
  // The seed is in `finally` so a timed-out or throwing inventory still leaves
  // the operator a row (the picker then offers hand entry — fail-closed).
  try {
    await shLoadTopics();
  } finally {
    const host = shRowsHost();
    if (host && !host.querySelector('.sh-bind-row')) shSeedDefaultRow();
  }
}

function shCloseModal() {
  const m = $('sh-modal');
  if (m) m.setAttribute('hidden', '');
  shPickClose();
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
  var pen = shPenEl();
  if (pen) pen.addEventListener('click', function (ev) {
    ev.preventDefault(); ev.stopPropagation();
    if (shTitleEditing) shCommitTitleEdit(); else shStartTitleEdit();
  });
  var nin = shNinEl();
  if (nin) nin.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); shCommitTitleEdit(); }
    else if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); shCancelTitleEdit(); }
  });
  const roomList = $('sh-room-list');
  if (roomList) roomList.addEventListener('click', shRoomListClick);
  // Delegated from the containers, which are static markup — a rebuilt row
  // keeps working without re-binding anything.
  const rows = shRowsHost();
  if (rows) {
    rows.addEventListener('click', shRowsClick);
    rows.addEventListener('change', shRowsChange);
  }
  const pickList = $('sh-pick-list');
  if (pickList) pickList.addEventListener('click', shPickListClick);
  const pickChips = $('sh-pick-chips');
  if (pickChips) pickChips.addEventListener('click', shPickChipsClick);
  const pickSearch = $('sh-pick-search');
  if (pickSearch) {
    pickSearch.addEventListener('input', shPickSearchInput);
    pickSearch.addEventListener('keydown', shPickSearchKey);
  }
  const dtype = $('sh-dev-type');
  if (dtype) {
    shFillTypeSelect();
    dtype.addEventListener('change', shDtypeChanged);
  }
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
    sel.querySelectorAll('option').forEach(function (opt) {
      const ru = SH_DEV_TYPES[opt.value];
      if (ru) opt.textContent = uiT(ru);
    });
  }
  // The picker's device titles are composed (uiT at build time), so they are
  // rebuilt here rather than walked by the DICT observer.
  if (shInvOk) shTopicMeta = shBuildTopicMeta(shInvDevices);
  shSyncAllBindButtons();
  if (shPickIsOpen()) shPickRender();
  if (_shLastData) shOnData(_shLastData);
  if (shEditId && shDevCache[shEditId]) shSetModalTitle(shDevCache[shEditId].name || '', true);
  else shSetModalTitle('', false);
}

// Only functions invoked from HTML onclick handlers need a global handle.
window.refreshSmartHomeI18n = shRefreshI18n;
window.shOpenModal = shOpenModal;
window.shCloseModal = shCloseModal;
window.shModalBackdrop = shModalBackdrop;
window.shPickClose = shPickClose;
window.shPickBackdrop = shPickBackdrop;
window.shPickManualApply = shPickManualApply;
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
