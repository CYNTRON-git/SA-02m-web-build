#!/usr/bin/env node
// comment-mutation-proof-exempt: executes real resolved JSONata, Node-RED util APIs and a deployed Change/Switch flow; old packaged JSONata fails version and inert prototype-access regressions, with provenance required for prebuilt test payloads.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const vendor = path.join(repo, 'vendor-src/nodered');
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const fileHash = filename => hash(fs.readFileSync(filename));

function treeDigest(root) {
  const entries = [];
  const visit = relative => {
    const directory = path.join(root, relative);
    for (const name of fs.readdirSync(directory).sort()) {
      const child = path.join(relative, name), absolute = path.join(root, child);
      const info = fs.lstatSync(absolute), key = child.split(path.sep).join('/');
      if (info.isSymbolicLink()) {
        const target = fs.realpathSync(absolute);
        assert.ok(target.startsWith(root + path.sep), 'runtime link must remain inside the payload');
        entries.push([key, 'link', fs.readlinkSync(absolute)]);
      } else if (info.isDirectory()) visit(child);
      else { assert.ok(info.isFile()); entries.push([key, 'file', fileHash(absolute)]); }
    }
  };
  visit('');
  return hash(JSON.stringify(entries));
}

function runtimeRoot(scratch) {
  const supplied = process.env.SA02M_NODERED_TEST_ROOT;
  const receiptPath = process.env.SA02M_NODERED_TEST_RECEIPT;
  if (supplied || receiptPath) {
    assert.ok(supplied && receiptPath, 'prebuilt test runtime requires both root and provenance receipt');
    const root = fs.realpathSync(supplied), receipt = JSON.parse(fs.readFileSync(receiptPath));
    assert.equal(receipt.schema, 1);
    assert.equal(receipt.source_manifest_sha256, fileHash(path.join(vendor, 'package.json')));
    assert.equal(receipt.source_lock_sha256, fileHash(path.join(vendor, 'package-lock.json')));
    const archive = path.resolve(path.dirname(receiptPath), receipt.archive);
    assert.equal(fileHash(archive), receipt.archive_sha256);
    assert.equal(treeDigest(root), receipt.runtime_tree_sha256, 'prebuilt runtime must equal the recorded extracted payload');
    console.log('Verified prebuilt runtime, source manifest/lock and canonical archive provenance');
    return root;
  }
  const install = path.join(scratch, 'locked-install');
  fs.mkdirSync(install);
  for (const name of ['package.json', 'package-lock.json']) fs.copyFileSync(path.join(vendor, name), path.join(install, name));
  const result = spawnSync('npm', ['ci', '--omit=optional', '--ignore-scripts', '--no-audit', '--no-fund'],
    { cwd: install, encoding: 'utf8', timeout: 180000 });
  assert.equal(result.status, 0, result.error?.message || result.stdout + result.stderr);
  for (const name of ['package.json', 'package-lock.json']) assert.equal(fileHash(path.join(install, name)), fileHash(path.join(vendor, name)));
  return path.join(install, 'node_modules/node-red');
}

// Inert adaptations of the three upstream advisories. Constructors, if exposed,
// return only a sentinel string; no expression performs I/O or invokes an OS tool.
const security = {
  lookup: '$lookup({}, "constructor")',
  environment: `($hasOwnProperty := $spread($string); $__proto__ := $constructor; $constructor("return 'INERT_JSONATA_SENTINEL'")())`,
  transform: `(
    $obj := {}; $clone := function($o) { $o }; $m := ($merge.*)[1];
    $fn := function($a) { ($a({"value":"lg"},"__lookupGetter__"); $a({"value":"x"},"x")) };
    $nop := function() { $ };
    $capture := function($val) { $obj ~> | $obj | {"x":1,"y":1,"lg":$lg} | };
    $ ~> | $ | $m([$nop,{"_jsonata_lambda":false}]) |;
    $ ~> | $ | {"arguments":{"forEach":$spread($fn)}} |;
    $ ~> | $ | {"body":$m([$capture,{"_jsonata_lambda":false}]).body} |;
    $func := $m([$, {"_jsonata_lambda":true}]); $func();
    $gP := $obj.lg("__proto__"); $afn := $spread($fn);
    $afn{"x":$gP().constructor("return 'INERT_JSONATA_SENTINEL'")()}
  )`,
};
const securityWorker = `
const {createRequire}=require('node:module');
const jsonata=createRequire(process.argv[1])('jsonata');
(async()=>{try { const value=await jsonata(process.argv[2]).evaluate({});
  console.log(JSON.stringify({kind:typeof value, value:JSON.stringify(value,(_key,v)=>typeof v==='function'?'[Function]':v)}));
} catch(error) { console.log(JSON.stringify({error:error.code})); }})();
`;

function request(url, options = {}, body) {
  return new Promise((resolve, reject) => {
    const req = http.request(url, options, res => {
      let text = ''; res.setEncoding('utf8');
      res.on('data', data => { text += data; });
      res.on('end', () => resolve({status:res.statusCode, body:text}));
    });
    req.setTimeout(2000, () => req.destroy(new Error('local HTTP timeout')));
    req.on('error', reject); req.end(body);
  });
}
async function waitFor(predicate, message) {
  const deadline = performance.now() + 10000;
  while (performance.now() < deadline) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert.fail(message);
}

async function realFlow(root, scratch) {
  const reservation = http.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const userDir = path.join(scratch, 'runtime'); fs.mkdirSync(userDir);
  const settings = path.join(userDir, 'settings.js'), logPath = path.join(userDir, 'node-red.log');
  fs.writeFileSync(settings, 'module.exports='+JSON.stringify({uiHost:'127.0.0.1',uiPort:port,
    httpAdminRoot:'/admin',httpNodeRoot:'/',flowFile:'flows.json',credentialSecret:false,
    editorTheme:{projects:{enabled:false}},logging:{console:{level:'info',metrics:false,audit:false}}})+';\n');
  const log = fs.openSync(logPath, 'w');
  const child = spawn(process.execPath, [path.join(root, 'red.js'), '--userDir', userDir, '--settings', settings],
    {stdio:['ignore',log,log]}); fs.closeSync(log);
  const base = `http://127.0.0.1:${port}`;
  try {
    await waitFor(async () => {
      assert.equal(child.exitCode, null, fs.readFileSync(logPath, 'utf8'));
      try { return (await request(base+'/admin/flows')).status === 200; } catch { return false; }
    }, 'Node-RED admin must start within 10000ms');
    const flows = [
      {id:'jsonata-tab',type:'tab',label:'JSONata compatibility',disabled:false},
      {id:'jsonata-in',type:'http in',z:'jsonata-tab',url:'/jsonata-compat',method:'post',upload:false,wires:[['jsonata-change']]},
      {id:'jsonata-change',type:'change',z:'jsonata-tab',rules:[{t:'set',p:'payload',pt:'msg',
        to:'{"result": payload.value * 2, "label": $uppercase(payload.label)}',tot:'jsonata'}],wires:[['jsonata-switch']]},
      {id:'jsonata-switch',type:'switch',z:'jsonata-tab',property:'payload.result',propertyType:'jsonata',
        rules:[{t:'eq',v:'42',vt:'num'}],checkall:'true',repair:false,outputs:1,wires:[['jsonata-out']]},
      {id:'jsonata-out',type:'http response',z:'jsonata-tab',statusCode:'',headers:{},wires:[]},
    ];
    const offset = fs.statSync(logPath).size, body = JSON.stringify(flows);
    const response = await request(base+'/admin/flows', {method:'POST',headers:{'content-type':'application/json'}}, body);
    assert.equal(response.status, 204, response.body);
    await waitFor(() => {
      assert.equal(child.exitCode, null);
      const appended = fs.readFileSync(logPath).subarray(offset).toString();
      const updated = appended.indexOf('[info] Updated flows');
      return updated >= 0 && appended.slice(updated).includes('[info] Started flows');
    }, 'one deployment must actually complete within 10000ms');
    const result = await request(base+'/jsonata-compat', {method:'POST',headers:{'content-type':'application/json'}},
      JSON.stringify({value:21,label:'ok'}));
    assert.equal(result.status, 200, result.body);
    assert.deepEqual(JSON.parse(result.body), {result:42,label:'OK'});
  } finally {
    child.kill('SIGTERM');
    await new Promise(resolve => {
      if (child.exitCode !== null) return resolve();
      const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }
}

async function main() {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'sa02m-jsonata-test-'));
  let failures = 0;
  const check = async (name, fn) => {
    try { await fn(); console.log('PASS '+name); }
    catch (error) { failures++; console.error('FAIL '+name+': '+error.message); }
  };
  try {
    const root = runtimeRoot(scratch), fromRoot = createRequire(path.join(root, 'package.json'));
    const utilPath = fromRoot.resolve('@node-red/util/package.json'), fromUtil = createRequire(utilPath);
    const util = fromRoot('@node-red/util').util;
    await check('resolved runtime version guard', () => {
      assert.equal(fromRoot('./package.json').version, '4.1.13');
      assert.equal(fromUtil('jsonata/package.json').version, '2.2.1');
    });
    const evaluate = (source, message, configure) => new Promise((resolve, reject) => {
      const expression = util.prepareJSONataExpression(source, null);
      if (configure) configure(expression);
      util.evaluateJSONataExpression(expression, message, (error, value) => error ? reject(error) : resolve(value));
    });
    await check('util arithmetic and mapping', async () => {
      assert.equal(await evaluate('$sum(payload.values)', {payload:{values:[1,2,3]}}), 6);
      const mapped = await evaluate('$map(payload.values,function($v){$v*2})', {payload:{values:[1,2,3]}});
      assert.equal(Array.isArray(mapped), true);
      assert.deepEqual(Array.from(mapped), [2,4,6]);
    });
    await check('util object transform preserves input', async () => {
      const message = {payload:{value:21}};
      assert.deepEqual(await evaluate('payload ~> | $ | {"total":value*2} |', message), {value:21,total:42});
      assert.deepEqual(message, {payload:{value:21}});
    });
    await check('util async function evaluation', async () => {
      assert.equal(await evaluate('$testAsync(payload.value)', {payload:{value:41}}, expression =>
        expression.registerFunction('testAsync', async value => value+1, '<n:n>')), 42);
    });
    for (const [name, source] of Object.entries(security)) await check('inert security '+name, () => {
      const result = spawnSync(process.execPath, ['-e', securityWorker, utilPath, source], {encoding:'utf8',timeout:5000});
      assert.equal(result.status, 0, result.error?.message || result.stderr);
      const value = JSON.parse(result.stdout);
      if (name === 'lookup') assert.equal(value.kind, 'undefined');
      else {
        assert.ok(value.error && /^[DT][0-9]+$/.test(value.error), 'crafted expression must be refused by JSONata');
        assert.ok(!JSON.stringify(value).includes('INERT_JSONATA_SENTINEL'));
      }
    });
    await check('actual Change/Switch JSONata flow', () => realFlow(root, scratch));
  } finally { fs.rmSync(scratch, {recursive:true,force:true}); }
  if (failures) process.exitCode = 1;
}
export {treeDigest};
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(error); process.exitCode = 1; });
}
