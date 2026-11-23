// comment-mutation-proof-exempt: executes shipped MQTT and service control functions with delayed HTTP and a deterministic clock.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {join, dirname} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
const root = process.env.MQTT_TEST_ROOT || join(dirname(fileURLToPath(import.meta.url)), '../..');
const {loadMqttJs, El} = await import(pathToFileURL(join(root, 'scripts/dev/mqtt-js-sandbox.mjs')));
const services = fs.readFileSync(join(root, 'www/network_config/static/js/app/services.js'), 'utf8');
const app = fs.readFileSync(join(root, 'www/network_config/static/js/app.js'), 'utf8');
const stateHelpers = app.slice(app.indexOf('function normSvcState('), app.indexOf('function svcBadge('));
const opId = 'b'.repeat(32);
const running = {mosquitto_active:1, mosquitto_disabled:0, bridge_active:1, bridge_disabled:0, clients_connected:2};
const stopped = {...running, bridge_active:0, bridge_disabled:1};
const response = data => ({ok:true, status:200, json:async()=>data, text:async()=>JSON.stringify(data)});
function setup(handler, lang='ru') {
  const ids = Object.fromEntries(['mqtt-broker-badge','mqtt-bridge-badge','mqtt-broker-clients','mqtt-bridge-toggle-btn','mqtt-bridge-control-status','mqtt-save-btn'].map(id=>[id,new El('span')]));
  const box = loadMqttJs({ids, lang});
  const c = box.ctx;
  vm.runInContext(services, c);
  vm.runInContext(stateHelpers, c);
  let now=1000, next=0;
  const timers=new Map(), requests=[], toasts=[];
  c.Date={now:()=>now}; c.AbortController=AbortController;
  c.setTimeout=(fn,ms)=>{const id=++next;timers.set(id,{at:now+ms,fn});return id;};
  c.clearTimeout=id=>timers.delete(id);
  c.setInterval=()=>0; c.clearInterval=()=>{};
  c.withCsrfHeaders=h=>({...h,'X-CSRF-Token':'fixture'});
  c.renderMqttClientInfo=()=>{}; c.bindMqttClientInfoCells=()=>{};
  c.renderServicesControl=()=>{}; c.showToast=(...args)=>toasts.push(args); c.toast=c.showToast;
  c.loadServicesControl=()=>{};
  c.stopMonitor=()=>{}; c.stopChannelPoll=()=>{}; c.hideScanModal=()=>{}; c.hideAddModal=()=>{};
  c.loadConfig=async()=>{}; c.paintMqttDevices=()=>{};
  c.fetch=(url,opts={})=>{requests.push({url,opts,at:now});return handler(url,opts,now);};
  const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
  const setState=st=>{c.fixtureState=st;box.run('_lastMqttBrokerStatus=fixtureState;applyBrokerStatusUI(fixtureState)');};
  setState(running);
  return {...box, c, requests, timers, toasts, setState, flush,
    posts:()=>requests.filter(r=>r.opts.method==='POST'),
    button:ids['mqtt-bridge-toggle-btn'], saveButton:ids['mqtt-save-btn'], badge:ids['mqtt-bridge-badge'], broker:ids['mqtt-broker-badge'], hint:ids['mqtt-bridge-control-status'],
    async advance(ms){const end=now+ms;await flush();while(true){const pair=[...timers].filter(([,v])=>v.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!pair)break;now=pair[1].at;timers.delete(pair[0]);pair[1].fn();await flush();}now=end;await flush();},
  };
}
function normal(url,opts,now){
  if(opts.method==='POST'){
    const body=JSON.parse(opts.body);
    return Promise.resolve(response({ok:true,pending:true,id:'mqtt-bridge',action:body.action.replace('_bridge',''),op_id:opId}));
  }
  if(url.includes('result=1'))return Promise.resolve(response(now<7000?{ok:true,pending:true}:{ok:true,id:'mqtt-bridge',action:'stop',op_id:opId}));
  if(url==='cgi-bin/services_ctrl.cgi')return Promise.resolve(response({ok:true,services:[{id:'mqtt-bridge',installed:true,active:now<7000?'active':'inactive',enabled:now<7000,user_disabled:now>=7000}]}));
  return Promise.resolve(response(now<7000?running:stopped));
}
let failures=0, groups=0;
async function test(name,fn){groups++;try{await fn();console.log('PASS '+name);}catch(e){failures++;console.error('FAIL '+name+': '+e.message);}}
await test('ack is pending; 6-second stop stays locked, broker independent, final readback',async()=>{
  const t=setup(normal);let settled=false;
  t.c.window.mqttToggleBridge().then(()=>settled=true);await t.flush();
  assert.equal(settled,false,'async acknowledgement cannot complete stop');
  assert.equal(t.button.disabled,true);assert.match(t.badge.textContent,/Остановка/);
  assert.equal(t.run('_lastMqttBrokerStatus.bridge_active'),1,'do not invent a stopped sample');
  await t.advance(3500);assert.equal(t.button.disabled,true);assert.match(t.badge.textContent,/Остановка/);
  assert.equal(t.broker.textContent,'Работает');assert.equal(t.posts().length,1);
  await t.advance(5000);assert.equal(settled,true);assert.equal(t.button.disabled,false);
  assert.equal(t.badge.textContent,'Отключен');assert.equal(t.button.textContent,'Запустить');
  assert.equal(t.broker.textContent,'Работает');assert.equal(t.toasts.length,0);
  assert.deepEqual(JSON.parse(t.posts()[0].opts.body),{id:'mqtt-bridge',action:'stop'});
  assert.equal(t.posts()[0].url,'cgi-bin/services_ctrl.cgi');assert.equal(t.posts()[0].opts.headers['X-CSRF-Token'],'fixture');
});
await test('double click and Management re-entry send exactly one POST',async()=>{
  const t=setup(normal);void t.c.window.mqttToggleBridge();await t.flush();
  void t.c.window.mqttToggleBridge();
  t.c.serviceCtlAction({dataset:{svcId:'mqtt-bridge',svcAction:'start'},closest:()=>null});await t.flush();
  assert.equal(t.posts().length,1);await t.advance(9000);
});
await test('unknown or malformed status cannot infer start from old button text',async()=>{
  for(const st of [null,{error:'unauthorized'},{},{bridge_active:'garbage',bridge_disabled:0}]){
    const t=setup(normal);t.setState(st);t.button.textContent='Запустить';
    await t.c.window.mqttToggleBridge();await t.flush();assert.equal(t.posts().length,0);assert.equal(t.button.disabled,true);
  }
});
await test('late pre-command status cannot undo verified stop',async()=>{
  let oldResolve;const t=setup((url,opts,now)=>{if(url.includes('mqtt_status')&&!oldResolve)return new Promise(r=>oldResolve=r);return normal(url,opts,now);});
  const old=t.c.refreshBrokerStatus();await t.flush();void t.c.window.mqttToggleBridge();await t.advance(9000);
  oldResolve(response(running));await old;await t.flush();assert.equal(t.badge.textContent,'Отключен');assert.equal(t.run('_lastMqttBrokerStatus.bridge_active'),0);
});
await test('overlapping status reads cannot paint an older generation last',async()=>{
  let oldResolve;const t=setup(()=>oldResolve?Promise.resolve(response(stopped)):new Promise(r=>oldResolve=r));
  const old=t.c.refreshBrokerStatus();await t.c.refreshBrokerStatus();oldResolve(response(running));await old;
  assert.equal(t.badge.textContent,'Отключен');
});
await test('leaving and returning to MQTT ignores old tab response and preserves pending',async()=>{
  let oldResolve;const t=setup((url,opts,now)=>{if(url.includes('mqtt_status')&&!oldResolve)return new Promise(r=>oldResolve=r);return normal(url,opts,now);});
  const old=t.c.refreshBrokerStatus();t.c.window.mqttTabDestroy();oldResolve(response(stopped));await old;
  assert.equal(t.run('_lastMqttBrokerStatus.bridge_active'),1,'destroy invalidates old read');
  void t.c.window.mqttToggleBridge();await t.flush();t.c.window.mqttTabDestroy();t.c.window.mqttTabInit();await t.flush();
  assert.equal(t.button.disabled,true);assert.match(t.badge.textContent,/Остановка/);await t.advance(9000);assert.equal(t.posts().length,1);assert.equal(t.button.disabled,false);
});
await test('matching terminal failure stays inline, never claims success or retries',async()=>{
  const t=setup((url,opts,now)=>url.includes('result=1')?Promise.resolve(response({ok:false,error:'port_busy',id:'mqtt-bridge',action:'stop',op_id:opId})):normal(url,opts,now));
  void t.c.window.mqttToggleBridge();await t.advance(3000);
  assert.equal(t.posts().length,1);assert.match(t.hint.textContent,/порт RS-485/);assert.equal(t.toasts.length,0);assert.notEqual(t.badge.textContent,'Отключен');
});
await test('missing or foreign operation identity is refused',async()=>{
  for(const ack of [{ok:true,pending:true},{ok:true,pending:true,id:'mosquitto',action:'stop',op_id:opId}]){
    const t=setup((url,opts,now)=>opts.method==='POST'?Promise.resolve(response(ack)):normal(url,opts,now));
    void t.c.window.mqttToggleBridge();await t.advance(2500);assert.equal(t.posts().length,1);assert.match(t.hint.textContent,/результат команды/);assert.equal(t.requests.filter(r=>r.url.includes('result=1')).length,0);
  }
});
await test('unrelated terminal result cannot finish bridge operation',async()=>{
  const t=setup((url,opts,now)=>url.includes('result=1')?Promise.resolve(response({ok:true,id:'mqtt-bridge',action:'stop',op_id:'a'.repeat(32)})):normal(url,opts,now));
  void t.c.window.mqttToggleBridge();await t.advance(8000);assert.equal(t.button.disabled,true);assert.match(t.badge.textContent,/Остановка/);
  await t.advance(83000);assert.match(t.hint.textContent,/время ожидания/);assert.equal(t.posts().length,1);
});
await test('fresh state must agree after successful result',async()=>{
  const t=setup((url,opts,now)=>url.includes('mqtt_status')?Promise.resolve(response(running)):normal(url,opts,now));
  void t.c.window.mqttToggleBridge();await t.advance(9000);assert.equal(t.button.disabled,true);assert.match(t.badge.textContent,/Остановка/);
  await t.advance(82000);assert.match(t.hint.textContent,/время ожидания/);assert.equal(t.posts().length,1);
});
for(const phase of ['post','result','state','mqtt'])for(const stall of ['fetch','body'])await test(`${phase} hung ${stall} is bounded with one POST and inline failure`,async()=>{
  let heldSignal;
  const t=setup((url,opts,now)=>{
    const target=phase==='post'?opts.method==='POST':phase==='result'?url.includes('result=1'):phase==='state'?url==='cgi-bin/services_ctrl.cgi'&&opts.method!=='POST':url.includes('mqtt_status');
    if(target){heldSignal=opts.signal;return stall==='fetch'?new Promise(()=>{}):Promise.resolve({ok:true,json:()=>new Promise(()=>{}),text:()=>new Promise(()=>{})});}
    return normal(url,opts,now);
  });
  void t.c.window.mqttToggleBridge();await t.advance(50000);
  assert.match(t.hint.textContent,/время ожидания/);assert.equal(t.posts().length,1);assert.equal(heldSignal?.aborted,true);assert.equal(t.toasts.length,0);
});
await test('POST time is included in the 90-second operation deadline',async()=>{
  let postResolve;const t=setup((url,opts)=>opts.method==='POST'?new Promise(r=>postResolve=r):Promise.resolve(response({ok:true,pending:true})));
  let settled=false;t.c.window.mqttToggleBridge().then(()=>settled=true);await t.advance(19000);
  postResolve(response({ok:true,pending:true,id:'mqtt-bridge',action:'stop',op_id:opId}));await t.advance(70999);assert.equal(settled,false);await t.advance(1);
  assert.equal(settled,true);assert.match(t.hint.textContent,/время ожидания/);assert.equal(t.posts().length,1);
});
await test('start path has the same pending/result/fresh-state contract in English',async()=>{
  const t=setup((url,opts,now)=>{
    if(opts.method==='POST')return Promise.resolve(response({ok:true,pending:true,id:'mqtt-bridge',action:'start',op_id:opId}));
    if(url.includes('result=1'))return Promise.resolve(response({ok:true,id:'mqtt-bridge',action:'start',op_id:opId}));
    if(url==='cgi-bin/services_ctrl.cgi')return Promise.resolve(response({ok:true,services:[{id:'mqtt-bridge',installed:true,active:'active',enabled:true}]}));
    return Promise.resolve(response(running));
  },'en');t.setState(stopped);void t.c.window.mqttToggleBridge();await t.flush();assert.match(t.badge.textContent,/Starting/);assert.equal(t.button.disabled,true);
  await t.advance(2000);assert.equal(t.button.textContent,'Stop');assert.equal(t.button.disabled,false);assert.equal(t.posts().length,1);
});
await test('existing Management pending command excludes a bridge POST',async()=>{
  const t=setup(normal);t.run("_svcCtlPending['mqtt-bridge']='stop'");
  t.c.applyBrokerStatusUI(running);await t.c.window.mqttToggleBridge();assert.equal(t.posts().length,0);assert.equal(t.button.disabled,true);
});
await test('rejected POST remains unknown until fresh poll and never auto-retries',async()=>{
  const t=setup((url,opts,now)=>opts.method==='POST'?Promise.resolve(response({ok:false,error:'port_busy'})):normal(url,opts,now));
  await t.c.window.mqttToggleBridge();assert.equal(t.button.disabled,true);assert.equal(t.badge.textContent,'Мост —');
  await t.c.window.mqttToggleBridge();assert.equal(t.posts().length,1);
  await t.c.refreshBrokerStatus();assert.equal(t.button.disabled,false);assert.equal(t.button.textContent,'Остановить');
  assert.match(t.hint.textContent,/порт RS-485/);assert.equal(t.posts().length,1);
});
await test('inline error is translated again when language changes',async()=>{
  const t=setup((url,opts,now)=>opts.method==='POST'?Promise.resolve(response({ok:false,error:'port_busy'})):normal(url,opts,now),'en');
  await t.c.window.mqttToggleBridge();assert.match(t.hint.textContent,/RS-485 port/);
  t.c.window.sa02mI18n.setLang('ru');t.c.window.mqttRefreshI18n();assert.match(t.hint.textContent,/порт RS-485/);
});
await test('Stop pending excludes Save/Apply without a config POST',async()=>{
  const t=setup(normal);void t.c.window.mqttToggleBridge();await t.flush();
  assert.equal(t.saveButton.disabled,true);
  await t.c.window.mqttSaveAndApply();await t.flush();
  assert.equal(t.posts().length,1);assert.equal(t.posts()[0].url,'cgi-bin/services_ctrl.cgi');
  await t.advance(9000);assert.equal(t.saveButton.disabled,false);
});
await test('Management pending also excludes Save/Apply',async()=>{
  const t=setup(normal);t.run("_svcCtlPending['mqtt-bridge']='start'");
  t.c.applyBrokerStatusUI(running);await t.c.window.mqttSaveAndApply();
  assert.equal(t.saveButton.disabled,true);assert.equal(t.posts().length,0);
});
function applyFlow({pauseAt='',failureAt='',foreignAt='',hangAt='',lang='ru'}={}){
  let action='',open=false;const releases=[];
  const identity=a=>(a==='stop'?'c':'d').repeat(32);
  const t=setup((url,opts)=>{
    if(url==='cgi-bin/mqtt_config.cgi'&&opts.method==='POST'){
      if(pauseAt==='save'&&!open)return new Promise(r=>releases.push(()=>r(response({ok:true}))));
      if(hangAt==='save')return new Promise(()=>{});
      return Promise.resolve(response(failureAt==='save'?{ok:false,error:'invalid_device',id:'broken',reason:'invalid_device_id'}:{ok:true}));
    }
    if(opts.method==='POST'){
      const body=JSON.parse(opts.body);action=body.action;
      return Promise.resolve(response({ok:true,pending:true,id:'mqtt-bridge',action,op_id:identity(action)}));
    }
    if(url.includes('result=1')){
      if(hangAt===action)return new Promise(()=>{});
      if(pauseAt===action&&!open)return Promise.resolve(response({ok:true,pending:true}));
      return Promise.resolve(response({ok:failureAt!==action,error:failureAt===action?'port_busy':undefined,
        id:'mqtt-bridge',action,op_id:foreignAt===action?'f'.repeat(32):identity(action)}));
    }
    if(url==='cgi-bin/services_ctrl.cgi')return Promise.resolve(response({ok:true,services:[{
      id:'mqtt-bridge',installed:true,active:action==='start'?'active':'inactive',enabled:action==='start',user_disabled:action!=='start'}]}));
    if(url.includes('mqtt_status')){
      if(pauseAt==='readback'&&!open)return new Promise(r=>releases.push(()=>r(response(running))));
      if(hangAt==='readback')return new Promise(()=>{});
      return Promise.resolve(response(failureAt==='readback'?stopped:running));
    }
    return Promise.resolve(response({ok:true}));
  },lang);
  t.run("_config={mqtt:{broker:'127.0.0.1'},devices:[]};_unsaved=true");
  return {...t,release(){open=true;for(const release of releases)release();}};
}
for(const phase of ['save','stop','start','readback'])await test(`Save/Apply ${phase} pending excludes Stop, Save and Management`,async()=>{
  const t=applyFlow({pauseAt:phase});let done=false;
  t.c.window.mqttSaveAndApply().then(()=>done=true);await t.advance(5000);
  assert.equal(done,false);assert.equal(t.saveButton.disabled,true);assert.equal(t.button.disabled,true);
  const posts=t.posts().length;
  await t.c.window.mqttToggleBridge();await t.c.window.mqttSaveAndApply();
  t.c.serviceCtlAction({dataset:{svcId:'mqtt-bridge',svcAction:'stop'},closest:()=>null});await t.flush();
  assert.equal(t.posts().length,posts,'no competing mutation in any phase');
  t.c.window.mqttTabDestroy();t.c.window.mqttTabInit();await t.flush();
  assert.equal(t.button.disabled,true,'navigation preserves shared apply lock');
  t.release();await t.advance(9000);assert.equal(done,true);
  assert.deepEqual(t.posts().map(p=>[p.url,JSON.parse(p.opts.body).action??JSON.parse(p.opts.body).restart]),[
    ['cgi-bin/mqtt_config.cgi',false],['cgi-bin/services_ctrl.cgi','stop'],['cgi-bin/services_ctrl.cgi','start']]);
  assert.equal(t.button.textContent,'Остановить');assert.equal(t.saveButton.disabled,false);
  assert.equal(t.run('_unsaved'),false);assert.equal(t.toasts.length,1,'success is reported only after correlated apply and fresh state');
});
for(const phase of ['save','stop','start'])await test(`Save/Apply ${phase} failure is truthful and does not continue or retry`,async()=>{
  const t=applyFlow({failureAt:phase});void t.c.window.mqttSaveAndApply();await t.advance(9000);
  assert.equal(t.posts().length,phase==='save'?1:phase==='stop'?2:3);
  assert.equal(t.toasts.length,0);assert.equal(t.run('_unsaved'),phase==='save');
  assert.match(t.hint.textContent,phase==='save'?/Сохранение настроек не подтверждено/:/Настройки сохранены, применение не завершено/);
  if(phase!=='save')assert.match(t.hint.textContent,/порт RS-485/);
  assert.equal(t.button.disabled,true,'no guessed service state after partial failure');
  await t.advance(100000);assert.equal(t.posts().length,phase==='save'?1:phase==='stop'?2:3);
});
for(const phase of ['save','stop','start','readback'])await test(`Save/Apply hung ${phase} is bounded and never sends the next mutation`,async()=>{
  const t=applyFlow({hangAt:phase});let done=false;
  t.c.window.mqttSaveAndApply().then(()=>done=true);await t.advance(90000);
  assert.equal(done,true);assert.equal(t.toasts.length,0);assert.match(t.hint.textContent,/время ожидания/);
  assert.equal(t.posts().length,phase==='save'?1:phase==='stop'?2:3);
});
for(const phase of ['stop','start'])await test(`Save/Apply foreign ${phase} terminal cannot advance the composition`,async()=>{
  const t=applyFlow({foreignAt:phase});void t.c.window.mqttSaveAndApply();await t.advance(90000);
  assert.equal(t.posts().length,phase==='stop'?2:3);assert.equal(t.toasts.length,0);assert.match(t.hint.textContent,/время ожидания/);
});
await test('Save/Apply needs final active readback within the one overall deadline',async()=>{
  const t=applyFlow({failureAt:'readback'});let done=false;
  t.c.window.mqttSaveAndApply().then(()=>done=true);await t.advance(89999);
  assert.equal(done,false);assert.equal(t.toasts.length,0);await t.advance(1);
  assert.equal(done,true);assert.equal(t.posts().length,3);assert.match(t.hint.textContent,/время ожидания/);
});
await test('Save/Apply partial failure remains translatable and permits only a later explicit action',async()=>{
  const t=applyFlow({failureAt:'start',lang:'en'});void t.c.window.mqttSaveAndApply();await t.advance(9000);
  assert.match(t.hint.textContent,/Settings saved, applying them did not complete/);assert.equal(t.posts().length,3);
  t.c.window.sa02mI18n.setLang('ru');t.c.window.mqttRefreshI18n();assert.match(t.hint.textContent,/Настройки сохранены, применение не завершено/);
  await t.c.refreshBrokerStatus();assert.equal(t.button.disabled,false);assert.equal(t.posts().length,3);
});
console.log(JSON.stringify({groups,failures}));
if(failures)process.exitCode=1;
