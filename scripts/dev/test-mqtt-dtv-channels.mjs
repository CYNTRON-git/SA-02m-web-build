#!/usr/bin/env node
// comment-mutation-proof-exempt: behavioral tests execute the shipped module;
// no source-line pins. RED on the original AUTO count and render mutation.
import assert from 'node:assert/strict';
import { loadMqttJs, El } from './mqtt-js-sandbox.mjs';

const keys = ['temp_ds18b20', 'temp_mcp9808', 'temp_hdc1080', 'temp_bme280',
  'temp_bme680', 'temp_ext', 'humidity_hdc1080', 'humidity_bme280', 'humidity_bme680',
  'pressure_bme280_mmhg', 'pressure_bme680_mmhg', 'pressure_bme280_kpa', 'pressure_bme680_kpa',
  'altitude_bme280', 'altitude_bme680', 'gas_resist_bme680', 'iaq_bme680', 'eco2_bme680',
  'tvoc_zmod', 'iaq_zmod', 'eco2_zmod', 'etoh_zmod', 'light_pct', 'input_pb2', 'presence',
  'moving_distance', 'still_distance', 'detect_distance'];
const capability = { sensor_keys: keys, always_polled_keys: ['buzzer', 'leds'],
  poll_defaults: { poll_sensors_s: 10, poll_presence_s: 2, poll_diag_s: 60, poll_uptime_s: 5 } };
// This DOM operation is the only extra surface needed by the DTV renderer.
El.prototype.insertBefore = function (node, before) {
  const index = this.children.indexOf(before);
  node.parent = this;
  if (index < 0) this.children.push(node); else this.children.splice(index, 0, node);
  return node;
};
const plain = value => JSON.parse(JSON.stringify(value));
let failures = 0;
async function check(name, fn) {
  try { await fn(); console.log('PASS ' + name); }
  catch (error) { failures++; console.error('FAIL ' + name + ': ' + error.message); }
}
async function setup(dev, caps = capability, lang = 'ru') {
  const s = loadMqttJs({ lang });
  s.ctx.CSS = { escape: value => String(value) };
  s.ctx.fetch = async () => ({ ok: true, json: async () => plain({ devices: [dev],
    capabilities: { dtv_channels: caps } }) });
  await s.run('loadConfig()');
  s.ctx.container = new El('div');
  return s;
}
const auto = { id: 'dtv-COM3-15', type: 'dtv', port: '/dev/COM3', address: 15,
  fast_modbus: true, custom: { keep: 1 } };
await check('AUTO never reports zero', async () => {
  const s = await setup(auto);
  assert.equal(s.run('countChannelsEnabled(_config.devices[0])'), null);
  assert.equal(s.run('countPollEnabled(_config.devices[0])'), null);
});
await check('viewing DTV never writes defaults or selection', async () => {
  const s = await setup(auto);
  s.run('buildDTVChannels(_config.devices[0], container)');
  assert.deepEqual(plain(s.run('_config.devices[0]')), auto);
  assert.equal(s.run('_unsaved'), false);
});
await check('ALL counts sensors, outputs and system rows', async () => {
  const s = await setup({ ...auto, sensors_present: [] });
  assert.equal(s.run('countChannelsEnabled(_config.devices[0])'), 30);
  assert.equal(s.run('countChannelsTotal(_config.devices[0])'), 30);
  assert.equal(s.run('countPollEnabled(_config.devices[0])'), 33);
});
await check('explicit counts ignore unknown and duplicate/coil mask entries', async () => {
  const s = await setup({ ...auto, sensors_present: ['temp_ext', 'temp_ext', 'future', 'buzzer', 'leds'],
    channels: { sys: [{ key: 'mcu_temp', enabled: false }] } });
  assert.equal(s.run('countChannelsEnabled(_config.devices[0])'), 3);
  assert.equal(s.run('countPollEnabled(_config.devices[0])'), 5);
});
await check('absent and malformed capability do not guess numeric counts', async () => {
  for (const caps of [null, {}, { ...capability, sensor_keys: [] },
    { ...capability, sensor_keys: ['bad/key'] },
    { ...capability, sensor_keys: ['x', 'x'] },
    { ...capability, sensor_keys: Array(65).fill('x') },
    { ...capability, always_polled_keys: ['temp_ext'] },
    { ...capability, poll_defaults: { ...capability.poll_defaults, poll_sensors_s: 0 } }]) {
    const s = await setup({ ...auto, sensors_present: [] }, caps);
    assert.equal(s.run('countChannelsTotal(_config.devices[0])'), null);
    s.run('buildDTVChannels(_config.devices[0], container)');
    assert.deepEqual(plain(s.run('_config.devices[0]')), { ...auto, sensors_present: [] });
  }
});
await check('first edit expands only supported sensors from AUTO and ALL', async () => {
  for (const mask of [undefined, null, []]) {
    const dev = { ...auto };
    if (mask !== undefined) dev.sensors_present = mask;
    const s = await setup(dev);
    assert.equal(s.run('dtvSelectionChange(_config.devices[0], "temp_hdc1080", false)'), true);
    assert.deepEqual(plain(s.run('_config.devices[0].sensors_present')), keys.filter(k => k !== 'temp_hdc1080'));
    assert.equal(s.run('_unsaved'), true);
  }
});
await check('last recognized sensor refusal preserves dirty flag and original entries', async () => {
  const dev = { ...auto, sensors_present: ['future', 'temp_ext', 'buzzer', 'temp_ext'] };
  for (const dirty of [false, true]) {
    const s = await setup(dev);
    s.run('_unsaved = ' + dirty);
    assert.equal(s.run('dtvSelectionChange(_config.devices[0], "temp_ext", false)'), false);
    assert.deepEqual(plain(s.run('_config.devices[0]')), dev);
    assert.equal(s.run('_unsaved'), dirty);
  }
});
await check('explicit edit preserves unknown fields/order and only the requested key changes', async () => {
  const dev = { ...auto, sensors_present: ['future', 'temp_ext', 'buzzer', 'temp_hdc1080', 'leds'] };
  const s = await setup(dev);
  s.run('dtvSelectionChange(_config.devices[0], "temp_ext", false)');
  assert.deepEqual(plain(s.run('_config.devices[0].sensors_present')), ['future', 'buzzer', 'temp_hdc1080', 'leds']);
  s.run('dtvSelectionChange(_config.devices[0], "temp_ext", true)');
  assert.deepEqual(plain(s.run('_config.devices[0].sensors_present')), ['future', 'buzzer', 'temp_hdc1080', 'leds', 'temp_ext']);
});
await check('successive single-key additions preserve the current list order', async () => {
  const s = await setup({ ...auto, sensors_present: ['future', 'temp_ext', 'buzzer'] });
  s.run('dtvSelectionChange(_config.devices[0], "temp_mcp9808", true)');
  s.run('dtvSelectionChange(_config.devices[0], "temp_ds18b20", true)');
  assert.deepEqual(plain(s.run('_config.devices[0].sensors_present')),
    ['future', 'temp_ext', 'buzzer', 'temp_mcp9808', 'temp_ds18b20']);
});
await check('missing/null/ALL/unknown-only inspection is pure with Fast absent/true/false', async () => {
  for (const mask of [undefined, null, [], ['future']]) for (const fast of [undefined, true, false]) {
    const dev = { ...auto }; delete dev.fast_modbus;
    if (mask !== undefined) dev.sensors_present = mask;
    if (fast !== undefined) dev.fast_modbus = fast;
    const s = await setup(dev);
    s.run('buildDTVChannels(_config.devices[0], container)');
    assert.deepEqual(plain(s.run('_config.devices[0]')), dev);
    assert.equal(s.run('_unsaved'), false);
  }
});
if (failures) process.exitCode = 1;
