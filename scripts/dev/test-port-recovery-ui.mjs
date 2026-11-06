// comment-mutation-proof-exempt: executes shipped functions against observable UI state and API replies; assertions fail when behavior is removed.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const src = fs.readFileSync(new URL('../../www/network_config/static/js/flasher.js', import.meta.url), 'utf8');
function extract(name) {
  const match = new RegExp('(async )?function ' + name + '\\(').exec(src);
  assert.ok(match, name + ' exists');
  const start = src.indexOf('{', match.index);
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    if (src[i] === '{') depth++;
    if (src[i] === '}' && --depth === 0) return src.slice(match.index, i + 1);
  }
  throw Error('unterminated ' + name);
}
const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, {value: 'COM4', classList: {add() {}, remove() {}}, disabled: false});
  return nodes.get(id);
};
const selected = {key: 'COM4', exists: true, busy_pids: [], managed_services: ['MQTT'], released_services: []};
const state = {ports: [selected], firmware: []};
const messages = [];
let reply;
const calls = [];
const ctx = vm.createContext({state, $, currentPort: () => selected, uiT: s => s,
  isFlasherOperationActive: () => false, selectedDevicesForFlash: () => [],
  selectedFirmwareEntry: () => null, firmwareSelectionMismatch: () => false,
  updateFlashButtonLabel() {}, cancelInlineStatusAutoClear() {},
  unitUiLabel: s => s, toast: s => messages.push(s),
  apiPost: async path => { calls.push(path); return reply; }, loadPorts: async () => {},
  setConfigBanner() {},
  configModalEl: $, scheduleInlineStatusAutoClear() {},
  setBadge() {},
});
vm.runInContext(['syncActionButtons', 'portReleaseFailure', 'releasePortPollers', 'restorePortPollers', 'updatePortHint', 'reconcilePortRestoration'].map(extract).join('\n'), ctx);
vm.runInContext('let _portOpPromise = null;\n' + extract('_autoReleasePortForConfig') + '\n' + extract('_autoRestorePortForConfig'), ctx);
const tests = [];
function test(name, body) { tests.push([name, body]); }
test('reserved empty-scope port enables Restore', () => {
  selected.reserved = true;
  ctx.syncActionButtons();
  assert.equal($('flasher-restore-port-btn').disabled, false);
});
test('reservation on another COM enables global Restore', () => {
  selected.reserved = false;
  state.ports.push({key: 'COM1', reserved: true});
  ctx.syncActionButtons();
  assert.equal($('flasher-restore-port-btn').disabled, false);
});
test('empty-scope reserve is successful and permits rescan', async () => {
  reply = {ok: true, port: {...selected, reserved: true}, reserved_ports: ['/dev/COM4'], manual_reserved_ports: ['/dev/COM4'], failed: []};
  assert.equal(await ctx.releasePortPollers(), true);
  assert.match(messages.at(-1), /COM4/);
});
test('partial refusal names retained ports without false success', async () => {
  reply = {ok: false, port: selected, failed: ['/dev/COM4: UART already leased'], manual_reserved_ports: ['/dev/COM1']};
  assert.equal(await ctx.releasePortPollers(), false);
  assert.match(messages.at(-1), /COM1/);
  assert.match(messages.at(-1), /COM4/);
});
test('idle Restore disabled, active job refuses even with reservations', () => {
  state.ports = [selected]; selected.reserved = false;
  ctx.syncActionButtons(); assert.equal($('flasher-restore-port-btn').disabled, true);
  selected.reserved = true; selected.active_job = 'job';
  ctx.syncActionButtons(); assert.equal($('flasher-restore-port-btn').disabled, true);
  selected.active_job = null;
});
test('failed auto configuration retains partial reservations without blanket restore', async () => {
  state.configPortReleased = false;
  calls.length = 0;
  reply = {ok: false, port: selected, failed: ['/dev/COM4: busy'], manual_reserved_ports: ['/dev/COM1']};
  await ctx._autoReleasePortForConfig();
  assert.deepEqual(calls, ['/ports/release']);
  assert.equal(state.configPortReleased, false);
  assert.match(state.portRecoveryError, /COM1/);
});
test('failed auto restore keeps cleanup obligation until successful retry', async () => {
  state.configPortReleased = true; state.configReservedPort = 'COM4';
  reply = {ok: false, failed: ['restore failed']};
  await ctx._autoRestorePortForConfig();
  await vm.runInContext('_portOpPromise', ctx);
  assert.equal(state.configPortReleased, true);
  assert.match(state.portRecoveryError, /restore failed/);
  reply = {ok: true, failed: []};
  await ctx._autoRestorePortForConfig();
  await vm.runInContext('_portOpPromise', ctx);
  assert.equal(state.configPortReleased, false);
  assert.equal(state.portRecoveryError, '');
});
test('automatic snapshot success cannot erase the persistent recovery banner', () => {
  vm.runInContext(extract('setConfigBanner'), ctx);
  state.portRecoveryError = 'COM4 busy; restore COM1';
  ctx.setConfigBanner('', '');
  assert.equal($('flasher-config-banner').hidden, false);
  assert.equal($('flasher-config-banner').textContent, state.portRecoveryError);
});
test('fresh page shows broker recovery separately from manual reserves', () => {
  selected.reserved = false;
  state.ports = [selected, {key: 'COM1', restore_pending: true, restore_services: ['mplc4.service'], restore_error: 'start failed', coordinator_available: false}];
  state.configPortReleased = false;
  state.portRecoveryError = '';
  ctx.updatePortHint();
  assert.equal($('flasher-restore-port-btn').disabled, false);
  assert.match($('flasher-port-hint').textContent, /Ожидается восстановление опроса: COM1/);
  assert.match($('flasher-port-hint').textContent, /Координатор недоступен/);
  assert.doesNotMatch($('flasher-port-hint').textContent, /Ручной резерв:/);
});
test('repeated pending restore cannot erase the recovery error', async () => {
  reply = {ok: false, failed: ['COM1: start failed'], pending_restores: ['/dev/COM1']};
  await ctx.restorePortPollers();
  await ctx.restorePortPollers();
  assert.match(state.portRecoveryError, /start failed/);
  reply = {ok: true, failed: [], pending_restores: ['/dev/COM1']};
  await ctx.restorePortPollers();
  assert.match(state.portRecoveryError, /не подтверждено/);
});
test('only authoritative recovery completion clears the remembered error', () => {
  ctx.reconcilePortRestoration();
  assert.equal(state.portRestorePending, true);
  state.ports = [{key: 'COM1', restore_pending: false, coordinator_available: false}];
  ctx.reconcilePortRestoration();
  assert.ok(state.portRecoveryError);
  state.ports[0].coordinator_available = true;
  ctx.reconcilePortRestoration();
  assert.equal(state.portRecoveryError, '');
  assert.equal(state.portRestorePending, false);
});
let failures = 0;
for (const [name, body] of tests) {
  try { await body(); console.log('PASS ' + name); }
  catch (err) { failures++; console.error('FAIL ' + name + ': ' + err.message); }
}
if (failures) process.exit(1);
