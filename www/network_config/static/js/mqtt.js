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
  if (stored && /\([^)]*addr=\d+/i.test(stored)) return stored;
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
  const base = (shortName || '').trim() || `Устройство ${addr}`;
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
  energy_active_import: 'kWh', energy_active_export: 'kWh',
  energy_reactive_import: 'kvarh', energy_reactive_export: 'kvarh',
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

/** 6AO6AI6: чётный AI — N-нога пары (P = ch − 1), как в прошивке MR-02m. */
const MR02M_AI_N_PARENT = { 2: 1, 4: 3, 6: 5 };

function mr02mAiIsNLeg(mt, ch) {
  return Number(mt) === 6 && ch % 2 === 0 && MR02M_AI_N_PARENT[ch] != null;
}

function mr02mAiPairParent(ch) {
  return MR02M_AI_N_PARENT[ch] || null;
}

const _AI_NTC_CODES = new Set([1, 10, 11, 12, 13, 19, 20, 25, 26]);
const _AI_RTD_CODES = new Set([
  2, 3, 8, 9, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
  27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37,
]);
const _AI_TC_K_CODES = new Set([6]);
/** Трёхпроводные RTD (как module_profiles.AI_RTD_CODES_3_WIRE / flasher.js). */
const _AI_RTD_3WIRE = new Set([
  0x001B, 0x001C, 0x001D, 0x001E, 0x001F, 0x0020, 0x0021, 0x0022, 0x0023, 0x0024, 0x0025,
]);

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
  const st = pCfg.sensor_type != null ? Number(pCfg.sensor_type) : 2;
  if (!mr02mAiMirrorTypeToN(st)) return;
  const nCfg = getOrCreateChannel(dev.channels, 'ai', nCh);
  nCfg.sensor_type = st;
}

function normalizeMr02mAiPairsAll(dev) {
  if (dev.type !== 'mr02m' || getModuleTypeCode(dev) !== 6) return;
  if (!dev.channels) dev.channels = {};
  for (const nCh of [2, 4, 6]) {
    const pCh = mr02mAiPairParent(nCh);
    if (!pCh) continue;
    const pCfg = getOrCreateChannel(dev.channels, 'ai', pCh);
    const st = pCfg.sensor_type != null ? Number(pCfg.sensor_type) : 2;
    if (!mr02mAiMirrorTypeToN(st)) continue;
    const nCfg = getOrCreateChannel(dev.channels, 'ai', nCh);
    nCfg.sensor_type = st;
  }
}

function topicPath(deviceId, chName) {
  return `/devices/${deviceId}/controls/${chName}`;
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

function aiUnitsForCode(code) {
  const c = Number(code) & 0xffff;
  if (c === 0) return '';
  if (c === 4 || c === 24 || c === 38) return 'В';
  if (c === 23) return 'мВ';
  if (c === 5 || c === 21 || c === 22) return 'мА';
  if (c === 7) return '';
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
  const mt = getModuleTypeCode(dev);
  const parent = mr02mAiPairParent(ch);
  if (mr02mAiIsNLeg(mt, ch) && parent) {
    const pCfg = getOrCreateChannel(channels, 'ai', parent);
    const pst = pCfg.sensor_type != null ? Number(pCfg.sensor_type) : 2;
    if (mr02mAiMirrorTypeToN(pst)) return pst;
  }
  const chCfg = getOrCreateChannel(channels, 'ai', ch);
  return chCfg.sensor_type != null ? Number(chCfg.sensor_type) : 2;
}

function liveUnitFor(devId, controlName, fallback) {
  if (controlName === 'uptime_s') return '';
  const u = _liveUnits[devId] && _liveUnits[devId][controlName];
  const raw = u != null && u !== '' ? u : (fallback || '');
  return formatUnitLabel(raw);
}

function formatLiveDisplay(controlName, rec) {
  if (!rec) return '—';
  if (rec.isError) return '⚠';
  const v = rec.value;
  if (v === '' || v == null) return '—';
  if (controlName === 'uptime_s') {
    const s = parseInt(v, 10);
    if (!Number.isNaN(s) && s >= 0) {
      const d = Math.floor(s / 86400);
      const h = Math.floor((s % 86400) / 3600);
      const m = Math.floor((s % 3600) / 60);
      if (d > 0) return `${d}д ${h}ч ${m}м`;
      if (h > 0) return `${h}ч ${m}м`;
      return `${m}м ${s % 60}с`;
    }
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
  if (opts && opts.skipDom) return;
  if (isAccordionOpen(devId)) scheduleLiveDomRefresh(devId);
}

function updateLiveCell(devId, controlName) {
  const el = document.querySelector(`[data-live="${devId}:${controlName}"]`);
  if (!el) return;
  const rec = _liveByDevice[devId] && _liveByDevice[devId][controlName];
  el.textContent = formatLiveDisplay(controlName, rec);
  el.className = 'mqtt-ch-live' + (rec && rec.isError ? ' mqtt-ch-live-err' : rec ? ' mqtt-ch-live-ok' : '');
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
}

function refreshAllLiveCells() {
  for (const devId of _accordionBuilt) refreshLiveCellsForDevice(devId);
}

async function prefetchDeviceLive(devId) {
  const data = await apiGet(
    `/cgi-bin/mqtt_live.cgi?device=${encodeURIComponent(devId)}`).catch(() => null);
  if (!data || !data.ok) return;
  for (const [ctrl, val] of Object.entries(data.controls || {})) {
    setLiveValue(devId, ctrl, val, false, {skipDom: true});
  }
  for (const [ctrl, u] of Object.entries(data.units || {})) {
    if (!_liveUnits[devId]) _liveUnits[devId] = Object.create(null);
    _liveUnits[devId][ctrl] = String(u);
  }
  for (const [ctrl, err] of Object.entries(data.errors || {})) {
    setLiveValue(devId, ctrl, err, true, {skipDom: true});
  }
  refreshLiveCellsForDevice(devId);
}

function stopChannelPoll() {
  if (_channelPollTimer) {
    clearInterval(_channelPollTimer);
    _channelPollTimer = null;
  }
  _channelPollDevId = null;
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
}

function liveSpan(devId, controlName, unit) {
  const rec = _liveByDevice[devId] && _liveByDevice[devId][controlName];
  const u = liveUnitFor(devId, controlName, unit || '');
  const wrap = h('span', {'class': 'mqtt-ch-live-wrap'});
  wrap.appendChild(h('span', {
    'class': 'mqtt-ch-live' + (rec && rec.isError ? ' mqtt-ch-live-err' : rec ? ' mqtt-ch-live-ok' : ''),
    'data-live': `${devId}:${controlName}`,
    'title': 'Текущее значение с шины (MQTT)',
  }, formatLiveDisplay(controlName, rec)));
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
            markUnsaved();
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
  const st = await apiGet('/cgi-bin/mqtt_status.cgi').catch(() => null);
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

  document.querySelectorAll('.mqtt-copy-cell').forEach((el) => {
    el.addEventListener('click', async () => {
      const text = el.dataset.copyText || el.textContent || '';
      if (!text || text === '—') {
        showToast('Нечего копировать', 'warn');
        return;
      }
      if (await copyTextToClipboard(text)) showToast('Скопировано: ' + text, 'ok');
      else showToast('Не удалось скопировать', 'err');
    });
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
  const bridgeToggle = document.getElementById('mqtt-bridge-toggle-btn');
  if (bridgeToggle) {
    const on = !!st.bridge_active;
    bridgeToggle.textContent = on ? 'Остановить' : 'Запустить';
    bridgeToggle.className = on ? 'btn btn-sm btn-warn' : 'btn btn-sm btn-primary';
    bridgeToggle.title = on
      ? 'Остановить мост Modbus→MQTT и освободить COM-порты'
      : 'Запустить мост Modbus→MQTT';
  }
  renderMqttClientInfo(st);
  bindMqttClientInfoCells();
}

// ── Load config ───────────────────────────────────────────────────────────────
async function loadConfig() {
  const data = await apiGet('/cgi-bin/mqtt_config.cgi').catch(() => null);
  if (data && !data.error) {
    _config = data;
    for (const dev of _config.devices || []) {
      normalizeMr02mAiPairsAll(dev);
    }
    renderDeviceList();
    renderAccordion();
  }
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
  tbody.innerHTML = '';
  refreshMonitorFilterOptions();
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
    for (const kind of ['do','di','ao','ai','sys']) {
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
    h('span', {'class':'mqtt-accordion-arrow', 'id': `acc-arrow-${dev.id}`}, '▶'),
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
      if (el.id !== `acc-arrow-${id}`) el.textContent = '▶';
    });
    body.removeAttribute('hidden');
    if (arrow) arrow.textContent = '▼';
    const dev = (_config.devices || []).find(d => d.id === id);
    if (dev) void openDeviceChannels(dev);
    return;
  }
  body.setAttribute('hidden', '');
  if (arrow) arrow.textContent = '▶';
  if (_channelPollDevId === id) stopChannelPoll();
}

async function openDeviceChannels(dev) {
  const devId = dev.id;
  const body = document.getElementById(`acc-body-${devId}`);
  if (body) body.classList.add('mqtt-ch-loading');
  const liveP = prefetchDeviceLive(devId);
  ensureAccordionBody(dev);
  await liveP;
  if (body) body.classList.remove('mqtt-ch-loading');
  if (isAccordionOpen(devId)) startChannelPoll(devId);
}

function appendMr02mDoGroup(dev, channels, count) {
  const pack = buildChannelWidget('DO — дискретные выходы');
  for (let i = 1; i <= count; i++) {
    const chCfg = getOrCreateChannel(channels, 'do', i);
    pack.body.appendChild(buildChannelRow(chCfg, topicPath(dev.id, `do_${i}`), 'do', i, dev, `do_${i}`, ''));
  }
  return pack.widget;
}

function appendMr02mDiGroup(dev, channels, count) {
  const pack = buildChannelWidget('DI — дискретные входы');
  for (let i = 1; i <= count; i++) {
    const chCfg = getOrCreateChannel(channels, 'di', i);
    const row = buildChannelRow(chCfg, topicPath(dev.id, `di_${i}`), 'di', i, dev, `di_${i}`, '');
    const countLive = liveSpan(dev.id, `di_${i}_count`, '');
    if (!chCfg.counter) countLive.hidden = true;
    row.appendChild(h('label', {'class': 'mqtt-counter-label'},
      h('input', {'type': 'checkbox', checked: chCfg.counter ? '' : undefined,
        'onchange': e => {
          chCfg.counter = e.target.checked;
          countLive.hidden = !e.target.checked;
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
    pack.body.appendChild(buildChannelRow(
      chCfg, topicPath(dev.id, `ao_${i}`), 'ao', i, dev, `ao_${i}`, 'В'));
  }
  return pack.widget;
}

function appendMr02mAiGroup(dev, channels, mtCode, count) {
  const pack = buildChannelWidget('AI — аналоговые входы');
  for (let i = 1; i <= count; i++) {
    const chCfg = getOrCreateChannel(channels, 'ai', i);
    const ctrl = `ai_${i}`;
    const effType = mr02mAiEffectiveSensorType(dev, i, channels);
    const row = buildChannelRow(
      chCfg, topicPath(dev.id, ctrl), 'ai', i, dev, ctrl, '', {skipLive: true});
    const sel = h('select', {'class': 'mqtt-select-small',
      'onchange': e => {
        chCfg.sensor_type = Number(e.target.value);
        if (!mr02mAiIsNLeg(mtCode, i)) syncMr02mPairAfterParentChange(dev, i);
        const u = aiUnitsForCode(chCfg.sensor_type);
        const unitEl = row.querySelector(`[data-unit="${dev.id}:${ctrl}"]`);
        if (unitEl) {
          unitEl.setAttribute('data-unit-default', u);
          unitEl.textContent = liveUnitFor(dev.id, ctrl, u);
          unitEl.hidden = !u ? '' : undefined;
        }
        markUnsaved();
      }});
    const nMirror = mr02mAiIsNLeg(mtCode, i) && mr02mAiMirrorTypeToN(effType);
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

function buildMR02mChannels(dev, container) {
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
      h('span', {'class':'topic-preview', 'title': topic}, topic),
      liveSpan(dev.id, s.key, s.unit || ''),
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

  const row = mqttChannelWidgetsRow();
  container.appendChild(row);
  row.appendChild(buildSysChannelGroup(dev, 'Системные', CE02M3_SYS_VARS));

  const ctPack = buildChannelWidget('Параметры CT');
  ctPack.body.appendChild(h('div', {'class': 'mqtt-form-row'},
    h('label', {}, 'Коэффициент CT (K×1000):'),
    h('input', {'type': 'number', 'class': 'mqtt-input-small', 'value': dev.ct_ratio || 4000,
      'oninput': e => { dev.ct_ratio = Number(e.target.value); markUnsaved(); }}),
    h('span', {}, '(4000 = CT 4А, 1000 = 1А)'),
  ));
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
            if (enKey) { en[enKey] = e.target.checked; markUnsaved(); }
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
          markUnsaved();
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

  for (const dev of _config.devices || []) {
    normalizeMr02mAiPairsAll(dev);
  }
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
async function pollMonitorDevice(deviceId) {
  if (_monitorPaused || !deviceId) return;
  const data = await apiGet(
    `/cgi-bin/mqtt_monitor_poll.cgi?device=${encodeURIComponent(deviceId)}`).catch(() => null);
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
    for (const ev of batch) queueMonitorLine(ev.ts, ev.topic, ev.value);
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
    logEl.appendChild(h('div', {'class': 'mqtt-monitor-hint'}, 'Загрузка…'));
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
  refreshBrokerStatus();
  loadConfig();
  // Refresh broker status every 10s
  const timer = setInterval(refreshBrokerStatus, 10000);
  window._mqttStatusTimer = timer;
};

window.mqttTabDestroy = function() {
  stopMonitor();
  stopChannelPoll();
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

window.mqttToggleBridge = async function mqttToggleBridge() {
  const st = await apiGet('/cgi-bin/mqtt_status.cgi').catch(() => null);
  const on = !!(st && st.bridge_active);
  const action = on ? 'stop_bridge' : 'start_bridge';
  const msg = on
    ? 'Остановить мост Modbus→MQTT? COM-порты освободятся для прошивальщика и сканирования.'
    : 'Запустить мост Modbus→MQTT?';
  if (!confirm(msg)) return;
  const btn = document.getElementById('mqtt-bridge-toggle-btn');
  if (btn) btn.disabled = true;
  try {
    const res = await apiPost('/cgi-bin/mqtt_ctrl.cgi', {action});
    if (res?.ok) {
      showToast(on ? 'Мост остановлен' : 'Мост запущен', 'success');
      setTimeout(refreshBrokerStatus, 1200);
    } else {
      showToast('Ошибка: ' + (res?.error || '?'), 'err');
    }
  } catch (_) {
    showToast('Ошибка управления мостом', 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
};

})();
