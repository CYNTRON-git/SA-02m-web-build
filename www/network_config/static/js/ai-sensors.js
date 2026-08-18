/* SA-02m — MR-02m AI sensor-type code↔label map (shared ES module).
 *
 * One home for the AI «тип датчика» selection codes 0..42 and their labels,
 * imported by both the MQTT tab dropdown (mqtt.js) and the Устройства MR-02m
 * card captions (devices.js). Native ES module (docs/decisions/es-modules.md):
 * `.js` extension, imported with a `?v=` cache-bust that sync-app-version.py
 * patches; no `window.*` shim (importers are modules).
 */

// Modbus selection codes 0..42 (регистр «тип датчика», MR-02m ≥1.0.9.1) — must match module_profiles.py / modbus_mqtt_bridge.py
export const AI_SENSOR_LABELS = [
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

const _LABEL_BY_CODE = new Map(AI_SENSOR_LABELS.map((s) => [s.code, s.label]));

// Curated one-line captions for the Устройства MR-02m card (Operator Q1: SHORT
// technical — «NTC 10k», «Pt100», «ТХА»). Keyed by code; only codes whose clean
// short form differs from the «N — »-prefix strip are listed — the rest (0
// «Выключен», 34/35 «Напряжение …», 36/37 «Дифф. …», 38–40 «Ток …», 42 «Сухой
// контакт») already read correctly after the strip and fall through. Kept ≤~14
// chars so the caption never truncates in the cell. NTC/Pt/Ni forms are
// language-neutral (no EN DICT needed); «ТХА» reuses the shared DICT entry.
const AI_SENSOR_SHORT = {
  1: 'NTC 1.8k', 2: 'NTC 5k', 3: 'NTC 10k', 4: 'NTC 10k', 5: 'NTC 10k',
  6: 'NTC 10k', 7: 'NTC 100k',
  8: 'Pt50', 9: 'Pt100', 10: 'Pt500', 11: 'Pt1000',
  12: 'Pt50', 13: 'Pt100', 14: 'Pt1000',
  15: 'Pt50', 16: 'Pt100', 17: 'Pt1000',
  18: 'Ni100', 19: 'Ni500', 20: 'Ni1000',
  21: 'Pt50', 22: 'Pt100', 23: 'Pt500', 24: 'Pt1000',
  25: 'Pt50', 26: 'Pt100', 27: 'Pt1000',
  28: 'Pt50', 29: 'Pt100', 30: 'Pt1000',
  31: 'Ni100', 32: 'Ni500', 33: 'Ni1000',
  41: 'ТХА',
};

/**
 * Human label for an AI sensor-type code (0..42).
 * @param {number|string} code sensor-type selection code.
 * @param {{short?: boolean}} [opts] short=true → the compact caption used by the
 *   Устройства MR-02m card (curated AI_SENSOR_SHORT entry, else the «N — »-prefix
 *   strip of the full label); false / omitted → the full «N — …» dropdown label
 *   used by the MQTT tab.
 * @returns {string} the label, or '' for an unrecognised code.
 */
export function aiSensorLabel(code, opts) {
  const c = Number(code);
  const full = _LABEL_BY_CODE.get(c);
  if (full == null) return '';
  if (!opts || !opts.short) return full;
  if (Object.prototype.hasOwnProperty.call(AI_SENSOR_SHORT, c)) {
    return AI_SENSOR_SHORT[c];
  }
  return full.replace(/^\s*\d+\s*—\s*/, '');
}
