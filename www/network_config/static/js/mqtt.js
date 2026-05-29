/* SA-02m MQTT tab — v1.0 */

(function() {
'use strict';

// ── Module types map ──────────────────────────────────────────────────────────
// Must match MR02M_MODULE_TYPES in modbus_mqtt_bridge.py exactly
const MR02M_TYPES = {
  1:  {name:'DO6DI8',    do:6,  di:8,  ao:0,  ai:0},
  2:  {name:'DO16',      do:16, di:0,  ao:0,  ai:0},
  3:  {name:'AO12',      do:0,  di:0,  ao:12, ai:0},
  4:  {name:'DO6',       do:6,  di:0,  ao:0,  ai:0},
  5:  {name:'DI14',      do:0,  di:14, ao:0,  ai:0},
  6:  {name:'AO6AI6',    do:0,  di:0,  ao:6,  ai:6},
  7:  {name:'AI12',      do:0,  di:0,  ao:0,  ai:12},
  8:  {name:'DO4DI6',    do:4,  di:6,  ao:0,  ai:0},
  9:  {name:'TENZO2',    do:0,  di:0,  ao:0,  ai:0},
  10: {name:'10DIcon',   do:0,  di:10, ao:0,  ai:0},
  11: {name:'6DO5DI2AO', do:6,  di:5,  ao:2,  ai:0},
  12: {name:'AI6AO2',    do:0,  di:0,  ao:2,  ai:6},
  15: {name:'4TO6DI',    do:4,  di:6,  ao:4,  ai:0},
};

/** Русские подписи типов МР-02м для таблицы и аккордеона (порядок каналов: DO/DI/AO/AI). */
const MR02M_TYPE_LABELS_RU = {
  1:  '6DO 8DI',
  2:  '16DO',
  3:  '12AO',
  4:  '6DO',
  5:  '14DI',
  6:  '6AI 6AO',
  7:  '12AI',
  8:  '4DO 6DI',
  9:  'Тензо 2',
  10: '10 DI',
  11: '6DO 5DI 2AO',
  12: '6AI 2AO',
  15: '4TO 6DI',
};

function mr02mTypeLabelRu(mtCode) {
  const c = Number(mtCode);
  if (MR02M_TYPE_LABELS_RU[c]) return MR02M_TYPE_LABELS_RU[c];
  const mt = MR02M_TYPES[c];
  return mt ? mt.name : String(mtCode);
}

function formatDeviceDisplayName(dev) {
  const comName = (dev.port || '').replace('/dev/', '');
  const addr = dev.address != null ? dev.address : (dev.addr != null ? dev.addr : '—');
  if (dev.type === 'mr02m') {
    const typePart = mr02mTypeLabelRu(getModuleTypeCode(dev));
    return `МР-02м ${typePart} (${comName} addr=${addr})`;
  }
  if (dev.type === 'dtv') {
    const base = (dev.name && String(dev.name).trim()) ? String(dev.name).trim() : 'ДТВ-RS-485';
    return `${base} (${comName} addr=${addr})`;
  }
  if (dev.type === 'ce02m3') {
    const base = (dev.name && String(dev.name).trim()) ? String(dev.name).trim() : 'СЭ-02м-3';
    return `${base} (${comName} addr=${addr})`;
  }
  return `${dev.name || dev.id} (${comName} addr=${addr})`;
}

function defaultScanDeviceName(scanDev, type, port) {
  const comName = (port || '').replace('/dev/', '');
  const addr = scanDev.addr;
  if (type === 'mr02m') {
    return `МР-02м ${mr02mTypeLabelRu(scanDev.module_type || 1)} (${comName} addr=${addr})`;
  }
  if (type === 'dtv') return `ДТВ-RS-485 (${comName} addr=${addr})`;
  if (type === 'ce02m3') return `СЭ-02м-3 (${comName} addr=${addr})`;
  return `Устройство (${comName} addr=${addr})`;
}

// ai_sensor_t codes 0..38 — must match MODBUS_VARIABLES.txt and modbus_mqtt_bridge.py
const AI_SENSOR_LABELS = [
  {code:0,  label:'0 — Отключён'},
  {code:1,  label:'1 — NTC 10k B3950'},
  {code:2,  label:'2 — Pt1000 (α0.00385)'},
  {code:3,  label:'3 — Pt100 (α0.00385)'},
  {code:4,  label:'4 — Напряжение 0–10 В'},
  {code:5,  label:'5 — Ток 4–20 мА'},
  {code:6,  label:'6 — Термопара K (ТХА)'},
  {code:7,  label:'7 — Сухой контакт'},
  {code:8,  label:'8 — Pt50 (α0.00385)'},
  {code:9,  label:'9 — Pt500 (α0.00385)'},
  {code:10, label:'10 — NTC 100k B3950'},
  {code:11, label:'11 — NTC 10k B3988'},
  {code:12, label:'12 — NTC 10k B3435'},
  {code:13, label:'13 — NTC 10k B3470'},
  {code:14, label:'14 — Pt100 100П (α0.00391)'},
  {code:15, label:'15 — Pt1000 1000П (α0.00391)'},
  {code:16, label:'16 — Pt100 100М (α0.00428)'},
  {code:17, label:'17 — Pt1000 1000М (α0.00428)'},
  {code:18, label:'18 — Ni100 (α0.00617)'},
  {code:19, label:'19 — Ni500 (α0.00617)'},
  {code:20, label:'20 — Ni1000 (α0.00617)'},
  {code:21, label:'21 — Ток 0–5 мА'},
  {code:22, label:'22 — Ток 0–20 мА'},
  {code:23, label:'23 — Дифф. ±50 мВ'},
  {code:24, label:'24 — Дифф. ±2 В'},
  {code:25, label:'25 — NTC 5k B3470'},
  {code:26, label:'26 — NTC 1.8k B3380'},
  {code:27, label:'27 — Pt50 3-wire'},
  {code:28, label:'28 — Pt100 3-wire'},
  {code:29, label:'29 — Pt500 3-wire'},
  {code:30, label:'30 — Pt1000 3-wire'},
  {code:31, label:'31 — Pt100 100П 3-wire'},
  {code:32, label:'32 — Pt1000 1000П 3-wire'},
  {code:33, label:'33 — Pt100 100М 3-wire'},
  {code:34, label:'34 — Pt1000 1000М 3-wire'},
  {code:35, label:'35 — Ni100 3-wire'},
  {code:36, label:'36 — Ni500 3-wire'},
  {code:37, label:'37 — Ni1000 3-wire'},
  {code:38, label:'38 — Напряжение 0–30 В'},
];

const DTV_SENSORS = [
  {key:'temp_ds18b20',  label:'Темп. DS18B20',    group:'temperature'},
  {key:'temp_mcp9808',  label:'Темп. MCP9808',    group:'temperature'},
  {key:'temp_hdc1080',  label:'Темп. HDC1080',    group:'temperature'},
  {key:'temp_bme280',   label:'Темп. BME280',     group:'temperature'},
  {key:'temp_bme680',   label:'Темп. BME680',     group:'temperature'},
  {key:'temp_ext',      label:'Темп. внешний',    group:'temperature'},
  {key:'humidity_hdc1080',label:'Влажность HDC1080',group:'humidity'},
  {key:'humidity_bme280', label:'Влажность BME280',  group:'humidity'},
  {key:'humidity_bme680', label:'Влажность BME680',  group:'humidity'},
  {key:'pressure_bme280_kpa',label:'Давление BME280',group:'pressure'},
  {key:'pressure_bme680_kpa',label:'Давление BME680',group:'pressure'},
  {key:'iaq_bme680',    label:'IAQ BME680',       group:'iaq'},
  {key:'eco2_bme680',   label:'eCO2 BME680',      group:'iaq'},
  {key:'gas_resist_bme680',label:'Газ. сопр. BME680',group:'iaq'},
  {key:'tvoc_zmod',     label:'TVOC ZMOD4410',    group:'iaq'},
  {key:'iaq_zmod',      label:'IAQ ZMOD',         group:'iaq'},
  {key:'eco2_zmod',     label:'eCO2 ZMOD',        group:'iaq'},
  {key:'presence',      label:'Присутствие',      group:'presence'},
  {key:'moving_distance',label:'Дистанция движения',group:'presence'},
  {key:'light_pct',     label:'Освещённость',     group:'presence'},
  {key:'buzzer',        label:'Зуммер',           group:'outputs'},
  {key:'leds',          label:'Светодиоды',       group:'outputs'},
];

const CE02M3_CHANNELS = [
  {key:'voltage_a',   label:'Ua', group:'voltages'},
  {key:'voltage_b',   label:'Ub', group:'voltages'},
  {key:'voltage_c',   label:'Uc', group:'voltages'},
  {key:'voltage_ab',  label:'Uab',group:'voltages'},
  {key:'voltage_bc',  label:'Ubc',group:'voltages'},
  {key:'voltage_ca',  label:'Uca',group:'voltages'},
  {key:'current_a',   label:'Ia', group:'currents'},
  {key:'current_b',   label:'Ib', group:'currents'},
  {key:'current_c',   label:'Ic', group:'currents'},
  {key:'current_n',   label:'In', group:'currents'},
  {key:'power_a',     label:'Pa', group:'power'},
  {key:'power_b',     label:'Pb', group:'power'},
  {key:'power_c',     label:'Pc', group:'power'},
  {key:'power_total', label:'P сумм.', group:'power'},
  {key:'reactive_a',  label:'Qa', group:'reactive'},
  {key:'reactive_b',  label:'Qb', group:'reactive'},
  {key:'reactive_c',  label:'Qc', group:'reactive'},
  {key:'reactive_total',label:'Q сумм.', group:'reactive'},
  {key:'pf_a',        label:'cosφ A', group:'pf'},
  {key:'pf_b',        label:'cosφ B', group:'pf'},
  {key:'pf_c',        label:'cosφ C', group:'pf'},
  {key:'pf_total',    label:'cosφ сумм.', group:'pf'},
  {key:'frequency',   label:'Частота', group:'pf'},
  {key:'energy_active_import',  label:'Энергия акт. импорт', group:'energy'},
  {key:'energy_active_export',  label:'Энергия акт. экспорт',group:'energy'},
  {key:'energy_reactive_import',label:'Энергия реакт. импорт',group:'energy'},
  {key:'energy_apparent',       label:'Полная энергия',      group:'energy'},
];

// ── State ─────────────────────────────────────────────────────────────────────
let _config = {mqtt:{broker:'127.0.0.1',port:1883,qos:1,retain:true}, devices:[]};
let _monitorEs = null;
let _monitorPaused = false;
let _unsaved = false;

// ── Helpers ───────────────────────────────────────────────────────────────────
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return el;
}

function showToast(msg, type = 'ok') {
  const t = document.getElementById('mqtt-toast');
  if (!t) return;
  t.textContent = msg;
  t.className = `mqtt-toast mqtt-toast-${type} mqtt-toast-show`;
  clearTimeout(t._timeout);
  t._timeout = setTimeout(() => t.classList.remove('mqtt-toast-show'), 3500);
}

function makeDeviceId(type, port, addr) {
  const comName = port.replace('/dev/', '');
  const prefix = type === 'dtv' ? 'dtv' : type === 'ce02m3' ? 'ce02m3' : 'mr02m';
  return `${prefix}-${comName}-${addr}`;
}

/** Если module_type не сохранён в YAML — угадать по legacy-имени (AO6AI6, DO4DI6, …). */
function inferModuleTypeFromName(name) {
  const n = String(name || '').toUpperCase().replace(/[\s_-]/g, '');
  const tokens = [
    ['AO6AI6', 6], ['6AO6AI', 6],
    ['AI6AO2', 12], ['6AI2AO', 12],
    ['DO6DI8', 1], ['6DO8DI', 1],
    ['DO4DI6', 8], ['4DO6DI', 8],
    ['6DO5DI2AO', 11],
    ['4TO6DI', 15], ['TO4DI6', 15],
    ['10DICON', 10],
    ['DO16', 2], ['AO12', 3], ['AI12', 7], ['DI14', 5], ['DO6', 4],
    ['TENZO2', 9],
  ];
  for (const [tok, code] of tokens) {
    if (n.includes(tok)) return code;
  }
  return null;
}

function getModuleTypeCode(dev) {
  const mt = dev.module_type ?? dev._module_type_code;
  if (mt != null && mt !== '') return Number(mt);
  const inferred = inferModuleTypeFromName(dev.name);
  if (inferred != null) return inferred;
  return 1;
}

function topicPath(deviceId, chName) {
  return `/devices/${deviceId}/controls/${chName}`;
}

// ── API calls ─────────────────────────────────────────────────────────────────
async function apiGet(url) {
  const r = await fetch(url, {credentials:'include'});
  return r.json();
}

async function apiPost(url, data) {
  const r = await fetch(url, {
    method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json'},
    body: typeof data === 'string' ? data : JSON.stringify(data),
  });
  return r.json();
}

// ── Broker status ─────────────────────────────────────────────────────────────
async function refreshBrokerStatus() {
  const st = await apiGet('/cgi-bin/mqtt_status.cgi').catch(() => null);
  if (!st) return;

  const badge = document.getElementById('mqtt-broker-badge');
  const clients = document.getElementById('mqtt-broker-clients');
  const bridgeBadge = document.getElementById('mqtt-bridge-badge');

  if (badge) {
    badge.className = `badge ${st.mosquitto_active ? 'badge-ok' : 'badge-err'}`;
    badge.textContent = st.mosquitto_active ? '● Работает' : '● Остановлен';
  }
  if (clients) clients.textContent = `Клиентов: ${st.clients_connected}`;
  if (bridgeBadge) {
    bridgeBadge.className = `badge ${st.bridge_active ? 'badge-ok' : 'badge-unk'}`;
    bridgeBadge.textContent = st.bridge_active ? '● Мост активен' : '● Мост остановлен';
  }
}

// ── Load config ───────────────────────────────────────────────────────────────
async function loadConfig() {
  const data = await apiGet('/cgi-bin/mqtt_config.cgi').catch(() => null);
  if (data && !data.error) {
    _config = data;
    renderDeviceList();
    renderAccordion();
  }
}

// ── Device list table ─────────────────────────────────────────────────────────
function renderDeviceList() {
  const tbody = document.getElementById('mqtt-device-tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  for (const dev of _config.devices || []) {
    const chEnabled = countChannelsEnabled(dev);
    const chTotal = countChannelsTotal(dev);
    const comName = (dev.port || '').replace('/dev/', '');
    const tr = h('tr', {},
      h('td', {}, deviceTypeBadge(dev.type)),
      h('td', {}, formatDeviceDisplayName(dev)),
      h('td', {'class':'mono'}, comName),
      h('td', {'class':'mono'}, String(dev.address || '—')),
      h('td', {}, `${chEnabled}/${chTotal}`),
      h('td', {},
        h('button', {'class':'btn btn-sm', 'onclick': () => removeDevice(dev.id)}, '✕ Удалить')
      )
    );
    tbody.appendChild(tr);
  }
}

function deviceTypeBadge(type) {
  const labels = {mr02m:'МР-02м', dtv:'ДТВ-RS-485', ce02m3:'СЭ-02м-3'};
  return h('span', {'class':'badge badge-info'}, labels[type] || type);
}

function countChannelsEnabled(dev) {
  if (dev.type === 'mr02m') {
    let n = 0;
    for (const kind of ['do','di','ao','ai']) {
      for (const ch of (dev.channels?.[kind] || [])) {
        if (ch.enabled !== false) n++;
      }
    }
    return n;
  }
  if (dev.type === 'dtv') return (dev.sensors_present || []).length;
  if (dev.type === 'ce02m3') {
    const en = dev.channels_enabled || {};
    let n = 0;
    if (en.voltages !== false) n += 6;
    if (en.currents !== false) n += 4;
    if (en.power_active !== false) n += 4;
    if (en.power_reactive !== false) n += 4;
    if (en.power_factor !== false) n += 4;
    if (en.frequency !== false) n += 1;
    if (en.energy !== false) n += 4;
    return n;
  }
  return 0;
}

function countChannelsTotal(dev) {
  if (dev.type === 'mr02m') {
    for (const [code, mt] of Object.entries(MR02M_TYPES)) {
      if (String(code) === String(getModuleTypeCode(dev))) {
        return mt.do + mt.di + mt.ao + mt.ai;
      }
    }
  }
  if (dev.type === 'dtv') return DTV_SENSORS.length;
  if (dev.type === 'ce02m3') return CE02M3_CHANNELS.length;
  return 0;
}

// ── Accordion ─────────────────────────────────────────────────────────────────
function renderAccordion() {
  const container = document.getElementById('mqtt-accordion');
  if (!container) return;
  container.innerHTML = '';
  for (const dev of _config.devices || []) {
    container.appendChild(buildDeviceSection(dev));
  }
}

function buildDeviceSection(dev) {
  const title = formatDeviceDisplayName(dev);

  const body = h('div', {'class':'mqtt-accordion-body', 'id': `acc-body-${dev.id}`, 'hidden': ''});

  if (dev.type === 'mr02m') buildMR02mChannels(dev, body);
  else if (dev.type === 'dtv') buildDTVChannels(dev, body);
  else if (dev.type === 'ce02m3') buildCE02M3Channels(dev, body);

  const header = h('div', {'class':'mqtt-accordion-header', 'onclick': () => toggleAccordion(dev.id)},
    h('span', {'class':'mqtt-accordion-arrow', 'id': `acc-arrow-${dev.id}`}, '▶'),
    h('span', {}, title)
  );

  const section = h('div', {'class':'mqtt-accordion-section'}, header, body);
  return section;
}

function toggleAccordion(id) {
  const body = document.getElementById(`acc-body-${id}`);
  const arrow = document.getElementById(`acc-arrow-${id}`);
  if (!body) return;
  const open = body.hasAttribute('hidden');
  body.toggleAttribute('hidden', !open);
  if (arrow) arrow.textContent = open ? '▼' : '▶';
}

function buildMR02mChannels(dev, container) {
  const channels = dev.channels || {};
  const mtCode = getModuleTypeCode(dev);
  const mt = MR02M_TYPES[mtCode] || {do:6,di:8,ao:0,ai:0};

  // DO channels
  if (mt.do > 0) {
    const grp = buildChannelGroup('DO — Дискретные выходы');
    for (let i = 1; i <= mt.do; i++) {
      const chCfg = getOrCreateChannel(channels, 'do', i);
      const topicHint = topicPath(dev.id, `do_${i}`);
      grp.appendChild(buildChannelRow(chCfg, topicHint, 'do', i, dev));
    }
    container.appendChild(grp);
  }
  // DI channels
  if (mt.di > 0) {
    const grp = buildChannelGroup('DI — Дискретные входы');
    for (let i = 1; i <= mt.di; i++) {
      const chCfg = getOrCreateChannel(channels, 'di', i);
      const topicHint = topicPath(dev.id, `di_${i}`);
      const row = buildChannelRow(chCfg, topicHint, 'di', i, dev);
      // Counter toggle
      const counterLabel = h('label', {'class':'mqtt-counter-label'},
        h('input', {'type':'checkbox', checked: chCfg.counter ? '' : undefined,
          'onchange': e => { chCfg.counter = e.target.checked; markUnsaved(); }}),
        ' счётчик'
      );
      row.appendChild(counterLabel);
      grp.appendChild(row);
    }
    container.appendChild(grp);
  }
  // AO channels
  if (mt.ao > 0) {
    const grp = buildChannelGroup('AO — Аналоговые выходы (0–1000 ‰)');
    for (let i = 1; i <= mt.ao; i++) {
      const chCfg = getOrCreateChannel(channels, 'ao', i);
      grp.appendChild(buildChannelRow(chCfg, topicPath(dev.id, `ao_${i}`), 'ao', i, dev));
    }
    container.appendChild(grp);
  }
  // AI channels
  if (mt.ai > 0) {
    const grp = buildChannelGroup('AI — Аналоговые входы');
    for (let i = 1; i <= mt.ai; i++) {
      const chCfg = getOrCreateChannel(channels, 'ai', i);
      const row = buildChannelRow(chCfg, topicPath(dev.id, `ai_${i}`), 'ai', i, dev);
      // Sensor type dropdown
      const sel = h('select', {'class':'mqtt-select-small',
        'onchange': e => { chCfg.sensor_type = Number(e.target.value); markUnsaved(); }});
      for (const s of AI_SENSOR_LABELS) {
        const opt = h('option', {'value': String(s.code)}, s.label);
        if (s.code === (chCfg.sensor_type ?? 2)) opt.selected = true;
        sel.appendChild(opt);
      }
      row.appendChild(sel);
      grp.appendChild(row);
    }
    container.appendChild(grp);
  }
}

function buildDTVChannels(dev, container) {
  if (!dev.sensors_present) dev.sensors_present = DTV_SENSORS.map(s => s.key);
  const groups = {};
  for (const s of DTV_SENSORS) {
    if (!groups[s.group]) {
      const label = {temperature:'Температура',humidity:'Влажность / Давление',
        pressure:'Давление', iaq:'Качество воздуха',presence:'Присутствие LD2412',
        outputs:'Выходы'}[s.group] || s.group;
      groups[s.group] = buildChannelGroup(label);
      container.appendChild(groups[s.group]);
    }
    const enabled = dev.sensors_present.includes(s.key);
    const topic = topicPath(dev.id, s.key);
    const row = h('div', {'class':'mqtt-ch-row'},
      h('label', {'class':'mqtt-ch-toggle'},
        h('input', {'type':'checkbox', checked: enabled ? '' : undefined,
          'onchange': e => {
            if (e.target.checked) {
              if (!dev.sensors_present.includes(s.key)) dev.sensors_present.push(s.key);
            } else {
              dev.sensors_present = dev.sensors_present.filter(k => k !== s.key);
            }
            markUnsaved();
          }}),
        h('span', {'class':'mqtt-ch-name'}, s.label)
      ),
      h('span', {'class':'topic-preview', 'title': topic}, topic)
    );
    groups[s.group].appendChild(row);
  }
  // Polling intervals
  const intervals = h('div', {'class':'mqtt-form-row'},
    h('label', {}, 'Опрос датчиков:'),
    h('input', {'type':'number','class':'mqtt-input-small','value': dev.poll_sensors_s || 1,
      'oninput': e => { dev.poll_sensors_s = Number(e.target.value); markUnsaved(); }}),
    h('span', {}, 'с'),
    h('label', {'style':'margin-left:12px'}, 'Присутствие:'),
    h('input', {'type':'number','class':'mqtt-input-small','value': dev.poll_presence_s || 1,
      'oninput': e => { dev.poll_presence_s = Number(e.target.value); markUnsaved(); }}),
    h('span', {}, 'с')
  );
  container.insertBefore(intervals, container.firstChild);
}

function buildCE02M3Channels(dev, container) {
  if (!dev.channels_enabled) dev.channels_enabled = {};
  const en = dev.channels_enabled;

  // CT ratio
  const ctRow = h('div', {'class':'mqtt-ch-group'},
    h('div', {'class':'mqtt-ch-group-title'}, 'Параметры CT'),
    h('div', {'class':'mqtt-form-row'},
      h('label', {}, 'Коэффициент CT (K×1000):'),
      h('input', {'type':'number','class':'mqtt-input-small','value': dev.ct_ratio || 4000,
        'oninput': e => { dev.ct_ratio = Number(e.target.value); markUnsaved(); }}),
      h('span', {}, '(4000 = CT 4А, 1000 = 1А)')
    )
  );
  container.appendChild(ctRow);

  const groupMeta = {
    voltages: 'Напряжения (В)',
    currents: 'Токи (А)',
    power: 'Активная мощность (Вт)',
    reactive: 'Реактивная мощность (вар)',
    pf: 'Cos φ / Частота',
    energy: 'Счётчики энергии',
  };

  // Poll intervals row
  const intervals = h('div', {'class':'mqtt-form-row'},
    h('label', {}, 'Опрос мощности:'),
    h('input', {'type':'number','class':'mqtt-input-small','value': dev.poll_power_s || 1,
      'oninput': e => { dev.poll_power_s = Number(e.target.value); markUnsaved(); }}),
    h('span', {}, 'с'),
    h('label', {'style':'margin-left:12px'}, 'Счётчики:'),
    h('input', {'type':'number','class':'mqtt-input-small','value': dev.poll_energy_s || 60,
      'oninput': e => { dev.poll_energy_s = Number(e.target.value); markUnsaved(); }}),
    h('span', {}, 'с')
  );
  container.appendChild(intervals);

  const groups = {};
  for (const [gk, gl] of Object.entries(groupMeta)) {
    groups[gk] = buildChannelGroup(gl);
    container.appendChild(groups[gk]);
  }

  for (const ch of CE02M3_CHANNELS) {
    const gk = ch.group;
    const topic = topicPath(dev.id, ch.key);
    // Map channel to channels_enabled key
    const enKey = gk === 'voltages' ? 'voltages'
      : gk === 'currents' ? 'currents'
      : gk === 'power' ? 'power_active'
      : gk === 'reactive' ? 'power_reactive'
      : gk === 'pf' && ch.key === 'frequency' ? 'frequency'
      : gk === 'pf' ? 'power_factor'
      : gk === 'energy' ? 'energy' : null;

    const enabled = enKey ? (en[enKey] !== false) : true;
    const row = h('div', {'class':'mqtt-ch-row'},
      h('label', {'class':'mqtt-ch-toggle'},
        h('input', {'type':'checkbox', checked: enabled ? '' : undefined,
          'onchange': e => {
            if (enKey) { en[enKey] = e.target.checked; markUnsaved(); }
          }}),
        h('span', {'class':'mqtt-ch-name'}, ch.label)
      ),
      h('span', {'class':'topic-preview', 'title': topic}, topic)
    );
    groups[gk].appendChild(row);
  }
}

function buildChannelGroup(title) {
  return h('div', {'class':'mqtt-ch-group'},
    h('div', {'class':'mqtt-ch-group-title'}, title)
  );
}

function buildChannelRow(chCfg, topicHint, kind, idx, dev) {
  const row = h('div', {'class':'mqtt-ch-row'},
    h('label', {'class':'mqtt-ch-toggle'},
      h('input', {'type':'checkbox', checked: chCfg.enabled !== false ? '' : undefined,
        'onchange': e => { chCfg.enabled = e.target.checked; markUnsaved(); }}),
    ),
    h('input', {'type':'text','class':'mqtt-ch-label-input',
      'placeholder': `${kind.toUpperCase()}${idx}`,
      'value': chCfg.label || '',
      'oninput': e => { chCfg.label = e.target.value; markUnsaved(); }}),
    h('span', {'class':'topic-preview', 'title': topicHint}, topicHint)
  );
  return row;
}

function getOrCreateChannel(channels, kind, idx) {
  if (!channels[kind]) channels[kind] = [];
  let ch = channels[kind].find(c => c.ch === idx);
  if (!ch) {
    ch = {ch: idx, label: '', enabled: true};
    channels[kind].push(ch);
  }
  return ch;
}

// ── Scan modal ────────────────────────────────────────────────────────────────
function showScanModal() {
  const m = document.getElementById('mqtt-scan-modal');
  if (!m) return;
  m.removeAttribute('hidden');
  document.getElementById('mqtt-scan-results').innerHTML = '';
  document.getElementById('mqtt-scan-status').textContent = '';
  const btn = document.getElementById('mqtt-scan-btn');
  if (btn) { btn.disabled = false; btn.textContent = 'Сканировать'; }
}

function hideScanModal() {
  const m = document.getElementById('mqtt-scan-modal');
  if (m) m.setAttribute('hidden', '');
}

async function runScan() {
  const port  = document.getElementById('mqtt-scan-port').value;
  const baud  = Number(document.getElementById('mqtt-scan-baud').value);
  const range = Number(document.getElementById('mqtt-scan-range').value);
  const btn   = document.getElementById('mqtt-scan-btn');
  const statusEl  = document.getElementById('mqtt-scan-status');
  const resultsEl = document.getElementById('mqtt-scan-results');

  btn.disabled = true;
  btn.textContent = 'Сканирование…';
  statusEl.innerHTML = '<span class="mqtt-scan-spinner"></span>Поиск устройств на ' + port + ' (' + baud + ' бод)…';
  resultsEl.innerHTML = '';

  const data = await apiPost('/cgi-bin/mqtt_scan.cgi', {port, baudrate: baud, max_addr: range})
    .catch(e => ({ok: false, error: String(e), devices: []}));

  btn.disabled = false;
  btn.textContent = 'Сканировать';

  if (!data.ok) {
    statusEl.textContent = 'Ошибка: ' + (data.error || 'неизвестная');
    return;
  }

  const devices = data.devices || [];
  if (devices.length === 0) {
    statusEl.textContent = 'Устройства не найдены. Проверьте порт, скорость и подключение.';
    return;
  }

  statusEl.textContent = 'Найдено: ' + devices.length + ' устройств(а). Выберите тип и добавьте нужные.';
  renderScanResults(port, baud, devices);
}

function renderScanResults(port, baud, devices) {
  const el = document.getElementById('mqtt-scan-results');
  el.innerHTML = '';

  const table = h('table', {'class': 'mqtt-device-table'});
  table.appendChild(h('thead', {}, h('tr', {},
    h('th', {}, 'Адрес'), h('th', {}, 'Тип'), h('th', {}, 'Имя'), h('th', {})
  )));
  const tbody = h('tbody');

  for (const dev of devices) {
    const typeSelect = h('select', {'class': 'mqtt-select-small'});
    for (const [val, lbl] of [['mr02m','МР-02м'], ['dtv','ДТВ-RS-485'], ['ce02m3','СЭ-02м-3']]) {
      const opt = h('option', {value: val}, lbl);
      if (val === dev.type) opt.selected = true;
      typeSelect.appendChild(opt);
    }

    const nameInput = h('input', {
      'type': 'text', 'class': 'mqtt-ch-label-input',
      'placeholder': 'Имя устройства',
      'value': dev.name || defaultScanDeviceName(dev, dev.type || 'mr02m', port),
      'style': 'width:130px'
    });

    const addBtn = h('button', {'class': 'btn btn-primary btn-sm'}, '+ Добавить');
    addBtn.onclick = () => {
      addDeviceFromScan(dev, typeSelect.value, nameInput.value, port, baud);
      addBtn.disabled = true;
      addBtn.textContent = '✓';
    };

    tbody.appendChild(h('tr', {},
      h('td', {'class': 'mono'}, String(dev.addr)),
      h('td', {}, typeSelect),
      h('td', {}, nameInput),
      h('td', {}, addBtn)
    ));
  }

  table.appendChild(tbody);

  const addAllBtn = h('button', {'class': 'btn btn-sm', 'style': 'margin-top:8px'},
    '+ Добавить все (' + devices.length + ')');
  addAllBtn.onclick = () => {
    tbody.querySelectorAll('tr').forEach((tr, i) => {
      const d = devices[i];
      const sel = tr.querySelector('select');
      const inp = tr.querySelector('input');
      const btn = tr.querySelector('button');
      if (!btn.disabled) {
        addDeviceFromScan(d, sel.value, inp.value, port, baud);
        btn.disabled = true;
        btn.textContent = '✓';
      }
    });
  };

  el.appendChild(table);
  el.appendChild(addAllBtn);
}

function addDeviceFromScan(scanDev, type, name, port, baud) {
  const addr = scanDev.addr;
  const id   = makeDeviceId(type, port, addr);

  if (_config.devices.find(d => d.id === id)) {
    showToast('Устройство ' + id + ' уже добавлено', 'warn');
    return;
  }

  const dev = {id, type, port, baudrate: baud || 115200, address: addr, name: name || id};
  if (type === 'mr02m') {
    dev.module_type = scanDev.module_type || 1;
    dev.poll_s = 1; dev.poll_do_di_s = 1; dev.poll_ai_ao_s = 1; dev.poll_diag_s = 60;
    dev.channels = {};
  } else if (type === 'dtv') {
    dev.poll_sensors_s = 1; dev.poll_presence_s = 1; dev.poll_diag_s = 60;
    dev.sensors_present = DTV_SENSORS.map(s => s.key);
  } else if (type === 'ce02m3') {
    dev.poll_power_s = 1; dev.poll_energy_s = 60; dev.poll_diag_s = 120;
    dev.ct_ratio = 4000; dev.phases = ['A','B','C']; dev.channels_enabled = {};
  }

  _config.devices.push(dev);
  markUnsaved();
  renderDeviceList();
  renderAccordion();
  showToast((name || id) + ' добавлено');
}

// ── Add/Remove device ─────────────────────────────────────────────────────────
function removeDevice(id) {
  if (!confirm(`Удалить устройство ${id}?`)) return;
  _config.devices = _config.devices.filter(d => d.id !== id);
  markUnsaved();
  renderDeviceList();
  renderAccordion();
}

function showAddModal() {
  document.getElementById('mqtt-add-modal').removeAttribute('hidden');
  document.getElementById('mqtt-add-type').value = 'mr02m';
  document.getElementById('mqtt-add-port').value = '/dev/COM1';
  document.getElementById('mqtt-add-addr').value = '1';
  document.getElementById('mqtt-add-name').value = '';
  updateAddModalId();
}

function hideAddModal() {
  document.getElementById('mqtt-add-modal').setAttribute('hidden', '');
}

function updateAddModalId() {
  const type = document.getElementById('mqtt-add-type').value;
  const port = document.getElementById('mqtt-add-port').value;
  const addr = document.getElementById('mqtt-add-addr').value;
  const idEl = document.getElementById('mqtt-add-id');
  if (idEl) idEl.value = makeDeviceId(type, port, addr);
}

function confirmAddDevice() {
  const type = document.getElementById('mqtt-add-type').value;
  const port = document.getElementById('mqtt-add-port').value;
  const addr = parseInt(document.getElementById('mqtt-add-addr').value, 10);
  const name = document.getElementById('mqtt-add-name').value.trim();
  const id = document.getElementById('mqtt-add-id').value.trim() || makeDeviceId(type, port, addr);
  const baudrate = type === 'dtv' ? 19200 : 115200;

  if (_config.devices.find(d => d.id === id)) {
    showToast(`Устройство ${id} уже добавлено`, 'warn');
    return;
  }

  const dev = {id, type, port, baudrate, address: addr, name: name || id};
  if (type === 'mr02m') {
    dev.module_type = 1;
    dev.poll_s = 1;
    dev.poll_do_di_s = 1;
    dev.poll_ai_ao_s = 1;
    dev.poll_diag_s = 60;
    dev.channels = {};
  } else if (type === 'dtv') {
    dev.poll_sensors_s = 1;
    dev.poll_presence_s = 1;
    dev.poll_diag_s = 60;
    dev.sensors_present = DTV_SENSORS.map(s => s.key);
  } else if (type === 'ce02m3') {
    dev.poll_power_s = 1;
    dev.poll_energy_s = 60;
    dev.poll_diag_s = 120;
    dev.ct_ratio = 4000;
    dev.phases = ['A','B','C'];
    dev.channels_enabled = {};
  }

  _config.devices.push(dev);
  markUnsaved();
  hideAddModal();
  renderDeviceList();
  renderAccordion();
}

// ── Save & Apply ──────────────────────────────────────────────────────────────
async function saveAndApply() {
  const btn = document.getElementById('mqtt-save-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Сохранение...'; }

  const payload = Object.assign({}, _config, {restart: true});
  const res = await apiPost('/cgi-bin/mqtt_config.cgi', payload).catch(() => null);

  if (btn) { btn.disabled = false; btn.textContent = 'Сохранить и применить'; }

  if (res && res.ok) {
    _unsaved = false;
    showToast('Настройки сохранены. Мост MQTT перезапущен.');
    setTimeout(refreshBrokerStatus, 2000);
  } else {
    showToast('Ошибка сохранения: ' + (res?.error || 'неизвестная'), 'err');
  }
}

function markUnsaved() {
  _unsaved = true;
  const btn = document.getElementById('mqtt-save-btn');
  if (btn && !btn.textContent.includes('*')) btn.textContent += ' *';
}

// ── Topic monitor ─────────────────────────────────────────────────────────────
function startMonitor() {
  stopMonitor();
  _monitorPaused = false;
  const filter = document.getElementById('mqtt-monitor-filter')?.value || '';
  const url = '/cgi-bin/mqtt_monitor.cgi' + (filter ? `?device=${encodeURIComponent(filter)}` : '');
  _monitorEs = new EventSource(url);
  _monitorEs.onmessage = e => {
    if (_monitorPaused) return;
    try {
      const msg = JSON.parse(e.data);
      appendMonitorLine(msg.ts, msg.topic, msg.value);
    } catch {}
  };
  _monitorEs.onerror = () => {
    const el = document.getElementById('mqtt-monitor-log');
    if (el) { const p = h('div', {'class':'mqtt-monitor-err'}, '[SSE ошибка — переподключение...]'); el.appendChild(p); }
  };
}

function stopMonitor() {
  if (_monitorEs) { _monitorEs.close(); _monitorEs = null; }
}

function appendMonitorLine(ts, topic, value) {
  const el = document.getElementById('mqtt-monitor-log');
  if (!el) return;
  const line = h('div', {'class':'mqtt-monitor-line'},
    h('span', {'class':'mqtt-monitor-ts'}, ts),
    h('span', {'class':'mqtt-monitor-topic'}, topic),
    h('span', {'class':'mqtt-monitor-val'}, value)
  );
  el.appendChild(line);
  // Keep max 100 lines
  while (el.children.length > 100) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

function clearMonitor() {
  const el = document.getElementById('mqtt-monitor-log');
  if (el) el.innerHTML = '';
}

// ── Tab init ──────────────────────────────────────────────────────────────────
window.mqttTabInit = function() {
  refreshBrokerStatus();
  loadConfig();
  // Start monitor when tab opens
  startMonitor();
  // Refresh broker status every 10s
  const timer = setInterval(refreshBrokerStatus, 10000);
  window._mqttStatusTimer = timer;
};

window.mqttTabDestroy = function() {
  stopMonitor();
  if (window._mqttStatusTimer) clearInterval(window._mqttStatusTimer);
};

// ── Expose to global for HTML onclick ────────────────────────────────────────
window.mqttSaveAndApply  = saveAndApply;
window.mqttShowAddModal  = showAddModal;
window.mqttHideAddModal  = hideAddModal;
window.mqttConfirmAdd    = confirmAddDevice;
window.mqttUpdateId      = updateAddModalId;
window.mqttShowScanModal = showScanModal;
window.mqttHideScanModal = hideScanModal;
window.mqttRunScan       = runScan;
window.mqttStartMonitor  = startMonitor;
window.mqttStopMonitor   = stopMonitor;
window.mqttClearMonitor  = clearMonitor;
window.mqttTogglePause   = () => { _monitorPaused = !_monitorPaused; };
window.mqttCtrl          = async (action) => {
  const res = await apiPost('/cgi-bin/mqtt_ctrl.cgi', {action}).catch(() => null);
  if (res?.ok) { showToast(`Выполнено: ${action}`); setTimeout(refreshBrokerStatus, 1500); }
  else showToast('Ошибка: ' + (res?.error || '?'), 'err');
};

})();
