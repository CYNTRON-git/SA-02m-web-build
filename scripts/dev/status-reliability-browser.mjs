// comment-mutation-proof-exempt: drives real Chromium over served UI and injected failures, asserting rendered states and lifecycle behavior.
// Behavioural regressions over the shipped scripts and actual HTTP bodies.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {appendFileSync, existsSync} from 'node:fs';
import assert from 'node:assert/strict';
const www=new URL('../../www/network_config/',import.meta.url);
// Playwright lives in the gitignored scripts/dev/node_modules (ui-layout's
// loader shape). Absent package or browser ⇒ SKIP (77 + QUALITY_SKIP_LOG line),
// never a FAIL on a box that cannot render and never a vacuous PASS.
function skip(reason) {
 console.log('SKIP  '+reason);
 if(process.env.QUALITY_SKIP_LOG) appendFileSync(process.env.QUALITY_SKIP_LOG, reason+'\n');
 process.exit(77);
}
async function loadPlaywright() {
 const norm=(m)=>(m&&m.chromium?m:(m&&m.default)||m);
 const local=new URL('./node_modules/playwright/index.js',import.meta.url);
 if(existsSync(local)) {try {return norm(await import(local.href));}catch {/* fall through */}}
 try {return norm(await import('playwright'));}catch {return null;}
}
const pw=await loadPlaywright();
if(!pw||!pw.chromium) skip('status-reliability-browser: playwright not installed (npm run ui-layout:install)');
let mode='normal', cloudRequests=0;
let serviceEvents=[], serviceSubmitted=false, serviceStateReads=0, serviceHeld=[];
const serviceOp='c'.repeat(32);
const serviceList=active=>({ok:true,flasher_busy:false,services:[{id:'vplc',label:'vPLC',unit:'vplc.service',installed:true,active:active?'active':'inactive',enabled:active,masked:!active,user_disabled:!active}]});
const server=createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');
 if(url.pathname.includes('/cgi-bin/')||url.pathname.startsWith('/api/')) {
  res.setHeader('Content-Type','application/json');
  if(url.pathname==='/api/devices') {res.end(JSON.stringify({ok:true,mtd:[{id:'mtdx62-COM2-1',kind:'mtd',label:'MTD262-MB',ok:true,age_s:1,presence:1,illuminance_lux:1234.5,target_distance_m:7.99,detection_distance_m:8.00,detection_shielding_m:1.00,admission_delay_s:9.99,departure_delay_s:120,trigger_sensitivity:9,maintain_sensitivity:9,entrance_reduction_m:0.50}]}));return;}

  // A dead session: every CGI answers 200 + the unauthorized body, and the
  // canonical re-check (auth_check.cgi) answers a real 401 — as on the board.
  if(mode==='unauthorized') {
   if(url.pathname.endsWith('/auth_check.cgi')) {res.writeHead(401);res.end('unauthorized');return;}
   res.end(JSON.stringify({error:'unauthorized'}));return;
  }
  if(mode==='status-build-error'&&url.searchParams.get('part')==='network') {res.end('{"error":"status_build_failed"}');return;}
  if(mode.startsWith('service-')&&url.pathname.endsWith('/services_ctrl.cgi')) {
   const phase=req.method==='POST'?'post':url.searchParams.has('result')?'result':serviceSubmitted?'state':'list';
   serviceEvents.push(phase);
   if(phase==='post') serviceSubmitted=true;
   if(phase==='state') serviceStateReads++;
   // Recovery list reads after a timeout are answered normally.
   if(mode==='service-'+phase+'-stall'&&(phase!=='state'||serviceStateReads===1)) {res.writeHead(200);res.write('{');return;}
   if(phase==='post') {res.end(JSON.stringify({ok:true,pending:true,id:'vplc',action:'stop',op_id:serviceOp}));return;}
   if(phase==='result') {res.end(JSON.stringify({ok:true,id:'vplc',action:'stop',op_id:serviceOp}));return;}
   if(phase==='state'&&mode==='service-success'&&serviceStateReads===1) {serviceHeld.push(res);return;}
   res.end(JSON.stringify(serviceList(!serviceSubmitted)));return;
  }
  if(mode==='stall'&&url.searchParams.get('part')==='network') {res.writeHead(200);res.write('{');return;}
  if(mode==='cards'&&['cloud.cgi','sa02m_homekit_api.cgi','sa02m_homeconnect_api.cgi','sa02m_audio_api.cgi'].some(v=>url.pathname.endsWith(v))) {cloudRequests++;res.writeHead(200);res.write('{');return;}
  res.end(JSON.stringify({_status_meta:{age_s:0,ttl_s:0},cpu_usage:42,temp_c:51,ram_pct:20,disk_pct:20}));return;
 }
 try {const file=new URL('.'+url.pathname,www);assert.ok(file.href.startsWith(www.href));const data=await readFile(file);res.setHeader('Content-Type',url.pathname.endsWith('.js')?'text/javascript':url.pathname.endsWith('.css')?'text/css':'text/html');res.end(data);}catch {res.writeHead(404);res.end();}
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const origin=`http://127.0.0.1:${server.address().port}`;
let browser;
try {browser=await pw.chromium.launch({headless:true});}
catch(e) {
 const msg=String(e&&e.message||e);
 if(/Executable doesn't exist|npx playwright install|Looks like Playwright|browserType.launch/i.test(msg)) skip('status-reliability-browser: chromium not installed for playwright ('+msg.split('\n')[0]+')');
 throw e;
}
try {
 const context=await browser.newContext({viewport:{width:500,height:900}});await context.addCookies([{name:'session_token',value:'test',url:origin}]);
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin+'/index.html');
 await page.evaluate(()=>{teardownStatusPolling();});
 const warmup = await page.evaluate(()=>{
  const results=[];
  for(const age of [null,-2,'2','bad']) {
   sessionStorage.setItem(PRIORITY_WARMUP_KEY,JSON.stringify({cpu:{ts:Date.now()-5000,data:{_status_meta:{age_s:age}}}}));
   const hit=getPriorityWarmupPart('cpu');noteStatusSuccess('priority',hit);
   results.push([hit._status_meta.age_s,statusFreshness.priority.state]);
  }
  sessionStorage.removeItem(PRIORITY_WARMUP_KEY);return results;
 });
 assert.deepEqual(warmup,[[null,'unknown'],[null,'unknown'],[null,'unknown'],[null,'unknown']]);
 mode='stall';
 await page.evaluate(()=>fetchBackgroundPart('network',applyNetworkStatus,true));
 await page.waitForTimeout(5500);
 assert.deepEqual(await page.evaluate(()=>({busy:backgroundBusy.network,inflight:_statusFetchInFlight,failed:statusFailures.network})),{busy:false,inflight:false,failed:1});
 mode='normal';
 await page.evaluate(()=>{fetchBackgroundPart('network',applyNetworkStatus,true);fetchPriorityPart('priority');});
 await page.waitForFunction(()=>statusFailures.network===0&&!_statusFetchInFlight);
 mode='status-build-error';
 await page.evaluate(()=>fetchBackgroundPart('network',applyNetworkStatus,true));
 await page.waitForFunction(()=>statusFailures.network===1&&!_statusFetchInFlight);
 assert.match(await page.locator('#dashboard-poll-alert').innerText(),/Ошибка обновления/);
 mode='normal';await page.evaluate(()=>fetchBackgroundPart('network',applyNetworkStatus,true));
 await page.waitForFunction(()=>statusFailures.network===0&&!_statusFetchInFlight);
 await page.evaluate(()=>{noteStatusFailure('priority',new Error('offline'));noteStatusSuccess('network',{_status_meta:{age_s:0}});});
 const alert=await page.locator('#dashboard-poll-alert').innerText();
 assert.match(alert,/^CPU, температура, ОЗУ, диск: /);assert.doesNotMatch(alert,/priority/);
 await page.evaluate(()=>{noteStatusSuccess('priority',{_status_meta:{age_s:statusStaleAfter('priority',0)/1000+1}});});
 assert.match(await page.locator('#dashboard-poll-alert').innerText(),/устарели/);
 for(const theme of ['dark','light']) for(const variant of ['sa02m-1eth','sa02m-2eth']) for(const locale of ['ru','en']) {
  await page.evaluate(({theme,variant,locale})=>{document.documentElement.setAttribute('data-theme',theme);applyVariantVisibility(variant);if(window.sa02mI18n)window.sa02mI18n.setLang(locale);},{theme,variant,locale});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 }
 await page.evaluate(()=>switchTab('devices'));
 await page.waitForSelector('.dev-card--mtd');
 await page.locator('.dev-card--mtd').evaluate(el=>{el.classList.add('is-open');});
 for(const width of [500,1101,1600]) for(const locale of ['ru','en']) {
  await page.setViewportSize({width,height:1000});
  await page.evaluate(locale=>{window.sa02mI18n.setLang(locale);},locale);await page.waitForTimeout(80);
  const clipped=await page.locator('.dev-card--mtd .dev-kpi--set').evaluateAll(els=>els.filter(el=>{
   if(!el.getBoundingClientRect().width)return false;
   const label=el.querySelector('.dev-kpi-lbl');if(label&&label.scrollHeight>label.clientHeight+1)return true;
   const outer=el.getBoundingClientRect();return [...el.querySelectorAll('input,.dev-kpi-unit')].some(v=>{const r=v.getBoundingClientRect();return r.right>outer.right+1||r.left<outer.left-1;});
  }).map(e=>e.dataset.key));
  assert.deepEqual(clipped,[],`MTD settings fit at ${width}/${locale}`);
  if(locale==='en') assert.doesNotMatch(await page.locator('.dev-card--mtd [data-f=presence]').innerText(),/[а-яё]/i);
 }
 mode='cards';
 await page.evaluate(async()=>{
  window._nativeTimedFetch=fetchWithTimeout;fetchWithTimeout=(u,o,t)=>window._nativeTimedFetch(u,o,100);
  cloudTabInit();cloudTabInit();audioTabInit();audioTabInit();
  await Promise.all([homekitRefresh(),homekitRefresh(),homeconnectRefresh(),homeconnectRefresh()]);
 });
 await page.waitForTimeout(200);assert.equal(cloudRequests,4);
 await page.evaluate(async()=>{cloudTabInit();audioTabInit();await Promise.all([homekitRefresh(),homeconnectRefresh()]);});
 await page.waitForTimeout(200);assert.equal(cloudRequests,8);
 await page.evaluate(()=>{cloudTabDestroy();audioTabDestroy();homekitTabDestroy();homeconnectTabDestroy();fetchWithTimeout=window._nativeTimedFetch;});
 // Real DOM actions, fetch guard and HTTP streams; only the device responses
 // are synthetic. The page clock makes 20-second body deadlines deterministic.
 await page.clock.install();
 page.on('dialog',dialog=>dialog.accept());
 await page.evaluate(()=>{
  teardownStatusPolling();
  window._serviceTestToasts=[];
  const original=toast;toast=function(...args){window._serviceTestToasts.push(args);return original(...args);};
 });
 for(const [width,theme] of [[1600,'dark'],[500,'light']]) for(const locale of ['ru','en']) {
  await page.setViewportSize({width,height:1000});
  await page.evaluate(({theme,locale})=>{document.documentElement.setAttribute('data-theme',theme);window.sa02mI18n.setLang(locale);},{theme,locale});
  for(const scenario of ['success','post-stall','result-stall','state-stall']) {
   serviceEvents=[];serviceSubmitted=false;serviceStateReads=0;serviceHeld=[];mode='service-'+scenario;
   await page.evaluate(()=>{switchTab('system');window._serviceTestToasts=[];});
   if(width===500) await page.locator('#nav-burger').click();
   await page.locator('[data-system-sub="services"]').click();
   if(width===500) await page.locator('#nav-burger').click();
   await page.evaluate(()=>loadServicesControl(false));
   const stop=page.locator('#svc-ctl-list [data-svc-id="vplc"] button[data-svc-action="stop"]');
   await stop.waitFor();
   await page.waitForFunction(()=>!document.querySelector('#svc-ctl-list button[data-svc-id="vplc"][data-svc-action="stop"]').disabled);
   await stop.click();
   await page.waitForFunction(()=>!!_svcCtlPending.vplc);
   assert.equal(await stop.isDisabled(),true);
   if(scenario!=='post-stall') {
    // Wait for the real admission body before advancing result-poll timers.
    await page.waitForFunction(()=>window._serviceTestToasts.some(v=>v[1]==='info'));
    await page.clock.fastForward(1500);
   }
   const wanted=scenario==='post-stall'?'post':scenario==='result-stall'?'result':'state';
   const until=Date.now()+5000;
   while(!serviceEvents.includes(wanted)&&Date.now()<until) await new Promise(r=>setTimeout(r,20));
   assert.ok(serviceEvents.includes(wanted),`${scenario}: actual HTTP ${wanted} reached`);
   if(scenario==='success') {
    assert.equal(serviceHeld.length,1);
    assert.equal(await stop.isDisabled(),true,'matching result alone cannot unlock before state verification');
    assert.equal(await page.evaluate(()=>window._serviceTestToasts.filter(v=>v[1]==='success').length),0);
    serviceHeld.pop().end(JSON.stringify(serviceList(false)));
   } else {
    await page.clock.fastForward(20500);
   }
   await page.waitForFunction(()=>!_svcCtlPending.vplc);
   const toasts=await page.evaluate(()=>window._serviceTestToasts.map(v=>v[1]));
   assert.equal(toasts.filter(v=>v==='success').length,scenario==='success'?1:0);
   assert.equal(toasts.filter(v=>v==='error').length,scenario==='success'?0:1);
   assert.equal(serviceEvents.filter(v=>v==='post').length,1,'never resend an accepted or timed-out POST');
   await page.waitForFunction(()=>!document.getElementById('svc-ctl-refresh-btn').disabled);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  }
  mode='service-list-stall';serviceSubmitted=false;
  await page.locator('#svc-ctl-refresh-btn').click();
  await page.waitForFunction(()=>document.getElementById('svc-ctl-refresh-btn').disabled);
  await page.clock.fastForward(20500);
  await page.waitForFunction(()=>!document.getElementById('svc-ctl-refresh-btn').disabled);
 }
 console.log('service browser: 20 desktop/mobile RU/EN lifecycle cases; actual unfinished JSON bodies, no duplicate POST, verified state before unlock');
 mode='unauthorized';await page.evaluate(()=>fetchPriorityPart('priority'));await page.waitForURL('**/login.html');
 assert.equal(errors.length,0,errors.join('\n'));console.log('status browser: stalled body recovery, partial failure, stale cache, 8 narrow UI combinations, MTD at 3 widths/2 locales, four card single-flight/timeout, CGI unauthorized passed');
}finally {await browser.close();server.closeAllConnections();server.close();}
