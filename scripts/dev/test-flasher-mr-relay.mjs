// comment-mutation-proof-exempt: backend-generated capabilities, shipped UI and real HTTP.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
import {chromium} from './node_modules/playwright/index.mjs';
const root = fileURLToPath(new URL('../../', import.meta.url));
const source = `import json
from unittest.mock import patch
from sa02m_flasher import device_config as dc, module_profiles as mp
from tests.test_mr_relay_capabilities import RelayBus
from tests.test_mr_readback_truth import device
out={}
for code in (1,2,4,8,11,15):
 for mode in mp.mr_relay_capabilities(mp.kind_from_type_code(code))['modes']:
  bus=RelayBus(code,mode)
  with patch.object(dc,'_open_transport',return_value=(bus,bus.close)):
   out[str(code)+':'+str(mode)]=dc.snapshot_for_device('unused',device(code))
print(json.dumps(out))`;
const generated = spawnSync(process.env.PYTHON || 'python3', ['-c', source], {cwd: root, env: {...process.env, PYTHONPATH: fileURLToPath(new URL('../../opt/sa02m-flasher', import.meta.url))}, encoding: 'utf8'});
assert.equal(generated.status, 0, generated.stderr);
const fixtures = JSON.parse(generated.stdout);
const www = new URL('../../www/network_config/', import.meta.url);
let code = 1, mode = 2, unknown = false, requests = [];
const snapshot = () => {const value = structuredClone(fixtures[`${code}:${mode}`]); if (unknown) {value.mr.relay.mode_known = false; value.mr.relay.options_known = false; value.mr.relay.fans = {known: false};} return value;};
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    let data = {ok: true, jobs: [], entries: []};
    if (url.pathname.endsWith('/ports')) data = {ports: [{key: 'COM4', exists: true, reserved: true, busy_pids: [], active_services: []}]};
    if (url.pathname.endsWith('/ports/release')) data = {ok: true, port: {reserved: true}};
    if (url.pathname.includes('/device_config/')) {
      const chunks = []; for await (const chunk of req) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString());
      if (!url.pathname.endsWith('/snapshot')) requests.push({route: url.pathname.split('/').pop(), body});
      if (url.pathname.endsWith('/import')) {
        if (body.configuration.settings.relay.mode != null) mode = body.configuration.settings.relay.mode;
        data = {ok: true, complete: false, applied: [], failed: [], unavailable: ['do.1.safe']};
      } else data = {ok: true, ...snapshot()};
    }
    res.end(JSON.stringify(data)); return;
  }
  try {
    const file = new URL('.' + url.pathname, www); assert.ok(file.href.startsWith(www.href));
    let data = await readFile(file);
    if (url.pathname.endsWith('/flasher.js')) data = data.toString().replace(/\}\)\(\);\s*$/, 'window.mrTest={state,openConfigModal,closeConfigModal,stopConfigPolling,refreshConfigSnapshot,toggleModuleDo,saveModuleDi,saveModuleRelay};\n})();');
    res.setHeader('Content-Type', url.pathname.endsWith('.js') ? 'text/javascript' : url.pathname.endsWith('.css') ? 'text/css' : url.pathname.endsWith('.svg') ? 'image/svg+xml' : 'text/html'); res.end(data);
  } catch {res.writeHead(404); res.end();}
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await chromium.launch({headless: true});
let failures = 0;
try {
  for (const [theme, width, lang] of [['dark', 1600, 'ru'], ['light', 500, 'en']]) {
    const context = await browser.newContext({viewport: {width, height: 1000}});
    await context.addCookies([{name: 'session_token', value: 'test', url: origin}]);
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message)); page.on('dialog', dialog => dialog.accept());
    await page.goto(origin + '/index.html');
    await page.evaluate(({theme, lang}) => {teardownStatusPolling(); switchTab('flasher'); document.documentElement.setAttribute('data-theme', theme); sa02mI18n.setLang(lang);}, {theme, lang});
    await page.locator('#flasher-port').selectOption('COM4');
    const idle = () => page.waitForFunction(() => !mrTest.state.configBusy && !mrTest.state.configBackgroundBusy);
    const open = async (nextCode, nextMode) => {
      code = nextCode; mode = nextMode; unknown = false; requests = [];
      const info = snapshot().info;
      await page.evaluate(async info => {if (mrTest.state.configOpen) mrTest.closeConfigModal(); mrTest.state.devices = [{...info, address: 2, baudrate: 9600, parity: 'N', stopbits: 1}]; await mrTest.openConfigModal(0); mrTest.stopConfigPolling();}, info);
      await idle();
    };
    const tab = async id => {await page.locator(`[data-config-tab="${id}"]`).click(); await idle();};
    const check = async (name, fn) => {try {await fn(); console.log('PASS', theme, name);} catch (error) {failures++; console.error('FAIL', theme, name, error.message);}};
    for (const model of [1, 2, 4, 8, 11, 15]) {
      await open(model, 0); await tab('relay');
      await check('capability modes type ' + model, async () => assert.deepEqual(await page.locator('#cfg-mr-relay-mode option').evaluateAll(options => options.map(option => Number(option.value))), fixtures[`${model}:0`].mr.module.relay_capabilities.modes));
    }
    await open(1, 2); await tab('do_1');
    await check('manual transition warning is visible before action', async () => assert.match(await page.locator('#cfg-mr-do-command-note').innerText(), /Modbus/));
    await page.locator('[data-mr-do-on="1"]').click(); await idle();
    await check('ACK does not optimistically turn physical output on', async () => {
      assert.equal(requests.at(-1).route, 'coil'); assert.equal(requests.at(-1).body.value, true);
      assert.match(await page.locator('#cfg-mr-do-state-1').innerText(), /Выкл|Off/i);
    });
    await open(8, 5); await tab('relay');
    await check('two physical drives expose independent direction cancellation while OFF', async () => {
      assert.equal(await page.locator('[data-relay-drive]').count(), 8);
      assert.equal(await page.locator('[data-relay-drive="1"][data-relay-on="false"]').isDisabled(), false);
    });
    await page.locator('[data-relay-drive="1"][data-relay-on="true"]').click(); await idle();
    await page.locator('[data-relay-drive="1"][data-relay-on="false"]').click(); await idle();
    await check('open/cancel send explicit mode-preserving intent once', async () => assert.deepEqual(requests.map(request => [request.route, request.body.relay_action, request.body.coil, request.body.value]), [['coil', 'drive', 1, true], ['coil', 'drive', 1, false]]));
    await page.locator('#flasher-config-body details summary').click();
    await check('RAM limitation and named drive fields are visible', async () => {
      assert.match(await page.locator('#flasher-config-body details').innerText(), /RAM/);
      assert.match(await page.locator('label').filter({has: page.locator('[data-relay-field="relay.drives.channels.1.opening_deciseconds"]')}).innerText(), /Привод 1|Drive 1/);
    });
    await page.locator('[data-relay-field="relay.drives.channels.1.opening_deciseconds"]').fill('20');
    await page.locator('#cfg-mr-relay-advanced-save').click(); await idle();
    await check('advanced settings use one typed scoped operation', async () => {
      const request = requests.at(-1); assert.equal(request.route, 'import'); assert.equal(request.body.allow_partial, true);
      assert.equal(request.body.configuration.settings.relay.drives.channels[0].opening_deciseconds, 20);
    });
    await open(11, 4); await tab('do_2');
    await check('fan-owned DO2 cannot issue a request', async () => {
      assert.equal(await page.locator('[data-mr-do-on="2"]').isDisabled(), true);
      await page.evaluate(() => mrTest.toggleModuleDo(2, true)); assert.equal(requests.length, 0);
    });
    await tab('di_1');
    await check('fan mode blocks DI settings before any request', async () => {
      assert.equal(await page.locator('[data-mr-di-save="1"]').isDisabled(), true);
      await page.evaluate(() => mrTest.saveModuleDi(1)); assert.equal(requests.length, 0);
    });
    await tab('do_1'); await page.locator('[data-mr-do-on="1"]').click(); await idle();
    await check('fan start uses only coil1 explicit intent', async () => assert.deepEqual([requests.at(-1).body.relay_action, requests.at(-1).body.coil], ['fan', 1]));
    await tab('relay'); await page.locator('#cfg-mr-relay-stagger').fill('22');
    unknown = true; await page.evaluate(() => mrTest.refreshConfigSnapshot(true, 'panel')); await idle();
    await check('unknown mode preserves edit and disables dependent actions', async () => {
      assert.equal(await page.locator('#cfg-mr-relay-stagger').inputValue(), '22');
      assert.equal(await page.locator('#cfg-mr-relay-save-btn').isDisabled(), true);
      assert.match(await page.locator('#cfg-mr-relay-observed').innerText(), /—/);
      const count = requests.length; await page.evaluate(() => mrTest.saveModuleRelay()); assert.equal(requests.length, count);
      assert.deepEqual(errors, []);
    });
    await context.close();
  }
} finally {await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));}
process.exitCode = failures ? 1 : 0;
