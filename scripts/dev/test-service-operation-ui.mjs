// comment-mutation-proof-exempt: runs actual services.js renderer and polling functions with queued HTTP replies.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(process.env.SERVICE_UI_SOURCE || new URL('../../www/network_config/static/js/app/services.js', import.meta.url), 'utf8');
const appSource = fs.readFileSync(new URL('../../www/network_config/static/js/app.js', import.meta.url), 'utf8');
const stateHelpers = appSource.slice(appSource.indexOf('function normSvcState('), appSource.indexOf('function svcBadge('));
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
const context = vm.createContext({window: {}, document: {}, AbortController,
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
// A deterministic clock exercises the real request and operation lifecycles,
// including bodies which do not themselves react to an abort signal.
function clockContext(fetcher) {
  let now = 1000, next = 0;
  const timers = new Map(), toasts = [], renders = [];
  const host = {}, refresh = {disabled: false};
  const c = vm.createContext({window: {}, AbortController, Date: {now: () => now},
    setTimeout(fn, delay) { const id = ++next; timers.set(id, {at: now + delay, fn}); return id; },
    clearTimeout(id) { timers.delete(id); },
    document: {getElementById: id => id === 'svc-ctl-list' ? host : id === 'svc-ctl-refresh-btn' ? refresh : null},
    fetch: fetcher, confirm: () => true, withCsrfHeaders: h => h,
    toast: (...args) => toasts.push(args), fetchBackgroundPart() {}, fetchPriorityPart() {},
  });
  vm.runInContext(source, c);
  vm.runInContext(stateHelpers, c);
  c.renderServicesControl = value => renders.push(value);
  c.showServicesControlNow = () => {};
  c.servicesSubOpen = () => true;
  c.markServicesControlNoAnswer = () => renders.push('no-answer');
  const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
  return {c, toasts, renders, refresh, timers, flush,
    async advance(ms) {
      const end = now + ms;
      await flush();
      while (true) {
        const due = [...timers].filter(([,v]) => v.at <= end).sort((a,b) => a[1].at - b[1].at)[0];
        if (!due) break;
        now = due[1].at; timers.delete(due[0]); due[1].fn(); await flush();
      }
      now = end; await flush();
    },
  };
}
const response = data => ({ok: true, status: 200, json: async () => data});
for (const phase of ['post','result','state','list']) for (const stall of ['fetch','body']) {
  try {
    let calls = 0, signal;
    const t = clockContext((_url, options) => {
      calls++; signal = options.signal;
      return stall === 'fetch' ? new Promise(() => {}) : Promise.resolve({ok: true, json: () => new Promise(() => {})});
    });
    let settled = false, error;
    if (phase === 'post') {
      const button = {dataset: {svcId: 'vplc', svcAction: 'stop'}, closest: () => null};
      t.c.loadServicesControl = () => {};
      t.c.serviceCtlAction(button).then(() => { settled = true; });
      t.c.serviceCtlAction(button);
    } else if (phase === 'list') {
      t.c.loadServicesControl(false);
    } else {
      const p = phase === 'result' ? t.c.pollServiceCtlResult('vplc', 90000, 2000, current, 'stop')
        : t.c.pollServiceCtlState('vplc', 'stop', 90000, 2000);
      p.then(() => { settled = true; }, e => { settled = true; error = e.message; });
    }
    await t.advance(22000);
    assert.equal(calls, 1, 'no repeated request after a stalled response');
    assert.equal(signal?.aborted, true, 'the transport must also be aborted');
    assert.equal(t.timers.size, 0, 'request/operation timers cleared');
    if (phase === 'list') {
      assert.equal(t.refresh.disabled, false);
      assert.deepEqual(t.renders, ['no-answer']);
    } else {
      assert.equal(settled, true, `${phase}/${stall} remains pending`);
      if (phase === 'post') {
        assert.equal(vm.runInContext('!!_svcCtlPending.vplc', t.c), false);
        assert.equal(t.toasts.filter(v => v[1] === 'success').length, 0);
        assert.equal(t.toasts.filter(v => v[1] === 'error').length, 1);
      } else assert.equal(error, 'timeout');
    }
    console.log(`PASS ${phase}: bounded stalled ${stall}, abort and cleanup`);
  } catch (e) { failures++; console.error(`FAIL ${phase}/${stall}: ${e.message}`); }
}
try {
  const requests = [];
  let t;
  t = clockContext((url) => {
    requests.push({url, at: t.c.Date.now()});
    if (url.includes('result=1')) return Promise.resolve(response({ok: true, id: 'vplc', action: 'stop', op_id: current}));
    return Promise.resolve(response({ok: true, services: [{id: 'vplc', installed: true, active: 'active', enabled: true}]}));
  });
  let error;
  const p = t.c.pollServiceCtlDone('vplc', 'stop', 2000, 2000, current).catch(e => {error = e.message;});
  await t.advance(1500);
  assert.equal(requests.length, 2, 'post-result state verification must start immediately');
  assert.equal(requests[1].at, requests[0].at);
  await t.advance(500);
  await p;
  assert.equal(error, 'timeout', 'state stage must not restart the result-stage budget');
  assert.equal(requests.length, 2, 'no request after the absolute deadline');
  assert.equal(t.timers.size, 0);
  console.log('PASS immediate state verification and one shared absolute deadline');
} catch (e) { failures++; console.error('FAIL shared deadline: ' + e.message); }
for (const action of ['stop','install']) {
  try {
    let t, postResolve, posts = 0, settled = false;
    const expectedBudget = action === 'install' ? 600000 : 90000;
    const requests = [];
    t = clockContext((url, options) => {
      requests.push({url, at: t.c.Date.now()});
      if (options.method === 'POST') { posts++; return new Promise(resolve => {postResolve = resolve;}); }
      return Promise.resolve(response({ok: true, pending: true, id: 'vplc', action, op_id: current}));
    });
    t.c.loadServicesControl = () => {};
    const button = {dataset: {svcId: 'vplc', svcAction: action}, closest: () => null};
    t.c.serviceCtlAction(button).then(() => {settled = true;});
    await t.advance(19000);
    postResolve(response({ok: true, pending: true, id: 'vplc', action, op_id: current}));
    await t.advance(expectedBudget - 19001);
    assert.equal(settled, false);
    assert.equal(vm.runInContext('!!_svcCtlPending.vplc', t.c), true);
    await t.advance(1);
    assert.equal(settled, true, 'the POST time is part of the operation budget');
    assert.equal(posts, 1);
    assert.equal(vm.runInContext('!!_svcCtlPending.vplc', t.c), false);
    assert.equal(t.toasts.filter(v => v[1] === 'success').length, 0);
    assert.equal(t.toasts.filter(v => v[1] === 'error').length, 1);
    assert.ok(requests.every(v => v.at < 1000 + expectedBudget));
    assert.equal(t.timers.size, 0);
    console.log(`PASS ${action}: POST and result polling share ${expectedBudget / 1000}s budget`);
  } catch (e) { failures++; console.error(`FAIL ${action} admission budget: ` + e.message); }
}
try {
  let resolveOld;
  const t = clockContext(() => resolveOld ? Promise.resolve(response({ok:true, services:[], marker:'new'}))
    : new Promise(resolve => {resolveOld = resolve;}));
  t.c.servicesSubOpen = () => false;
  t.c.loadServicesControl(false); await t.flush();
  t.c.loadServicesControl(false); await t.flush();
  resolveOld(response({ok:true, services:[], marker:'old'})); await t.flush();
  assert.equal(t.renders.length, 1);
  assert.equal(t.renders[0].marker, 'new');
  assert.equal(t.refresh.disabled, false);
  assert.equal(t.timers.size, 0);
  console.log('PASS older list generation cannot replace the current list');
} catch (e) { failures++; console.error('FAIL list generation: ' + e.message); }
if (failures) process.exit(1);
