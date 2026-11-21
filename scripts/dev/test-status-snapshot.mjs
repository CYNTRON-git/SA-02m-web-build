// comment-mutation-proof-exempt: executes the shipped snapshot/scheduler functions and asserts batching, stale data, hardware command barriers and lifecycle cancellation.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {test} from 'node:test';
const path = process.env.STATUS_TEST_SOURCE || new URL('../../www/network_config/static/js/app/status.js', import.meta.url);
const source = readFileSync(path, 'utf8');
function world(fetcher = async () => ({ok: true, json: async () => packet()})) {
  const rendered = [], elements = new Map();
  const document = {hidden: false, addEventListener(){}, getElementById(id) {
    if (!elements.has(id)) elements.set(id, {style: {}, textContent: ''});
    return elements.get(id);
  }};
  const context = vm.createContext({fetch: fetcher, AbortController, DOMException, Date, console,
    setTimeout, clearTimeout, setInterval, clearInterval, document, window: {addEventListener(){}},
    applyPriorityStatus: data => rendered.push(['priority', data]), applyRs485Status: data => rendered.push(['rs485', data]),
    getBackgroundPartApply: part => data => rendered.push([part, data]), _lastPartStatus: {},
    writePriorityWarmupPart(){}, clearHwHintPending(){}, mainStatusEpoch: 0,
    fetchBackgroundPart(){}, fetchPriorityPart(){}, fetchStatusRs485(){}, fetchServicesFast(){}, bootstrapBackgroundWidgets(){}});
  vm.runInContext(source.slice(0, source.indexOf('function cssVar')), context);
  // The command epoch function is declared later in the shipped script.
  const start=source.indexOf('function bumpMainStatusEpoch()');
  const end=source.indexOf('/**\n * One-shot early paint', start);
  vm.runInContext(source.slice(start,end),context);
  return {context, rendered, elements, run: code=>vm.runInContext(code,context)};
}
function packet(time = 100, age = 0) {
  const parts = {blocks: {time: 0}};
  for (const part of ['priority','uptime','load','storage','network','system','services','hardware','rs485']) {
    parts[part] = {cpu_usage: 12, _status_meta: {measured_at: time, age_s: age, ttl_s: 6, sample_started_uptime: time}};
  }
  parts.coordinator = {port_coordinator: {available: true, leases: []}, _status_meta:{measured_at:time,age_s:age,ttl_s:6}};
  return {served_at: time, served_uptime: time, snapshot: {schema: 1, boot_id: "boot-a", generated_at: time, generated_uptime: time, parts, errors: {}}};
}
test('one ready packet paints every card, repeats do not rebuild DOM, age still advances',()=>{
  const w = world();w.context.reply=packet();w.run('applyStatusSnapshot(reply)');
  assert.equal(w.rendered.length,9);
  w.context.reply.served_at+=12;w.context.reply.served_uptime+=12;w.run('applyStatusSnapshot(reply)');
  assert.equal(w.rendered.length,9);
  assert.ok(w.run('statusFreshness.priority.sourceAgeMs')>=12000);
  assert.equal(w.run('backgroundLoaded.main'),true);
});
test('cold data is warming, failures are not fresh, disabled block does not linger as error',()=>{
  const w=world();w.context.reply=packet();delete w.context.reply.snapshot.parts.network;
  w.context.reply.snapshot.errors.services='failed';w.run('applyStatusSnapshot(reply)');
  assert.equal(w.run('statusFreshness.network.warming'),true);
  assert.equal(w.run('statusFreshness.services.state'),'error');
  w.context.reply.snapshot.errors.network='failed';w.run('applyStatusSnapshot(reply)');
  assert.equal(w.run('statusFreshness.network.state'),'error');
  w.context.reply.snapshot.parts.blocks.network=0;w.run('applyStatusSnapshot(reply)');
  assert.equal(w.run('statusFreshness.network'),undefined);
});
test('expired coordinator retains unfinished leases but never advertises liveness',()=>{
  const w=world();w.context.reply=packet();
  w.context.reply.snapshot.parts.coordinator.port_coordinator.leases=[{port:'/dev/COM1',phase:'restore_failed'}];
  w.context.reply.served_at+=40;w.context.reply.served_uptime+=40;w.run('applyStatusSnapshot(reply)');
  const services=w.rendered.find(row=>row[0]==='services')[1];
  assert.equal(services.port_coordinator.available,false);
  assert.equal(services.port_coordinator.leases[0].phase,'restore_failed');
});
test('hardware packet measured before a successful command cannot overwrite readback',()=>{
  const w=world();w.context.reply=packet(Math.floor(Date.now()/1000));w.run('applyStatusSnapshot(reply);_statusSnapshotMode=true;bumpMainStatusEpoch()');
  const count=w.rendered.filter(row=>row[0]==='hardware').length;
  w.context.reply.snapshot.parts.hardware._status_meta.measured_at+=.1;
  w.run('applyStatusSnapshot(reply)');
  assert.equal(w.rendered.filter(row=>row[0]==='hardware').length,count);
  w.context.reply.snapshot.parts.hardware._status_meta.measured_at+=20;w.context.reply.snapshot.parts.hardware._status_meta.sample_started_uptime+=20;w.run('applyStatusSnapshot(reply)');
  assert.equal(w.rendered.filter(row=>row[0]==='hardware').length,count+1);
});
test('initial ready snapshot creates one packet timer and no staggered legacy timers',async()=>{
  const urls=[];const w=world(async url=>{urls.push(url);return {ok:true,json:async()=>packet()};});
  try {
    w.run('initStatusPolling()');await new Promise(resolve=>setTimeout(resolve,25));
    assert.deepEqual(urls,['cgi-bin/status_snapshot.cgi']);
    assert.equal(w.run('_statusSnapshotMode'),true);
    assert.equal(w.run('Boolean(_statusPollTimers.snapshot)'),true);
    assert.equal(w.run('Boolean(_statusPollTimers.priority)'),false);
  } finally {w.run('teardownStatusPolling()');}
});
test('unsupported endpoint falls back once; old response after teardown cannot start polling',async()=>{
  const urls=[];const w=world(async url=>{urls.push(url);return {ok:true,json:async()=>url.includes('snapshot')?{}:{time:0}};});
  try {
    w.run('initStatusPolling()');await new Promise(resolve=>setTimeout(resolve,30));
    assert.equal(urls.filter(url=>url.includes('snapshot')).length,1);
    assert.ok(urls.some(url=>url.includes('part=blocks')));
    assert.equal(w.run('_statusSnapshotMode'),false);
  } finally {w.run('teardownStatusPolling()');}
  let resolveBody;const late=world(async()=>({ok:true,json:()=>new Promise(resolve=>{resolveBody=resolve;})}));
  late.run('initStatusPolling()');await new Promise(resolve=>setTimeout(resolve,0));
  late.run('teardownStatusPolling()');resolveBody(packet());await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(late.rendered.length,0);assert.equal(late.run('_statusSnapshotMode'),false);
});

test('hardware barrier survives browser clock jumps, server NTP rollback and collector restart',()=>{
  const w=world(); w.context.reply=packet(1000); w.run('applyStatusSnapshot(reply);bumpMainStatusEpoch()');
  const count=()=>w.rendered.filter(row=>row[0]==='hardware').length;
  w.context.Date=class extends Date {static now(){return 99999999999999;}};
  w.context.reply.served_at=100; w.context.reply.snapshot.generated_at=100;
  w.run('applyStatusSnapshot(reply)'); assert.equal(count(),1);
  w.context.reply.snapshot.started_at=100;
  w.context.reply.snapshot.parts.hardware._status_meta.measured_at=100;
  w.context.reply.snapshot.parts.hardware._status_meta.sample_started_uptime=1010;
  w.context.reply.served_uptime=1011; w.run('applyStatusSnapshot(reply)'); assert.equal(count(),2);
  w.run('bumpMainStatusEpoch()');w.context.reply.snapshot.boot_id='boot-b';
  w.context.reply.snapshot.parts.hardware._status_meta.sample_started_uptime=2;
  w.context.reply.served_uptime=3;w.run('applyStatusSnapshot(reply)');assert.equal(count(),3);
});
test('command while the FIRST snapshot body is pending cannot apply pre-command hardware',async()=>{
  let body;const w=world(async()=>({ok:true,json:()=>new Promise(resolve=>body=resolve)}));
  try {
    w.run('initStatusPolling()');await new Promise(r=>setTimeout(r,0));
    w.run('bumpMainStatusEpoch()');body(packet());await new Promise(r=>setTimeout(r,0));
    assert.equal(w.rendered.filter(row=>row[0]==='hardware').length,0);
    w.context.reply=packet(110);w.run('applyStatusSnapshot(reply)');
    assert.equal(w.rendered.filter(row=>row[0]==='hardware').length,0);
    w.context.reply=packet(120);w.run('applyStatusSnapshot(reply)');
    assert.equal(w.rendered.filter(row=>row[0]==='hardware').length,1);
  } finally {w.run('teardownStatusPolling()');}
});
test('forced service readback survives old body plus changed/expired coordinator until a later collection',async()=>{
  const w=world(async()=>({ok:true,json:async()=>({svc_mosquitto:'active',_status_meta:{measured_at:50,age_s:0,ttl_s:0}})}));
  w.context.reply=packet(100);w.context.reply.snapshot.parts.services.svc_mosquitto='inactive';
  w.run('applyStatusSnapshot(reply);scheduleStatusFetch=(part,delay,run)=>run(()=>{});fetchBackgroundPart("services",null,true)');
  await new Promise(r=>setTimeout(r,0));
  assert.equal(w.rendered.filter(row=>row[0]==='services').at(-1)[1].svc_mosquitto,'active');
  w.context.reply.served_uptime=116;w.run('applyStatusSnapshot(reply)');
  let last=w.rendered.filter(row=>row[0]==='services').at(-1)[1];
  assert.equal(last.svc_mosquitto,'active');assert.equal(last.port_coordinator.available,false);
  w.context.reply.snapshot.parts.services.svc_mosquitto='failed';
  w.context.reply.snapshot.parts.services._status_meta.sample_started_uptime=117;
  w.context.reply.snapshot.parts.services._status_meta.measured_at=51;
  w.run('applyStatusSnapshot(reply)');
  assert.equal(w.rendered.filter(row=>row[0]==='services').at(-1)[1].svc_mosquitto,'failed');
});
test('browser wall clock changes do not alter freshness age',()=>{
  const w=world();let monotonic=1000;w.context.performance={now:()=>monotonic};
  w.context.reply=packet();w.run('_statusSnapshotMode=true;applyStatusSnapshot(reply)');
  for(const wall of [99999999999999,1]) {
    w.context.Date=class extends Date {static now(){return wall;}};
    monotonic+=1000;w.run('renderStatusFreshness()');assert.equal(w.run('statusFreshness.priority.state'),'fresh');
  }
  monotonic+=30000;w.run('renderStatusFreshness()');assert.equal(w.run('statusFreshness.priority.state'),'stale');
});

test('snapshot cadence and queued requests continue across both browser wall-clock jumps',async()=>{
  let wall=20000000, mono=0, requests=0;
  const w=world(async()=>{requests++;return {ok:true,json:async()=>packet()};});
  w.context.Date=class extends Date {static now(){return wall;}};
  w.context.performance={now:()=>mono};
  try {
    w.run('fetchStatusSnapshot(true)');await new Promise(r=>setTimeout(r,0));
    assert.equal(requests,1,'first packet must start immediately at monotonic zero');
    wall-=3600000;mono+=6000;
    w.run('fetchStatusSnapshot(false)');await new Promise(r=>setTimeout(r,0));
    assert.equal(requests,2,'clock rollback must not pause snapshot polling');
    wall+=7200000;mono+=6000;
    w.run('fetchStatusSnapshot(false)');await new Promise(r=>setTimeout(r,0));
    assert.equal(requests,3);
    w.context.completed=[];
    w.run('scheduleStatusFetch("network",1000,release=>{completed.push(1);release()})');
    wall-=7200000;mono+=1000;w.run('pumpStatusFetchQueue()');
    assert.equal(w.context.completed.length,1,'queued deadline must use monotonic time');
  } finally {w.run('teardownStatusPolling()');}
});
