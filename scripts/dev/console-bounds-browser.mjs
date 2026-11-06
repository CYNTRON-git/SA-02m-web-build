// comment-mutation-proof-exempt: exercises actual console code and authenticated CGI, with response failure injections in Chromium.
import {createServer} from 'node:http';
import {readFile, writeFile, mkdir, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
import {chromium} from './node_modules/playwright/index.mjs';
if (process.platform === 'win32') { console.log('SKIP console-bounds-browser: real CGI requires POSIX'); process.exit(77); }
const www = new URL('../../www/network_config/', import.meta.url);
const out = new URL('../../.tmp/remediation-20261106/console-browser/', import.meta.url);
const sessions = await mkdtemp(join(tmpdir(), 'console-browser-'));
const token = 'a'.repeat(64), csrf = 'b'.repeat(64);
const digest = createHash('sha256').update(token).digest('hex');
await writeFile(join(sessions, digest), `${Math.floor(Date.now()/1000)+3600} tester\n`);
await writeFile(join(sessions, digest+'.csrf'), csrf+'\n');
let injected = null;
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.endsWith('/cmd_exec.cgi')) {
    if (injected) { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(injected)); return; }
    let body = '';
    for await (const chunk of req) body += chunk;
    const child = spawn('bash', [fileURLToPath(new URL('cgi-bin/cmd_exec.cgi', www))], {
      env: {...process.env, SA02M_SESSION_DIR: sessions, REQUEST_METHOD: 'POST', CONTENT_LENGTH: String(Buffer.byteLength(body)),
        HTTP_COOKIE: req.headers.cookie || '', HTTP_X_SA02M_CSRF: req.headers['x-sa02m-csrf'] || ''}
    });
    let output = ''; child.stdout.on('data', data => {output += data;});
    child.stderr.resume(); child.stdin.end(body);
    child.on('close', rc => {
      res.setHeader('Content-Type', 'application/json');
      res.end(rc === 0 ? output.split('\n\n').slice(1).join('\n\n') : JSON.stringify({ok:false,error:'fixture_cgi_failed'}));
    });
    return;
  }
  if (url.pathname.includes('/cgi-bin/') || url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ok:true, csrf_token:csrf, jobs:[], entries:[]})); return;
  }
  try {
    const file = new URL('.'+url.pathname, www); assert.ok(file.href.startsWith(www.href));
    const data = await readFile(file);
    res.setHeader('Content-Type', url.pathname.endsWith('.js') ? 'text/javascript' : url.pathname.endsWith('.css') ? 'text/css' : url.pathname.endsWith('.svg') ? 'image/svg+xml' : url.pathname.endsWith('.png') ? 'image/png' : 'text/html');
    res.end(data);
  } catch {res.writeHead(404);res.end();}
});
await mkdir(out,{recursive:true});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin='http://127.0.0.1:'+server.address().port;
const browser=await chromium.launch({headless:true});
let cells=0;
try {
  for(const theme of ['dark','light']) for(const width of [1600,500]) {
    const context=await browser.newContext({viewport:{width,height:1000}});
    await context.addCookies([{name:'session_token',value:token,url:origin},{name:'sa02m_csrf',value:csrf,url:origin}]);
    const page=await context.newPage(),errors=[];
    page.on('pageerror', e=>errors.push(e.message));
    await page.goto(origin+'/index.html');
    await page.evaluate(theme=>{teardownStatusPolling();document.documentElement.setAttribute('data-theme',theme);switchTab('system');systemSelectSub('manage');sa02mI18n.setLang('ru');},theme);
    const input=page.locator('#cmd-input'), button=page.locator('[onclick="runCommandLine()"]');
    injected=null;
    await input.fill("python3 -c 'print(\"x\"*262144+\"FINAL-MARKER\")'");await button.click();
    await page.waitForFunction(()=>{const text=document.getElementById('cmd-output').textContent;return text.includes('FINAL-MARKER')&&text.includes('[output truncated to last 32768 bytes]');});
    assert.ok((await page.locator('#cmd-output').innerText()).includes('exit 0'));
    for(const lang of ['ru','en']) {
      await page.evaluate(lang=>sa02mI18n.setLang(lang),lang);
      injected={ok:true,rc:0,mode:'web',output:'parent done',truncated:false,capture_timed_out:true};
      await input.fill('printf parent');await button.click();
      const warning=lang==='ru'?'Сбор вывода остановлен по времени.':'Output collection timed out.';
      await page.waitForFunction(text=>document.getElementById('cmd-output').textContent.includes(text),warning);
      assert.ok((await page.locator('#cmd-output').innerText()).includes('exit 0'));
      injected={ok:false,error:'capture_failed'};
      await input.fill('printf failed');await button.click();
      await page.waitForFunction(text=>document.getElementById('cmd-output').textContent.includes(text),lang==='ru'?'Не удалось получить вывод команды.':'Could not capture command output.');
      assert.ok(!(await page.locator('#cmd-output').innerText()).includes('capture_failed'));
    }
    await page.screenshot({path:fileURLToPath(new URL(`${theme}-${width}.png`,out)),fullPage:true});
    injected={ok:true,rc:77,mode:'root',output:'root authentication failed\n',truncated:false,capture_timed_out:false};
    await page.evaluate(()=>executeCommandLine('printf test','root','synthetic'));
    await page.waitForFunction(()=>document.getElementById('cmd-output').textContent.includes('wrong root password'));
    assert.equal(await page.locator('#cmd-root-password').isVisible(),true);
    await page.evaluate(()=>{cmdAwaitPassword=false;cmdMode='web';updateCommandPasswordVisibility();});
    injected=null;await input.fill('printf recovered; exit 7');await button.click();
    await page.waitForFunction(()=>document.getElementById('cmd-output').textContent.includes('[mode web, exit 7]'));
    assert.ok((await page.locator('#cmd-output').innerText()).endsWith('recovered'));
    assert.deepEqual(errors,[]);await context.close();cells++;
  }
  console.log(`PASS console-bounds-browser: ${cells} cells; real CGI large output/exit7, RU/EN capture/auth failures and recovery`);
} finally {
  await browser.close();await new Promise(resolve=>server.close(resolve));await rm(sessions,{recursive:true,force:true});
}
