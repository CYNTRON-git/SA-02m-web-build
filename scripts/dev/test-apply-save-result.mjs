#!/usr/bin/env node
/* test-apply-save-result — what the panel tells the Operator after a save
   through apply.cgi (www/network_config/static/js/app/forms.js submitForm).

   WHY. apply.cgi answers a success with a 302 (hidden by redirect:'manual' —
   status 0 / opaqueredirect) but REFUSES a save with HTTP 200 + a JSON body:
   `{"ok":false,"error":"conf_write_refused"}` when the root write helper
   rejects the canonical interface conf (docs/contracts/ethernet-iface-naming.md
   §5.4 — the whole save is then refused, nothing applied), and
   `{"error":"invalid input: <field>"}` on a bad field. submitForm read every
   200 as success: the Operator saw «конфиг сохранён» for a save that wrote and
   applied nothing. A third 200 answer, `{"ok":true,"applied":false,
   "reason":"reboot_required",…}`, means the conf was saved but NOT applied live
   (the legacy-name mirror was refused in the rename window, §5.4) — a warning,
   never the success toast.

   WHAT RUNS. The SHIPPED forms.js is evaluated whole in a vm (its top level
   only declares functions) with a scripted `fetch`, a recording `toast`, a
   fake form/button; the observables are the toasts, whether onSuccess ran, and
   the button re-enabled. The three texts are then put through the SHIPPED
   i18n.js runtime (setLang('en')) — toast() translates through it, and the
   i18n-dict-contract row does not sweep toast() literals.

   Cases: S1 conf_write_refused -> its error toast, no onSuccess; S2 an
   invalid-input body -> the generic refusal toast, no onSuccess; S3 an E_CSRF
   body that reached the caller -> no toast and no onSuccess (the fetch guard
   owns that report); S4 opaqueredirect (the real 302) -> onSuccess, no toast;
   S5 a non-JSON 200 (a proxy that followed the redirect) -> onSuccess; S6 a
   500 -> «Ошибка сервера: 500»; S7 the two refusal texts and the deferred-apply
   text translate to English; S8 `applied:false` -> ONE warning toast (saved,
   applies after the reboot), no onSuccess, the config marked for reload; S9 a
   JSON `{"ok":true}` without `applied:false` -> still a success.
   Non-vacuous: submitForm, applyRefusalText and applyDeferredText must exist,
   every case must observe its fetch call, and the i18n oracle must translate a
   known string.

   PROVEN RED against forms.js of origin/main (FORMS_JS=<git show copy>):
   S1, S2, S3, S8 FAIL (onSuccess ran, no refusal/warning toast) and S7 FAIL
   (the texts and helpers do not exist there). Against the round-2 forms.js
   (the 200 branch without the applyDeferredText lines): S8 FAIL (the success
   toast instead of the warning). GREEN after.

   Run: node scripts/dev/test-apply-save-result.mjs
        (FORMS_JS=<path> / I18N_JS=<path> drive other copies — the RED recipe) */
import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const JS = path.join(HERE, '..', '..', 'www', 'network_config', 'static', 'js');
const FORMS = process.env.FORMS_JS || path.join(JS, 'app', 'forms.js');
const I18N = process.env.I18N_JS || path.join(JS, 'i18n.js');

let fails = 0;
const ok = (m) => console.log(`ok    ${m}`);
const bad = (m) => { console.log(`FAIL  ${m}`); fails++; };

for (const f of [FORMS, I18N]) {
  if (!fs.existsSync(f) || fs.statSync(f).size === 0) { console.log(`FAIL  missing or empty: ${f}`); process.exit(1); }
}

function world(answer) {
  const rec = { toasts: [], success: 0, fetches: 0 };
  const btn = { disabled: false };
  const form = {
    querySelector: (sel) => (sel.indexOf('button') !== -1 ? btn : sel.indexOf('net_iface') !== -1 ? {} : null),
  };
  const sb = {
    console,
    URLSearchParams,
    FormData: function () { return {}; },
    toast: (msg, type) => rec.toasts.push({ msg, type }),
    withCsrfHeaders: (h) => h,
    fetch: () => { rec.fetches++; return Promise.resolve(answer); },
  };
  sb.window = sb;
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(FORMS, 'utf8'), sb, { filename: 'forms.js' });
  // A loaded form: a case that must mark the config stale can be seen doing it.
  vm.runInContext('configLoaded = true', sb);
  return { sb, rec, btn, form };
}

function resp({ status, type = 'basic', body, location = '' }) {
  return {
    status, type, ok: status >= 200 && status < 300,
    headers: { get: (k) => (String(k).toLowerCase() === 'location' ? location : null) },
    json: () => (body === undefined ? Promise.reject(new SyntaxError('not JSON')) : Promise.resolve(body)),
  };
}

async function run(answer) {
  const w = world(answer);
  if (typeof w.sb.submitForm !== 'function') { bad('forms.js defines no submitForm — nothing to test'); process.exit(1); }
  w.sb.submitForm(w.form, () => { w.rec.success++; });
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  if (w.rec.fetches !== 1) bad(`the case never reached fetch (calls=${w.rec.fetches})`);
  if (w.btn.disabled) bad('the submit button stayed disabled');
  return w;
}

const T_CONF = 'Сетевые настройки не сохранены: устройство отклонило конфиг интерфейса. Ничего не применено; подробности — в /var/log/sa02m_install.log.';
const T_GENERIC = 'Настройки не применены: устройство отклонило запрос.';
const errToasts = (w) => w.rec.toasts.filter((t) => t.type === 'error');

{ // S1
  const w = await run(resp({ status: 200, body: { ok: false, error: 'conf_write_refused' } }));
  const e = errToasts(w);
  if (w.rec.success === 0 && e.length === 1 && e[0].msg === T_CONF && w.rec.toasts.length === 1) {
    ok('S1: conf_write_refused -> one error toast naming the rejected interface config, onSuccess NOT run');
  } else bad(`S1: success=${w.rec.success} toasts=${JSON.stringify(w.rec.toasts)}`);
}
{ // S2
  const w = await run(resp({ status: 200, body: { error: 'invalid input: ip' } }));
  const e = errToasts(w);
  if (w.rec.success === 0 && e.length === 1 && e[0].msg === T_GENERIC) {
    ok('S2: an invalid-input JSON refusal -> the generic refusal toast, onSuccess NOT run');
  } else bad(`S2: success=${w.rec.success} toasts=${JSON.stringify(w.rec.toasts)}`);
}
{ // S3
  const w = await run(resp({ status: 200, body: { ok: false, error: 'csrf', error_code: 'E_CSRF', reason: 'no_header' } }));
  if (w.rec.success === 0 && w.rec.toasts.length === 0) {
    ok('S3: an E_CSRF body reaching the caller -> no toast here (the fetch guard reports it) and no success');
  } else bad(`S3: success=${w.rec.success} toasts=${JSON.stringify(w.rec.toasts)}`);
}
{ // S4
  const w = await run(resp({ status: 0, type: 'opaqueredirect' }));
  if (w.rec.success === 1 && w.rec.toasts.length === 0) ok('S4: the real 302 (opaqueredirect) -> onSuccess, no toast');
  else bad(`S4: success=${w.rec.success} toasts=${JSON.stringify(w.rec.toasts)}`);
}
{ // S5
  const w = await run(resp({ status: 200 }));
  if (w.rec.success === 1 && w.rec.toasts.length === 0) ok('S5: a non-JSON 200 (proxy followed the redirect) -> still a success');
  else bad(`S5: success=${w.rec.success} toasts=${JSON.stringify(w.rec.toasts)}`);
}
{ // S6
  const w = await run(resp({ status: 500 }));
  const e = errToasts(w);
  if (w.rec.success === 0 && e.length === 1 && e[0].msg === 'Ошибка сервера: 500') ok('S6: HTTP 500 -> «Ошибка сервера: 500», no success');
  else bad(`S6: success=${w.rec.success} toasts=${JSON.stringify(w.rec.toasts)}`);
}
// apply.cgi's deferred answer, verbatim (a refused legacy mirror, §5.4).
const DEFERRED = { ok: true, applied: false, reason: 'reboot_required', status: 'applied' };
const T_DEFERRED = 'Сетевые настройки сохранены, но применятся только после перезагрузки устройства.';
{ // S8
  const w = await run(resp({ status: 200, body: DEFERRED }));
  const warn = w.rec.toasts.filter((t) => t.type === 'warn');
  let reload = null;
  try { reload = vm.runInContext('configLoaded', w.sb); } catch (e) { reload = 'unreadable'; }
  if (w.rec.success === 0 && w.rec.toasts.length === 1 && warn.length === 1 && warn[0].msg === T_DEFERRED && reload === false) {
    ok('S8: applied:false -> one warning toast (saved, applies after the reboot), onSuccess NOT run, config marked for reload');
  } else bad(`S8: success=${w.rec.success} configLoaded=${reload} toasts=${JSON.stringify(w.rec.toasts)}`);
}
{ // S9
  const w = await run(resp({ status: 200, body: { ok: true } }));
  if (w.rec.success === 1 && w.rec.toasts.length === 0) ok('S9: a JSON {"ok":true} without applied:false -> still a success');
  else bad(`S9: success=${w.rec.success} toasts=${JSON.stringify(w.rec.toasts)}`);
}

{ // S7 — the refusal texts the panel can show, through the shipped i18n runtime
  const sb = {
    localStorage: { getItem: () => null, setItem: () => {} },
    document: {
      readyState: 'complete', addEventListener: () => {}, documentElement: {}, body: null,
      getElementById: () => null, querySelectorAll: () => [], createTreeWalker: () => ({ nextNode: () => null }),
    },
    MutationObserver: function () { this.observe = () => {}; this.disconnect = () => {}; },
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3, DOCUMENT_NODE: 9 },
    NodeFilter: { SHOW_TEXT: 4, SHOW_ELEMENT: 1 },
    requestAnimationFrame: () => {},
    console,
  };
  sb.window = sb; sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(I18N, 'utf8'), sb, { filename: 'i18n.js' });
  const api = sb.window.sa02mI18n;
  if (!api || typeof api.t !== 'function' || typeof api.setLang !== 'function') { bad('S7: i18n.js defined no usable sa02mI18n'); }
  else {
    api.setLang('en');
    if (api.t('Ошибка отправки') !== 'Submit failed') bad('S7: the i18n oracle does not translate a known string — no oracle');
    const f = world(resp({ status: 0 })).sb;
    const texts = typeof f.applyRefusalText === 'function' && typeof f.applyDeferredText === 'function'
      ? [f.applyRefusalText({ ok: false, error: 'conf_write_refused' }), f.applyRefusalText({ error: 'invalid input: ip' }),
         f.applyDeferredText(DEFERRED)]
      : [];
    if (texts.length !== 3 || texts.some((s) => !s)) bad('S7: forms.js lacks applyRefusalText/applyDeferredText giving all three texts');
    else {
      const untranslated = texts.filter((s) => { const en = api.t(s); return en === s || /[А-Яа-яЁё]/u.test(en); });
      if (untranslated.length) bad(`S7: no English for: ${untranslated.join(' | ')}`);
      else ok('S7: both refusal texts and the deferred-apply warning translate to English through the shipped i18n.js');
    }
  }
}

console.log('');
if (fails) { console.log(`test-apply-save-result: ${fails} FAILED`); process.exit(1); }
console.log('test-apply-save-result: all cases passed');
