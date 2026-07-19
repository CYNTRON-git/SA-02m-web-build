# Web diagnostic tools — catalog and selection rule

Project-local quick catalog for every agent: how to run, render, and verify
this web UI WITHOUT a device, plus the on-repo helper scripts. Adapted from
the MR-02m `hw-diagnostic-tools.md` (same role: read this BEFORE writing any
ad-hoc verification script). POINTERS ONLY — each tool's full doc stays in its
one home. Machine-facing: English. Loaded via `CLAUDE.md` `@`-import.

## Standing rules

1. **Verify visibly, not by assertion.** Any UI-visible change is verified by
   the headless render recipe below (screenshots of the affected states) —
   "the CSS looks right" is not verification.
2. **No ad-hoc frameworks.** The stack below (python http.server + Playwright
   + CGI stubs) is the standing harness; extend it, don't reinvent it.
3. **State coverage** — verify the states the change touches: dark AND light
   theme; `up/down/absent` link; warn thresholds (CPU ≥80, t ≥80 °C, RAM/disk
   ≥90); USB storage vs modem; narrow viewport (~500 px); both HW variants
   where `data-hide-for` is involved.
4. Scratch scripts live in the session scratchpad, never committed; a recipe
   worth keeping graduates into `scripts/` + a row here.

## Headless UI verification (the standing recipe)

No backend is needed — stub CGI and drive the `apply*` functions directly:

```js
// Playwright, chromium at /opt/pw-browsers/chromium (pre-installed in CCR)
// 1. serve statics:  cd www/network_config && python3 -m http.server 8901
// 2. auth guard:     add cookie session_token=test for http://127.0.0.1:8901
// 3. stub backend:   page.route('**/cgi-bin/**', r => r.fulfill({status:200,
//                      contentType:'application/json', body:'{}'}))
// 4. inject states:  page.evaluate(() => applyPriorityStatus({cpu_usage:87,
//                      temp_c:83.5, ...}))  — same for applyNetworkStatus,
//                      applyServicesStatus, applyRs485Status, applyStorageStatus
// 5. screenshot:     page.screenshot() / locator('.widget').screenshot()
// Light theme: document.documentElement.setAttribute('data-theme','light')
// High-DPI icon check: newPage({ deviceScaleFactor: 4 })
```

Red 404 toasts in these screenshots are the stubbed backend, not a UI bug —
say so when relaying screenshots to the Operator.

### Geometry / layout driver (просмотр вёрстки)

To ASSERT layout (not just eyeball it) and to get full-page screenshots per
viewport for a human to review the вёрстка, use the geometry driver
`.ai-dev/quality/checks/ui-layout.mjs` (quality row `ui-layout`, review beat):
`npm run ui-layout:install` once, then `npm run ui-layout`. It renders the
dashboard + Управление services block across phone/tablet/desktop × both themes
× both HW variants and measures real `getBoundingClientRect` geometry — services
column alignment, no horizontal overflow (gated at supported widths), touch-target
sizes vs a printed deviation ledger, no card overlap. Screenshots land in the
gitignored `.ai-dev/quality/screenshots/`. Chromium-only, fixed viewports,
geometry-not-visual-regression — limits documented in the driver's file header.

## Contrast (color work)

WCAG 2.1 relative-luminance math (what gradients.app / convertico compute):
`ratio = (L1+0.05)/(L2+0.05)`. Keep/extend the session script pattern
(`contrast.js` — pairs table → ratio + AA/AAA grade) and paste the resulting
ratios into the plan/review. Floors: `web-code-rigor.md ## CSS / UI floors`.

## Quality gates

| Need | Command | Trap / note |
|---|---|---|
| Builder green check | `node .ai-dev/quality/run.mjs build` (`--touched` for the diff subset) | rows: js-syntax, bash-cgi-syntax, version-consistency |
| Reviewer beat | `node .ai-dev/quality/run.mjs review` | install.sh syntax |
| Registry (what "green" means) | `.ai-dev/quality/tools.json` | add a tool = add a row, never edit the runner |

## Version / release helpers

| Need | Command | Trap / note |
|---|---|---|
| Sync version from branch | `python3 scripts/sync-app-version.py` | branch name must be `X.Y.Z[.W]`; updates VERSION + APP_VERSION + every `?v=` |
| Check only | `python3 scripts/sync-app-version.py --check` | exit 1 on skew — quality row |
| Deploy www only to a device | `scripts/update-www-only.sh` (see script header) | device deploys are per `docs/` runbooks — never improvised (invariant 4) |

## Backend probes (against a real device)

| Need | Command | Trap / note |
|---|---|---|
| Status part | `curl -s 'http://<ip>/cgi-bin/status.cgi?part=priority'` | needs session cookie for protected endpoints: login via `login.cgi` first |
| CGI syntax before deploy | `bash -n www/network_config/cgi-bin/<x>.cgi` | quality row runs all |
| Flasher daemon state | `curl -s http://<ip>:<flasher-port>/status` | port lease semantics: `sa02m-domain.md ## Subsystems` |

## Symptom → tool (quick dispatch)

| Symptom | First move |
|---|---|
| Widget stuck at `—` / not updating | trace recipe `web-workflow.md ## «Значение —»` — code first, not logs |
| UI broken after deploy, works locally | cache: check `?v=` version skew (`sync-app-version.py --check`) |
| Page fully blank | `node --check` every bundle (one syntax error bricks all) |
| Colors unreadable / theme complaint | contrast script + both-theme screenshots |
| Button does nothing | id contract grep + browser console via Playwright `page.on('console')` |
| CGI returns 500 | `bash -n` the endpoint, then run it locally with stubbed env vars |
