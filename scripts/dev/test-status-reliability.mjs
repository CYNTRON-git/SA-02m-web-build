// comment-mutation-proof-exempt: executes shipped scheduler and request functions against delayed HTTP responses and aborts; accepts observable lifecycle state.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {test} from 'node:test';
const root = new URL('../../', import.meta.url);
const source = readFileSync(new URL('www/network_config/static/js/app/status.js',root),'utf8');
function world(fetcher=fetch) {
 const elements = new Map();
 const document={getElementById(id){ if(!elements.has(id)) elements.set(id,{style:{},textContent:'',dataset:{}}); return elements.get(id); },querySelectorAll(){return [];}};
 const c=vm.createContext({fetch:fetcher,AbortController,DOMException,setTimeout,clearTimeout,setInterval,clearInterval,Date,document,window:{},console});
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
 const {run}=world();
 run("noteStatusSuccess('priority',{_status_meta:{age_s:20}})");assert.equal(run('statusFreshness.priority.state'),'stale');
 run("noteStatusSuccess('priority',{_status_meta:{age_s:20}})");assert.equal(run('statusFreshness.priority.state'),'stale');
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
 const {run,elements}=world();
 run("noteStatusFailure('priority',new Error('offline'))");
 const text=elements.get('dashboard-poll-alert').textContent;
 assert.match(text,/^CPU, температура, ОЗУ, диск: Ошибка обновления$/);assert.doesNotMatch(text,/priority/);
 run("noteStatusSuccess('storage',{_status_meta:{age_s:20,ttl_s:10}})");assert.equal(run('statusFreshness.storage.state'),'fresh');
 run("noteStatusSuccess('storage',{_status_meta:{age_s:23,ttl_s:10}})");assert.equal(run('statusFreshness.storage.state'),'stale');
 run("noteStatusSuccess('storage',{_status_meta:{age_s:23,ttl_s:45}})");assert.equal(run('statusFreshness.storage.state'),'fresh');
 run("noteStatusSuccess('services',{_status_meta:{age_s:70,ttl_s:45}})");assert.equal(run('statusFreshness.services.state'),'stale');
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
