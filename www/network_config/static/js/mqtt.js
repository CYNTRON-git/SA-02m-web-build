/* SA-02m MQTT tab — v1.0 */

(function() {
'use strict';

function uiT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(String(s)) : String(s);
}

// ── Module types map ──────────────────────────────────────────────────────────
// Must match MR02M_MODULE_TYPES in modbus_mqtt_bridge.py exactly
const MR02M_TYPES = {
  1:  {name:'6DO8DI',    do:6,  di:8,  ao:0,  ai:0},
  2:  {name:'16DO',      do:16, di:0,  ao:0,  ai:0},
  3:  {name:'12AO',      do:0,  di:0,  ao:12, ai:0},
  4:  {name:'6DO',       do:6,  di:0,  ao:0,  ai:0},
  5:  {name:'14DI',      do:0,  di:14, ao:0,  ai:0},
  6:  {name:'6AI6AO',    do:0,  di:0,  ao:6,  ai:6},
  7:  {name:'12AI',      do:0,  di:0,  ao:0,  ai:12},
  8:  {name:'4DO6DI',    do:4,  di:6,  ao:0,  ai:0},
  9:  {name:'TENZO2',    do:0,  di:0,  ao:0,  ai:0},
  10: {name:'10DIcon',   do:0,  di:10, ao:0,  ai:0},
  11: {name:'6DO5DI2AO', do:6,  di:5,  ao:2,  ai:0},
  12: {name:'6AI2AO',    do:0,  di:0,  ao:2,  ai:6},
  15: {name:'4TO6DI',    do:4,  di:6,  ao:4,  ai:0},
};

/** Русские подписи типов МР-02м (как в интерфейсе: МР-02м 6АИ 6АО). */
const MR02M_TYPE_LABELS_RU = {
  1:  '6ДО 8ДИ',
  2:  '16ДО',
  3:  '12АО',
  4:  '6ДО',
  5:  '14ДИ',
  6:  '6АИ 6АО',
  7:  '12АИ',
  8:  '4ДО 6ДИ',
  9:  'Тензо 2',
  10: '10ДИ',
  11: '6ДО 5ДИ 2АО',
  12: '6АИ 2АО',
  15: '4ТО 6ДИ',
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
  const stored = (dev.name && String(dev.name).trim()) ? String(dev.name).trim() : '';
  if (dev.type === 'mr02m') {
    // Count-first product names (6AI6AO / 4DO6DI), matching MR-02m main.h /
    // MR02M_TYPE_NAMES. Rewrite legacy letter-first YAML (AO6AI6, DO4DI6, …).
    const mt = getModuleTypeCode(dev);
    const auto = `МР-02м ${mr02mTypeLabelRu(mt)} (${comName} addr=${addr})`;
    if (!stored) return auto;
    if (/MR-?02m|МР-?02м/i.test(stored) || inferModuleTypeFromName(stored) != null) {
      return auto;
    }
    if (/\([^)]*addr=\d+/i.test(stored)) return stored;
    return `${stored} (${comName} addr=${addr})`;
  }
  if (dev.type === 'dtv' || dev.type === 'ce02m3') {
    const portNum = String(comName || '').replace(/^COM/i, '') || '—';
    const sku = dev.type === 'dtv' ? 'ДТВ-RS-485' : 'СЭ-02м-3';
    const auto = `${sku} № ${addr} порт ${portNum}`;
    if (!stored || /№\s*\d+\s+порт\s+\d+/i.test(stored) || /\([^)]*addr=\d+/i.test(stored)) {
      return auto;
    }
    return `${stored} · ${auto}`;
  }
  if (stored && /\([^)]*addr=\d+/i.test(stored)) return stored;
  return `${dev.name || dev.id} (${comName} addr=${addr})`;
}

function normalizeSigKey(s) {
  return String(s || '').toUpperCase().replace(/[\s_.\-]/g, '');
}

/** Латиница + кириллица А/О/И/Д/Т → DO/AI/AO/DI для сравнения с EEPROM. */
function latinizeMr02mStr(s) {
  return String(s || '').toUpperCase()
    .replace(/\u0410/g, 'A')
    .replace(/\u0412/g, 'V')
    .replace(/\u041e/g, 'O')
    .replace(/\u0418/g, 'I')
    .replace(/\u0414/g, 'D')
    .replace(/\u0422/g, 'T')
    .replace(/[\s_.\-]/g, '');
}

function mr02mSigDescribesType(signature, moduleType) {
  const mt = Number(moduleType);
  if (!mt || !MR02M_TYPES[mt]) return false;
  const sig = (signature || '').trim();
  if (!sig) return true;
  const sk = latinizeMr02mStr(sig);
  const labelK = latinizeMr02mStr(MR02M_TYPE_LABELS_RU[mt] || '');
  const codeK = latinizeMr02mStr(MR02M_TYPES[mt].name || '');
  if (sk === labelK || sk === codeK) return true;
  return inferModuleTypeFromName(sig) === mt;
}

function mr02mScanDisplayLabel(dev) {
  const label = mr02mTypeLabelRu(dev.module_type);
  const sig = (dev.signature || '').trim();
  if (!sig || mr02mSigDescribesType(sig, dev.module_type)) return label;
  return `${label} · ${sig}`;
}

function scanShortName(scanDev, type) {
  const addr = scanDev.addr;
  if (type === 'mr02m' && scanDev.module_type && MR02M_TYPES[scanDev.module_type]) {
    const sig = (scanDev.signature || '').trim();
    if (sig && !mr02mSigDescribesType(sig, scanDev.module_type)) return sig;
    return `МР-02м ${mr02mTypeLabelRu(scanDev.module_type)}`;
  }
  if (type === 'dtv') return 'ДТВ-RS-485';
  if (type === 'ce02m3') return 'СЭ-02м-3';
  if (scanDev.signature) return String(scanDev.signature).trim();
  return `Устройство ${addr}`;
}

function stripComAddrSuffix(name) {
  return String(name || '').replace(/\s*\([^)]*addr=\d+[^)]*\)\s*$/i, '').trim();
}

function findListedDevice(port, addr) {
  const a = Number(addr);
  return (_config.devices || []).find((d) => {
    const da = Number(d.address != null ? d.address : d.addr);
    return d.port === port && da === a;
  }) || null;
}

function buildDeviceConfigName(shortName, port, addr) {
  const comName = (port || '').replace('/dev/', '');
  const portNum = String(comName || '').replace(/^COM/i, '') || '—';
  const base = (shortName || '').trim();
  if (base === 'ДТВ-RS-485' || base === 'СЭ-02м-3') {
    return `${base} № ${addr} порт ${portNum}`;
  }
  if (!base) return `Устройство № ${addr} порт ${portNum}`;
  return `${base} (${comName} addr=${addr})`;
}

function scanTypeHint(dev) {
  if (dev.type === 'mr02m' && dev.module_type && MR02M_TYPES[dev.module_type]) {
    return mr02mScanDisplayLabel(dev);
  }
  if (dev.type === 'dtv') return 'ДТВ-RS-485';
  if (dev.type === 'ce02m3') return 'СЭ-02м-3';
  const tn = (dev.type_name || '').trim();
  const sig = (dev.signature || '').trim();
  if (tn && tn !== 'unknown' && sig && normalizeSigKey(tn) !== normalizeSigKey(sig)) {
    return `${tn} · ${sig}`;
  }
  if (tn && tn !== 'unknown') return tn;
  if (sig) return sig;
  return '—';
}

function scanListedCheck() {
  return h('span', {'class': 'mqtt-scan-check', 'title': 'Уже в списке'}, '✓');
}

// Modbus selection codes 0..42 (регистр «тип датчика», MR-02m ≥1.0.9.1) — must match module_profiles.py / modbus_mqtt_bridge.py
const AI_SENSOR_LABELS = [
  {code:0,  label:'0 — Выключен'},
  {code:1,  label:'1 — NTC 1.8k (B3380)'},
  {code:2,  label:'2 — NTC 5k (B3470)'},
  {code:3,  label:'3 — NTC 10k (B3950)'},
  {code:4,  label:'4 — NTC 10k (B3988)'},
  {code:5,  label:'5 — NTC 10k (B3435)'},
  {code:6,  label:'6 — NTC 10k (B3470)'},
  {code:7,  label:'7 — NTC 100k (B3950)'},
  {code:8,  label:'8 — Pt50 (α385), 2-пров.'},
  {code:9,  label:'9 — Pt100 (α385), 2-пров.'},
  {code:10, label:'10 — Pt500 (α385), 2-пров.'},
  {code:11, label:'11 — Pt1000 (α385), 2-пров.'},
  {code:12, label:'12 — Pt50 (α391), 50П'},
  {code:13, label:'13 — Pt100 (α391), 100П'},
  {code:14, label:'14 — Pt1000 (α391), 1000П'},
  {code:15, label:'15 — Pt50 (α428), 50М'},
  {code:16, label:'16 — Pt100 (α428), 100М'},
  {code:17, label:'17 — Pt1000 (α428), 1000М'},
  {code:18, label:'18 — Ni100 (α617)'},
  {code:19, label:'19 — Ni500 (α617)'},
  {code:20, label:'20 — Ni1000 (α617)'},
  {code:21, label:'21 — Pt50 (α385), 3-пров.'},
  {code:22, label:'22 — Pt100 (α385), 3-пров.'},
  {code:23, label:'23 — Pt500 (α385), 3-пров.'},
  {code:24, label:'24 — Pt1000 (α385), 3-пров.'},
  {code:25, label:'25 — Pt50 (α391), 50П, 3-пров.'},
  {code:26, label:'26 — Pt100 (α391), 100П, 3-пров.'},
  {code:27, label:'27 — Pt1000 (α391), 1000П, 3-пров.'},
  {code:28, label:'28 — Pt50 (α428), 50М, 3-пров.'},
  {code:29, label:'29 — Pt100 (α428), 100М, 3-пров.'},
  {code:30, label:'30 — Pt1000 (α428), 1000М, 3-пров.'},
  {code:31, label:'31 — Ni100 (α617), 3-пров.'},
  {code:32, label:'32 — Ni500 (α617), 3-пров.'},
  {code:33, label:'33 — Ni1000 (α617), 3-пров.'},
  {code:34, label:'34 — Напряжение 0–10 В'},
  {code:35, label:'35 — Напряжение 0–30 В'},
  {code:36, label:'36 — Дифф. ±50 мВ'},
  {code:37, label:'37 — Дифф. ±2 В'},
  {code:38, label:'38 — Ток 0–5 мА'},
  {code:39, label:'39 — Ток 0–20 мА'},
  {code:40, label:'40 — Ток 4–20 мА'},
  {code:41, label:'41 — Термопара K (ТХА)'},
  {code:42, label:'42 — Сухой контакт'},
];

// Legacy ai_sensor_t enum (0x00..0x26, MR-02m <1.0.9.1) → Modbus selection codes 0..42.
const AI_SENSOR_LEGACY_ENUM_MIGRATION = {
  0x00: 0, 0x01: 3, 0x02: 11, 0x03: 9, 0x04: 34, 0x05: 40, 0x06: 41, 0x07: 42,
  0x08: 8, 0x09: 10, 0x0A: 7, 0x0B: 4, 0x0C: 5, 0x0D: 6, 0x0E: 13, 0x0F: 14,
  0x10: 16, 0x11: 17, 0x12: 18, 0x13: 19, 0x14: 20, 0x15: 38, 0x16: 39, 0x17: 36,
  0x18: 37, 0x19: 2, 0x1A: 1, 0x1B: 21, 0x1C: 22, 0x1D: 23, 0x1E: 24, 0x1F: 26,
  0x20: 27, 0x21: 29, 0x22: 30, 0x23: 31, 0x24: 32, 0x25: 33, 0x26: 35,
};
const AI_SENSOR_SCHEMA_MODBUS = 2;

function migrateLegacyAiSensorCode(code) {
  const c = Number(code) & 0xffff;
  if (Object.prototype.hasOwnProperty.call(AI_SENSOR_LEGACY_ENUM_MIGRATION, c)) {
    return AI_SENSOR_LEGACY_ENUM_MIGRATION[c];
  }
  return (c >= 0 && c <= 42) ? c : 0;
}

function isLegacyAiRegisterCode(code) {
  const c = Number(code) & 0xffff;
  return Object.prototype.hasOwnProperty.call(AI_SENSOR_LEGACY_ENUM_MIGRATION, c)
    && AI_SENSOR_LEGACY_ENUM_MIGRATION[c] !== c;
}

function migrateDeviceLegacyAiSensorTypes(dev) {
  if (!dev || dev.type !== 'mr02m') return;
  const schema = Number(dev.ai_sensor_schema || 1);
  if (schema >= AI_SENSOR_SCHEMA_MODBUS) return;
  const aiList = dev.channels && dev.channels.ai;
  if (!Array.isArray(aiList)) return;
  for (const entry of aiList) {
    if (!entry || entry.sensor_type == null) continue;
    entry.sensor_type = migrateLegacyAiSensorCode(entry.sensor_type);
  }
  dev.ai_sensor_schema = AI_SENSOR_SCHEMA_MODBUS;
}

const DTV_SENSOR_UNITS = {
  temp_ds18b20: '°C', temp_mcp9808: '°C', temp_hdc1080: '°C', temp_bme280: '°C',
  temp_bme680: '°C', temp_ext: '°C', humidity_hdc1080: '%', humidity_bme280: '%',
  humidity_bme680: '%', pressure_bme280_mmhg: 'mmHg', pressure_bme680_mmhg: 'mmHg',
  pressure_bme280_kpa: 'kPa', iaq_bme680: 'IAQ', presence_ld2412: '',
};

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

const CE02M3_UNITS = {
  voltage_a: 'V', voltage_b: 'V', voltage_c: 'V', voltage_ab: 'V', voltage_bc: 'V', voltage_ca: 'V',
  current_a: 'A', current_b: 'A', current_c: 'A', current_n: 'A',
  power_a: 'W', power_b: 'W', power_c: 'W', power_total: 'W',
  reactive_a: 'var', reactive_b: 'var', reactive_c: 'var', reactive_total: 'var',
  pf_a: '', pf_b: '', pf_c: '', pf_total: '', frequency: 'Hz',
  // Bridge publishes raw Wh / varh / VAh (docs/MQTT_TOPICS.md §CE-02m-3).
  energy_active_import: 'Wh', energy_active_export: 'Wh',
  energy_reactive_import: 'varh', energy_reactive_export: 'varh',
  energy_apparent: 'VAh',
};

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
let _monitorPollTimer = null;
let _monitorPaused = false;
let _monitorLastVal = Object.create(null);
let _monitorPrimed = false;
let _channelPollTimer = null;
let _channelPollDevId = null;
let _accordionBuilt = new Set();
let _liveDomScheduled = false;
let _liveDirtyDevices = new Set();
/** Типы AI с шины (Modbus), ключ ai_N — приоритет над YAML при отображении. */
const _liveSensorTypes = Object.create(null);
/** Каналы AI под редактированием — live-poll не затирает (как _mc_entry_edit_guard). */
const _aiTypeEditGuard = new Set();
const _aiTypeEditGuardTimers = Object.create(null);
/** Ожидаемый тип после выбора до подтверждения с шины (как _ai_sensor_pending). */
const _aiTypePending = Object.create(null);
const _AI_TYPE_EDIT_GUARD_MS = 450;
let _unsaved = false;
/** Выделенные строки таблицы «Устройства на шине» (id устройств). */
let _selectedDeviceIds = new Set();

function toggleDeviceListSelection(devId) {
  if (!devId) return;
  if (_selectedDeviceIds.has(devId)) _selectedDeviceIds.delete(devId);
  else _selectedDeviceIds.add(devId);
}

function pruneDeviceListSelection() {
  const ids = new Set((_config.devices || []).map(d => d.id));
  for (const id of _selectedDeviceIds) {
    if (!ids.has(id)) _selectedDeviceIds.delete(id);
  }
}

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
  t.textContent = uiT(msg);
  t.className = `mqtt-toast mqtt-toast-${type} mqtt-toast-show`;
  clearTimeout(t._timeout);
  t._timeout = setTimeout(() => t.classList.remove('mqtt-toast-show'), 3500);
}

function makeDeviceId(type, port, addr) {
  const comName = port.replace('/dev/', '');
  const prefix = type === 'dtv' ? 'dtv' : type === 'ce02m3' ? 'ce02m3' : 'mr02m';
  return `${prefix}-${comName}-${addr}`;
}

/** Уже в конфиге на этом COM и Modbus-адресе. */
function scanDeviceAlreadyListed(port, addr) {
  return !!findListedDevice(port, addr);
}

/** Если module_type не сохранён в YAML — угадать по legacy-имени (AO6AI6, DO4DI6, …). */
function inferModuleTypeFromName(name) {
  const n = latinizeMr02mStr(name);
  const tokens = [
    ['AO6AI6', 6], ['6AO6AI', 6], ['6AI6AO', 6], ['AI6AO6', 6],
    ['AI6AO2', 12], ['6AI2AO', 12], ['6AO2AI', 12], ['AO6AI2', 12],
    ['DO6DI8', 1], ['6DO8DI', 1], ['8DI6DO', 1],
    ['DO4DI6', 8], ['4DO6DI', 8], ['6DI4DO', 8],
    ['6DO5DI2AO', 11],
    ['4TO6DI', 15], ['TO4DI6', 15], ['4TO6DI', 15],
    ['10DICON', 10], ['10DI', 10],
    ['DO16', 2], ['16DO', 2],
    ['AO12', 3], ['12AO', 3],
    ['AI12', 7], ['12AI', 7],
    ['DI14', 5], ['14DI', 5],
    ['DO6', 4], ['6DO', 4],
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

/** Модули с P/N-парами AI (ТХА / 3-проводный RTD): 6AI6AO, 12AI, 6AI2AO. */
const MR02M_AI_PAIR_TYPES = new Set([6, 7, 12]);

function mr02mAiSupportsPairs(mt) {
  return MR02M_AI_PAIR_TYPES.has(Number(mt));
}

/** Чётный AI — N-нога пары (P = ch − 1), как в прошивке MR-02m. */
function mr02mAiPairParent(ch) {
  const c = Number(ch);
  return (c >= 2 && c % 2 === 0) ? c - 1 : null;
}

function mr02mAiIsNLeg(mt, ch) {
  if (!mr02mAiSupportsPairs(mt)) return false;
  const aiCount = (MR02M_TYPES[Number(mt)] || {}).ai || 0;
  const c = Number(ch);
  return c >= 2 && c % 2 === 0 && c <= aiCount && mr02mAiPairParent(c) != null;
}

const _AI_NTC_CODES = new Set([1, 2, 3, 4, 5, 6, 7]);
const _AI_RTD_CODES = new Set([
  8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
  21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33,
]);
const _AI_TC_K_CODES = new Set([41]);
/** Трёхпроводные RTD (Modbus codes 21–33). */
const _AI_RTD_3WIRE = new Set([
  21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33,
]);
const _AI_VOLT_CODES = new Set([34, 35, 36, 37]);
const _AI_CURR_CODES = new Set([38, 39, 40]);
const _AI_DRY_CODES = new Set([42]);

function mr02mAiMirrorTypeToN(code) {
  const c = Number(code) & 0xffff;
  return _AI_TC_K_CODES.has(c) || _AI_RTD_3WIRE.has(c);
}

function aiSensorBucketJs(code) {
  const c = Number(code) & 0xffff;
  if (c === 0) return 'off';
  if (_AI_NTC_CODES.has(c)) return 'ntc';
  if (_AI_RTD_CODES.has(c)) return 'rtd';
  if (_AI_TC_K_CODES.has(c)) return 'tc_k';
  return 'other';
}

/** Как в прошивальщике: тип на N дублируется с P только для ТХА и 3-проводного RTD. */
function syncMr02mPairAfterParentChange(dev, parentCh) {
  const mt = getModuleTypeCode(dev);
  const nCh = parentCh + 1;
  if (!mr02mAiIsNLeg(mt, nCh)) return;
  if (!dev.channels) dev.channels = {};
  const pCfg = getOrCreateChannel(dev.channels, 'ai', parentCh);
  const st = pCfg.sensor_type != null ? Number(pCfg.sensor_type) : 0;
  if (!mr02mAiMirrorTypeToN(st)) return false;
  const nCfg = getOrCreateChannel(dev.channels, 'ai', nCh);
  nCfg.sensor_type = st;
  aiTypeSetPending(dev.id, nCh, st);
  return true;
}

function aiTypeControlKey(devId, ch) {
  return `${devId}:ai_${ch}`;
}

function aiTypeEditGuardAdd(devId, ch) {
  const key = aiTypeControlKey(devId, ch);
  _aiTypeEditGuard.add(key);
  if (_aiTypeEditGuardTimers[key]) {
    clearTimeout(_aiTypeEditGuardTimers[key]);
    delete _aiTypeEditGuardTimers[key];
  }
}

function aiTypeEditGuardReleaseLater(devId, ch) {
  const key = aiTypeControlKey(devId, ch);
  if (_aiTypeEditGuardTimers[key]) clearTimeout(_aiTypeEditGuardTimers[key]);
  _aiTypeEditGuardTimers[key] = setTimeout(() => {
    _aiTypeEditGuard.delete(key);
    delete _aiTypeEditGuardTimers[key];
  }, _AI_TYPE_EDIT_GUARD_MS);
}

function aiTypeSetPending(devId, ch, code) {
  _aiTypePending[aiTypeControlKey(devId, ch)] = Number(code) & 0xffff;
}

function aiTypeClearPendingForDevice(devId) {
  const prefix = `${devId}:ai_`;
  for (const key of Object.keys(_aiTypePending)) {
    if (key.startsWith(prefix)) delete _aiTypePending[key];
  }
}

/** После «Сохранить и применить» — ожидаемые типы из YAML до подтверждения с шины (как _ai_sensor_pending). */
function aiTypeApplyPendingFromDeviceConfig(dev) {
  if (!dev || dev.type !== 'mr02m') return;
  const mtCode = getModuleTypeCode(dev);
  const aiCount = (MR02M_TYPES[mtCode] || {}).ai || 0;
  const channels = dev.channels || {};
  for (let i = 1; i <= aiCount; i++) {
    const chCfg = getOrCreateChannel(channels, 'ai', i);
    const st = chCfg.sensor_type != null ? Number(chCfg.sensor_type) : 0;
    aiTypeSetPending(dev.id, i, st);
  }
}

function aiTypeReconcilePending(devId, sensorTypes) {
  if (!sensorTypes) return;
  const prefix = `${devId}:ai_`;
  for (const key of Object.keys(_aiTypePending)) {
    if (!key.startsWith(prefix)) continue;
    const ctrl = key.slice(devId.length + 1);
    const live = sensorTypes[ctrl];
    if (live == null || live === '') continue;
    if ((Number(live) & 0xffff) === _aiTypePending[key]) delete _aiTypePending[key];
  }
}

function aiTypeIsEditBlocked(devId, ch, sel) {
  const key = aiTypeControlKey(devId, ch);
  if (_aiTypeEditGuard.has(key)) return true;
  return !!(sel && document.activeElement === sel);
}

function normalizeMr02mAiPairsAll(dev) {
  if (dev.type !== 'mr02m') return;
  const mt = getModuleTypeCode(dev);
  if (!mr02mAiSupportsPairs(mt)) return;
  if (!dev.channels) dev.channels = {};
  const aiCount = (MR02M_TYPES[mt] || {}).ai || 0;
  for (let nCh = 2; nCh <= aiCount; nCh += 2) {
    const pCh = mr02mAiPairParent(nCh);
    if (!pCh) continue;
    const pCfg = getOrCreateChannel(dev.channels, 'ai', pCh);
    const st = pCfg.sensor_type != null ? Number(pCfg.sensor_type) : 0;
    if (!mr02mAiMirrorTypeToN(st)) continue;
    const nCfg = getOrCreateChannel(dev.channels, 'ai', nCh);
    nCfg.sensor_type = st;
  }
}

function topicPath(deviceId, chName) {
  return `/devices/${deviceId}/controls/${chName}`;
}

/** MQTT wildcard filter for a device (subscribe to all its controls/meta). */
function deviceTopicFilter(deviceId) {
  return `/devices/${deviceId}/#`;
}

/** Системные показатели модуля MR-02m (публикует мост в _poll_diag). */
const MR02M_SYS_VARS = [
  {key: 'uptime_s', label: 'Время работы', unit: ''},
  {key: 'serial', label: 'Серийный номер', unit: ''},
  {key: 'mcu_temp', label: 'Температура МК', unit: '°C'},
  {key: 'mcu_vdd', label: 'Питание МК', unit: 'В'},
  {key: 'op_days', label: 'Наработка', unit: 'дн'},
  {key: 'mcu_ram_free', label: 'ОЗУ свободно', unit: 'байт'},
  {key: 'mcu_ram_used', label: 'ОЗУ занято', unit: 'байт'},
  {key: 'reset_reason', label: 'Причина перезагрузки', unit: ''},
  {key: 'fw_updates', label: 'Обновлений прошивки', unit: ''},
];

const DTV_SYS_VARS = [
  {key: 'uptime_s', label: 'Время работы', unit: ''},
  {key: 'mcu_temp', label: 'Температура МК', unit: '°C'},
  {key: 'mcu_vdd', label: 'Питание МК', unit: 'В'},
];

const CE02M3_SYS_VARS = [
  {key: 'uptime_s', label: 'Время работы', unit: ''},
  {key: 'mcu_temp', label: 'Температура МК', unit: '°C'},
  {key: 'mcu_vdd', label: 'Питание МК', unit: 'В'},
];

const _liveByDevice = Object.create(null);
const _liveUnits = Object.create(null);
const _liveUptimeAnchor = Object.create(null);
let _uptimeTickTimer = null;
/** Output-write reconciliation: devId:ctrl → {target, prev, deadline}.
    The button renders the target OPTIMISTICALLY on click (Operator rule:
    the relay clicks instantly — the button must not sit gray); the bus echo
    only reconciles: match → silently confirmed, deadline/error → revert to
    the live state. `prev` = pre-click live value, so a stale pre-write poll
    reading (== prev) is not mistaken for a contradicting echo. */
const _doPending = Object.create(null);
const _DO_CONFIRM_DEADLINE_MS = 5000;

/** AO setpoint write reconciliation (numeric sibling of _doPending): devId:ctrl
    → {target, prev, deadline}. Same optimistic-then-echo model as the DO path;
    `prev` = pre-write live raw (-1 when unknown) so a stale pre-write poll is not
    mistaken for a confirming echo. */
const _aoPending = Object.create(null);
/** AO input edit-guard (mirrors the AI-type select guard): while the operator
    is typing a setpoint, the 1.5 s live poll must not overwrite the field. */
const _aoEditGuard = new Set();
const _aoEditGuardTimers = Object.create(null);
const _AO_EDIT_GUARD_MS = 450;

function aiUnitsForCode(code) {
  const c = Number(code) & 0xffff;
  if (c === 0) return '';
  if (c === 34 || c === 35 || c === 37) return 'В';
  if (c === 36) return 'мВ';
  if (c === 38 || c === 39 || c === 40) return 'мА';
  if (c === 42) return '';
  return '°C';
}

/** Единицы MQTT meta (WB) → подпись как в MR-02m-flasher. */
function formatUnitLabel(u) {
  if (!u) return '';
  switch (String(u)) {
    case 'V': return 'В';
    case 'mA': return 'мА';
    case 'mV': return 'мВ';
    case 'A': return 'А';
    case 'kV': return 'кВ';
    case 'Hz': return 'Гц';
    case 'B':
    case 'byte':
    case 'bytes':
      return 'байт';
    default: return String(u);
  }
}

function mr02mAiEffectiveSensorType(dev, ch, channels) {
  const key = aiTypeControlKey(dev.id, ch);
  const pending = _aiTypePending[key];
  if (pending != null) return pending;
  const chCfg = getOrCreateChannel(channels, 'ai', ch);
  const cfgType = chCfg.sensor_type != null ? Number(chCfg.sensor_type) : null;
  const live = _liveSensorTypes[dev.id]?.[`ai_${ch}`];
  if (_aiTypeEditGuard.has(key) && cfgType != null) return cfgType & 0xffff;
  if (cfgType != null && live != null && live !== '') {
    const liveN = Number(live) & 0xffff;
    if (isLegacyAiRegisterCode(liveN)) return cfgType & 0xffff;
    if (liveN === 0 && (cfgType & 0xffff) !== 0) return cfgType & 0xffff;
  }
  if (live != null && live !== '') {
    const liveN = Number(live) & 0xffff;
    if (isLegacyAiRegisterCode(liveN) && cfgType != null) return cfgType & 0xffff;
    return liveN;
  }
  const mt = getModuleTypeCode(dev);
  const parent = mr02mAiPairParent(ch);
  if (mr02mAiIsNLeg(mt, ch) && parent) {
    const pCfg = getOrCreateChannel(channels, 'ai', parent);
    const pst = pCfg.sensor_type != null ? Number(pCfg.sensor_type) : 0;
    if (mr02mAiMirrorTypeToN(pst)) return pst;
  }
  return cfgType != null ? cfgType & 0xffff : 0;
}

function liveUnitFor(devId, controlName, fallback) {
  if (controlName === 'uptime_s') return '';
  const u = _liveUnits[devId] && _liveUnits[devId][controlName];
  const raw = u != null && u !== '' ? u : (fallback || '');
  return formatUnitLabel(raw);
}

function formatUptimeSeconds(s) {
  s = parseInt(s, 10);
  if (Number.isNaN(s) || s < 0) return '—';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}д ${h}ч ${m}м`;
  if (h > 0) return `${h}ч ${m}м`;
  return `${m}м ${s % 60}с`;
}

function setLiveUptimeAnchor(devId, sec) {
  const s = parseInt(sec, 10);
  if (Number.isNaN(s) || s < 0) return;
  _liveUptimeAnchor[devId] = { sec: s, at: Date.now() };
}

function effectiveUptimeSec(devId) {
  const anchor = _liveUptimeAnchor[devId];
  if (anchor) {
    return anchor.sec + Math.floor((Date.now() - anchor.at) / 1000);
  }
  const rec = _liveByDevice[devId] && _liveByDevice[devId].uptime_s;
  if (!rec || rec.isError) return null;
  const s = parseInt(rec.value, 10);
  return Number.isNaN(s) ? null : s;
}

function formatLiveDisplay(controlName, rec) {
  if (!rec) return '—';
  if (rec.isError) return '⚠';
  const v = rec.value;
  if (v === '' || v == null) return '—';
  if (controlName === 'uptime_s') {
    return formatUptimeSeconds(v);
  }
  if (/^do_\d+$/.test(controlName) || /^di_\d+$/.test(controlName)) {
    if (v === '1') return 'вкл';
    if (v === '0') return 'выкл';
  }
  if (/^ao_\d+$/.test(controlName)) {
    const raw = parseInt(v, 10);
    if (!Number.isNaN(raw)) return (raw / 100).toFixed(2);
  }
  return String(v);
}

function isAccordionOpen(devId) {
  const body = document.getElementById(`acc-body-${devId}`);
  return body && !body.hasAttribute('hidden');
}

function scheduleLiveDomRefresh(devId) {
  if (!devId || !isAccordionOpen(devId)) return;
  _liveDirtyDevices.add(devId);
  if (_liveDomScheduled) return;
  _liveDomScheduled = true;
  requestAnimationFrame(() => {
    _liveDomScheduled = false;
    for (const id of _liveDirtyDevices) refreshLiveCellsForDevice(id);
    _liveDirtyDevices.clear();
  });
}

function setLiveValue(devId, controlName, value, isError, opts) {
  if (!_liveByDevice[devId]) _liveByDevice[devId] = Object.create(null);
  _liveByDevice[devId][controlName] = {
    value: value == null ? '' : String(value),
    isError: !!isError,
  };
  if (controlName === 'uptime_s' && !isError && value != null && value !== '') {
    setLiveUptimeAnchor(devId, value);
  }
  if (opts && opts.skipDom) return;
  if (isAccordionOpen(devId)) scheduleLiveDomRefresh(devId);
}

function updateLiveCell(devId, controlName) {
  const el = document.querySelector(`[data-live="${devId}:${controlName}"]`);
  if (!el) return;
  const rec = _liveByDevice[devId] && _liveByDevice[devId][controlName];
  if (controlName === 'uptime_s') {
    const s = effectiveUptimeSec(devId);
    el.textContent = s == null ? '—' : formatUptimeSeconds(s);
    el.className = 'mqtt-ch-live' + (rec && rec.isError ? ' mqtt-ch-live-err' : rec ? ' mqtt-ch-live-ok' : '');
  } else {
    el.textContent = formatLiveDisplay(controlName, rec);
    el.className = 'mqtt-ch-live' + (rec && rec.isError ? ' mqtt-ch-live-err' : rec ? ' mqtt-ch-live-ok' : '');
  }
  const unitEl = document.querySelector(`[data-unit="${devId}:${controlName}"]`);
  if (unitEl) {
    const u = liveUnitFor(devId, controlName, unitEl.getAttribute('data-unit-default') || '');
    unitEl.textContent = u;
    unitEl.hidden = !u;
  }
}

function refreshLiveCellsForDevice(devId) {
  if (!devId || !_accordionBuilt.has(devId)) return;
  const prefix = `${devId}:`;
  document.querySelectorAll(`[data-live^="${prefix}"]`).forEach(el => {
    const key = el.getAttribute('data-live');
    if (!key || key.indexOf(':') < 1) return;
    updateLiveCell(devId, key.slice(prefix.length));
  });
  refreshDoTogglesForDevice(devId);
  refreshAoInputsForDevice(devId);
}

function refreshAllLiveCells() {
  for (const devId of _accordionBuilt) refreshLiveCellsForDevice(devId);
}

// ── DO / coil write controls (toggle per output row) ─────────────────────────
// Dashboard HW-block idiom (hw.js updateHwToggleBtn/toggleHw): one button,
// blue when ON, "н/д" when the live value is unknown. Write path: POST
// mqtt_set.cgi → mosquitto → bridge → Modbus → echo; only the echo confirms.

function doToggleKey(devId, ctrl) {
  return `${devId}:${ctrl}`;
}

function doToggleBtnEl(devId, ctrl) {
  return document.querySelector(`[data-do-toggle="${devId}:${ctrl}"]`);
}

/** Logical output value from the bus: 1 / 0; error or unknown → -1. */
function doLiveLogical(devId, ctrl) {
  const rec = _liveByDevice[devId] && _liveByDevice[devId][ctrl];
  if (!rec || rec.isError) return -1;
  if (rec.value === '1') return 1;
  if (rec.value === '0') return 0;
  return -1;
}

/** Reflect the output state on its button (like updateHwToggleBtn in hw.js). */
function updateDoToggleBtn(devId, ctrl, btnOpt) {
  const btn = btnOpt || doToggleBtnEl(devId, ctrl);
  if (!btn) return;
  const pending = _doPending[doToggleKey(devId, ctrl)];
  btn.classList.remove('hw-active-blue', 'na', 'is-pending');
  if (btn.dataset.chDisabled === '1') {
    btn.disabled = true;
    btn.textContent = uiT('н/д');
    btn.classList.add('na');
    btn.title = uiT('Канал отключён');
    return;
  }
  if (pending) {
    // Optimistic: full target-state visual right away; the pending hint is the
    // tooltip + wait cursor only. Re-clicks are ignored by the _doPending
    // guard in setDoOutput, not by a gray disabled state.
    btn.disabled = false;
    btn.classList.add('is-pending');
    btn.textContent = uiT(pending.target ? 'ВКЛ' : 'ВЫКЛ');
    if (pending.target) btn.classList.add('hw-active-blue');
    btn.title = uiT('Ожидание подтверждения с шины');
    return;
  }
  btn.disabled = false;
  const nv = doLiveLogical(devId, ctrl);
  if (nv === -1) {
    btn.textContent = uiT('н/д');
    btn.classList.add('na');
    btn.title = uiT('Включить выход');
    return;
  }
  btn.textContent = uiT(nv ? 'ВКЛ' : 'ВЫКЛ');
  if (nv) btn.classList.add('hw-active-blue');
  btn.title = uiT(nv ? 'Выключить выход' : 'Включить выход');
}

function refreshDoTogglesForDevice(devId) {
  const prefix = `${devId}:`;
  document.querySelectorAll(`[data-do-toggle^="${prefix}"]`).forEach(btn => {
    const key = btn.getAttribute('data-do-toggle');
    if (!key || key.indexOf(':') < 1) return;
    updateDoToggleBtn(devId, key.slice(prefix.length), btn);
  });
}

/** Reconcile pending with the bus. The button already shows the target
    (optimistic), so: echo matches — silently confirmed (no visible change);
    a known value differing from BOTH target and the pre-click value — a
    genuine contradicting echo, snap to it (a reading equal to `prev` is
    likely a stale pre-write poll and is left to the deadline); deadline
    passed — revert to the live state + timeout toast. */
function sweepDoPending(devId) {
  const prefix = `${devId}:`;
  const now = Date.now();
  for (const key of Object.keys(_doPending)) {
    if (!key.startsWith(prefix)) continue;
    const ctrl = key.slice(prefix.length);
    const p = _doPending[key];
    const live = doLiveLogical(devId, ctrl);
    if (live === p.target) {
      delete _doPending[key]; // confirmed — already rendered
      updateDoToggleBtn(devId, ctrl);
    } else if (live !== -1 && live !== p.prev) {
      delete _doPending[key]; // contradicting echo — show the bus truth
      updateDoToggleBtn(devId, ctrl);
    } else if (now > p.deadline) {
      delete _doPending[key];
      updateDoToggleBtn(devId, ctrl);
      showToast('Нет подтверждения от моста (мост остановлен или модуль не отвечает)', 'warn');
    }
  }
}

/** Toggle click: unknown value counts as OFF → a click turns it ON (toggleHw idiom). */
function setDoOutput(dev, ctrl) {
  const devId = dev.id;
  const key = doToggleKey(devId, ctrl);
  if (_doPending[key]) return; // double-click guard
  const btn = doToggleBtnEl(devId, ctrl);
  if (!btn || btn.disabled) return;
  const prev = doLiveLogical(devId, ctrl);
  const target = prev === 1 ? 0 : 1;
  _doPending[key] = {target, prev, deadline: Date.now() + _DO_CONFIRM_DEADLINE_MS};
  updateDoToggleBtn(devId, ctrl); // optimistic: target state rendered right away
  fetch('cgi-bin/mqtt_set.cgi', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: `device=${encodeURIComponent(devId)}&control=${encodeURIComponent(ctrl)}&value=${target}`,
    credentials: 'same-origin',
  })
    .then(r => r.json())
    .then(j => {
      if (j && j.ok) return; // "ok" = published; only the bus echo confirms (sweepDoPending)
      delete _doPending[key];
      updateDoToggleBtn(devId, ctrl);
      if (j && j.error === 'unauthorized') showToast('Нет доступа', 'err');
      else if (j && j.error === 'publish_failed') showToast('Брокер MQTT недоступен', 'err');
      else showToast('Ошибка: ' + ((j && j.error) || 'unknown'), 'err');
    })
    .catch(() => {
      delete _doPending[key];
      updateDoToggleBtn(devId, ctrl);
      showToast('Нет связи с сервером', 'err');
    });
}

/** Toggle button for a DO / DTV-coil channel row. */
function buildDoToggleBtn(dev, ctrl, enabled) {
  const btn = h('button', {
    'type': 'button',
    'class': 'btn btn-sm mqtt-do-toggle',
    'data-do-toggle': `${dev.id}:${ctrl}`,
    'onclick': () => setDoOutput(dev, ctrl),
  });
  btn.dataset.chDisabled = enabled ? '0' : '1';
  updateDoToggleBtn(dev.id, ctrl, btn);
  return btn;
}

/** A disabled channel is excluded from polling — no echo can come, so the button goes inert. */
function setDoToggleChannelEnabled(devId, ctrl, enabled) {
  const btn = doToggleBtnEl(devId, ctrl);
  if (!btn) return;
  btn.dataset.chDisabled = enabled ? '0' : '1';
  updateDoToggleBtn(devId, ctrl, btn);
}

// ── AO setpoint write controls (numeric input per output row) ────────────────
// Numeric sibling of the DO toggle: an input holding the 0..1000 raw setpoint
// (= 0..10.00 V), written on Enter / blur (Operator decision F=B, no button).
// Write path: POST mqtt_set.cgi → mosquitto → bridge → Modbus holding 33+ch-1 →
// echo; only the bus echo confirms. The separate live span keeps showing volts.

function aoInputKey(devId, ctrl) {
  return `${devId}:${ctrl}`;
}

function aoInputEl(devId, ctrl) {
  return document.querySelector(`[data-ao-input="${devId}:${ctrl}"]`);
}

/** Raw integer setpoint from the bus: 0..1000; error or unknown → -1. */
function aoLiveRaw(devId, ctrl) {
  const rec = _liveByDevice[devId] && _liveByDevice[devId][ctrl];
  if (!rec || rec.isError) return -1;
  const n = parseInt(rec.value, 10);
  return Number.isNaN(n) ? -1 : n;
}

/** Clamp any typed value to an integer 0..1000; empty/non-numeric → -1. */
function aoClampSetpoint(v) {
  if (v == null || String(v).trim() === '') return -1;
  let n = Math.round(Number(v));
  if (!Number.isFinite(n)) return -1;
  if (n < 0) n = 0;
  if (n > 1000) n = 1000;
  return n;
}

function aoEditGuardAdd(devId, ctrl) {
  const key = aoInputKey(devId, ctrl);
  _aoEditGuard.add(key);
  if (_aoEditGuardTimers[key]) {
    clearTimeout(_aoEditGuardTimers[key]);
    delete _aoEditGuardTimers[key];
  }
}

function aoEditGuardReleaseLater(devId, ctrl) {
  const key = aoInputKey(devId, ctrl);
  if (_aoEditGuardTimers[key]) clearTimeout(_aoEditGuardTimers[key]);
  _aoEditGuardTimers[key] = setTimeout(() => {
    _aoEditGuard.delete(key);
    delete _aoEditGuardTimers[key];
  }, _AO_EDIT_GUARD_MS);
}

function aoInputIsEditBlocked(devId, ctrl, inp) {
  if (_aoEditGuard.has(aoInputKey(devId, ctrl))) return true;
  return !!(inp && document.activeElement === inp);
}

/** Reflect bus/pending state on the setpoint input. While the operator is
    editing (focused / guard) or a write is pending, the field is never
    clobbered by the poll — mirrors updateDoToggleBtn's optimistic rule. */
function updateAoInput(devId, ctrl, inpOpt) {
  const inp = inpOpt || aoInputEl(devId, ctrl);
  if (!inp) return;
  if (inp.dataset.chDisabled === '1') {
    inp.disabled = true;
    inp.classList.remove('is-pending');
    inp.title = uiT('Канал отключён');
    return;
  }
  inp.disabled = false;
  if (_aoPending[aoInputKey(devId, ctrl)]) {
    inp.classList.add('is-pending');
    inp.title = uiT('Ожидание подтверждения с шины');
    return; // keep the optimistically-shown target
  }
  inp.classList.remove('is-pending');
  inp.title = uiT('Задание, 0–1000 (1000 = 10.00 В)');
  if (aoInputIsEditBlocked(devId, ctrl, inp)) return; // operator typing — do not overwrite
  const live = aoLiveRaw(devId, ctrl);
  inp.value = live < 0 ? '' : String(live);
}

function refreshAoInputsForDevice(devId) {
  const prefix = `${devId}:`;
  document.querySelectorAll(`[data-ao-input^="${prefix}"]`).forEach(inp => {
    const key = inp.getAttribute('data-ao-input');
    if (!key || key.indexOf(':') < 1) return;
    updateAoInput(devId, key.slice(prefix.length), inp);
  });
}

/** Reconcile pending AO writes with the bus (numeric sibling of sweepDoPending):
    exact echo → confirmed; any new echo differing from the pre-write value →
    show the bus truth (tolerates module quantisation and a contradicting write);
    deadline → revert to live + timeout toast. */
function sweepAoPending(devId) {
  const prefix = `${devId}:`;
  const now = Date.now();
  for (const key of Object.keys(_aoPending)) {
    if (!key.startsWith(prefix)) continue;
    const ctrl = key.slice(prefix.length);
    const p = _aoPending[key];
    const live = aoLiveRaw(devId, ctrl);
    if (live >= 0 && live === p.target) {
      delete _aoPending[key]; // confirmed
      updateAoInput(devId, ctrl);
    } else if (live >= 0 && live !== p.prev) {
      delete _aoPending[key]; // new echo (quantised or contradicting) — show bus truth
      updateAoInput(devId, ctrl);
    } else if (now > p.deadline) {
      delete _aoPending[key];
      updateAoInput(devId, ctrl);
      showToast('Нет подтверждения от моста (мост остановлен или модуль не отвечает)', 'warn');
    }
  }
}

/** Commit the setpoint currently in the input (Enter / blur). Clamps to
    0..1000, skips a no-op or an in-flight write, then POSTs like setDoOutput. */
function setAoOutput(dev, ctrl) {
  const devId = dev.id;
  const key = aoInputKey(devId, ctrl);
  const inp = aoInputEl(devId, ctrl);
  if (!inp || inp.disabled) return;
  const target = aoClampSetpoint(inp.value);
  if (target < 0) { // empty / non-numeric → restore the bus value, no write
    updateAoInput(devId, ctrl, inp);
    return;
  }
  inp.value = String(target); // reflect the clamp immediately
  if (_aoPending[key]) return; // a write is already in flight — ignore re-submit
  const prev = aoLiveRaw(devId, ctrl);
  if (prev === target) return; // already at target — no redundant analog re-drive
  _aoPending[key] = {target, prev, deadline: Date.now() + _DO_CONFIRM_DEADLINE_MS};
  updateAoInput(devId, ctrl, inp); // optimistic pending
  fetch('cgi-bin/mqtt_set.cgi', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: `device=${encodeURIComponent(devId)}&control=${encodeURIComponent(ctrl)}&value=${target}`,
    credentials: 'same-origin',
  })
    .then(r => r.json())
    .then(j => {
      if (j && j.ok) return; // "ok" = published; only the bus echo confirms (sweepAoPending)
      delete _aoPending[key];
      updateAoInput(devId, ctrl);
      if (j && j.error === 'unauthorized') showToast('Нет доступа', 'err');
      else if (j && j.error === 'publish_failed') showToast('Брокер MQTT недоступен', 'err');
      else showToast('Ошибка: ' + ((j && j.error) || 'unknown'), 'err');
    })
    .catch(() => {
      delete _aoPending[key];
      updateAoInput(devId, ctrl);
      showToast('Нет связи с сервером', 'err');
    });
}

/** Numeric setpoint input for an AO channel row (write on Enter / blur). */
function buildAoSetpointInput(dev, ctrl, enabled) {
  const inp = h('input', {
    'type': 'number',
    'class': 'mqtt-ao-input',
    'min': '0',
    'max': '1000',
    'step': '1',
    'inputmode': 'numeric',
    'data-ao-input': `${dev.id}:${ctrl}`,
    'onfocus': () => aoEditGuardAdd(dev.id, ctrl),
    'onblur': () => { aoEditGuardReleaseLater(dev.id, ctrl); setAoOutput(dev, ctrl); },
    'onkeydown': e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        setAoOutput(dev, ctrl);
        if (e.target && typeof e.target.blur === 'function') e.target.blur();
      }
    },
  });
  inp.dataset.chDisabled = enabled ? '0' : '1';
  updateAoInput(dev.id, ctrl, inp);
  return inp;
}

/** A disabled channel is excluded from polling — no echo can come, so the input goes inert. */
function setAoInputChannelEnabled(devId, ctrl, enabled) {
  const inp = aoInputEl(devId, ctrl);
  if (!inp) return;
  inp.dataset.chDisabled = enabled ? '0' : '1';
  updateAoInput(devId, ctrl, inp);
}

function mr02mAiNMirrorActive(dev, ch, channels, mtCode) {
  if (!mr02mAiIsNLeg(mtCode, ch)) return false;
  const parent = mr02mAiPairParent(ch);
  if (!parent) return false;
  const pEff = mr02mAiEffectiveSensorType(dev, parent, channels);
  return mr02mAiMirrorTypeToN(pEff);
}

function refreshAiTypeSelects(dev) {
  if (!dev || dev.type !== 'mr02m') return;
  const body = document.getElementById(`acc-body-${dev.id}`);
  if (!body) return;
  const mtCode = getModuleTypeCode(dev);
  const channels = dev.channels || {};
  const aiCount = (MR02M_TYPES[mtCode] || {}).ai || 0;
  for (let i = 1; i <= aiCount; i++) {
    const effType = mr02mAiEffectiveSensorType(dev, i, channels);
    const row = body.querySelector(`.mqtt-ch-row[data-ai-ch="${i}"]`);
    if (!row) continue;
    const sel = row.querySelector('select.mqtt-select-small');
    if (!sel) continue;
    if (aiTypeIsEditBlocked(dev.id, i, sel)) continue;
    sel.value = String(effType);
    const nMirror = mr02mAiNMirrorActive(dev, i, channels, mtCode);
    sel.disabled = !!nMirror;
    sel.title = nMirror
      ? 'Тип наследуется с P-канала (ТХА / 3-проводный RTD)'
      : '';
    const ctrl = `ai_${i}`;
    const unitEl = row.querySelector(`[data-unit="${dev.id}:${ctrl}"]`);
    if (unitEl) {
      const u = aiUnitsForCode(effType);
      unitEl.setAttribute('data-unit-default', u);
      unitEl.textContent = liveUnitFor(dev.id, ctrl, u);
      unitEl.hidden = !u ? '' : undefined;
    }
  }
}

async function prefetchDeviceLive(devId) {
  const data = await apiGet(
    `cgi-bin/mqtt_live.cgi?device=${encodeURIComponent(devId)}`).catch(() => null);
  if (!data || !data.ok) return null;
  if (data.sensor_types && typeof data.sensor_types === 'object') {
    _liveSensorTypes[devId] = Object.assign(Object.create(null), data.sensor_types);
    aiTypeReconcilePending(devId, data.sensor_types);
  }
  for (const [ctrl, val] of Object.entries(data.controls || {})) {
    if (ctrl === 'uptime_s') setLiveUptimeAnchor(devId, val);
    setLiveValue(devId, ctrl, val, false, {skipDom: true});
  }
  for (const [ctrl, u] of Object.entries(data.units || {})) {
    if (!_liveUnits[devId]) _liveUnits[devId] = Object.create(null);
    _liveUnits[devId][ctrl] = String(u);
  }
  for (const [ctrl, err] of Object.entries(data.errors || {})) {
    setLiveValue(devId, ctrl, err, true, {skipDom: true});
  }
  const dev = (_config.devices || []).find(d => d.id === devId);
  if (dev) refreshAiTypeSelects(dev);
  refreshLiveCellsForDevice(devId);
  sweepDoPending(devId);
  sweepAoPending(devId);
  return data;
}

function stopUptimeTick() {
  if (_uptimeTickTimer) {
    clearInterval(_uptimeTickTimer);
    _uptimeTickTimer = null;
  }
}

function startUptimeTick() {
  stopUptimeTick();
  _uptimeTickTimer = setInterval(() => {
    const devId = _channelPollDevId;
    if (!devId || !isAccordionOpen(devId)) return;
    updateLiveCell(devId, 'uptime_s');
    // Pending expiry must not depend on a successful poll (mqtt_live may be failing).
    sweepDoPending(devId);
    sweepAoPending(devId);
  }, 1000);
}

function stopChannelPoll() {
  if (_channelPollTimer) {
    clearInterval(_channelPollTimer);
    _channelPollTimer = null;
  }
  _channelPollDevId = null;
  stopUptimeTick();
}

function startChannelPoll(devId) {
  stopChannelPoll();
  if (!devId) return;
  _channelPollDevId = devId;
  _channelPollTimer = setInterval(() => {
    if (_channelPollDevId === devId && isAccordionOpen(devId)) {
      prefetchDeviceLive(devId);
    }
  }, 1500);
  startUptimeTick();
}

function liveSpan(devId, controlName, unit) {
  const rec = _liveByDevice[devId] && _liveByDevice[devId][controlName];
  const u = liveUnitFor(devId, controlName, unit || '');
  let display;
  if (controlName === 'uptime_s') {
    const s = effectiveUptimeSec(devId);
    display = s == null ? formatLiveDisplay(controlName, rec) : formatUptimeSeconds(s);
  } else {
    display = formatLiveDisplay(controlName, rec);
  }
  const wrap = h('span', {'class': 'mqtt-ch-live-wrap'});
  wrap.appendChild(h('span', {
    'class': 'mqtt-ch-live' + (rec && rec.isError ? ' mqtt-ch-live-err' : rec ? ' mqtt-ch-live-ok' : ''),
    'data-live': `${devId}:${controlName}`,
    'title': 'Текущее значение с шины (MQTT)',
  }, display));
  wrap.appendChild(h('span', {
    'class': 'mqtt-ch-unit',
    'data-unit': `${devId}:${controlName}`,
    'data-unit-default': unit || '',
    hidden: !u ? '' : undefined,
  }, u));
  return wrap;
}

function ingestMonitorTopic(topic, value) {
  const unitsM = /^\/devices\/([^/]+)\/controls\/([^/]+)\/meta\/units$/.exec(topic);
  if (unitsM) {
    if (!_liveUnits[unitsM[1]]) _liveUnits[unitsM[1]] = Object.create(null);
    _liveUnits[unitsM[1]][unitsM[2]] = String(value);
    scheduleLiveDomRefresh(unitsM[1]);
    return;
  }
  const errM = /^\/devices\/([^/]+)\/controls\/([^/]+)\/meta\/error$/.exec(topic);
  if (errM) {
    setLiveValue(errM[1], errM[2], value, value !== '' && value != null);
    return;
  }
  const m = /^\/devices\/([^/]+)\/controls\/([^/]+)$/.exec(topic);
  if (!m || m[2] === 'module_type' || m[2] === 'connection') return;
  if (m[2] === 'uptime_s') setLiveUptimeAnchor(m[1], value);
  setLiveValue(m[1], m[2], value, false);
}

function getOrCreateSysChannel(dev, item) {
  if (!dev.channels) dev.channels = {};
  if (!dev.channels.sys) dev.channels.sys = [];
  let ch = dev.channels.sys.find(c => c.key === item.key);
  if (!ch) {
    ch = {key: item.key, label: item.label, enabled: true};
    dev.channels.sys.push(ch);
  }
  return ch;
}

function buildChannelWidget(title) {
  const widget = h('div', {'class': 'widget mqtt-ch-widget'});
  widget.appendChild(h('div', {'class': 'widget-title'}, title));
  const body = h('div', {'class': 'mqtt-ch-widget-body'});
  widget.appendChild(body);
  return {widget, body};
}

function buildSysChannelGroup(dev, title, vars) {
  const pack = buildChannelWidget(title);
  for (const item of vars) {
    const chCfg = getOrCreateSysChannel(dev, item);
    const topic = topicPath(dev.id, item.key);
    pack.body.appendChild(h('div', {'class': 'mqtt-ch-row'},
      h('label', {'class': 'mqtt-ch-toggle'},
        h('input', {
          'type': 'checkbox',
          checked: chCfg.enabled !== false ? '' : undefined,
          'onchange': e => {
            chCfg.enabled = e.target.checked;
            onPollConfigChanged(dev.id);
          },
        }),
      ),
      h('input', {
        'type': 'text',
        'class': 'mqtt-ch-label-input',
        'placeholder': item.label,
        'value': chCfg.label || item.label,
        'oninput': e => {
          chCfg.label = e.target.value;
          markUnsaved();
        },
      }),
      h('span', {'class': 'topic-preview mono', 'title': topic}, item.key),
      liveSpan(dev.id, item.key, item.unit || ''),
    ));
  }
  return pack.widget;
}

function mqttChannelWidgetsRow() {
  return h('div', {'class': 'mqtt-ch-widgets dash-grid'});
}

// ── API calls ─────────────────────────────────────────────────────────────────
async function apiGet(url) {
  const r = await fetch(url, { credentials: 'include', cache: 'no-store' });
  const data = await r.json().catch(() => null);
  if (!r.ok || !data) return null;
  if (data.error) return data;
  return data;
}

async function apiPost(url, data) {
  const r = await fetch(url, {
    method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json'},
    body: typeof data === 'string' ? data : JSON.stringify(data),
  });
  const text = await r.text();
  let parsed = null;
  try { parsed = JSON.parse(text); } catch (_) { /* non-JSON (nginx 403 page) */ }
  if (!r.ok) {
    const err = (parsed && parsed.error) ? parsed.error : ('HTTP ' + r.status);
    return { ok: false, error: err };
  }
  return parsed || { ok: false, error: 'invalid_response' };
}

// ── External MQTT credentials (tab UI) ───────────────────────────────────────
const MQTT_PASS_MASK = '******';
let _mqttClientInfoBound = false;

async function copyTextToClipboard(text) {
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }
}

function renderMqttClientInfo(st) {
  const hostEl = document.getElementById('mqtt-ext-host');
  const portEl = document.getElementById('mqtt-ext-port');
  const userEl = document.getElementById('mqtt-ext-user');
  const passEl = document.getElementById('mqtt-ext-pass');
  const host = (st.host || '').trim() || window.location.hostname || '—';
  const port = String(st.port_external != null ? st.port_external : 1884);
  const user = (st.mqtt_user || 'mqttuser').trim();
  const secret = (st.mqtt_password || '').trim();

  if (hostEl) {
    hostEl.textContent = host;
    hostEl.dataset.copyText = host === '—' ? '' : host;
  }
  if (portEl) {
    portEl.textContent = port;
    portEl.dataset.copyText = port;
  }
  if (userEl) {
    userEl.textContent = user;
    userEl.dataset.copyText = user;
  }
  if (passEl) {
    passEl.dataset.secret = secret;
    passEl.dataset.revealed = '0';
    passEl.classList.remove('mqtt-secret-revealed');
    if (secret) {
      passEl.textContent = MQTT_PASS_MASK;
      passEl.title = 'Нажмите, чтобы показать и скопировать';
    } else {
      passEl.textContent = '—';
      passEl.title = 'Пароль не получен с устройства; нажмите для повторной проверки';
    }
  }
}

async function fetchMqttExternalCredentials() {
  const st = await apiGet('cgi-bin/mqtt_status.cgi').catch(() => null);
  if (!st || st.error) return null;
  return st;
}

async function resolveMqttPassword(passEl) {
  let secret = (passEl.dataset.secret || '').trim();
  if (secret) return secret;
  const st = await fetchMqttExternalCredentials();
  if (!st) return '';
  secret = (st.mqtt_password || '').trim();
  if (secret) {
    passEl.dataset.secret = secret;
    if (passEl.dataset.revealed !== '1') {
      passEl.textContent = MQTT_PASS_MASK;
      passEl.title = 'Нажмите, чтобы показать и скопировать';
    }
  }
  return secret;
}

function bindMqttClientInfoCells() {
  if (_mqttClientInfoBound) return;
  _mqttClientInfoBound = true;

  document.addEventListener('click', async (e) => {
    const el = e.target.closest('.mqtt-copy-cell');
    if (!el) return;
    const text = el.dataset.copyText || el.textContent || '';
    if (!text || text === '—') {
      showToast('Нечего копировать', 'warn');
      return;
    }
    if (await copyTextToClipboard(text)) {
      if (el.dataset.copyToast === 'topic') showToast('Топик скопирован', 'ok');
      else showToast('Скопировано: ' + text, 'ok');
    } else {
      showToast('Не удалось скопировать', 'err');
    }
  });

  const passEl = document.getElementById('mqtt-ext-pass');
  if (passEl) {
    passEl.addEventListener('click', async () => {
      const secret = await resolveMqttPassword(passEl);
      if (!secret) {
        showToast(
          'Пароль MQTT не найден. На устройстве проверьте /etc/sa02m_mqtt.env (MQTT_PASS=…) или выполните scripts/05-mqtt.sh.',
          'warn'
        );
        return;
      }
      passEl.textContent = secret;
      passEl.dataset.revealed = '1';
      passEl.classList.add('mqtt-secret-revealed');
      if (await copyTextToClipboard(secret)) showToast('Пароль скопирован', 'ok');
      else showToast('Не удалось скопировать пароль', 'err');
    });
    passEl.addEventListener('blur', () => {
      if (passEl.dataset.revealed === '1') {
        passEl.textContent = MQTT_PASS_MASK;
        passEl.dataset.revealed = '0';
        passEl.classList.remove('mqtt-secret-revealed');
      }
    });
  }
}

// ── Broker status ─────────────────────────────────────────────────────────────
let _lastMqttBrokerStatus = null;

function mqttBridgeUiState(st) {
  if (!st || st.error) return 'unknown';
  if (st.bridge_disabled) return 'disabled';
  return st.bridge_active ? 'active' : 'inactive';
}

function applyBrokerStatusUI(st) {
  const badge = document.getElementById('mqtt-broker-badge');
  const clients = document.getElementById('mqtt-broker-clients');
  const bridgeBadge = document.getElementById('mqtt-bridge-badge');

  if (!st || st.error) {
    if (badge) {
      badge.className = 'badge badge-err';
      badge.textContent = st && st.error === 'unauthorized' ? uiT('Нет доступа') : uiT('Нет данных');
    }
    if (bridgeBadge) {
      bridgeBadge.className = 'badge badge-unk';
      bridgeBadge.textContent = uiT('Мост —');
    }
    if (clients) clients.textContent = uiT('Клиентов: —');
    return;
  }

  if (badge) {
    if (st.mosquitto_disabled) {
      badge.className = 'badge badge-err';
      badge.textContent = uiT('Отключен');
    } else {
      badge.className = `badge ${st.mosquitto_active ? 'badge-ok' : 'badge-err'}`;
      badge.textContent = st.mosquitto_active ? uiT('Работает') : uiT('Остановлен');
    }
  }
  if (clients) clients.textContent = uiT(`Клиентов: ${st.clients_connected}`);
  if (bridgeBadge) {
    const bridgeSt = mqttBridgeUiState(st);
    if (bridgeSt === 'disabled') {
      bridgeBadge.className = 'badge badge-err';
      bridgeBadge.textContent = uiT('Отключен');
    } else if (bridgeSt === 'active') {
      bridgeBadge.className = 'badge badge-ok';
      bridgeBadge.textContent = uiT('Мост активен');
    } else {
      bridgeBadge.className = 'badge badge-unk';
      bridgeBadge.textContent = uiT('Мост остановлен');
    }
  }
  const bridgeToggle = document.getElementById('mqtt-bridge-toggle-btn');
  if (bridgeToggle) {
    const on = mqttBridgeUiState(st) === 'active';
    bridgeToggle.textContent = uiT(on ? 'Остановить' : 'Запустить');
    bridgeToggle.className = on
      ? 'btn btn-sm hw-io-btn hw-io-to-off'
      : 'btn btn-sm hw-io-btn hw-io-to-on';
    bridgeToggle.title = uiT(on
      ? 'Остановить мост Modbus→MQTT (не запустится после перезагрузки)'
      : 'Запустить мост Modbus→MQTT');
  }
  renderMqttClientInfo(st);
  bindMqttClientInfoCells();
}

async function refreshBrokerStatus() {
  const st = await apiGet('cgi-bin/mqtt_status.cgi');
  _lastMqttBrokerStatus = st;
  applyBrokerStatusUI(st);
}

window.mqttRefreshI18n = function () {
  if (_lastMqttBrokerStatus) applyBrokerStatusUI(_lastMqttBrokerStatus);
};

// ── Load config ───────────────────────────────────────────────────────────────
async function loadConfig() {
  const data = await apiGet('cgi-bin/mqtt_config.cgi').catch(() => null);
  if (data && !data.error) {
    _config = data;
    for (const dev of _config.devices || []) {
      migrateDeviceLegacyAiSensorTypes(dev);
      normalizeMr02mAiPairsAll(dev);
    }
    renderDeviceList();
    renderAccordion();
    return;
  }
  const msg = (data && data.error) ? data.error : 'не удалось загрузить конфигурацию (CGI недоступен)';
  showToast('Ошибка загрузки MQTT: ' + msg, 'err');
}

// ── Device list table ─────────────────────────────────────────────────────────
function refreshMonitorFilterOptions() {
  const sel = document.getElementById('mqtt-monitor-filter');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '';
  sel.appendChild(h('option', {value: ''}, '— выберите устройство —'));
  for (const dev of _config.devices || []) {
    sel.appendChild(h('option', {value: dev.id}, formatDeviceDisplayName(dev)));
  }
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
}

function renderDeviceList() {
  const tbody = document.getElementById('mqtt-device-tbody');
  if (!tbody) return;
  pruneDeviceListSelection();
  tbody.innerHTML = '';
  refreshMonitorFilterOptions();
  for (const dev of _config.devices || []) {
    const ioEnabled = countChannelsEnabled(dev);
    const ioTotal = countChannelsTotal(dev);
    const pollCount = countPollEnabled(dev);
    const comName = (dev.port || '').replace('/dev/', '');
    const topic = deviceTopicFilter(dev.id);
    const rowClass = 'is-selectable' + (_selectedDeviceIds.has(dev.id) ? ' is-selected' : '');
    const tr = h('tr', {'data-device-id': dev.id, 'class': rowClass},
      h('td', {}, deviceTypeBadge(dev.type)),
      h('td', {}, formatDeviceDisplayName(dev)),
      h('td', {'class':'mono'}, comName),
      h('td', {'class':'mono'}, String(dev.address || '—')),
      h('td', {'class': 'mqtt-io-count'}, `${ioEnabled}/${ioTotal}`),
      h('td', {'class': 'mqtt-poll-count'}, String(pollCount)),
      h('td', {},
        h('span', {
          'class': 'mono mqtt-copy-cell mqtt-device-topic',
          'data-copy-text': topic,
          'data-copy-toast': 'topic',
          'title': uiT('Нажмите, чтобы скопировать'),
        }, topic)
      ),
      h('td', {},
        h('button', {'class':'btn btn-sm', 'onclick': () => removeDevice(dev.id)}, 'Удалить')
      )
    );
    tr.addEventListener('click', (ev) => {
      if (ev.target && ev.target.closest && ev.target.closest('button, input, select, label, a')) return;
      toggleDeviceListSelection(dev.id);
      renderDeviceList();
    });
    tbody.appendChild(tr);
  }
}

function refreshDeviceListCounts(devId) {
  const dev = (_config.devices || []).find(d => d.id === devId);
  if (!dev) return;
  const tr = document.querySelector(`#mqtt-device-tbody tr[data-device-id="${devId}"]`);
  if (!tr) return;
  const ioTd = tr.querySelector('.mqtt-io-count');
  const pollTd = tr.querySelector('.mqtt-poll-count');
  if (ioTd) ioTd.textContent = `${countChannelsEnabled(dev)}/${countChannelsTotal(dev)}`;
  if (pollTd) pollTd.textContent = String(countPollEnabled(dev));
}

function onPollConfigChanged(devId) {
  markUnsaved();
  refreshDeviceListCounts(devId);
}

function deviceTypeBadge(type) {
  const labels = {mr02m:'МР-02м', dtv:'ДТВ-RS-485', ce02m3:'СЭ-02м-3'};
  return h('span', {'class':'badge badge-info'}, labels[type] || type);
}

function mr02mProfile(dev) {
  return MR02M_TYPES[getModuleTypeCode(dev)] || {do: 0, di: 0, ao: 0, ai: 0};
}

/** Физический канал включён, если в YAML нет записи или enabled !== false (как getOrCreateChannel). */
function mr02mPhysicalChannelEnabled(channels, kind, idx) {
  const ch = (channels?.[kind] || []).find(c => c.ch === idx);
  return ch ? ch.enabled !== false : true;
}

function countChannelsEnabled(dev) {
  if (dev.type === 'mr02m') {
    const mt = mr02mProfile(dev);
    const channels = dev.channels || {};
    let n = 0;
    for (let i = 1; i <= mt.do; i++) if (mr02mPhysicalChannelEnabled(channels, 'do', i)) n++;
    for (let i = 1; i <= mt.di; i++) if (mr02mPhysicalChannelEnabled(channels, 'di', i)) n++;
    for (let i = 1; i <= mt.ao; i++) if (mr02mPhysicalChannelEnabled(channels, 'ao', i)) n++;
    for (let i = 1; i <= mt.ai; i++) if (mr02mPhysicalChannelEnabled(channels, 'ai', i)) n++;
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
    const mt = mr02mProfile(dev);
    return mt.do + mt.di + mt.ao + mt.ai;
  }
  if (dev.type === 'dtv') return DTV_SENSORS.length;
  if (dev.type === 'ce02m3') return CE02M3_CHANNELS.length;
  return 0;
}

function sysVarsForDevice(dev) {
  if (dev.type === 'mr02m') return MR02M_SYS_VARS;
  if (dev.type === 'dtv') return DTV_SYS_VARS;
  if (dev.type === 'ce02m3') return CE02M3_SYS_VARS;
  return [];
}

/** Системный канал включён для опроса (enabled !== false, как в modbus_mqtt_bridge._sys_enabled). */
function sysChannelPollEnabled(dev, key) {
  const sys = dev.channels?.sys;
  if (!sys || !Array.isArray(sys)) return true;
  const ch = sys.find(c => c.key === key);
  return ch ? ch.enabled !== false : true;
}

function countSysPollEnabled(dev) {
  let n = 0;
  for (const item of sysVarsForDevice(dev)) {
    if (sysChannelPollEnabled(dev, item.key)) n++;
  }
  return n;
}

/** Число MQTT-контролов с активным опросом: системные + физические I/O / датчики. */
function countPollEnabled(dev) {
  let n = countSysPollEnabled(dev);
  if (dev.type === 'mr02m' || dev.type === 'ce02m3') {
    n += countChannelsEnabled(dev);
  } else if (dev.type === 'dtv') {
    n += (dev.sensors_present || []).length;
  }
  return n;
}

// ── Accordion ─────────────────────────────────────────────────────────────────
function renderAccordion() {
  const container = document.getElementById('mqtt-accordion');
  if (!container) return;
  stopChannelPoll();
  _accordionBuilt.clear();
  container.innerHTML = '';
  for (const dev of _config.devices || []) {
    container.appendChild(buildDeviceSection(dev));
  }
}

function buildDeviceChannels(dev, container) {
  if (dev.type === 'mr02m') buildMR02mChannels(dev, container);
  else if (dev.type === 'dtv') buildDTVChannels(dev, container);
  else if (dev.type === 'ce02m3') buildCE02M3Channels(dev, container);
}

function ensureAccordionBody(dev) {
  if (_accordionBuilt.has(dev.id)) return;
  const body = document.getElementById(`acc-body-${dev.id}`);
  if (!body) return;
  body.innerHTML = '';
  buildDeviceChannels(dev, body);
  _accordionBuilt.add(dev.id);
}

function buildDeviceSection(dev) {
  const title = formatDeviceDisplayName(dev);
  const body = h('div', {
    'class': 'mqtt-accordion-body',
    'id': `acc-body-${dev.id}`,
    'hidden': '',
    'data-device-id': dev.id,
  });
  const header = h('div', {'class':'mqtt-accordion-header', 'onclick': () => toggleAccordion(dev.id)},
    h('span', {'class':'mqtt-accordion-arrow', 'id': `acc-arrow-${dev.id}`}, '+'),
    h('span', {}, title)
  );
  return h('div', {'class':'mqtt-accordion-section'}, header, body);
}

function toggleAccordion(id) {
  const body = document.getElementById(`acc-body-${id}`);
  const arrow = document.getElementById(`acc-arrow-${id}`);
  if (!body) return;
  const opening = body.hasAttribute('hidden');
  if (opening) {
    document.querySelectorAll('.mqtt-accordion-body').forEach(el => {
      if (el.id !== `acc-body-${id}`) el.setAttribute('hidden', '');
    });
    document.querySelectorAll('.mqtt-accordion-arrow').forEach(el => {
      if (el.id !== `acc-arrow-${id}`) el.textContent = '+';
    });
    body.removeAttribute('hidden');
    if (arrow) arrow.textContent = '-';
    const dev = (_config.devices || []).find(d => d.id === id);
    if (dev) void openDeviceChannels(dev);
    return;
  }
  body.setAttribute('hidden', '');
  if (arrow) arrow.textContent = '+';
  if (_channelPollDevId === id) stopChannelPoll();
}

async function openDeviceChannels(dev) {
  const devId = dev.id;
  const body = document.getElementById(`acc-body-${devId}`);
  if (body) body.classList.add('mqtt-ch-loading');
  ensureAccordionBody(dev);
  await prefetchDeviceLive(devId);
  if (body) body.classList.remove('mqtt-ch-loading');
  if (isAccordionOpen(devId)) startChannelPoll(devId);
}

function appendMr02mDoGroup(dev, channels, count) {
  const pack = buildChannelWidget('DO — дискретные выходы');
  for (let i = 1; i <= count; i++) {
    const chCfg = getOrCreateChannel(channels, 'do', i);
    const ctrl = `do_${i}`;
    const row = buildChannelRow(chCfg, topicPath(dev.id, ctrl), 'do', i, dev, ctrl, '');
    row.appendChild(buildDoToggleBtn(dev, ctrl, chCfg.enabled !== false));
    const cb = row.querySelector('.mqtt-ch-toggle input[type=checkbox]');
    if (cb) cb.addEventListener('change', e => setDoToggleChannelEnabled(dev.id, ctrl, e.target.checked));
    pack.body.appendChild(row);
  }
  return pack.widget;
}

function syncMr02mDiCounterFlagsFromDom(dev) {
  if (dev.type !== 'mr02m') return;
  const body = document.getElementById(`acc-body-${dev.id}`);
  if (!body) return;
  ensureMr02mChannels(dev);
  body.querySelectorAll('.mqtt-ch-row[data-di-ch]').forEach(row => {
    const ch = Number(row.getAttribute('data-di-ch'));
    if (!ch) return;
    const cb = row.querySelector('.mqtt-counter-label input[type=checkbox]');
    if (!cb) return;
    getOrCreateChannel(dev.channels, 'di', ch).counter = cb.checked;
  });
}

function appendMr02mDiGroup(dev, channels, count) {
  const pack = buildChannelWidget('DI — дискретные входы');
  for (let i = 1; i <= count; i++) {
    const chCfg = getOrCreateChannel(channels, 'di', i);
    const row = buildChannelRow(chCfg, topicPath(dev.id, `di_${i}`), 'di', i, dev, `di_${i}`, '');
    row.setAttribute('data-di-ch', String(i));
    const countCtrl = `di_${i}_count`;
    const countLive = liveSpan(dev.id, countCtrl, '');
    row.appendChild(h('label', {'class': 'mqtt-counter-label'},
      h('input', {'type': 'checkbox', checked: chCfg.counter !== false ? '' : undefined,
        'onchange': e => {
          chCfg.counter = e.target.checked;
          updateLiveCell(dev.id, countCtrl);
          markUnsaved();
        }}),
      ' счётчик',
    ));
    row.appendChild(countLive);
    pack.body.appendChild(row);
  }
  return pack.widget;
}

function appendMr02mAoGroup(dev, channels, count) {
  const pack = buildChannelWidget('AO — аналоговые выходы');
  for (let i = 1; i <= count; i++) {
    const chCfg = getOrCreateChannel(channels, 'ao', i);
    const ctrl = `ao_${i}`;
    const row = buildChannelRow(chCfg, topicPath(dev.id, ctrl), 'ao', i, dev, ctrl, 'В');
    row.appendChild(buildAoSetpointInput(dev, ctrl, chCfg.enabled !== false));
    const cb = row.querySelector('.mqtt-ch-toggle input[type=checkbox]');
    if (cb) cb.addEventListener('change', e => setAoInputChannelEnabled(dev.id, ctrl, e.target.checked));
    pack.body.appendChild(row);
  }
  return pack.widget;
}

/** Static AI row sizes (type "Выключен" one-line). Survives stale main.css cache. */
function ensureAiRowLayoutStyles() {
  if (document.getElementById('mqtt-ai-row-layout-css')) return;
  const style = document.createElement('style');
  style.id = 'mqtt-ai-row-layout-css';
  style.textContent = [
    '.mqtt-ch-row[data-ai-ch],.mqtt-ch-row-ai{flex-wrap:nowrap!important;min-width:0}',
    '.mqtt-ch-row[data-ai-ch] .mqtt-ch-label-input,.mqtt-ch-row-ai .mqtt-ch-label-input{',
    'flex:0 0 7.5rem!important;width:7.5rem!important;min-width:7.5rem!important;max-width:7.5rem!important;box-sizing:border-box}',
    '.mqtt-ch-row[data-ai-ch] .topic-preview,.mqtt-ch-row-ai .topic-preview{',
    'flex:0 0 auto;max-width:2.75rem;overflow:hidden;text-overflow:ellipsis}',
    '.mqtt-ch-row[data-ai-ch] .mqtt-select-small,.mqtt-ch-row[data-ai-ch] .mqtt-ai-type-select,',
    '.mqtt-ch-row-ai .mqtt-select-small,.mqtt-ch-row-ai .mqtt-ai-type-select{',
    'flex:0 0 14.5rem!important;width:14.5rem!important;min-width:14.5rem!important;max-width:14.5rem!important;',
    'box-sizing:border-box;field-sizing:fixed;overflow:hidden;text-overflow:ellipsis}',
    '.mqtt-ch-row[data-ai-ch] .mqtt-ch-live,.mqtt-ch-row-ai .mqtt-ch-live{',
    'width:3.5rem!important;min-width:3.5rem!important;max-width:3.5rem!important}',
    '.mqtt-ch-row[data-ai-ch] .mqtt-ch-live-wrap,.mqtt-ch-row-ai .mqtt-ch-live-wrap{',
    'margin-left:auto;flex:0 0 auto;flex-shrink:0}',
  ].join('');
  document.head.appendChild(style);
}

function appendMr02mAiGroup(dev, channels, mtCode, count) {
  ensureAiRowLayoutStyles();
  const pack = buildChannelWidget('AI — аналоговые входы');
  for (let i = 1; i <= count; i++) {
    const chCfg = getOrCreateChannel(channels, 'ai', i);
    const ctrl = `ai_${i}`;
    const effType = mr02mAiEffectiveSensorType(dev, i, channels);
    const row = buildChannelRow(
      chCfg, topicPath(dev.id, ctrl), 'ai', i, dev, ctrl, '', {skipLive: true});
    row.classList.add('mqtt-ch-row-ai');
    row.setAttribute('data-ai-ch', String(i));
    const sel = h('select', {'class': 'mqtt-select-small mqtt-ai-type-select',
      'onfocus': () => aiTypeEditGuardAdd(dev.id, i),
      'onblur': () => aiTypeEditGuardReleaseLater(dev.id, i),
      'onchange': e => {
        const code = Number(e.target.value);
        chCfg.sensor_type = code;
        aiTypeSetPending(dev.id, i, code);
        if (!mr02mAiIsNLeg(mtCode, i)) syncMr02mPairAfterParentChange(dev, i);
        refreshAiTypeSelects(dev);
        markUnsaved();
      }});
    const nMirror = mr02mAiNMirrorActive(dev, i, channels, mtCode);
    if (nMirror) {
      chCfg.sensor_type = effType;
      sel.disabled = true;
      sel.title = 'Тип наследуется с P-канала (ТХА / 3-проводный RTD)';
    }
    for (const s of AI_SENSOR_LABELS) {
      const opt = h('option', {'value': String(s.code)}, s.label);
      if (s.code === effType) opt.selected = true;
      sel.appendChild(opt);
    }
    row.appendChild(sel);
    row.appendChild(liveSpan(dev.id, ctrl, aiUnitsForCode(effType)));
    pack.body.appendChild(row);
  }
  return pack.widget;
}

function ensureMr02mChannels(dev) {
  if (dev.type !== 'mr02m') return;
  const mt = mr02mProfile(dev);
  const channels = dev.channels || (dev.channels = {});
  for (let i = 1; i <= mt.do; i++) getOrCreateChannel(channels, 'do', i);
  for (let i = 1; i <= mt.di; i++) getOrCreateChannel(channels, 'di', i);
  for (let i = 1; i <= mt.ao; i++) getOrCreateChannel(channels, 'ao', i);
  for (let i = 1; i <= mt.ai; i++) getOrCreateChannel(channels, 'ai', i);
}

function buildMR02mChannels(dev, container) {
  ensureMr02mChannels(dev);
  const channels = dev.channels || {};
  const mtCode = getModuleTypeCode(dev);
  const mt = MR02M_TYPES[mtCode] || {do:6, di:8, ao:0, ai:0};

  const row = mqttChannelWidgetsRow();
  container.appendChild(row);

  row.appendChild(buildSysChannelGroup(dev, 'Системные', MR02M_SYS_VARS));
  if (mt.do > 0) row.appendChild(appendMr02mDoGroup(dev, channels, mt.do));
  if (mt.di > 0) row.appendChild(appendMr02mDiGroup(dev, channels, mt.di));
  if (mt.ai > 0) row.appendChild(appendMr02mAiGroup(dev, channels, mtCode, mt.ai));
  if (mt.ao > 0) row.appendChild(appendMr02mAoGroup(dev, channels, mt.ao));
}

function buildDTVChannels(dev, container) {
  if (!dev.sensors_present) dev.sensors_present = DTV_SENSORS.map(s => s.key);

  const row = mqttChannelWidgetsRow();
  container.appendChild(row);
  row.appendChild(buildSysChannelGroup(dev, 'Системные', DTV_SYS_VARS));

  const groups = {};
  for (const s of DTV_SENSORS) {
    if (!groups[s.group]) {
      const label = {temperature:'Температура',humidity:'Влажность / Давление',
        pressure:'Давление', iaq:'Качество воздуха',presence:'Присутствие LD2412',
        outputs:'Выходы'}[s.group] || s.group;
      const pack = buildChannelWidget(label);
      groups[s.group] = pack.body;
      row.appendChild(pack.widget);
    }
    const enabled = dev.sensors_present.includes(s.key);
    const topic = topicPath(dev.id, s.key);
    // chRow, not row: an inner `const row` shadowed the outer widgets-row used
    // earlier in this block (TDZ) and broke the whole DTV card body build.
    const chRow = h('div', {'class':'mqtt-ch-row'},
      h('label', {'class':'mqtt-ch-toggle'},
        h('input', {'type':'checkbox', checked: enabled ? '' : undefined,
          'onchange': e => {
            if (e.target.checked) {
              if (!dev.sensors_present.includes(s.key)) dev.sensors_present.push(s.key);
            } else {
              dev.sensors_present = dev.sensors_present.filter(k => k !== s.key);
            }
            onPollConfigChanged(dev.id);
          }}),
        h('span', {'class':'mqtt-ch-name'}, s.label)
      ),
      h('span', {'class':'topic-preview', 'title': topic}, topic),
      liveSpan(dev.id, s.key, s.unit || ''),
    );
    // DTV writable coils (bridge: DTV_COILS) get the same toggle as DO rows.
    if (s.key === 'buzzer' || s.key === 'leds') {
      chRow.appendChild(buildDoToggleBtn(dev, s.key, enabled));
      const cb = chRow.querySelector('.mqtt-ch-toggle input[type=checkbox]');
      if (cb) cb.addEventListener('change', e => setDoToggleChannelEnabled(dev.id, s.key, e.target.checked));
    }
    groups[s.group].appendChild(chRow);
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

  const row = mqttChannelWidgetsRow();
  container.appendChild(row);
  row.appendChild(buildSysChannelGroup(dev, 'Системные', CE02M3_SYS_VARS));

  const ctPack = buildChannelWidget('Параметры CT');
  // Keep label + value on one row (widget .mqtt-form-row defaults to column).
  ctPack.body.appendChild(h('div', {'class': 'mqtt-form-row mqtt-ct-ratio-row'},
    h('label', {}, 'Коэффициент CT (K×1000):'),
    h('input', {'type': 'number', 'class': 'mqtt-input-small', 'value': dev.ct_ratio || 4000,
      'oninput': e => { dev.ct_ratio = Number(e.target.value); markUnsaved(); }}),
  ));
  ctPack.body.appendChild(h('div', {'class': 'mqtt-ct-hint'},
    '(4000 = CT 4А, 1000 = 1А)'));
  row.appendChild(ctPack.widget);

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
    const pack = buildChannelWidget(gl);
    groups[gk] = pack.body;
    row.appendChild(pack.widget);
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
            if (enKey) { en[enKey] = e.target.checked; onPollConfigChanged(dev.id); }
          }}),
        h('span', {'class':'mqtt-ch-name'}, ch.label)
      ),
      h('span', {'class':'topic-preview', 'title': topic}, topic),
      liveSpan(dev.id, ch.key, CE02M3_UNITS[ch.key] || ''),
    );
    groups[gk].appendChild(row);
  }
}

function buildChannelRow(chCfg, topicHint, kind, idx, dev, controlName, unit, opts) {
  const ctrl = controlName || `${kind}_${idx}`;
  const row = h('div', {'class': 'mqtt-ch-row'},
    h('label', {'class': 'mqtt-ch-toggle'},
      h('input', {
        'type': 'checkbox',
        checked: chCfg.enabled !== false ? '' : undefined,
        'onchange': e => {
          chCfg.enabled = e.target.checked;
          onPollConfigChanged(dev.id);
        },
      }),
    ),
    h('input', {
      'type': 'text',
      'class': 'mqtt-ch-label-input',
      'placeholder': `${kind.toUpperCase()}${idx}`,
      'value': chCfg.label || '',
      'oninput': e => {
        chCfg.label = e.target.value;
        markUnsaved();
      },
    }),
    h('span', {'class': 'topic-preview mono', 'title': topicHint}, ctrl),
  );
  if (!opts || !opts.skipLive) {
    row.appendChild(liveSpan(dev.id, ctrl, unit || ''));
  }
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
  const resultsEl = document.getElementById('mqtt-scan-results');
  if (resultsEl) resultsEl.innerHTML = '';
  const statusEl = document.getElementById('mqtt-scan-status');
  if (statusEl) statusEl.textContent = '';
  const btn = document.getElementById('mqtt-scan-btn');
  if (btn) { btn.disabled = false; btn.textContent = 'Сканировать'; }
}

function hideScanModal() {
  const m = document.getElementById('mqtt-scan-modal');
  if (m) m.setAttribute('hidden', '');
}

async function runScan() {
  const portEl  = document.getElementById('mqtt-scan-port');
  const baudEl  = document.getElementById('mqtt-scan-baud');
  const rangeEl = document.getElementById('mqtt-scan-range');
  const btn   = document.getElementById('mqtt-scan-btn');
  const statusEl  = document.getElementById('mqtt-scan-status');
  const resultsEl = document.getElementById('mqtt-scan-results');
  if (!portEl || !baudEl || !rangeEl || !btn || !statusEl || !resultsEl) return;
  const port  = portEl.value;
  const baud  = Number(baudEl.value);
  const range = Number(rangeEl.value);

  btn.disabled = true;
  btn.textContent = 'Сканирование';
  statusEl.innerHTML = '<span class="mqtt-scan-spinner"></span>Поиск устройств на ' + port + ' (' + baud + ' бод)…';
  resultsEl.innerHTML = '';

  const data = await apiPost('cgi-bin/mqtt_scan.cgi', {port, baudrate: baud, max_addr: range})
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

  const method = data.scan_method === 'fast'
    ? 'быстрый Modbus'
    : (data.scan_method === 'standard' ? 'опрос адресов' : 'сканирование');
  statusEl.textContent = `Найдено: ${devices.length} (${method}). Проверьте имя и добавьте нужные.`;
  renderScanResults(port, baud, devices);
}

function renderScanResults(port, baud, devices) {
  const el = document.getElementById('mqtt-scan-results');
  el.innerHTML = '';

  const table = h('table', {'class': 'mqtt-device-table'});
  table.appendChild(h('thead', {}, h('tr', {},
    h('th', {}, 'Адрес'), h('th', {}, 'Сигнатура'), h('th', {}, 'Тип'), h('th', {}, 'Имя'), h('th', {})
  )));
  const tbody = h('tbody');

  for (const dev of devices) {
    const devType = (dev.type === 'unknown') ? 'mr02m' : (dev.type || 'mr02m');
    const typeSelect = h('select', {'class': 'mqtt-select-small'});
    for (const [val, lbl] of [['mr02m','МР-02м'], ['dtv','ДТВ-RS-485'], ['ce02m3','СЭ-02м-3']]) {
      const opt = h('option', {value: val}, lbl);
      if (val === devType) opt.selected = true;
      typeSelect.appendChild(opt);
    }
    typeSelect.addEventListener('change', () => {
      const d = {...dev, type: typeSelect.value};
      delete d.name;
      nameInput.value = scanShortName(d, typeSelect.value);
    });

    const listedDev = findListedDevice(port, dev.addr);
    const nameValue = listedDev
      ? (stripComAddrSuffix(listedDev.name) || scanShortName(dev, devType))
      : scanShortName(dev, devType);

    const nameInput = h('input', {
      'type': 'text', 'class': 'mqtt-ch-label-input',
      'placeholder': 'Сигнатура / имя',
      'value': nameValue,
    });

    const listed = !!listedDev;
    const actionCell = listed
      ? h('td', {'class': 'mqtt-scan-action'}, scanListedCheck())
      : h('td', {'class': 'mqtt-scan-action'}, (() => {
          const addBtn = h('button', {'class': 'btn btn-primary btn-sm'}, '+ Добавить');
          addBtn.onclick = () => {
            addDeviceFromScan(dev, typeSelect.value, nameInput.value, port, baud);
            addBtn.replaceWith(scanListedCheck());
          };
          return addBtn;
        })());

    tbody.appendChild(h('tr', {},
      h('td', {'class': 'mono'}, String(dev.addr)),
      h('td', {'class': 'mqtt-scan-sig'}, scanTypeHint(dev)),
      h('td', {}, typeSelect),
      h('td', {}, nameInput),
      actionCell
    ));
  }

  table.appendChild(tbody);

  const newCount = devices.filter((d) => !scanDeviceAlreadyListed(port, d.addr)).length;
  el.appendChild(table);
  if (newCount > 0) {
    const addAllBtn = h('button', {'class': 'btn btn-sm', 'style': 'margin-top:8px'},
      '+ Добавить все (' + newCount + ')');
    addAllBtn.onclick = () => {
      tbody.querySelectorAll('tr').forEach((tr, i) => {
        const d = devices[i];
        if (scanDeviceAlreadyListed(port, d.addr)) return;
        const sel = tr.querySelector('select');
        const inp = tr.querySelector('input');
        const btn = tr.querySelector('button');
        if (!btn) return;
        addDeviceFromScan(d, sel.value, inp.value, port, baud);
        btn.replaceWith(scanListedCheck());
      });
    };
    el.appendChild(addAllBtn);
  }
}

function addDeviceFromScan(scanDev, type, name, port, baud) {
  const addr = scanDev.addr;
  const id   = makeDeviceId(type, port, addr);

  if (_config.devices.find(d => d.id === id)) {
    showToast('Устройство ' + id + ' уже добавлено', 'warn');
    return;
  }

  const shortName = (name || '').trim() || scanShortName(scanDev, type);
  const dev = {
    id, type, port, baudrate: baud || 115200, address: addr,
    name: buildDeviceConfigName(shortName, port, addr),
  };
  if (type === 'mr02m') {
    const mt = Number(scanDev.module_type);
    dev.module_type = (mt && MR02M_TYPES[mt]) ? mt : 1;
    dev.fast_modbus = true;
    dev.poll_s = 1; dev.poll_do_di_s = 1; dev.poll_ai_ao_s = 1; dev.poll_diag_s = 60;
    dev.channels = {};
  } else if (type === 'dtv') {
    dev.fast_modbus = true;
    dev.poll_sensors_s = 1; dev.poll_presence_s = 1; dev.poll_diag_s = 60;
    dev.sensors_present = DTV_SENSORS.map(s => s.key);
  } else if (type === 'ce02m3') {
    // CE: classic first; FMB only with explicit fast_modbus:true after stable poll.
    dev.fast_modbus = false;
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
  _selectedDeviceIds.delete(id);
  markUnsaved();
  renderDeviceList();
  renderAccordion();
}

function showAddModal() {
  const modal = document.getElementById('mqtt-add-modal');
  if (modal) modal.removeAttribute('hidden');
  const typeEl = document.getElementById('mqtt-add-type');
  if (typeEl) typeEl.value = 'mr02m';
  const portEl = document.getElementById('mqtt-add-port');
  if (portEl) portEl.value = '/dev/COM1';
  const addrEl = document.getElementById('mqtt-add-addr');
  if (addrEl) addrEl.value = '1';
  const nameEl = document.getElementById('mqtt-add-name');
  if (nameEl) nameEl.value = '';
  updateAddModalId();
}

function hideAddModal() {
  const modal = document.getElementById('mqtt-add-modal');
  if (modal) modal.setAttribute('hidden', '');
}

function updateAddModalId() {
  const typeEl = document.getElementById('mqtt-add-type');
  const portEl = document.getElementById('mqtt-add-port');
  const addrEl = document.getElementById('mqtt-add-addr');
  const idEl = document.getElementById('mqtt-add-id');
  if (!typeEl || !portEl || !addrEl || !idEl) return;
  idEl.value = makeDeviceId(typeEl.value, portEl.value, addrEl.value);
}

function confirmAddDevice() {
  const typeEl = document.getElementById('mqtt-add-type');
  const portEl = document.getElementById('mqtt-add-port');
  const addrEl = document.getElementById('mqtt-add-addr');
  const nameEl = document.getElementById('mqtt-add-name');
  const idEl   = document.getElementById('mqtt-add-id');
  if (!typeEl || !portEl || !addrEl || !nameEl || !idEl) return;
  const type = typeEl.value;
  const port = portEl.value;
  const addr = parseInt(addrEl.value, 10);
  const name = nameEl.value.trim();
  const id = idEl.value.trim() || makeDeviceId(type, port, addr);
  // Cyntron DTV/MR/CE factory line: 115200 8N1 (not WB 19200).
  const baudrate = 115200;

  if (_config.devices.find(d => d.id === id)) {
    showToast(`Устройство ${id} уже добавлено`, 'warn');
    return;
  }

  const shortName = name || (type === 'dtv' ? 'ДТВ-RS-485' : type === 'ce02m3' ? 'СЭ-02м-3' : id);
  const dev = {
    id, type, port, baudrate, address: addr,
    name: (type === 'dtv' || type === 'ce02m3')
      ? buildDeviceConfigName(shortName === id ? (type === 'dtv' ? 'ДТВ-RS-485' : 'СЭ-02м-3') : shortName, port, addr)
      : (name || id),
  };
  if (type === 'mr02m') {
    dev.module_type = 1;
    dev.fast_modbus = true;
    dev.poll_s = 1;
    dev.poll_do_di_s = 1;
    dev.poll_ai_ao_s = 1;
    dev.poll_diag_s = 60;
    dev.channels = {};
  } else if (type === 'dtv') {
    dev.fast_modbus = true;
    dev.poll_sensors_s = 1;
    dev.poll_presence_s = 1;
    dev.poll_diag_s = 60;
    dev.sensors_present = DTV_SENSORS.map(s => s.key);
  } else if (type === 'ce02m3') {
    dev.fast_modbus = false;
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
  if (btn) { btn.disabled = true; btn.textContent = 'Сохранение'; }

  for (const dev of _config.devices || []) {
    if (dev.type === 'mr02m') {
      syncMr02mDiCounterFlagsFromDom(dev);
      ensureMr02mChannels(dev);
    }
    normalizeMr02mAiPairsAll(dev);
  }
  const payload = Object.assign({}, _config, {restart: true});
  // CGI writes YAML and returns immediately; bridge restart is async.
  const res = await apiPost('cgi-bin/mqtt_config.cgi', payload).catch(() => ({ ok: false, error: 'network' }));

  if (btn) { btn.disabled = false; btn.textContent = 'Сохранить и применить'; }

  if (res && res.ok) {
    _unsaved = false;
    for (const dev of _config.devices || []) {
      aiTypeApplyPendingFromDeviceConfig(dev);
      if (isAccordionOpen(dev.id)) refreshAiTypeSelects(dev);
    }
    showToast(res.restart === 'pending'
      ? 'Настройки сохранены. Мост MQTT перезапускается…'
      : 'Настройки сохранены.');
    setTimeout(refreshBrokerStatus, 1500);
    setTimeout(refreshBrokerStatus, 5000);
    setTimeout(() => {
      for (const dev of _config.devices || []) {
        if (isAccordionOpen(dev.id)) void prefetchDeviceLive(dev.id);
      }
    }, 4000);
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
async function pollMonitorDevice(deviceId) {
  if (_monitorPaused || !deviceId) return;
  const data = await apiGet(
    `cgi-bin/mqtt_monitor_poll.cgi?device=${encodeURIComponent(deviceId)}`).catch(() => null);
  const logEl = document.getElementById('mqtt-monitor-log');
  if (!data || !data.ok) return;
  const hint = logEl?.querySelector('.mqtt-monitor-hint');
  if (hint) hint.remove();
  const batch = [];
  for (const ev of data.events || []) {
    if (!ev || !ev.topic) continue;
    const prev = _monitorLastVal[ev.topic];
    if (!_monitorPrimed || prev !== ev.value) {
      _monitorLastVal[ev.topic] = ev.value;
      batch.push(ev);
    }
  }
  if (batch.length) {
    for (const ev of batch) {
      queueMonitorLine(ev.ts, ev.topic, ev.value);
      ingestMonitorTopic(ev.topic, ev.value);
    }
  }
  _monitorPrimed = true;
}

function startMonitor() {
  stopMonitor();
  _monitorPaused = false;
  _monitorLastVal = Object.create(null);
  _monitorPrimed = false;
  const filter = document.getElementById('mqtt-monitor-filter')?.value || '';
  const logEl = document.getElementById('mqtt-monitor-log');
  if (!filter) {
    if (logEl && !logEl.children.length) {
      logEl.appendChild(h('div', {'class': 'mqtt-monitor-hint'},
        'Выберите устройство в списке — поток топиков не запускается на «все», чтобы не блокировать каналы.'));
    }
    return;
  }
  if (logEl) {
    const hint = logEl.querySelector('.mqtt-monitor-hint');
    if (hint) hint.remove();
    logEl.appendChild(h('div', {'class': 'mqtt-monitor-hint'}, 'Загрузка'));
  }
  void pollMonitorDevice(filter);
  _monitorPollTimer = setInterval(() => pollMonitorDevice(filter), 1000);
}

function stopMonitor() {
  if (_monitorPollTimer) {
    clearInterval(_monitorPollTimer);
    _monitorPollTimer = null;
  }
}

const _monitorQueue = [];
let _monitorFlushRaf = 0;

function queueMonitorLine(ts, topic, value) {
  _monitorQueue.push({ts, topic, value});
  if (!_monitorFlushRaf) {
    _monitorFlushRaf = requestAnimationFrame(flushMonitorQueue);
  }
}

function flushMonitorQueue() {
  _monitorFlushRaf = 0;
  const el = document.getElementById('mqtt-monitor-log');
  if (!el || !_monitorQueue.length) return;
  const frag = document.createDocumentFragment();
  for (const m of _monitorQueue) {
    frag.appendChild(h('div', {'class': 'mqtt-monitor-line'},
      h('span', {'class': 'mqtt-monitor-ts'}, m.ts),
      h('span', {'class': 'mqtt-monitor-topic'}, m.topic),
      h('span', {'class': 'mqtt-monitor-val'}, m.value),
    ));
  }
  _monitorQueue.length = 0;
  el.appendChild(frag);
  while (el.children.length > 100) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

function appendMonitorLine(ts, topic, value) {
  queueMonitorLine(ts, topic, value);
}

function clearMonitor() {
  const el = document.getElementById('mqtt-monitor-log');
  if (el) el.innerHTML = '';
  _monitorLastVal = Object.create(null);
  _monitorPrimed = false;
}

// ── Tab init ──────────────────────────────────────────────────────────────────
window.mqttTabInit = function() {
  if (window._mqttStatusTimer) {
    clearInterval(window._mqttStatusTimer);
    window._mqttStatusTimer = null;
  }
  void refreshBrokerStatus();
  void loadConfig();
  window._mqttStatusTimer = setInterval(refreshBrokerStatus, 10000);
};

window.mqttTabDestroy = function() {
  stopMonitor();
  stopChannelPoll();
  if (window._mqttStatusTimer) {
    clearInterval(window._mqttStatusTimer);
    window._mqttStatusTimer = null;
  }
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
  const res = await apiPost('cgi-bin/mqtt_ctrl.cgi', {action}).catch(() => null);
  if (res?.ok) { showToast(`Выполнено: ${action}`); setTimeout(refreshBrokerStatus, 1500); }
  else showToast('Ошибка: ' + (res?.error || '?'), 'err');
};

window.mqttToggleBridge = async function mqttToggleBridge() {
  // Confirm IMMEDIATELY from cached UI state — never await mqtt_status.cgi
  // before confirm (that CGI used to take several seconds → 10 s blank wait).
  const btn = document.getElementById('mqtt-bridge-toggle-btn');
  const stCached = _lastMqttBrokerStatus;
  let on = mqttBridgeUiState(stCached) === 'active';
  if (mqttBridgeUiState(stCached) === 'unknown' && btn) {
    // Fallback: button label already reflects last paint — RU "Остановить"/
    // "Запустить" or the EN paint uiT() produced ("Stop"/"Start").
    const label = (btn.textContent || '').trim().toLowerCase();
    if (label.includes('останов') || label.includes('stop')) on = true;
    else if (label.includes('запуст') || label.includes('start')) on = false;
  }
  const action = on ? 'stop_bridge' : 'start_bridge';
  const msg = on
    ? 'Остановить мост Modbus→MQTT? Служба будет отключена и не запустится после перезагрузки до ручного включения.'
    : 'Запустить мост Modbus→MQTT?';
  if (!confirm(msg)) return;
  if (btn) btn.disabled = true;
  try {
    // CGI returns immediately (pending); service finishes in background.
    const res = await apiPost('cgi-bin/mqtt_ctrl.cgi', {action});
    if (res?.ok) {
      showToast(on ? 'Остановка моста' : 'Запуск моста', 'success');
      // Optimistic badge until status poll catches up.
      if (_lastMqttBrokerStatus && typeof _lastMqttBrokerStatus === 'object') {
        _lastMqttBrokerStatus = Object.assign({}, _lastMqttBrokerStatus, {
          bridge_active: on ? 0 : 1,
          bridge_disabled: on ? 1 : 0,
        });
        applyBrokerStatusUI(_lastMqttBrokerStatus);
      }
      setTimeout(refreshBrokerStatus, 800);
      setTimeout(refreshBrokerStatus, 3000);
    } else {
      showToast('Ошибка: ' + (res?.error || '?'), 'err');
      void refreshBrokerStatus();
    }
  } catch (_) {
    showToast('Ошибка управления мостом', 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
};

})();
