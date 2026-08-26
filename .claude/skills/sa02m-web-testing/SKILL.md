---
name: sa02m-web-testing
description: Use when verifying any UI or CGI change, reproducing a reported visual bug, or setting up screenshots for the Operator — verify SA-02m web changes without a device: headless Chromium/Playwright harness (static server, session-cookie auth bypass, CGI stubs, apply*-function state injection), state matrix (themes, variants, thresholds, link/modem states), quality gates, live-device curl probes.
---

# SA-02m web testing

Standing rules and the symptom dispatch table:
`docs/agent-rules/web-diagnostic-tools.md`. This skill is the executable detail.

## Harness (no backend needed)

```bash
cd www/network_config && python3 -m http.server 8901 &   # static server
```

```js
const { chromium } = require('playwright');               // npm i playwright --no-save (scratchpad)
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.on('pageerror', e => console.log('PAGE ERROR:', e.message));   // ALWAYS wire this
await page.context().addCookies([{ name: 'session_token', value: 'test',
  url: 'http://127.0.0.1:8901' }]);                        // auth guard bypass
await page.route('**/cgi-bin/**', r => r.fulfill({ status: 200,
  contentType: 'application/json', body: '{}' }));         // stub ALL CGI
await page.goto('http://127.0.0.1:8901/index.html');
```

Inject exactly the states under test via the real apply functions:

```js
await page.evaluate(() => applyPriorityStatus({ cpu_usage: 87, temp_c: 83.5,
  ram_used_kb: 412000, ram_total_kb: 1024000, ram_free_kb: 612000, ram_pct: 40,
  swap_total_kb: 0, disk_used_kb: 2400000, disk_total_kb: 7300000,
  disk_free_kb: 4900000, disk_pct: 33 }));
// applyNetworkStatus({eth0_operstate:'up'|'down'|'absent', ...})
// applyStorageStatus({usb_mounted:1,...} | {usb_modem_present:1, usb_modem_state:'up',...})
// applyServicesStatus({svc_*_installed:1, svc_*:'active', ..._uptime_s:N})
// applyRs485Status({rs485:[{n:1, dev:'/dev/COM1', st:'ok', open:1, tx:N, rx:N, fe:0,pe:0,oe:0},...]})
// switchTab('system'|'mqtt'|...)
```

Screenshots: whole page, `locator('.widget', {hasText:'…'})` for one card,
`deviceScaleFactor: 4` page for icon legibility.

## State matrix (cover what the change touches)

| Axis | States |
|---|---|
| Theme | dark (default) / `document.documentElement.setAttribute('data-theme','light')` |
| Variant | `applyVariantVisibility('sa02m-1eth'/'sa02m-2eth')` |
| Thresholds | normal vs warn (CPU 87, temp 83.5, RAM 91, disk 91) |
| Link | eth up / down / absent (pill wide state «Нет линка» is the layout worst case) |
| USB | storage mounted / not mounted / modem present |
| Viewport | 1600 px and ~500 px (KPI/grid reflow) |

## Known harness artifacts (say so to the Operator)

- Red toasts «Ошибка сервера (404)» = the stubbed CGI, not a UI bug.
- Empty «Система» card = no system part mocked.

## Gates & probes

- `node .ai-dev/quality/run.mjs build` — js-syntax, bash-cgi-syntax,
  version-consistency; `review` — install.sh. Green before handing back.
- Live device: `curl -s 'http://<ip>/cgi-bin/status.cgi?part=priority'`
  (login first for protected endpoints); flasher daemon `GET /status`.
- CGI locally: `bash -n` first; then run with stubbed env
  (`REQUEST_METHOD=GET QUERY_STRING=... ./x.cgi`) when logic allows.
