// comment-mutation-proof-exempt: backend-generated snapshots, shipped closures and real browser HTTP.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
import {chromium} from './node_modules/playwright/index.mjs';
const root = fileURLToPath(new URL('../../', import.meta.url));
const generated = spawnSync(process.env.PYTHON || 'python3', ['-c', `import json
from unittest.mock import patch
from sa02m_flasher import device_config as dc
from tests.test_mr_relay_capabilities import RelayBus
from tests.test_mr_readback_truth import device
out={}
for code in (2,3,5,7):
 bus=RelayBus(code)
 if code==7: bus.words.update({400:34,107:1,108:0})
 with patch.object(dc,'_open_transport',return_value=(bus,bus.close)):
  out[str(code)]=dc.snapshot_for_device('unused',device(code))
print(json.dumps(out))`], {cwd: root, env: {...process.env, PYTHONPATH: root + 'opt/sa02m-flasher'}, encoding: 'utf8'});
assert.equal(generated.status, 0, generated.stderr);
const fixtures = JSON.parse(generated.stdout), www = new URL('../../www/network_config/', import.meta.url);
let current = fixtures[7], writes = [], typeFailure = '', panelSnapshots = 0;
const server = createServer(async (req, res) => {
 const url = new URL(req.url, 'http://localhost');
 if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
  let data = {ok:true,jobs:[],entries:[]};
  if(url.pathname.endsWith('/ports')) data={ports:[{key:'COM4',exists:true,reserved:true,busy_pids:[],active_services:[]}]};
  if(url.pathname.endsWith('/ports/release')) data={ok:true,port:{reserved:true}};
  if(url.pathname.includes('/device_config/')) {
   const chunks=[];for await(const chunk of req) chunks.push(chunk);
   const body=JSON.parse(Buffer.concat(chunks).toString());
   if(url.pathname.endsWith('/snapshot') && body.snapshot_detail==='panel') panelSnapshots++;
   if(!url.pathname.endsWith('/snapshot')) writes.push(body);
   data={ok:true,...structuredClone(current)};
   if(url.pathname.endsWith('/holding') && body.reg===400 && typeFailure) {
    res.statusCode=409;data={ok:false,error:typeFailure};
   } else if(url.pathname.endsWith('/holding') && body.reg===400) {
    current.mr.ai.channels[0].sensor_code=body.value;
    data={ok:true,...structuredClone(current),ai_write:{status:current.mr.ai_apply.state==='idle'?'confirmed':'accepted_unconfirmed'}};
    if(data.ai_write.status==='accepted_unconfirmed') {
      res.statusCode=409;data.ok=false;data.error_code='AI_TYPE_ACCEPTED_UNCONFIRMED';
    }
   } else if(url.pathname.endsWith('/holding') && (body.reg===406 || body.reg===535)) {
    const ai=current.mr.ai.channels[0];
    if(body.reg===406) ai.limit_high=body.value; else ai.filters.tau=body.value;
    data={ok:true,...structuredClone(current),ai_write:{status:'confirmed'}};
   }
  }
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));return;
 }
 try {
  const file=new URL('.'+url.pathname,www);assert.ok(file.href.startsWith(www.href));
  let data=await readFile(file);
  if(url.pathname.endsWith('/flasher.js')) {
   if(process.env.MR_OPTIONAL_OLD_SOURCE) data=await readFile(process.env.MR_OPTIONAL_OLD_SOURCE);
   data=data.toString().replace(/\}\)\(\);\s*$/, 'window.mrTest={state,openConfigModal,closeConfigModal,stopConfigPolling,refreshConfigSnapshot,applyConfigSnapshot,mergeMrMinimalIntoFull,saveModuleDo,saveModuleDi,saveModuleAo,saveMrGlobalInactivity,applyAiFilters,holdQueue(){let release;_configApiTail=new Promise(r=>release=r);return release;}};\n})();');
  }
  res.setHeader('Content-Type',url.pathname.endsWith('.js')?'text/javascript':url.pathname.endsWith('.css')?'text/css':url.pathname.endsWith('.svg')?'image/svg+xml':'text/html');res.end(data);
 }catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin='http://127.0.0.1:'+server.address().port,browser=await chromium.launch({headless:true});let failures=0;
try {
 for(const [theme,width,lang] of [['dark',1600,'ru'],['light',500,'en']]) {
  const context=await browser.newContext({viewport:{width,height:1000}});await context.addCookies([{name:'session_token',value:'test',url:origin}]);
  await context.addInitScript(() => {Object.hasOwn = undefined;});
  const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.clock.install();
  await page.goto(origin+'/index.html');await page.evaluate(({theme,lang})=>{teardownStatusPolling();switchTab('flasher');document.documentElement.setAttribute('data-theme',theme);sa02mI18n.setLang(lang);},{theme,lang});
  await page.locator('#flasher-port').selectOption('COM4');
  const idle=()=>page.waitForFunction(()=>!mrTest.state.configBusy&&!mrTest.state.configBackgroundBusy);
  const openModal=async(code,adjust)=>{current=structuredClone(fixtures[code]);if(adjust)adjust(current);writes=[];await page.evaluate(async info=>{if(mrTest.state.configOpen)mrTest.closeConfigModal();mrTest.state.devices=[{...info,address:2,baudrate:9600,parity:'N',stopbits:1}];await mrTest.openConfigModal(0);mrTest.stopConfigPolling();},current.info);};
  const open=async(code,tab,adjust)=>{await openModal(code,adjust);await idle();await page.locator(`[data-config-tab="${tab}"]`).click();await idle();};
  const refresh=async()=>{await page.evaluate(()=>mrTest.refreshConfigSnapshot(true,'panel'));await idle();};
  const check=async(name,fn)=>{try{await fn();console.log('PASS',theme,name);}catch(error){failures++;console.error('FAIL',theme,name,error.message);}};
  await check('stopping modal polling cancels the pending first panel read',async()=>{
   const before=panelSnapshots;
   await openModal(7);
   await page.clock.fastForward(1000);
   await idle();
   assert.equal(panelSnapshots,before);
  });
  await check('ES2020 missing Object.hasOwn still merges a valid zero watchdog poll',async()=>{
   assert.equal(await page.evaluate(()=>typeof Object.hasOwn),'undefined');
   const result=await page.evaluate(()=>mrTest.mergeMrMinimalIntoFull({inactivity_s:9,inactivity_known:true},{inactivity_s:0,inactivity_known:true}));
   assert.equal(result.inactivity_s,0);assert.equal(result.inactivity_known,true);
  });
  await open(7,'ai_1',snapshot=>{snapshot.mr.ai.channels[0].fault_known=false;snapshot.mr.ai.channels[0].filters.known={kalman:false,sps:false,avg:false,tau:false};});
  await check('initial unknown diagnostics are distinct and filters unavailable',async()=>{assert.match(await page.locator('#cfg-mr-ai-fault-1').innerText(),/недоступен|unavailable/);assert.equal(await page.locator('#cfg-mr-ai-tau-1').inputValue(),'');assert.equal(await page.locator('#cfg-mr-ai-sps-1').isDisabled(),true);});
  await open(7,'ai_1');
  await check('known alarm is visible',async()=>{assert.equal(await page.locator('#cfg-mr-ai-fault-1').isVisible(),true);assert.match(await page.locator('#cfg-mr-ai-fault-1').getAttribute('class'),/badge-err/);});
  await page.locator('#cfg-mr-ai-cal-1').fill('27');current.mr.ai.channels[0].fault_known=false;current.mr.ai.channels[0].fault_low=false;await refresh();
  await check('failed optional alarm read preserves measurement and focused draft',async()=>{assert.match(await page.locator('#cfg-mr-ai-fault-1').innerText(),/Статус аварий недоступен|Alarm status unavailable/);assert.equal(await page.locator('#cfg-mr-ai-cal-1').inputValue(),'27');assert.notEqual(await page.locator('#cfg-mr-ai-measured-1').innerText(),'—');});
  current.mr.ai.channels[0].fault_known=true;await refresh();
  await check('valid zero clears unknown',async()=>assert.equal(await page.locator('#cfg-mr-ai-fault-1').isVisible(),false));
  current.mr.ai.channels[0].fault_high=true;await refresh();
  await check('fresh alarm returns during editing',async()=>assert.match(await page.locator('#cfg-mr-ai-fault-1').getAttribute('class'),/badge-err/));
  delete current.mr.ai.channels[0].fault_known;await refresh();
  await check('old backend missing known flag remains unknown',async()=>assert.match(await page.locator('#cfg-mr-ai-fault-1').innerText(),/недоступен|unavailable/));
  for(const sensor of [0,42]) {await open(7,'ai_1');current.mr.ai.channels[0].sensor_code=sensor;current.mr.ai.channels[0].fault_known=false;await refresh();await check('non-analog sensor has no invented alarm '+sensor,async()=>assert.equal(await page.locator('#cfg-mr-ai-fault-1').isVisible(),false));}
  await open(7,'ai_1');
  await check('queued filter save rechecks failed read before first request',async()=>{
   const failed=structuredClone(current);failed.snapshot_detail='panel';failed.mr.ai.channels[0].filters.known.sps=false;
   await page.evaluate(async failed=>{const release=mrTest.holdQueue();document.getElementById('cfg-mr-ai-tau-1').value='12';const work=mrTest.applyAiFilters(1);try{mrTest.applyConfigSnapshot(failed,false);}finally{release();}await work;},failed);assert.equal(writes.length,0);
   assert.equal(await page.locator('#cfg-mr-ai-tau-1').isDisabled(),true);
  });
  await refresh();await check('fresh filters enable valid zero values',async()=>{assert.equal(await page.locator('#cfg-mr-ai-tau-1').isDisabled(),false);assert.equal(await page.locator('#cfg-mr-ai-kalman-1').isChecked(),false);});
  await open(7,'ai_1',snap=>{snap.mr.ai_apply={state:'unsupported'};});
  await check('legacy type accepted once, dependent controls locked across intent echo',async()=>{
   assert.equal(await page.locator('#cfg-mr-ai-limit-lo-1').isDisabled(),true);
   assert.equal(await page.locator('#cfg-mr-ai-kalman-1').isDisabled(),false);
   const sensor=page.locator('#cfg-mr-ai-sensor-1');
   await sensor.selectOption('35');await idle();
   assert.deepEqual(writes.map(w=>[w.reg,w.value]),[[400,35]]);
   assert.match(await page.locator('#flasher-config-banner').innerText(),/Применение не подтверждено|Application is unconfirmed/);
   await refresh();
   assert.equal(await page.locator('#cfg-mr-ai-limit-hi-1').isDisabled(),true);
   assert.equal(await page.locator('#cfg-mr-ai-tau-1').isDisabled(),true);
   assert.equal(await page.locator('#cfg-mr-ai-cal-1').isDisabled(),false);
   assert.equal(writes.length,1);
  });
  for(const failure of ['AI completion timed out','AI application generation changed']) {
   await open(7,'ai_1');
   typeFailure=failure;
   await check('uncertain type blocks dependent save: '+failure,async()=>{
    const [reply]=await Promise.all([
     page.waitForResponse(res=>res.url().endsWith('/device_config/holding') && res.request().postDataJSON()?.reg===400),
     page.locator('#cfg-mr-ai-sensor-1').selectOption('35'),
    ]);
    assert.equal(reply.status(),409);
    assert.equal((await reply.json()).error,failure);
    await page.waitForFunction(reason=>document.getElementById('flasher-config-banner').textContent.includes(reason),failure);
    await idle();
    assert.match(await page.locator('#flasher-config-banner').innerText(),/не подтверждено|unconfirmed/);
    assert.equal(await page.locator('#cfg-mr-ai-limit-hi-1').isDisabled(),true);
    assert.equal(await page.locator('#cfg-mr-ai-tau-1').isDisabled(),true);
    assert.deepEqual(writes.map(w=>w.reg),[400]);
   });
   typeFailure='';
  }
  await open(7,'ai_1');
  await check('confirmed type permits one limit and one filter write',async()=>{
   await page.locator('#cfg-mr-ai-sensor-1').selectOption('35');await idle();
   const high=page.locator('#cfg-mr-ai-limit-hi-1'),tau=page.locator('#cfg-mr-ai-tau-1');
   await high.fill('200');await high.blur();await idle();
   await tau.fill('23');await tau.blur();await idle();
   assert.deepEqual(writes.map(w=>[w.reg,w.value]),[[400,35],[406,200],[535,23]]);
  });
  for(const [code,family,key] of [[2,'do','safe'],[5,'di','debounce'],[3,'ao','safe']]) {
   await open(code,family+'_1');
   await check(family+' known zero settings are editable',async()=>assert.equal(await page.locator(`[data-mr-${family}-save="1"]`).isDisabled(),false));
   const omitted=structuredClone(current);omitted.snapshot_detail='minimal';omitted.mr={[family]:{}};
   await page.evaluate(snapshot=>mrTest.applyConfigSnapshot(snapshot,false),omitted);
   await check(family+' omitted minimal settings preserve prior read',async()=>assert.equal(await page.locator(`[data-mr-${family}-save="1"]`).isDisabled(),false));
   current.mr[family][key]=[];await refresh();
   await check(family+' explicit missing group blocks handler before every write',async()=>{assert.equal(await page.locator(`[data-mr-${family}-save="1"]`).isDisabled(),true);await page.evaluate(family=>mrTest['saveModule'+({do:'Do',di:'Di',ao:'Ao'})[family]](1),family);assert.equal(writes.length,0);assert.deepEqual(await page.evaluate(({family,key})=>mrTest.state.configSnapshot.mr[family][key],{family,key}),[]);});
  }
  await open(3,'ao_1');current.mr.inactivity_known=false;await refresh();
  await check('unknown watchdog blocks live AO setpoint as well',async()=>{await page.evaluate(()=>mrTest.saveModuleAo(1));assert.equal(writes.length,0);assert.equal(await page.locator('[data-mr-ao-save="1"]').isDisabled(),true);});
  await open(2,'relay');
  await check('ES2020 relay options render without Object.hasOwn',async()=>{
   assert.equal(await page.locator('#cfg-mr-relay-mode').count(),1);
   assert.equal(await page.locator('#cfg-mr-relay-mode').inputValue(),'0');
   assert.equal(await page.locator('#cfg-mr-relay-opt-4').count(),1);
  });
  await page.locator('[data-config-tab="info"]').click();await idle();current.mr.inactivity_known=false;await refresh();
  await check('global watchdog unknown is blank and cannot write',async()=>{assert.equal(await page.locator('#cfg-mr-inactivity-global').inputValue(),'');assert.equal(await page.locator('#cfg-mr-inactivity-save-btn').isDisabled(),true);await page.evaluate(()=>mrTest.saveMrGlobalInactivity());assert.equal(writes.length,0);});
  await check('no page errors',async()=>assert.deepEqual(errors,[]));await context.close();
 }
}finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
process.exitCode=failures?1:0;
