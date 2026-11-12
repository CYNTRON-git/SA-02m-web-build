#!/usr/bin/env node
/* test-forms-time-prefill — the «Дата и время» field of the Время tab starts
   EMPTY (www/network_config/static/js/app/forms.js loadConfig).

   WHY (audit 2026-11-11 A4-05). loadConfig filled f-datetime with the device's
   time at the moment the tab loaded. The field's hint says «Оставьте пустым…»,
   but the Operator who only changed the time zone pressed «Применить вручную»
   with that stale time still in the field — apply.cgi then set the clock back
   by however long the tab had been open AND switched NTP off. The device time
   is already shown read-only in time-sys-disp; the field is for a NEW time only
   («Взять с ПК» / «Синхронизировать с ПК» still fill it, app/misc.js).

   WHAT RUNS. The SHIPPED forms.js is evaluated whole in a vm (its top level only
   declares functions) with a scripted fetch answering a config.cgi body that
   carries `datetime`, and a fake document whose elements record their value /
   textContent. loadConfig() runs; the observables are f-datetime.value and
   time-sys-disp.textContent.

   Cases: P1 f-datetime stays empty; P2 time-sys-disp still shows the device
   time (the read-only display is not lost with the prefill). Non-vacuous:
   loadConfig must exist, the fetch must be observed, and f-datetime must be
   looked up (a renamed id would otherwise pass P1 vacuously).

   PROVEN RED against forms.js of origin/main (FORMS_JS=<git show copy>): P1
   FAIL (f-datetime = the device time). GREEN after.

   MUTATION. P1 guards an ABSENCE, so it has no line to comment out — its RED
   is re-introducing `setVal('f-datetime', d.datetime)`. P2 does pin a line:
   commenting out forms.js's `setText('time-sys-disp', …)` turns it RED, so that
   line is a registered case in comment-mutation-proof, not an exemption.

   Run: node scripts/dev/test-forms-time-prefill.mjs */
import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FORMS = process.env.FORMS_JS
  || path.join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'app', 'forms.js');

let fails = 0;
const ok = (m) => console.log(`ok    ${m}`);
const bad = (m) => { console.log(`FAIL  ${m}`); fails++; };

if (!fs.existsSync(FORMS) || fs.statSync(FORMS).size === 0) {
  console.log(`FAIL  missing or empty: ${FORMS}`);
  process.exit(1);
}

const DEVICE_TIME = '2026-11-11 12:00:00';
const els = {};
const looked = new Set();
const el = (id) => {
  if (!els[id]) {
    els[id] = {
      id, value: '', textContent: '', checked: false, style: {}, options: [],
      classList: { add() {}, remove() {}, toggle() {} },
      appendChild() {}, setAttribute() {}, removeAttribute() {},
      querySelectorAll: () => [],
    };
  }
  return els[id];
};
let fetches = 0;
const sb = {
  console,
  setTimeout,
  Intl,
  document: {
    getElementById: (id) => { looked.add(id); return el(id); },
    createElement: () => el('_created'),
    querySelectorAll: () => [],
    querySelector: () => null,
  },
  fetch: () => {
    fetches++;
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({
        eth0: { enabled: true, ip: '192.168.1.135', netmask: '255.255.255.0' },
        eth1: { enabled: false },
        timezone: 'Europe/Moscow',
        datetime: DEVICE_TIME,
      }),
    });
  },
  setText: (id, v) => { el(id).textContent = v; },
  setRtcReadout: () => {},
  toast: () => {},
  uiT: (s) => s,
};
sb.window = sb;
vm.createContext(sb);
vm.runInContext(fs.readFileSync(FORMS, 'utf8'), sb, { filename: 'forms.js' });

if (typeof sb.loadConfig !== 'function') {
  console.log('FAIL  loadConfig is not defined by forms.js');
  process.exit(1);
}
sb.loadConfig();
await new Promise((r) => setTimeout(r, 50));

if (fetches !== 1) bad(`fetch observed ${fetches} time(s), expected 1 (config.cgi)`);
if (!looked.has('time-sys-disp')) bad('loadConfig never reached the time block (time-sys-disp not looked up)');
const v = els['f-datetime'] ? els['f-datetime'].value : '';
if (v === '') ok('P1: f-datetime stays empty after loadConfig (no stale device time to re-apply)');
else bad(`P1: f-datetime prefilled with '${v}' — a zone-only apply would set the clock back to it`);
if (els['time-sys-disp'] && els['time-sys-disp'].textContent === DEVICE_TIME) ok('P2: time-sys-disp still shows the device time');
else bad(`P2: time-sys-disp = '${els['time-sys-disp'] && els['time-sys-disp'].textContent}'`);

console.log('');
if (fails) { console.log(`test-forms-time-prefill: ${fails} case(s) FAILED`); process.exit(1); }
console.log('test-forms-time-prefill: all cases passed');
