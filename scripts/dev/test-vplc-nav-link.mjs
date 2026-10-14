#!/usr/bin/env node
// comment-mutation-proof-exempt: unit test - it evaluates the shipped applyVplcNavLink / svcStateIsActive and asserts the link's hidden flag, pinning no source line by text; a commented-out line changes the result it measures.
/* ═══════════════════════════════════════════════════════════════════════════
   js-unit-vplc-nav — the consumer half of `svc_vplc_ui`
   (docs/contracts/vplc-page-seam.md §4): the sidebar «vPLC ↗» link follows
   the services part's probe of the vPLC page server.
     - `active` / `running` / `activating` (any case, padded) -> shown;
     - `inactive` (and any other string) -> hidden;
     - `unknown`, a missing field, a non-string, no payload -> the link is
       LEFT AS IT IS (an older backend or a disabled part must not blink it),
       from both initial states;
     - no #nav-vplc element (a page without the link) -> no throw;
     - applyServicesStatus really calls it (the wiring, not just the helper).
   The producer enum (active|inactive) is pinned by `status-vplc-probe`.
   Same extraction idiom as test-alice-gateway-status.mjs: the functions are
   cut out of the SHIPPED app.js and app/status.js by brace matching and
   evaluated in a sandbox, so the test runs the served source.
   RED, measured on scratch copies: dropping the `=== 'unknown'` guard
   (unknown hides a shown link), dropping the typeof guard (a missing field
   hides it), dropping the `!link` guard (TypeError on a page without the
   link), dropping the applyVplcNavLink call from applyServicesStatus.
   Run: node scripts/dev/test-vplc-nav-link.mjs
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const JS = join(HERE, '..', '..', 'www', 'network_config', 'static', 'js');
const APP_JS = process.env.VPLC_NAV_APP_JS || join(JS, 'app.js');
const STATUS_JS = process.env.VPLC_NAV_STATUS_JS || join(JS, 'app', 'status.js');

function extractFn(src, name, file) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('function ' + name + '() not found in ' + file);
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name + '()');
}

let failures = 0;
let passes = 0;
function check(cond, msg) {
  if (cond) { passes++; console.log('  ok   - ' + msg); }
  else { failures++; console.error('  FAIL - ' + msg); }
}

let api;
try {
  const app = readFileSync(APP_JS, 'utf8');
  const status = readFileSync(STATUS_JS, 'utf8');
  api = new Function('document', 'renderServicesDynamic', [
    'let _lastServicesStatus = null;',
    extractFn(app, 'normSvcState', 'app.js'),
    extractFn(app, 'svcStateIsActive', 'app.js'),
    extractFn(status, 'applyVplcNavLink', 'app/status.js'),
    extractFn(status, 'applyServicesStatus', 'app/status.js'),
    'return { applyVplcNavLink, applyServicesStatus };',
  ].join('\n'));
} catch (e) {
  console.error('  FAIL - the shipped helpers could not be extracted: ' + e.message);
  console.error('js-unit-vplc-nav: 1 assertion(s) failed');
  process.exit(1);
}

const asked = [];
let link = null;
const doc = { getElementById(id) { asked.push(id); return id === 'nav-vplc' ? link : null; } };
const { applyVplcNavLink, applyServicesStatus } = api(doc, () => {});

function run(initialHidden, payload) {
  link = { hidden: initialHidden };
  applyVplcNavLink(payload);
  return link.hidden;
}
const show = JSON.stringify;

for (const v of ['active', 'running', 'activating', ' Active ']) {
  for (const init of [true, false]) {
    check(run(init, { svc_vplc_ui: v }) === false, `${show(v)} shows the link (initially hidden=${init})`);
  }
}
for (const v of ['inactive', 'failed']) {
  for (const init of [true, false]) {
    check(run(init, { svc_vplc_ui: v }) === true, `${show(v)} hides the link (initially hidden=${init})`);
  }
}
const untouched = [
  ['"unknown"', { svc_vplc_ui: 'unknown' }],
  ['a missing field', {}],
  ['null', { svc_vplc_ui: null }],
  ['a number', { svc_vplc_ui: 1 }],
  ['a boolean', { svc_vplc_ui: true }],
  ['no payload (null)', null],
  ['no payload (undefined)', undefined],
];
for (const [label, payload] of untouched) {
  for (const init of [true, false]) {
    let after;
    let threw = null;
    try { after = run(init, payload); } catch (e) { threw = e; }
    check(!threw && after === init, `${label} leaves the link as it is (hidden=${init})` + (threw ? ' — threw ' + threw.message : ''));
  }
}
check(asked.includes('nav-vplc'), 'the link is looked up by its index.html id «nav-vplc»');

link = null;
let threw = null;
try { applyVplcNavLink({ svc_vplc_ui: 'active' }); } catch (e) { threw = e; }
check(!threw, 'a page without #nav-vplc does not throw' + (threw ? ' — threw ' + threw.message : ''));

link = { hidden: true };
applyServicesStatus({ svc_vplc_ui: 'active' });
check(link.hidden === false, 'applyServicesStatus applies the link (the wiring, not only the helper)');

console.log('');
if (failures) {
  console.error(`js-unit-vplc-nav: ${failures} assertion(s) failed`);
  process.exit(1);
}
console.log(`js-unit-vplc-nav: ALL OK — ${passes} assertion(s) green`);
