---
name: sa02m-web-architecture
description: Use BEFORE editing app.js polling, status.cgi parts, adding widgets/endpoints, or for symptoms — widget stuck at «—», data stops after failures, UI element wiped after a poll — SA-02m web stack internals: nginx+fcgiwrap+Bash CGI backend, app.js rolling status scheduler (priority/main/rs485/background parts, warmup cache, pause-on-failure), tab/subsystem map (MQTT, flasher, gateway, kernel), i18n runtime, auth flow.
---

# SA-02m web architecture

One home for the runtime architecture. Domain rules: `docs/agent-rules/sa02m-domain.md`
(product identity, version discipline). This skill is the deep dive.

## Request path

```
browser ── static ──▶ nginx ──▶ /var/www/network_config (index.html, static/*)
        ── /cgi-bin/*.cgi ──▶ nginx ▶ fcgiwrap ▶ bash CGI ▶ device scripts //proc
        ── flasher ops ──▶ CGI proxy / daemon HTTP (sa02m-flasher.service, Python)
```

- Auth: `login.cgi` sets `session_token` cookie; `app.js` top guard redirects
  to `/login.html` when absent; CGI side validates via `lib_web_auth.sh`.
  Creds: `/etc/sa02m_web.env` (`web_creds.cgi` changes them).
- nginx `fastcgi_read_timeout` is raised (120 s) only for `services_ctrl.cgi`;
  every other endpoint must answer fast — long work returns `pending` and
  completes in the background (poll `?result=1`).

## app.js status scheduler (the heart)

- Parts: `priority` (CPU/temp/RAM/swap/disk — 6 s), `main` (6 s), `rs485`
  (12 s), and BACKGROUND_STATUS_PARTS = storage,time,uptime,network,load,
  system,services,hardware — rolling with phase shift, gated by
  `sa02m_status_blocks.conf` (`isStatusBlockEnabled`). `part=main` is real on
  both ends (`status.cgi` build_main_json / app.js fetchStatusMain).
- Single queue: `scheduleStatusFetch(part, delay, runner)` + `pumpStatusFetchQueue`
  — heavy parts serialize; NEVER add a bare `setInterval(fetch)` beside it.
- Failure handling: `noteStatusFailure` pauses a failing part
  (`statusPauseUntil`), 3 rs485 failures pause all background parts 25 s.
  A "widget stopped updating" symptom is often a paused part, not a dead CGI.
- Warmup: priority values replay from sessionStorage on load
  (`hydratePriorityWarmup` → `applyPriorityStatus(cachedData)`) — new
  priority-widget markup must render correctly from a replayed partial object.
- Apply-function map: `applyPriorityStatus` (cpu-bar/temp-bar/ram/disk),
  `applyNetworkStatus` (eth pills + traffic + topbar IP), `applyStorageStatus`
  (USB storage↔modem swap incl. `setUsbWidgetIcon`), `applyServicesStatus` →
  `renderServicesDynamic`, `applyHardwareStatus` (DO/beeper/LED/USB-power +
  variant visibility), `applyRs485Status` → `renderRs485`.

## Renderer-owned DOM (do not decorate inside)

`renderServicesDynamic` (`#svc-dynamic-list`), `renderRs485` (`#rs485-grid`),
USB widget title (`#usb-widget-title` textContent rewritten on every storage
poll). Static decorations (icon chips etc.) live OUTSIDE these nodes — id on
the inner text span, icon as a sibling (the 1.0.4.1 USB-icon lesson).

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

`i18n.js`: DICT maps exact Russian strings → English; MutationObserver
translates visible text; dynamic strings use `uiT('...')` with the SAME exact
key. Adding UI text = add DICT entry; changing RU text = change the DICT key
too, or the translation silently stops matching.
