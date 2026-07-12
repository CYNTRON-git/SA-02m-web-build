# SA-02m web interface — project-specific domain rules

Project-local domain knowledge for the SA-02m automation-server web interface.
Loaded into the orchestrator session via `CLAUDE.md` `@`-import. Machine-facing:
English (`PROTOCOL.md` invariant 5); the Operator conversation is Russian per
`docLanguage: ru`.

---

## Product identity

- **SA-02m / SA-02m-2** — automation servers by CYNTRON on the A40i-2eth SoM
  (Allwinner A40i, Armbian Linux ARM). SA-02m: 1×Ethernet, 5×RS-485 (COM1–COM5),
  DO/beeper/alarm-LED via I2C expander; SA-02m-2: 2×Ethernet, 4×RS-485.
  Variant is auto-detected by physical Ethernet count; config home:
  `/etc/sa02m_hw_variant.conf` (`sa02m-1eth` | `sa02m-2eth`). The web UI hides
  variant-specific widgets via `data-hide-for` attributes driven by
  `applyVariantVisibility()`.
- **This repo IS the deployed filesystem overlay**: `www/` (web root),
  `etc/` (system scripts, systemd units, nginx config), `opt/`, `usr/`,
  `scripts/` (dev/build helpers), `install.sh` (on-device installer),
  `firmware/`, `tools/` (imaging, rootfs). A path under `etc/` or `www/` maps
  1:1 onto the device's `/etc`, `/var/www` (per `install.sh`).

## Stack — hard facts

- **No build step.** The frontend is hand-written vanilla JS/CSS served as-is:
  `www/network_config/index.html` + `static/js/{app,i18n,flasher,mqtt,gateway}.js`
  + `static/css/main.css`. There is no bundler, no npm at runtime, no framework.
  A syntax error in any bundle bricks the whole page — `node --check` is the
  build gate (quality registry row `js-syntax`).
- **Backend = Bash CGI** under `www/network_config/cgi-bin/*.cgi` behind
  nginx + fcgiwrap. Shared helpers live in `cgi-bin/lib_*.sh`. Every endpoint
  prints headers itself; JSON is assembled by hand — quoting/escaping bugs are
  the top defect class (`web-code-rigor.md`).
- **Auth**: cookie `session_token`; guard at the top of `app.js` redirects to
  `/login.html`; CGI side checks via `lib_web_auth.sh` / `auth_check.cgi`.
  Credentials home: `/etc/sa02m_web.env`.
- **i18n**: Russian is the source language in markup; `i18n.js` translates
  visible text to English at runtime via its `DICT` (Russian string → English)
  plus a MutationObserver. EVERY new user-visible Russian string needs a DICT
  entry; dynamic JS strings go through `uiT()`.

## Dashboard status polling — the architecture to respect

`app.js` polls `status.cgi?part=<name>` on a rolling scheduler — parts:
`priority` (CPU/temp/RAM/disk, 6 s), `main`, `rs485` (12 s), and the
background set `storage,time,uptime,network,load,system,services,hardware`
(phase-shifted). Facts that bite:

- Each part has its own `apply*Status()` function; UI ids are contract between
  `index.html` and `app.js` — renaming an id in one place is a finding.
- `renderServicesDynamic`, `renderRs485` fully rebuild their DOM; anything
  hand-inserted inside those containers is wiped on the next poll. Same trap:
  `usb-widget-title` textContent is rewritten by `applyStorageStatus`/`applyUsbModem`
  — decorations must live OUTSIDE the rewritten node (learned 1.0.4.1: icon chip
  wiped by title update).
- Priority-part warmup cache in `sessionStorage` (`writePriorityWarmupPart`)
  replays the last values on load — a markup change to those widgets must stay
  compatible with a replayed `applyPriorityStatus(d)`.
- Heavy parts pause on failures (`statusPauseUntil`); do not add unconditional
  fetch loops outside `scheduleStatusFetch`.

## Subsystems behind the tabs

| Tab | Frontend | Backend | Device side |
|---|---|---|---|
| Сведения (dashboard) | `app.js` apply*/render* | `status.cgi` parts | `/proc`, `sa02m-web-service-ctl.sh list` |
| Сеть / Время | `app.js` forms | `config.cgi`, `apply.cgi` | ifupdown/netplan per installer |
| MQTT | `mqtt.js` | `mqtt_*.cgi` | mosquitto (1883 local / 1884 external+auth), `sa02m-modbus-mqtt` bridge |
| Устройства RS-485 (flasher) | `flasher.js` | `flasher` daemon HTTP + CGI | `sa02m-flasher.service` (Python), MR-02m/DTV/CE-02m-3 modules |
| Шлюз RS-485 | `gateway.js` | `gateway_*.cgi` | `sa02m-gateway.yaml` (Modbus TCP / RTU-over-TCP / transparent) |
| Управление | `app.js` | `services_ctrl.cgi`, `kernel_ctrl.cgi`, `cpu_profile.cgi`, `web_update_*.cgi`, `web_creds.cgi` | systemd + SysV (mplc4, codesys), `sa02m-kernel-select.sh` (RT/SMP zImage swap), `sa02m-cpu-profile.sh` |

Port-sharing invariant: RS-485 lines are shared between MPLC4 polling, the
MQTT bridge, and the flasher — the flasher takes a **port lease**
(stop/restore units via `MPLC_STOP_SERVICES`); `flasher_busy` gates MPLC/MQTT
start buttons. Any change to who opens a COM port must respect that lease
protocol (CHANGELOG 1.0.3.35 documents the prior regressions).

## Version discipline (the contract)

- **git branch name == web version** (e.g. `1.0.4.1`). New release = new branch
  `+1` from the latest version branch.
- Three version homes MUST agree: `www/network_config/VERSION`, `APP_VERSION`
  in `app.js`, every `?v=` cache-bust in `index.html`/`login.html`.
  `python3 scripts/sync-app-version.py` syncs all from the branch name;
  `--check` is the gate (quality row `version-consistency`).
- `CHANGELOG.md` gets a `## <version> - <summary> (<month>)` section per
  release branch, Russian, grouped by subsystem.
- Devices self-update via «Обновление веб» (semver compare, `web_update_*.cgi`)
  — shipping a version string lower than deployed hides the update.

## The canonical documents to open first

1. `README.md` — install, HW variants, component map (Russian, Operator-facing).
2. `CHANGELOG.md` — per-version history; grep it FIRST for any regression
   ("when did X break" is usually answered here).
3. `docs/` — deployment and integration notes (`vendor-integrations.md`,
   MPLC/CODESYS docs at repo root).

## Family projects

- **MR-02m** (`CYNTRON-git/MR-02m`) — the RS-485 I/O module firmware this web
  UI flashes and configures; its Modbus contract and flasher `.fw` format are
  owned THERE (`docs/agent-rules/mp02m-domain.md` in that repo), never here.
  Web-side rendering of MR-02m channels must follow the module's register map,
  not invent one.
- The ai-dev protocol and this ruleset's structure are shared with MR-02m —
  when improving one project's rules, consider whether the sibling needs the
  same fix (announce, don't silently diverge).
