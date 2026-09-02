---
name: sa02m-web-architecture
description: Use BEFORE editing app.js polling, status.cgi parts, adding widgets/endpoints, or for symptoms — widget stuck at «—», data stops after failures, UI element wiped after a poll — SA-02m web stack internals: nginx+fcgiwrap+Bash CGI backend, app.js rolling status scheduler (priority/main/rs485/background parts, warmup cache, pause-on-failure), tab/subsystem map (MQTT, flasher, gateway, kernel), i18n runtime, auth flow.
---

# SA-02m web architecture

Deep dive: the request path and the executable detail of the status scheduler.
The architectural FACTS (parts and cadences, pause-on-failure, warmup cache,
renderer-owned DOM, tab map) have one home — `docs/agent-rules/sa02m-domain.md`,
`@`-imported into every session. Read it first; this skill adds only what a
grep needs.

## Request path

```
browser ── static ──▶ nginx ──▶ /var/www/network_config (index.html, static/*)
        ── /cgi-bin/*.cgi ──▶ nginx ▶ fcgiwrap ▶ bash CGI ▶ device scripts //proc
        ── /api/flasher/* ──▶ sa02m-flasher.service (Python daemon)
        ── /api/devices*  ──▶ sa02m-devices-api :8765 (Python daemon)
```

- Auth: `login.cgi` sets `session_token` cookie; `app.js` top guard redirects
  to `/login.html` when absent; CGI side validates via `lib_web_auth.sh`.
  Creds: `/etc/sa02m_web.env` (`web_creds.cgi` changes them).
- nginx `fastcgi_read_timeout` is raised (120 s) only for `services_ctrl.cgi`;
  every other endpoint must answer fast — long work returns `pending` and
  completes in the background (poll `?result=1`).

## Scheduler — the symbols to grep

Home of the file: `static/js/app/status.js` (moved there by the F10 split).

- Queue: `scheduleStatusFetch(part, delay, runner)` + `pumpStatusFetchQueue`
  — heavy parts serialize; NEVER add a bare `setInterval(fetch)` beside it.
- Part gating: `BACKGROUND_STATUS_PARTS`, `isStatusBlockEnabled`
  (`sa02m_status_blocks.conf`); `part=main` is real on both ends
  (`status.cgi` build_main_json ↔ `fetchStatusMain`).
- Failure pause: `noteStatusFailure` → `statusPauseUntil`. A "widget stopped
  updating" symptom is often a paused part, not a dead CGI.
- Warmup replay: `writePriorityWarmupPart` → `hydratePriorityWarmup` →
  `applyPriorityStatus(cachedData)`.
- Apply-function map: `applyPriorityStatus` (cpu-bar/temp-bar/ram/disk),
  `applyNetworkStatus` (eth pills + traffic + topbar IP), `applyStorageStatus`
  (USB storage↔modem swap incl. `setUsbWidgetIcon`), `applyServicesStatus` →
  `renderServicesDynamic`, `applyHardwareStatus` (DO/beeper/LED/USB-power +
  variant visibility), `applyRs485Status` → `renderRs485`.
- Renderer-owned containers (`#svc-dynamic-list`, `#rs485-grid`,
  `#usb-widget-title`): the rule and its lesson live in the domain doc — this
  list is only so a grep finds the ids.

## Tabs → subsystems

The tab → frontend / backend / device map has ONE home:
`docs/agent-rules/sa02m-domain.md ## Subsystems behind the tabs` — cite it,
never restate (this skill's former second copy merged there, 2026-08-26).

## HW variants

`applyVariantVisibility(variant)` toggles `[data-hide-for~=variant]` elements;
`sa02m-1eth` (5 COM, 1 eth) vs `sa02m-2eth` (4 COM, 2 eth). Every dashboard /
network change is checked against both (the second Ethernet widget and eth1
form exist only on 2eth).

## i18n runtime

Mechanism (`DICT` + MutationObserver + `uiT()`): `docs/agent-rules/sa02m-domain.md`.
The reviewer floor (every visible string has its DICT entry):
`docs/agent-rules/web-code-rigor.md ## Frontend floors`. The trap worth
repeating for a grep: changing a Russian string changes the DICT **key** —
translation stops matching silently.
