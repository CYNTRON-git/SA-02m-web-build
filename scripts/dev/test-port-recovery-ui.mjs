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
const tests = [];
function test(name, body) { tests.push([name, body]); }
test('re-scan with COM5 occupied admits only the selected COM4 scan', async () => {
  const posted = [];
  const context = vm.createContext({state: {ports: [{key: 'COM5', busy_pids: ['99']}]},
    $: () => ({value: 'COM4'}), hidePollerBusyOffer() {}, startScan: async () => posted.push('scan'),
    releasePortPollers: async () => {posted.push('global release'); return false;}});
  vm.runInContext(extract('stopPollersAndRescan'), context);
  await context.stopPollersAndRescan('COM4');
  assert.deepEqual(posted, ['scan']);
});
test('re-scan identifies gateway and unknown PID, offers retry for a configured holder', () => {
  const offer = {}, port = {key: 'COM4', busy_pids: ['12'], active_services: ['RS-485 Gateway'], managed_services: ['MQTT']};
  const context = vm.createContext({state: {devices: [], ports: [port]}, pollerBusyOfferEl: () => offer,
    $: () => null, t: s => s, escapeHtml: s => s, unitUiLabel: s => s});
  vm.runInContext(extract('portHolderLabels') + '\n' + extract('maybeOfferPollerBusy'), context);
  context.maybeOfferPollerBusy('error', 'COM4');
  assert.match(offer.innerHTML, /Gateway/); assert.doesNotMatch(offer.innerHTML, /<button/);
  port.active_services = []; context.maybeOfferPollerBusy('error', 'COM4');
  assert.match(offer.innerHTML, /PID 12/); assert.doesNotMatch(offer.innerHTML, /<button/);
  port.active_services = ['MQTT']; context.maybeOfferPollerBusy('error', 'COM4');
  assert.match(offer.innerHTML, /<button/);
});
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
test('opening and closing a module uses the operation lease and preserves independent manual reserves', async () => {
  const events = [], idle = () => {};
  const modalState = {devices: [{address: 12}], ports: [{key: 'COM5', reserved: true}]};
  const context = vm.createContext({state: modalState, isFlasherOperationActive: () => false,
    isDeviceConfigSupported: () => true, stopConfigPolling: idle, setConfigPollError: idle,
    clearAiConfigEditState: idle, configModalEl: () => ({}), document: {body: {style: {}}},
    ensureConfigCloseEnabled: idle, buildConfigSnapshotStubFromDevice: () => null, renderConfigBody: idle,
    refreshConfigSnapshot: async () => events.push('selected snapshot'), startConfigPolling: idle,
    apiPost: name => events.push(name)});
  vm.runInContext('let _configPollSeq = 0;\n' + extract('openConfigModal') + '\n' + extract('closeConfigModal'), context);
  await context.openConfigModal(0); context.closeConfigModal();
  assert.deepEqual(events, ['selected snapshot']);
  assert.equal(modalState.ports[0].reserved, true);
});
test('background polling never repeatedly stops a whole service and can borrow a manual reservation', () => {
  let reads = 0;
  const port = {whole_service_scope: ['mplc4.service']};
  const context = vm.createContext({state: {configOpen: true}, currentPort: () => port,
    refreshConfigSnapshot: () => reads++});
  vm.runInContext(extract('configPollTick'), context);
  context.configPollTick(); context.configPollTick(); assert.equal(reads, 0);
  port.reserved = true; context.configPollTick(); assert.equal(reads, 1);
  port.reserved = false; port.whole_service_scope = []; context.configPollTick(); assert.equal(reads, 2);
});
test('automatic snapshot success cannot erase the persistent recovery banner', () => {
  vm.runInContext(extract('renderConfigBanner') + '\n' + extract('setConfigBanner'), ctx);
  state.portRecoveryError = 'COM4 busy; restore COM1';
  ctx.setConfigBanner('', '');
  assert.equal($('flasher-config-banner').hidden, false);
  assert.equal($('flasher-config-banner').textContent, state.portRecoveryError);
});
test('fresh page shows broker recovery separately from manual reserves', () => {
  selected.reserved = false;
  state.ports = [selected, {key: 'COM1', restore_pending: true, restore_services: ['mplc4.service'], restore_error: 'start failed', coordinator_available: false}];
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
