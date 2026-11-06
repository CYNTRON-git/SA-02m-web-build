// comment-mutation-proof-exempt: runs actual services.js renderer and polling functions with queued HTTP replies.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../../www/network_config/static/js/app/services.js', import.meta.url), 'utf8');
const old = 'a'.repeat(32), current = 'b'.repeat(32);
let requests = 0;
const replies = [
  {ok: true, id: 'vplc', action: 'stop', op_id: old},
  {ok: false, error: 'old_failure', id: 'vplc', action: 'stop', op_id: old},
  {ok: true, id: 'mqtt-bridge', action: 'stop', op_id: current},
  {ok: true, id: 'vplc', action: 'start', op_id: current},
  {ok: true, pending: true, id: 'vplc', action: 'stop', op_id: current},
  {ok: true, id: 'vplc', action: 'stop', op_id: current},
];
const context = vm.createContext({window: {}, document: {},
  setTimeout: fn => setImmediate(fn), clearTimeout,
  fetch: async () => ({ok: true, json: async () => {requests++; return replies.shift();}}),
});
vm.runInContext(source, context);
let failures = 0;
try {
  const result = await context.pollServiceCtlResult('vplc', 1000, 1, current, 'stop');
  assert.equal(result.op_id, current);
  assert.equal(requests, 6);
  console.log('PASS ignores earlier success/error and waits matching completion');
} catch (e) { failures++; console.error('FAIL operation correlation: ' + e.message); }
try {
  vm.runInContext("if (typeof _svcCtlPending !== 'undefined') _svcCtlPending.vplc = 'stop'", context);
  const button = {dataset: {svcId: 'vplc', svcAction: 'stop'}, disabled: true};
  context.svcCtlSyncButton({querySelector: () => button}, {id: 'vplc'}, 'stop', false);
  assert.equal(button.disabled, true);
  console.log('PASS pending command stays disabled on poll');
} catch (e) { failures++; console.error('FAIL pending state: ' + e.message); }
try {
  context.document.createElement = () => ({dataset: {}, addEventListener() {}});
  assert.equal(context.makeSvcCtlButton({id: 'vplc'}, 'start', false).disabled, true);
  let resolvePost, posts = 0;
  context.fetch = () => { posts++; return new Promise(resolve => {resolvePost = resolve;}); };
  context.confirm = () => true;
  context.withCsrfHeaders = h => h;
  context.toast = () => {};
  context.loadServicesControl = () => {};
  vm.runInContext('delete _svcCtlPending.vplc;', context);
  const button = {dataset: {svcId: 'vplc', svcAction: 'stop'}, closest: () => null};
  const first = context.serviceCtlAction(button);
  context.serviceCtlAction(button);
  assert.equal(posts, 1);
  resolvePost({ok: true, json: async () => ({ok: false, error: 'port_busy'})});
  await first;
  assert.equal(vm.runInContext('!!_svcCtlPending.vplc', context), false);
  console.log('PASS recreated button stays blocked, double click sends one POST, failure clears pending');
} catch (e) { failures++; console.error('FAIL reentry: ' + e.message); }
if (failures) process.exit(1);
