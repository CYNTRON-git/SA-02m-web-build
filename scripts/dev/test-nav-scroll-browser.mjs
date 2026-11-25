#!/usr/bin/env node
// comment-mutation-proof-exempt: real Chromium loads shipped CSS/nav, receives wheel/keyboard input and measures frame stability; no device services.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { mkdir } from 'node:fs/promises';

const { chromium } = createRequire(import.meta.url)('playwright');
const web = fileURLToPath(new URL('../../www/network_config/', import.meta.url));
const origin = 'http://sa02m-nav.test';
const html = `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/main.css"></head><body><div class="app">
<header class="topbar">Navigation regression</header><main class="main" tabindex="0">
<div style="height:1000px"><div id="nested" tabindex="0" style="position:sticky;top:0;width:180px;height:100px;overflow-y:auto">
<div style="height:500px">Nested scroll content</div></div>Scroll content</div><button id="action" class="btn">Action</button>
<input id="edit" aria-label="Edit" value=""><output id="count">0</output></main></div>
<script src="/static/js/app/nav.js"></script><script>initTopbarAutoHide();
document.querySelector('#action').onclick=()=>{document.querySelector('#count').textContent=String(Number(document.querySelector('#count').textContent)+1);};</script></body></html>`;
async function frames(page) {
  return page.evaluate(async () => {
    const rows = [];
    for (let i = 0; i < 45; i++) {
      await new Promise(requestAnimationFrame);
      const main = document.querySelector('.main'), top = document.querySelector('.topbar');
      rows.push({ hidden: document.querySelector('.app').classList.contains('topbar-hidden'),
        scroll: main.scrollTop, height: main.getBoundingClientRect().height,
        top: top.getBoundingClientRect().height });
    }
    return rows;
  });
}
function assertStable(rows, hidden, label) {
  assert.equal(rows.length, 45);
  assert(rows.every(row => row.hidden === hidden), label+' changed class without input');
  for (const key of ['scroll', 'height', 'top']) {
    assert(Math.max(...rows.map(row => row[key]))-Math.min(...rows.map(row => row[key])) <= 0.1,
      label+' geometry did not settle: '+key);
  }
}
const browser = await chromium.launch({ headless: true });
try {
  for (const viewport of [{ width:1280, height:720 }, { width:1024, height:600 },
    { width:812, height:375 }, { width:1280, height:900 }]) {
    const context = await browser.newContext({ viewport, hasTouch:true });
    try {
      await context.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.origin !== origin) return route.fulfill({ status:404, body:'' });
        if (url.pathname === '/') return route.fulfill({ contentType:'text/html', body:html });
        if (['/static/css/main.css', '/static/js/app/nav.js'].includes(url.pathname))
          return route.fulfill({ path:path.join(web, url.pathname) });
        return route.fulfill({ status:404, body:'' });
      });
      const page = await context.newPage(), errors = [];
      page.on('pageerror', error => errors.push(error.message));
      page.setDefaultTimeout(5000);
      await page.goto(origin);
      const main = page.locator('.main');
      const box = await main.boundingBox(); assert(box);
      await page.mouse.move(box.x+box.width/2, box.y+box.height/2);
      await page.mouse.wheel(0, 10000);
      await page.waitForTimeout(400); // shipped transition is250ms; keep it enabled
      const short = viewport.height <= 720;
      const down = await frames(page);
      assertStable(down, short, JSON.stringify(viewport)+' near-bottom');
      await page.mouse.wheel(0, -140);
      await page.waitForTimeout(400);
      assertStable(await frames(page), false, JSON.stringify(viewport)+' upward');
      // Ordinary input remains actionable with the real5000ms default.
      await page.locator('#action').click();
      assert.equal(await page.locator('#count').textContent(), '1');
      await page.locator('#edit').fill('keyboard');
      assert.equal(await page.locator('#edit').inputValue(), 'keyboard');
      if (process.env.SA02M_NAV_SCREENSHOTS) {
        await mkdir(process.env.SA02M_NAV_SCREENSHOTS, { recursive:true });
        await page.screenshot({ path:path.join(process.env.SA02M_NAV_SCREENSHOTS,
          `nav-${viewport.width}x${viewport.height}.png`), fullPage:true });
      }
      if (short) {
        await page.reload();
        await page.mouse.move(box.x+box.width/2, box.y+box.height/2);
        await page.mouse.wheel(0, 180);
        await page.waitForFunction(() => document.querySelector('.app').classList.contains('topbar-hidden'));
        // Reverse while the real 250ms transition is still changing geometry.
        await page.mouse.wheel(0, -90);
        await page.waitForTimeout(400);
        assertStable(await frames(page), false, JSON.stringify(viewport)+' immediate wheel reversal');
        await page.reload();
        await main.focus();
        await page.mouse.move(box.x+box.width/2, box.y+box.height/2);
        await page.mouse.wheel(0, 180);
        await page.waitForFunction(() => document.querySelector('.app').classList.contains('topbar-hidden'));
        await page.keyboard.press('ArrowUp');
        await page.waitForTimeout(400);
        assertStable(await frames(page), false, JSON.stringify(viewport)+' keyboard transition reversal');

        await page.reload();
        await page.mouse.move(box.x+box.width/2, box.y+box.height/2);
        await page.mouse.wheel(0, 180);
        await page.waitForFunction(() => document.querySelector('.app').classList.contains('topbar-hidden'));
        const cdp = await context.newCDPSession(page);
        const x = box.x+box.width/2, y = box.y+box.height/3;
        await cdp.send('Input.dispatchTouchEvent', { type:'touchStart', touchPoints:[{ x, y }] });
        await cdp.send('Input.dispatchTouchEvent', { type:'touchMove', touchPoints:[{ x, y:y+50 }] });
        await page.waitForTimeout(20);
        await cdp.send('Input.dispatchTouchEvent', { type:'touchMove', touchPoints:[{ x, y:y+90 }] });
        await page.waitForTimeout(20);
        await cdp.send('Input.dispatchTouchEvent', { type:'touchEnd', touchPoints:[] });
        await cdp.detach();
        await page.waitForTimeout(700);
        assertStable(await frames(page), false, JSON.stringify(viewport)+' touch transition reversal');

        await page.reload();
        await page.mouse.move(box.x+box.width/2, box.y+box.height/2);
        await page.mouse.wheel(0, 180);
        await page.waitForTimeout(400);
        const outerY = await main.evaluate(node => node.scrollTop);
        await page.locator('#nested').hover();
        await page.mouse.wheel(0, 80);
        await page.waitForTimeout(100);
        assert(await page.locator('#nested').evaluate(node => node.scrollTop) > 0, 'nested fixture did not scroll');
        assert.equal(await main.evaluate(node => node.scrollTop), outerY, 'nested input moved outer scrollport');
        assertStable(await frames(page), true, JSON.stringify(viewport)+' nested input ownership');

        await page.setViewportSize({ width:1280, height:900 });
        await page.waitForTimeout(400);
        assertStable(await frames(page), false, JSON.stringify(viewport)+' resize exit');
        await page.setViewportSize(viewport);
        await page.waitForTimeout(400);
        assertStable(await frames(page), false, JSON.stringify(viewport)+' resize reentry without gesture');
      }
      assert.deepEqual(errors, []);
      console.log('PASS nav geometry '+JSON.stringify(viewport)+' '+JSON.stringify(down.at(-1)));
    } finally { await context.close(); }
  }
} finally { await browser.close(); }
