// comment-mutation-proof-exempt: executes the shipped Alice IIFE with deferred transport, actual AbortController and virtual timers; asserts requests, notifications and retired results.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {test} from 'node:test';

const source=readFileSync(process.env.ALICE_TEST_SOURCE || new URL('../../www/network_config/static/js/app/alice.js',import.meta.url),'utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const payload=(marker,state='disabled')=>({marker,client_enabled:state!=='disabled',gateway:{available:true},status:{state}});

function world() {
  let now=0,wall=1700000000000,next=1;
  const timers=new Map(),requests=[],received=[],notices=[],events={},windowEvents={};
  const add=(fn,ms,interval=0)=>{const id=next++;timers.set(id,{fn,at:now+ms,interval});return id;};
  const card={},enable={dataset:{action:'enable'}};
  const document={hidden:false,addEventListener:(name,fn)=>(events[name]??=[]).push(fn),
    getElementById:id=>id==='alice-card'?card:id==='alice-btn-enable'?enable:null};
  const context={document,console,AbortController,DOMException,performance:{now:()=>now},Date:{now:()=>wall},
    escHtml:String,withCsrfHeaders:x=>x,sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},
    cardNotice:(...args)=>notices.push(args),open(){},
    addEventListener:(name,fn)=>(windowEvents[name]??=[]).push(fn),
    fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),
    setTimeout:(fn,ms)=>add(fn,ms),clearTimeout:id=>timers.delete(id),
    setInterval:(fn,ms)=>add(fn,ms,ms),clearInterval:id=>timers.delete(id)};
  context.window=context;
  vm.createContext(context);vm.runInContext(source,context);
  context.sa02mAliceOnData(data=>received.push(data.marker));
  return {context,requests,received,notices,timers,events,windowEvents,
    async settle(index,data) {requests[index].resolve({ok:true,json:async()=>data});await flush();},
    async advance(ms,wallJump=0) {
      const end=now+ms;wall+=ms+wallJump;
      let steps=0;
      while(true) {
        const first=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
        if(!first)break;
        assert.ok(++steps<100,'scheduler must not busy-loop');
        const [id,t]=first;now=t.at;
        if(t.interval)t.at+=t.interval;else timers.delete(id);
        t.fn();await flush();
      }
      now=end;await flush();
    },
    async hidden(value) {document.hidden=value;for(const fn of events.visibilitychange||[])fn();await flush();},
    async emit(name,event={}) {for(const fn of windowEvents[name]||[])fn(event);await flush();},
    async modal(active) {
      if(typeof context.sa02mAliceModalActive==='function') context.sa02mAliceModalActive(active);
      if(active) context.sa02mAliceRefresh();
      await flush();
    },
    close(){context.aliceTabDestroy();}
  };
}

test('an open modal is a shared reader without an integration tab; close retires it',async()=>{
  const w=world();
  try {
    await w.modal(true);await w.modal(true);
    assert.equal(w.requests.length,1,'a visible modal must load its catalogue even from the initial dashboard');
    await w.settle(0,payload('modal'));await w.advance(5000);
    assert.equal(w.requests.length,2);
    await w.modal(false);assert.equal(w.requests[1].options.signal.aborted,true);
    await w.settle(1,payload('closed-old'));assert.deepEqual(w.received,['modal']);
    await w.advance(20000);assert.equal(w.requests.length,2);
    await w.modal(true);assert.equal(w.requests.length,3);
    await w.settle(2,payload('reopened'));assert.deepEqual(w.received,['modal','reopened']);
  } finally {await w.modal(false);w.close();}
});
test('closing a modal or destroying a tab cannot stop the other live consumer',async()=>{
  const w=world();
  try {
    w.context.aliceTabInit();await w.modal(true);assert.equal(w.requests.length,1);
    await w.modal(false);assert.equal(w.requests[0].options.signal.aborted,false);
    await w.settle(0,payload('tab'));await w.advance(5000);assert.equal(w.requests.length,2);
    await w.modal(true);w.context.aliceTabDestroy();
    assert.equal(w.requests[1].options.signal.aborted,false,'the modal still owns the same read');
    await w.settle(1,payload('modal'));await w.advance(5000);assert.equal(w.requests.length,3);
    await w.modal(false);assert.equal(w.requests[2].options.signal.aborted,true);
    await w.advance(20000);assert.equal(w.requests.length,3);
  } finally {await w.modal(false);w.close();}
});
test('modal readers obey hidden/pagehide generations and do not revive after close',async()=>{
  const w=world();
  try {
    await w.modal(true);await w.hidden(true);await w.settle(0,payload('hidden-old'));
    assert.deepEqual(w.received,[]);await w.advance(20000);assert.equal(w.requests.length,1);
    await w.hidden(false);assert.equal(w.requests.length,2);
    await w.emit('pagehide');await w.settle(1,payload('departed-old'));assert.deepEqual(w.received,[]);
    await w.emit('pageshow',{persisted:true});assert.equal(w.requests.length,3);
    await w.modal(false);await w.hidden(true);await w.hidden(false);
    assert.equal(w.requests.length,3);assert.equal(w.requests[2].options.signal.aborted,true);
  } finally {await w.modal(false);w.close();}
});
test('modal mutation shares readback and closing cannot cancel an already sent POST',async()=>{
  const w=world();
  try {
    await w.modal(true);assert.equal(w.requests.length,1);
    const mutation=w.context.sa02mAliceApi({action:'fixture'});
    await w.settle(0,payload('pre-mutation'));assert.deepEqual(w.received,[]);
    await w.settle(1,{ok:true});await mutation;assert.equal(w.requests.length,3);
    await w.settle(2,payload('after-mutation'));assert.deepEqual(w.received,['after-mutation']);
    const sent=w.context.sa02mAliceApi({action:'fixture'});await w.modal(false);
    assert.equal(w.requests[3].options.signal,undefined);
    await w.settle(3,{ok:true});await sent;assert.equal(w.requests.length,4);
    await w.modal(true);assert.equal(w.requests.length,5);
  } finally {await w.modal(false);w.close();}
});

test('slow Alice GET is single-flight and the next poll follows completion',async()=>{
  const w=world();
  try {
    w.context.aliceTabInit();w.context.aliceTabInit();
    await w.advance(6000);
    assert.equal(w.requests.length,1,'a five-second tick cannot overlap a slow GET');
    const shared=w.context.sa02mAliceRefresh();
    assert.equal(w.requests.length,1,'manual consumers join the same read');
    await w.settle(0,payload('one'));await shared;
    assert.deepEqual(w.received,['one']);
    await w.advance(4999);assert.equal(w.requests.length,1);
    await w.advance(1);assert.equal(w.requests.length,2);
  } finally {w.close();}
});

test('hidden and destroyed reads cannot render or notify; visible return starts exactly once',async()=>{
  const w=world();
  try {
    w.context.aliceTabInit();await w.hidden(true);
    await w.settle(0,payload('hidden-old'));
    assert.deepEqual(w.received,[],'a response retired by hiding is not current data');
    assert.equal(w.requests[0].options.signal.aborted,true);
    await w.advance(20000);assert.equal(w.requests.length,1);
    await w.hidden(false);await w.hidden(false);
    assert.equal(w.requests.length,2);
    w.context.aliceTabDestroy();await w.settle(1,payload('destroyed-old'));
    assert.deepEqual(w.received,[]);
    await w.hidden(true);await w.hidden(false);
    assert.equal(w.requests.length,2,'visibility cannot activate an unselected integration');
    w.context.aliceTabInit();await w.settle(2,payload('returned'));
    assert.deepEqual(w.received,['returned']);
    assert.deepEqual(w.notices,[],'retirement is not a transport-error toast');
  } finally {w.close();}
});

test('mutation retires the older GET, joins one readback, and never aborts the sent POST',async()=>{
  const w=world();
  try {
    w.context.aliceTabInit();
    const mutation=w.context.sa02mAliceApi({action:'set_homekit_visible',visible:{lamp:true}});
    assert.equal(w.requests[1].options.method,'POST');
    await w.settle(0,payload('pre-mutation'));
    assert.deepEqual(w.received,[],'an old read must not overwrite command readback');
    assert.equal(w.requests[0].options.signal.aborted,true);
    await w.settle(1,{ok:true});assert.equal((await mutation).ok,true);
    const readback=w.context.sa02mAliceRefresh();
    assert.equal(w.requests.length,3,'the mutation and caller share one fresh readback');
    await w.settle(2,payload('after-mutation'));await readback;
    assert.deepEqual(w.received,['after-mutation']);
    const sent=w.context.sa02mAliceApi({action:'disable'});
    await w.hidden(true);
    assert.equal(w.requests[3].options.signal,undefined,'read cancellation must not abort an already sent command');
    await w.settle(3,{ok:true});assert.equal((await sent).ok,true);
    assert.equal(w.requests.length,4,'hidden completion must not restart polling');
    await w.hidden(false);assert.equal(w.requests.length,5);
  } finally {w.close();}
});

test('the GET deadline covers a hung body and late completion cannot escape timeout retirement',async()=>{
  const w=world();let finish;
  try {
    w.context.aliceTabInit();
    w.requests[0].resolve({ok:true,json:()=>new Promise(resolve=>{finish=resolve;})});await flush();
    await w.advance(8000);
    assert.equal(w.requests[0].options.signal.aborted,true);
    assert.equal(w.notices.length,1,'a real timeout remains visible');
    await w.advance(5000);assert.equal(w.requests.length,2);
    finish(payload('late-timeout'));await flush();assert.deepEqual(w.received,[]);
    await w.settle(1,payload('recovered'));assert.deepEqual(w.received,['recovered']);
  } finally {w.close();}
});

test('fast command readback shares the single-flight chain and settled state restores normal polling',async()=>{
  const w=world();
  try {
    w.context.aliceTabInit();await w.settle(0,payload('initial'));
    const command=w.context.aliceToggleClient();await w.settle(1,{ok:true});
    assert.equal(w.requests.length,3);
    await w.settle(2,payload('connecting','connecting'));await command;
    await w.advance(1000,-3600000);assert.equal(w.requests.length,4);
    await w.advance(1000,7200000);assert.equal(w.requests.length,4,'fast tick cannot overlap its GET');
    await w.settle(3,payload('connected','connected'));
    await w.advance(4999);assert.equal(w.requests.length,4);
    await w.advance(1);assert.equal(w.requests.length,5,'settled readback resumes the ordinary completion schedule');
  } finally {w.close();}
});

test('confirmed pagehide retires reads and persisted pageshow resumes one shared consumer stream',async()=>{
  const w=world(),cloud=[],home=[],homekit=[];
  try {
    for(const list of [cloud,home,homekit])w.context.sa02mAliceOnData(data=>list.push(data.marker));
    w.context.aliceTabInit();await w.emit('pagehide',{persisted:true});
    await w.context.sa02mAliceRefresh();assert.equal(w.requests.length,1,'confirmed pagehide blocks manual restart too');
    await w.settle(0,payload('retired'));assert.deepEqual(w.received,[]);
    await w.advance(1000);await w.emit('pageshow',{persisted:true});
    assert.equal(w.requests.length,2);
    await w.settle(1,payload('shared'));
    for(const list of [cloud,home,homekit])assert.deepEqual(list,['shared']);
    await w.emit('pageshow',{persisted:false});assert.equal(w.requests.length,2);
    assert.equal((w.windowEvents.pagehide||[]).length,1);
  } finally {w.close();}
});

for(const wallJump of [-3600000,3600000]) {
  test('transitional fast readback expires after monotonic 20 seconds despite wall jump '+wallJump,async()=>{
    const w=world();
    try {
      w.context.aliceTabInit();await w.settle(0,payload('initial'));
      const command=w.context.aliceToggleClient();await w.settle(1,{ok:true});
      await w.settle(2,payload('connecting','connecting'));await command;
      for(let second=1;second<=20;second++) {
        const count=w.requests.length;
        await w.advance(1000,second===1?wallJump:0);
        assert.equal(w.requests.length,count+1,'fast cadence remains one completion-scheduled second until its monotonic bound');
        await w.settle(count,payload('connecting-'+second,'connecting'));
      }
      const count=w.requests.length;
      await w.advance(4999);assert.equal(w.requests.length,count,'expired fast mode returns to the normal period');
      await w.advance(1);assert.equal(w.requests.length,count+1);
    } finally {w.close();}
  });
}

test('a locally unserializable command cannot poison the shared read gate',async()=>{
  const w=world();
  try {
    w.context.aliceTabInit();await w.settle(0,payload('initial'));
    const cyclic={action:'invalid'};cyclic.self=cyclic;
    await assert.rejects(w.context.sa02mAliceApi(cyclic),/circular/i);
    const read=w.context.sa02mAliceRefresh();
    assert.equal(w.requests.length,2,'no POST was sent and reads remain available');
    await w.settle(1,payload('still-live'));await read;
    assert.deepEqual(w.received,['initial','still-live']);
  } finally {w.close();}
});
