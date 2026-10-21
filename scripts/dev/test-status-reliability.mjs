// comment-mutation-proof-exempt: executes shipped scheduler and request functions against delayed HTTP responses and aborts; accepts observable lifecycle state.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {test} from 'node:test';
const root = new URL('../../', import.meta.url);
const source = readFileSync(new URL('www/network_config/static/js/app/status.js',root),'utf8');
function world(fetcher=fetch, overrides={}) {
 const elements = new Map();
 const document={getElementById(id){ if(!elements.has(id)) elements.set(id,{style:{},textContent:'',dataset:{}}); return elements.get(id); },querySelectorAll(){return [];}};
 const c=vm.createContext(Object.assign({fetch:fetcher,AbortController,DOMException,setTimeout,clearTimeout,setInterval,clearInterval,Date,document,window:{},console},overrides));
 vm.runInContext(source.slice(0,source.indexOf('function cssVar')),c);
 return {c,elements,run:s=>vm.runInContext(s,c)};
}
test('S1: headers plus stalled body times out and releases its controller',async()=>{
 const server=createServer((req,res)=>{res.writeHead(200,{'Content-Type':'application/json'});res.write('{');});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const {run,c}=world(); c.url=`http://127.0.0.1:${server.address().port}`;
 try {
  const result=await Promise.race([run("statusFetchJson('priority',url,70,0,()=>{})").then(()=> 'resolved',()=> 'aborted'),new Promise(r=>setTimeout(()=>r('hung'),250))]);
  assert.equal(result,'aborted'); assert.equal(run('_statusFetchAbort.priority'),null);
 } finally {server.closeAllConnections();server.close();}
});
test('S2: one successful part cannot clear another part failure',()=>{
 const {run,elements}=world();run("noteStatusFailure('priority',new Error('offline'));noteStatusSuccess('network')");
 assert.equal(elements.get('dashboard-poll-alert').style.display,'block');
});
test('S1: late body from a retired generation cannot be applied',async()=>{
 let resolve; const {run}=world(async()=>({ok:true,json:()=>new Promise(r=>{resolve=r})}));
 const promise=run("statusFetchJson('priority','/',100,0,()=>{})");
 await new Promise(r=>setTimeout(r,0));run('_statusPollGeneration++');resolve({cpu:1});
 await assert.rejects(promise,e=>e.stale===true);
});
test('request timeout does not include time spent waiting for the queue',()=>{
 const {run}=world();assert.equal(run("statusRequestTimeout('priority',90000)"),3000);
});
function extract(src,name) {
 const start=src.indexOf('function '+name+'(');assert.ok(start>=0,name);
 let depth=0;for(let i=src.indexOf('{',start);i<src.length;i++) {if(src[i]==='{')depth++;if(src[i]==='}'&&--depth===0)return src.slice(start,i+1);}
 throw new Error(name);
}
test('retired request release cannot release the new generation slot',()=>{
 const {c,run}=world();const releases=[];c.capture=r=>releases.push(r);
 run("scheduleStatusFetch('priority',0,capture)");assert.equal(releases.length,1);
 run('teardownStatusPolling();_statusLastGlobalFetchMs=0;_statusLastHeavyStartMs=0;');
 run("scheduleStatusFetch('network',0,capture)");assert.equal(releases.length,2);
 releases[0]();assert.equal(run('_statusFetchInFlight'),true);releases[1]();run('clearStatusFetchQueue()');
});
test('overdue priority bypasses background FIFO but cannot starve it',()=>{
 const {c,run}=world();const seen=[];c.capture=()=>{};
 run('_statusFetchInFlight=true');
 for(const name of ['services','network','priority']) {c.part=name;c.runner=()=>seen.push(name);run('scheduleStatusFetch(part,0,runner)');}
 run('_statusFetchInFlight=false;pumpStatusFetchQueue()');assert.deepEqual(seen,['priority']);
 run('_statusFetchInFlight=false;_statusLastGlobalFetchMs=0;_statusLastHeavyStartMs=0;pumpStatusFetchQueue()');assert.deepEqual(seen,['priority','services']);
 run('clearStatusFetchQueue()');
});
test('source cache age and absent metadata remain honest',()=>{
 const {run,c}=world();
 c.oldAge=run("statusStaleAfter('priority',0)")/1000+1;
 run("noteStatusSuccess('priority',{_status_meta:{age_s:oldAge}})");assert.equal(run('statusFreshness.priority.state'),'stale');
 run("noteStatusSuccess('priority',{_status_meta:{age_s:oldAge}})");assert.equal(run('statusFreshness.priority.state'),'stale');
 run("noteStatusSuccess('priority',{})");assert.equal(run('statusFreshness.priority.state'),'unknown');
});
test('S3/S6: service readiness and autostart are independent',()=>{
 const app=readFileSync(new URL('www/network_config/static/js/app.js',root),'utf8');
 const svc=readFileSync(new URL('www/network_config/static/js/app/services.js',root),'utf8');
 const {c,run,elements}=world();
 for(const fn of ['normSvcState','svcStateIsActive','svcBadge','coordinatedServiceState'])vm.runInContext(extract(app,fn),c);
 for(const fn of ['svcCtlRowState','svcCtlWantsStart'])vm.runInContext(extract(svc,fn),c);
 for(const state of ['activating','deactivating','failed','paused','inactive']) {
  c.state=state;run("svcBadge('badge',state)");assert.notEqual(elements.get('badge').className,'badge badge-ok',state);
 }
 assert.equal(run("svcCtlRowState({active:'active',masked:true,user_disabled:true})"),'active');
 assert.equal(run("svcCtlWantsStart({active:'active',masked:true,user_disabled:true})"),false);
});
test('coordinator projection distinguishes whole-service pause and restoration failure',()=>{
 const {c,run}=world();vm.runInContext(extract(source,'portCoordinatorLines'),c);
 c.snapshot={available:false,leases:[{port:'/dev/COM1',purpose:'gateway',phase:'recovering',services:['mplc4.service']}],services:{'mplc4.service':{phase:'restore_failed'}}};
 const lines=run('portCoordinatorLines(snapshot)').join('; ');
 assert.match(lines,/недоступен/);assert.match(lines,/COM1/);assert.match(lines,/целиком/);assert.match(lines,/Ошибка восстановления/);
});
test('OTA cooperative cancellation is pending; bootstrap/refusal cannot enable apply',()=>{
 const {c,run}=world();
 for(const fn of ['webUpdApplyRefusal','_webUpdLegacyStatus','_webUpdIsTerminal','_webUpdStageText'])vm.runInContext(extract(source,fn),c);
 assert.match(run("_webUpdStageText({stage:'backing_up',cancel_requested:true})"),/Отмена запрошена/);
 for(const code of ['E_BOOTSTRAP_REQUIRED','E_RECOVERY_REQUIRED','E_BUSY']) {c.code=code;assert.equal(run('webUpdApplyRefusal({ok:false,error_code:code}).canApply'),false);}
});
test('R5 warmup preserves unknown ages before elapsed arithmetic',()=>{
 const app=readFileSync(new URL('www/network_config/static/js/app.js',root),'utf8');
 const {c,run}=world();c.readPriorityWarmupCache=()=>c.cache;
 vm.runInContext('const PRIORITY_WARMUP_TTL_MS=15000;'+extract(app,'getPriorityWarmupPart'),c);
 for(const age of [null,undefined,-2,'2','n/a',NaN,Infinity]) {
  c.cache={cpu:{ts:Date.now()-5000,data:{_status_meta:{age_s:age}}}};
  const hit=run("getPriorityWarmupPart('cpu')");
  assert.equal(hit._status_meta.age_s,null,'age '+String(age));
  c.hit=hit;run("noteStatusSuccess('priority',hit)");
  assert.equal(run('statusFreshness.priority.state'),'unknown');
 }
 c.cache={cpu:{ts:Date.now()-5000,data:{_status_meta:{age_s:2}}}};
 const valid=run("getPriorityWarmupPart('cpu')");assert.ok(valid._status_meta.age_s>=7&&valid._status_meta.age_s<8);
 c.cache={cpu:{ts:Date.now()+5000,data:{_status_meta:{age_s:0}}}};
 assert.equal(run("getPriorityWarmupPart('cpu')"),null);
 c.cache={cpu:{ts:Date.now()-5000,data:{}}};
 assert.equal(run("getPriorityWarmupPart('cpu')._status_meta"),undefined);
});
test('F2: the autostart label reads the CGI boolean (and the older strings)',()=>{
 const svc=readFileSync(new URL('www/network_config/static/js/app/services.js',root),'utf8');
 const {c,run}=world();vm.runInContext(extract(svc,'svcCtlAutostartLabel'),c);
 assert.equal(run("svcCtlAutostartLabel({enabled:true})"),'Автозапуск включён');
 assert.equal(run("svcCtlAutostartLabel({enabled:false})"),'Автозапуск выключен');
 assert.equal(run("svcCtlAutostartLabel({enabled:true,user_disabled:true})"),'Автозапуск выключен');
 assert.equal(run("svcCtlAutostartLabel({enabled:'enabled'})"),'Автозапуск включён');
 assert.equal(run("svcCtlAutostartLabel({enabled:'disabled'})"),'Автозапуск выключен');
 assert.equal(run("svcCtlAutostartLabel({})"),'');
});
test('F8: a stopped/disabled/masked/failed service stays red; transitions and the pause are neutral',()=>{
 const app=readFileSync(new URL('www/network_config/static/js/app.js',root),'utf8');
 const {c,run,elements}=world();
 for(const fn of ['normSvcState','svcStateIsActive','svcBadge'])vm.runInContext(extract(app,fn),c);
 const cls=(state)=>{c.state=state;run("svcBadge('badge',state)");return elements.get('badge').className;};
 for(const state of ['inactive','disabled','masked','failed'])assert.equal(cls(state),'badge badge-err',state);
 for(const state of ['activating','deactivating','paused'])assert.equal(cls(state),'badge badge-unk',state);
 assert.equal(cls('active'),'badge badge-ok');
});
test('F6/F7: the freshness banner names widgets and takes the TTL from the answer',()=>{
 const {run,elements,c}=world();
 run("noteStatusFailure('priority',new Error('offline'))");
 const text=elements.get('dashboard-poll-alert').textContent;
 assert.match(text,/^CPU, температура, ОЗУ, диск: Ошибка обновления$/);assert.doesNotMatch(text,/priority/);
 // ttl_s moves the deadline: the same age is stale under ttl 10 and fresh under ttl 45
 c.age=run("statusStaleAfter('storage',10000)")/1000+1;
 run("noteStatusSuccess('storage',{_status_meta:{age_s:age-5,ttl_s:10}})");assert.equal(run('statusFreshness.storage.state'),'fresh');
 run("noteStatusSuccess('storage',{_status_meta:{age_s:age,ttl_s:10}})");assert.equal(run('statusFreshness.storage.state'),'stale');
 run("noteStatusSuccess('storage',{_status_meta:{age_s:age,ttl_s:45}})");assert.equal(run('statusFreshness.storage.state'),'fresh');
 c.age=run("statusStaleAfter('services',45000)")/1000+1;
 run("noteStatusSuccess('services',{_status_meta:{age_s:age,ttl_s:45}})");assert.equal(run('statusFreshness.services.state'),'stale');
 assert.match(elements.get('dashboard-poll-alert').textContent,/Службы: Данные устарели/);
 assert.equal(run('typeof STATUS_SOURCE_TTL_MS'),'undefined');
});
test('F13: the JSON guard logs out only after auth_check confirms the session is gone',async()=>{
 const app=readFileSync(new URL('www/network_config/static/js/app.js',root),'utf8');
 const {c,run}=world();
 vm.runInContext('var _sa02mRedirecting=false;var logouts=0;function sa02mCsrfLogout(){logouts++;}var _sa02mSessionGone=null;'+extract(app,'sa02mGuardJsonResponse'),c);
 c.body={error:'unauthorized'};
 const read=(ok)=>run("sa02mGuardJsonResponse({ok:"+ok+",json:()=>Promise.resolve(body)}).json()");
 await read(true);assert.equal(run('logouts'),0,'guard not installed yet: cannot confirm, never logs out');
 run('_sa02mSessionGone=()=>Promise.resolve(false)');await read(true);assert.equal(run('logouts'),0,'session alive: a transient unauthorized body is not a logout');
 run('asked=0;_sa02mSessionGone=()=>{asked++;return Promise.resolve(true)}');await read(false);assert.equal(run('logouts'),0);assert.equal(run('asked'),0,'HTTP 401 is left to the status path');
 assert.deepEqual(await read(true),{error:'unauthorized'});assert.equal(run('logouts'),1,'auth_check 401 ⇒ one logout');
 run('_sa02mSessionGone=()=>Promise.reject(new Error("offline"))');await read(true);assert.equal(run('logouts'),1,'a failed re-check never logs out');
});

// Bench 192.168.1.136, 2026-10-21, this tree, no fault injected: the second of every
// status.cgi HTTP 200 over 150 s (.ai-dev/audit/remediation-20261020/ui-136/
// bench-ui-firstload-136.log). The single-flight queue cycles the background set
// every 23–30 s and rs485 every 18–23 s although their timers tick at 6 / 12 s —
// the «real cadence» a deadline has to cover. services&fast=1 is not noted
// (fetchServicesFast never calls noteStatusSuccess); the 8 s services timeouts at
// ~50 s and ~103 s produced no 200 and are the second half of the defect.
const BENCH_136_OK=[[1.6,'storage'],[2.4,'priority'],[7.2,'hardware'],[7.6,'uptime'],[8.4,'priority'],[9.1,'load'],[10.1,'system'],[11.2,'network'],[16.5,'rs485'],[17.4,'priority'],[23.8,'priority'],[24.7,'storage'],[26.0,'priority'],[30.7,'hardware'],[31.1,'uptime'],[31.8,'priority'],[32.5,'load'],[33.3,'system'],[34.3,'network'],[39.8,'rs485'],[40.7,'priority'],[51.3,'priority'],[52.4,'storage'],[57.2,'hardware'],[58.0,'priority'],[58.7,'uptime'],[59.5,'load'],[60.6,'system'],[61.7,'network'],[63.9,'storage'],[64.8,'priority'],[66.9,'services'],[67.8,'hardware'],[69.2,'priority'],[69.9,'uptime'],[70.6,'load'],[71.4,'system'],[75.6,'rs485'],[76.4,'priority'],[78.9,'network'],[80.1,'storage'],[80.9,'priority'],[85.6,'hardware'],[86.4,'priority'],[87.1,'uptime'],[87.8,'load'],[88.5,'system'],[93.0,'rs485'],[93.8,'priority'],[104.4,'priority'],[105.5,'storage'],[106.9,'network'],[111.6,'hardware'],[112.4,'priority'],[113.1,'uptime'],[113.8,'load'],[114.8,'system'],[119.0,'rs485']];
// status.cgi cache table (ttl_s each part sends); the others are live (0).
const BENCH_TTL_S={storage:10,network:8,system:30,services:45,hardware:10,rs485:8};
test('bench 136: no part goes stale between two on-time 200s; a missed cycle does; ticks never clear it',()=>{
 let nowMs=0; class Clock extends Date { static now(){return nowMs;} }
 const {run,c,elements}=world(fetch,{Date:Clock});
 const alertText=()=>elements.get('dashboard-poll-alert').textContent;
 const note=(part)=>{c.part=part;c.meta={age_s:0,ttl_s:BENCH_TTL_S[part]||0};run("noteStatusSuccess(part,{_status_meta:meta})");};
 const flaps=[];let i=0;
 for(let t=0;t<=150;t++) {
  nowMs=t*1000;
  while(i<BENCH_136_OK.length&&BENCH_136_OK[i][0]<=t) note(BENCH_136_OK[i++][1]);
  run('renderStatusFreshness()');
  if(alertText()) flaps.push(t+'s '+alertText());
 }
 assert.deepEqual(flaps,[],'healthy steady state must never raise the freshness alert');
 assert.equal(run('statusFreshness.time'),undefined,'a part never fetched this page load is absent, not stale');
 // The deadline is the queue's own bound: never under the part's period plus its request timeout.
 for(const part of ['priority','rs485','storage','uptime','network','load','system','services','hardware']) {
  c.part=part;assert.ok(run("statusStaleAfter(part,0)>statusPartPeriodMs(part)+statusRequestTimeout(part,0)"),part);
 }
 // A missed cycle: the clock passes uptime's deadline with no further poll.
 const last=113.1*1000, deadline=run("statusStaleAfter('uptime',0)");
 nowMs=last+deadline-1000;run('renderStatusFreshness()');assert.equal(run('statusFreshness.uptime.state'),'fresh');
 nowMs=last+deadline+1000;run('renderStatusFreshness()');assert.equal(run('statusFreshness.uptime.state'),'stale');
 assert.match(alertText(),/Uptime: Данные устарели/);
 // Hysteresis: later ticks keep it; only a successful poll clears it.
 nowMs+=30000;run('renderStatusFreshness()');assert.equal(run('statusFreshness.uptime.state'),'stale');
 note('uptime');assert.equal(run('statusFreshness.uptime.state'),'fresh');assert.doesNotMatch(alertText(),/Uptime/);
});
test('services: an answer slower than the old 8 s timeout but inside the cold-build margin is accepted',async()=>{
 // Deterministic clock: timers are collected, then every timer due within the
 // bench cold build (9.3 s, 2026-10-21; audit p95 8.5 s) is fired by hand.
 const timers=[];let resolveFetch;
 const {run,c}=world(()=>new Promise(r=>{resolveFetch=r;}),{setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){}});
 c.timedOut=0;
 const p=run("statusFetchJson('services','/',statusRequestTimeout('services',0),_statusPollGeneration,()=>{timedOut++})");
 timers.filter(t=>t.ms<=9300).forEach(t=>t.fn());
 resolveFetch({ok:true,json:()=>Promise.resolve({services:[],_status_meta:{age_s:0,ttl_s:45}})});
 const d=await p;assert.ok(Array.isArray(d.services));assert.equal(run('timedOut'),0);
 assert.ok(run("statusRequestTimeout('services',0)")>=20000);assert.ok(run("statusRequestTimeout('main',0)")>=20000);
 assert.equal(run("statusRequestTimeout('priority',0)"),3000,'fast parts keep the short timeout');
 assert.ok(run("statusRequestTimeout('services_fast',0)")<run("statusRequestTimeout('services',0)"),'the init one-shot is best-effort and must not hold the queue 20 s');
});
test('the freshness banner is written only when its text changes',()=>{
 const {run,elements}=world();let writes=0,text='',display='none';
 elements.set('dashboard-poll-alert',{style:{get display(){return display;},set display(v){display=v;writes++;}},get textContent(){return text;},set textContent(v){text=v;writes++;},dataset:{}});
 run("noteStatusFailure('priority',new Error('offline'))");
 const shown=writes;assert.ok(shown>0);assert.equal(display,'block');
 run('renderStatusFreshness();renderStatusFreshness();renderStatusFreshness()');
 assert.equal(writes,shown,'unchanged text ⇒ no DOM write (no 1 s layout thrash)');
 run("noteStatusSuccess('priority',{_status_meta:{age_s:0}})");assert.equal(display,'none');assert.equal(text,'');
 const hidden=writes;run('renderStatusFreshness()');assert.equal(writes,hidden);
});
