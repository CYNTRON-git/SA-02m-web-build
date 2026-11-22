#!/usr/bin/env node
// comment-mutation-proof-exempt: real Chromium loads shipped CSS/nav, receives wheel/keyboard input and measures frame stability; no device services.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const { chromium } = createRequire(import.meta.url)('playwright');
const web = fileURLToPath(new URL('../../www/network_config/', import.meta.url));
const origin = 'http://sa02m-nav.test';
const html = `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/main.css"></head><body><div class="app">
<header class="topbar">Navigation regression</header><main class="main">
<div style="height:1000px">Scroll content</div><button id="action" class="btn">Action</button>
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
    const context = await browser.newContext({ viewport });
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
      assert.deepEqual(errors, []);
      console.log('PASS nav geometry '+JSON.stringify(viewport)+' '+JSON.stringify(down.at(-1)));
    } finally { await context.close(); }
  }
} finally { await browser.close(); }
