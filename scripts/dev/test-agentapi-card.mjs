#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it runs the shipped app/agentapi.js against a stub DOM and fetch and asserts the rendered card and the requests made, pinning no source line by text; a commented-out line changes the result it measures.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-agentapi-card — the card half of docs/contracts/agent-api.md
   «Плата без модуля» (the CGI half is install-profile P11).
     - GET `state: not_installed` → the state line reads «Не установлен на этой
       плате», #agentapi-body (the whole form) is hidden, no failure message;
     - a normal answer → the form is shown again (re-render survival), the
       state line and the toggle read as before;
     - a POST answered `not_installed` (toggle, create, revoke) → the state is
       re-read with a GET and no «Не удалось выполнить действие» is shown.
   The SHIPPED file runs whole in a vm sandbox: stub document (elements by id,
   listeners, createElement), stub fetch (records method + answers from a
   queue), stub withCsrfHeaders. No browser.
   RED, measured on a scratch copy (AGENTAPI_JS): the render branch deleted
   (state «Служба API выключена», form shown), the toggle / create / revoke
   `not_installed` lines deleted (failure message shown; create re-reads
   nothing).
   Run: node scripts/dev/test-agentapi-card.mjs
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = process.env.AGENTAPI_JS
  || join(HERE, '..', '..', 'www', 'network_config', 'static', 'js', 'app', 'agentapi.js');

let failures = 0;
let passes = 0;
function check(cond, msg) {
  if (cond) { passes++; console.log('  ok   - ' + msg); }
  else { failures++; console.error('  FAIL - ' + msg); }
}

function el(id) {
  return {
    id, hidden: false, textContent: '', value: '', checked: false, disabled: false,
    attrs: {}, listeners: {}, children: [], title: '', className: '', type: '',
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    appendChild(c) { this.children.push(c); return c; },
    click() { (this.listeners.click || []).forEach(f => f()); },
  };
}
const IDS = ['agentapi-state', 'agentapi-body', 'agentapi-toggle', 'agentapi-rows', 'agentapi-name',
  'agentapi-days', 'agentapi-scope-read', 'agentapi-scope-control', 'agentapi-scope-config',
  'agentapi-scope-admin', 'agentapi-root', 'agentapi-pass-wrap', 'agentapi-pass', 'agentapi-create',
  'agentapi-copy', 'agentapi-secret', 'agentapi-msg'];
const dom = {};
IDS.forEach(id => { dom[id] = el(id); });
dom['agentapi-toggle'].textContent = 'Включить';

const requests = [];
const answers = [];
function fetchStub(url, opts) {
  const method = (opts && opts.method) || 'GET';
  requests.push(method);
  const body = answers.length ? answers.shift() : { ok: true, active: 'inactive', tokens: [] };
  return Promise.resolve({ json: () => Promise.resolve(body) });
}
const sandbox = {
  window: {},
  document: {
    readyState: 'complete',
    getElementById: id => dom[id] || null,
    createElement: tag => el(tag),
    addEventListener() {},
  },
  fetch: fetchStub,
  withCsrfHeaders: h => h,
  location: { origin: 'http://board' },
  navigator: {},
  console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
try {
  vm.runInContext(readFileSync(SRC, 'utf8'), sandbox, { filename: 'agentapi.js' });
} catch (e) {
  console.error('  FAIL - the shipped agentapi.js does not run in the sandbox: ' + e.message);
  process.exit(1);
}
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
const msg = () => dom['agentapi-msg'].textContent;
const FAIL_MSG = 'Не удалось выполнить действие';
const NOT_INSTALLED = { ok: true, state: 'not_installed', active: 'inactive', tokens: [], message: 'Agent API module is not installed on this board (install profile)' };
const POST_NOT_INSTALLED = { ok: false, error: 'not_installed', state: 'not_installed' };

check(typeof sandbox.window.agentApiTabInit === 'function', 'the card exposes agentApiTabInit');

// 1. GET not_installed
answers.push(NOT_INSTALLED);
sandbox.window.agentApiTabInit();
await settle();
check(dom['agentapi-state'].textContent === 'Не установлен на этой плате',
  'GET not_installed: the state line reads «Не установлен на этой плате» — got «' + dom['agentapi-state'].textContent + '»');
check(dom['agentapi-body'].hidden === true, 'GET not_installed: the form (#agentapi-body) is hidden');
check(msg() === '', 'GET not_installed: no failure message — got «' + msg() + '»');

// 2. a normal answer afterwards shows the form again
answers.push({ ok: true, active: 'inactive', tokens: [{ id: 't1', name: 'bot', scopes: ['read'] }] });
sandbox.window.agentApiTabInit();
await settle();
check(dom['agentapi-body'].hidden === false, 'normal answer: the form is shown again');
check(dom['agentapi-state'].textContent === 'Служба API выключена', 'normal answer: the state line reads «Служба API выключена»');
check(dom['agentapi-toggle'].textContent === 'Включить', 'normal answer: the toggle reads «Включить»');
const row = dom['agentapi-rows'].children[0];
const revokeBtn = row && row.children[1];
check(!!revokeBtn, 'normal answer: a token row with its «Отозвать» button is rendered');

// 3. POSTs answered not_installed re-read the state, no failure message
async function postCase(label, fire) {
  dom['agentapi-msg'].textContent = '';
  requests.length = 0;
  answers.push(POST_NOT_INSTALLED, NOT_INSTALLED);
  fire();
  await settle();
  check(requests[0] === 'POST' && requests[1] === 'GET',
    label + ': POST not_installed is followed by a GET of the state — requests ' + JSON.stringify(requests));
  check(msg() !== FAIL_MSG, label + ': no «' + FAIL_MSG + '»');
  check(dom['agentapi-state'].textContent === 'Не установлен на этой плате' && dom['agentapi-body'].hidden === true,
    label + ': the card ends in the not-installed state');
  // back to a normal card for the next case
  answers.push({ ok: true, active: 'inactive', tokens: [{ id: 't1', name: 'bot', scopes: ['read'] }] });
  sandbox.window.agentApiTabInit();
  await settle();
}
await postCase('toggle', () => dom['agentapi-toggle'].click());
await postCase('create', () => dom['agentapi-create'].click());
await postCase('revoke', () => { const r = dom['agentapi-rows'].children[0]; r && r.children[1] && r.children[1].click(); });

console.log('');
if (failures) {
  console.error(`js-unit-agentapi-card: ${failures} assertion(s) failed`);
  process.exit(1);
}
console.log(`js-unit-agentapi-card: ALL OK — ${passes} assertion(s) green`);
