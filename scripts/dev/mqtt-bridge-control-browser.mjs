// comment-mutation-proof-exempt: real page clicks and delayed route responses verify the user-visible bridge operation lifecycle.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const {chromium} = createRequire(import.meta.url)('playwright');
const root = path.resolve(process.env.SA02M_UI_ROOT || fileURLToPath(new URL('../../www/network_config/', import.meta.url)));
const output = path.resolve(process.env.MQTT_CONTROL_SCREENSHOTS || '.tmp/mqtt-bridge-control-screenshots');
const origin = 'http://sa02m.test';
const opId = 'c'.repeat(32);
const running = {mosquitto_active:1,mosquitto_disabled:0,bridge_active:1,bridge_disabled:0,clients_connected:2};
const stopped = {...running,bridge_active:0,bridge_disabled:1};
await mkdir(output,{recursive:true});
async function exercise(browser,lang,theme,width){
  const context = await browser.newContext({viewport:{width,height:900},hasTouch:width===500});
  const pendingRoutes = new Set(), errors=[], posts=[],configPosts=[],applyActions=[];
  let applyOperation=null;
  let heldStatus=null, holdNextStatus=false, terminal=false, failed=false, resultReads=0, phase='initial';
  const fulfil = (route,json) => route.fulfill({json});
  try {
    await context.addCookies([{name:'session_token',value:'fixture',url:origin},{name:'sa02m_csrf',value:'fixture-csrf',url:origin}]);
    await context.addInitScript(({lang,theme})=>{
      localStorage.setItem('sa02m-lang',lang);localStorage.setItem('sa02m-theme',theme);
    },{lang,theme});
    await context.route('**/*',async route=>{
      pendingRoutes.add(route);
      try {
        const request=route.request(),url=new URL(request.url());
        assert.equal(url.origin,origin,'fixture must not reach external hosts');
        if(url.pathname.startsWith('/cgi-bin/')||url.pathname.startsWith('/api/')){
          if(url.pathname==='/cgi-bin/mqtt_config.cgi'){
            if(request.method()==='POST'){
              const body=request.postDataJSON();configPosts.push(body);assert.equal(body.restart,false);
              return await fulfil(route,{ok:true});
            }
            return await fulfil(route,{mqtt:{broker:'127.0.0.1'},devices:[]});
          }
          if(url.pathname==='/cgi-bin/mqtt_templates.cgi')return await fulfil(route,{ok:true,templates:[]});
          if(url.pathname==='/cgi-bin/mqtt_status.cgi'){
            if(holdNextStatus){holdNextStatus=false;heldStatus=route;await new Promise(resolve=>{route.release=resolve;});return;}
            return await fulfil(route,phase==='apply'&&applyOperation?.action==='start'?running:terminal?stopped:running);
          }
          if(url.pathname==='/cgi-bin/services_ctrl.cgi'){
            if(request.method()==='POST'){
              const body=request.postDataJSON();posts.push(body);
              if(phase==='apply'){
                assert.equal(body.id,'mqtt-bridge');applyActions.push(body.action);
                assert.deepEqual(applyActions,['stop','start'].slice(0,applyActions.length));
                applyOperation={...body,op_id:posts.length.toString(16).padStart(32,'0')};
                return await fulfil(route,{ok:true,pending:true,...applyOperation});
              }
              assert.deepEqual(body,{id:'mqtt-bridge',action:phase==='failure'?'start':'stop'});
              assert.equal(request.headers()['x-sa02m-csrf'],'fixture-csrf','CSRF header must survive actual UI dispatch');
              return await fulfil(route,{ok:true,pending:true,...body,op_id:opId});
            }
            if(url.searchParams.has('result')){
              resultReads++;assert.equal(url.searchParams.get('op_id'),phase==='apply'?applyOperation.op_id:opId);assert.equal(url.searchParams.get('id'),'mqtt-bridge');
              if(phase==='apply')return await fulfil(route,{ok:true,...applyOperation});
              return await fulfil(route,failed?{ok:false,error:'port_busy',id:'mqtt-bridge',action:'start',op_id:opId}
                :terminal?{ok:true,id:'mqtt-bridge',action:'stop',op_id:opId}:{ok:true,pending:true});
            }
            const active=phase==='apply'?applyOperation?.action==='start':!terminal;
            return await fulfil(route,{ok:true,services:[{id:'mqtt-bridge',installed:true,active:active?'active':'inactive',enabled:active,user_disabled:!active}]});
          }
          assert.notEqual(request.method(),'POST','no unrelated mutations from bridge control');
          if(url.pathname==='/cgi-bin/auth_check.cgi')return await fulfil(route,{ok:true,csrf_token:'fixture-csrf'});
          return await fulfil(route,{ok:true});
        }
        const file=path.resolve(root,'.'+url.pathname);
        assert.ok(file.startsWith(root+path.sep),'static path must stay in web root');
        try {await route.fulfill({path:file});}
        catch {await route.fulfill({status:404,body:''});}
      } catch(error){errors.push(error.message);await route.abort().catch(()=>{});}
      finally {pendingRoutes.delete(route);}
    });
    const page=await context.newPage();page.setDefaultTimeout(8000);
    const navigate=async tab=>{
      if(width===500){
        await page.locator('#nav-burger').tap();
        await page.locator(`.nav-item[data-tab="${tab}"]`).tap();
      } else await page.locator(`.nav-item[data-tab="${tab}"]`).click();
    };
    page.on('pageerror',e=>errors.push(e.message));
    page.on('dialog',dialog=>dialog.accept());
    await page.goto(origin+'/index.html');
    await page.evaluate(()=>{if(typeof teardownStatusPolling==='function')teardownStatusPolling();window.SA02M_CSRF='fixture-csrf';});
    await navigate('mqtt');
    const button=page.locator('#mqtt-bridge-toggle-btn'),badge=page.locator('#mqtt-bridge-badge'),broker=page.locator('#mqtt-broker-badge');
    await page.waitForFunction(()=>!document.getElementById('mqtt-bridge-toggle-btn').disabled);
    holdNextStatus=true;await page.evaluate(()=>window.mqttTabInit());
    const until=Date.now()+5000;while(!heldStatus&&Date.now()<until)await page.waitForTimeout(20);
    assert.ok(heldStatus,'pre-command status request was held');
    if(width===500)await button.tap();
    else {
      await button.focus();
      assert.equal(await button.evaluate(el=>document.activeElement===el),true,'native control receives keyboard focus');
      await page.keyboard.press('Enter');
    }
    await page.waitForFunction(()=>document.getElementById('mqtt-bridge-toggle-btn').disabled);
    assert.match(await badge.innerText(),lang==='ru'?/Остановка моста/:/Stopping the bridge/);
    assert.equal(await broker.innerText(),lang==='ru'?'Работает':'Running');
    await page.evaluate(()=>window.mqttToggleBridge());assert.equal(posts.length,1);
    assert.equal(await page.locator('#mqtt-save-btn').isDisabled(),true);
    await page.evaluate(()=>window.mqttSaveAndApply());assert.equal(configPosts.length,0,'pending Stop excludes config mutation');
    // A real navigation away/back must not release the local operation lock.
    await navigate('dashboard');await navigate('mqtt');
    assert.equal(await button.isDisabled(),true);
    await page.locator('.mqtt-broker-bar').screenshot({path:path.join(output,`${lang}-${theme}-${width}-pending.png`)});
    const polledUntil=Date.now()+5000;while(resultReads===0&&Date.now()<polledUntil)await page.waitForTimeout(20);
    assert.ok(resultReads>0);terminal=true;
    await page.waitForFunction(()=>!document.getElementById('mqtt-bridge-toggle-btn').disabled);
    assert.equal(await button.innerText(),lang==='ru'?'Запустить':'Start');
    await fulfil(heldStatus,running);heldStatus.release();heldStatus=null;
    await page.waitForTimeout(100);
    assert.equal(await badge.innerText(),lang==='ru'?'Отключен':'Disabled');
    assert.equal(await broker.innerText(),lang==='ru'?'Работает':'Running');
    await page.locator('.mqtt-broker-bar').screenshot({path:path.join(output,`${lang}-${theme}-${width}-stopped.png`)});
    phase='failure';failed=true;await button.click();
    const hint=page.locator('#mqtt-bridge-control-status');
    await hint.filter({hasText:lang==='ru'?'порт RS-485':'RS-485 port'}).waitFor({state:'visible'});
    assert.equal(await button.isDisabled(),true,'failed operation waits for a fresh status');
    assert.equal(posts.length,2);assert.equal(await page.locator('#mqtt-toast').innerText(),'');
    assert.equal(await button.getAttribute('aria-describedby'),'mqtt-bridge-control-status');
    assert.equal(await hint.getAttribute('role'),'status','inline failure is announced without a popup');
    const contrast=await hint.evaluate(el=>{
      const rgba=value=>{
        const parts=value.match(/[\d.]+/g).map(Number);
        return [...parts.slice(0,3),parts.length===4?parts[3]:1];
      };
      const composite=(front,back)=>front.slice(0,3).map((v,i)=>v*front[3]+back[i]*(1-front[3]));
      const chain=[];
      for(let node=el;node;node=node.parentElement){
        const style=getComputedStyle(node);
        if(style.backgroundImage!=='none'||Number(style.opacity)!==1)throw new Error('unsupported contrast surface');
        chain.push(rgba(style.backgroundColor));
      }
      let background=[255,255,255];
      for(const layer of chain.reverse())background=composite(layer,background);
      const foreground=composite(rgba(getComputedStyle(el).color),background);
      const luminance=rgb=>rgb.map(v=>v/255).map(v=>v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4)
        .reduce((sum,v,i)=>sum+v*[0.2126,0.7152,0.0722][i],0);
      const a=luminance(foreground),b=luminance(background);
      return {foreground,background,ratio:(Math.max(a,b)+0.05)/(Math.min(a,b)+0.05)};
    });
    assert.ok(contrast.ratio>=4.5,`inline error contrast ${contrast.ratio} must meet 4.5:1`);
    const geometry=await page.locator('.mqtt-broker-bar').evaluate(bar=>{
      const rect=bar.getBoundingClientRect(),hint=bar.querySelector('#mqtt-bridge-control-status').getBoundingClientRect();
      return {overflow:bar.scrollWidth>bar.clientWidth+1,hintInside:hint.left>=rect.left-1&&hint.right<=rect.right+1};
    });
    assert.deepEqual(geometry,{overflow:false,hintInside:true});
    await page.locator('.mqtt-broker-bar').screenshot({path:path.join(output,`${lang}-${theme}-${width}-error.png`)});
    phase='apply';failed=false;
    await page.locator('#mqtt-save-btn').click();
    assert.equal(await button.isDisabled(),true);
    await page.evaluate(()=>{void window.mqttToggleBridge();void window.mqttSaveAndApply();});
    await page.waitForFunction(()=>!document.getElementById('mqtt-save-btn').disabled);
    assert.equal(configPosts.length,1);assert.deepEqual(applyActions,['stop','start']);assert.equal(posts.length,4);
    assert.equal(await badge.innerText(),lang==='ru'?'Мост активен':'Bridge Active');
    assert.equal(await broker.innerText(),lang==='ru'?'Работает':'Running');
    assert.equal(await hint.isVisible(),false);
    await page.locator('.mqtt-broker-bar').screenshot({path:path.join(output,`${lang}-${theme}-${width}-applied.png`)});
    await page.evaluate(()=>{window.mqttTabDestroy();if(typeof teardownStatusPolling==='function')teardownStatusPolling();});
    await context.unrouteAll({behavior:'wait'});
    assert.equal(pendingRoutes.size,0);assert.deepEqual(errors,[]);
    console.log(JSON.stringify({lang,theme,width,result:'PASS',posts:posts.length,configPosts:configPosts.length,applyActions,resultReads,screenshots:4,
      activation:width===500?'touch':'keyboard',contrast,geometry}));
  } finally {
    if(heldStatus){await fulfil(heldStatus,running).catch(()=>{});heldStatus.release();}
    await context.close();
  }
}
const browser=await chromium.launch({headless:true});
try {
  for(const lang of ['ru','en'])for(const theme of ['dark','light'])for(const width of [500,1280])await exercise(browser,lang,theme,width);
} finally {await browser.close();}
