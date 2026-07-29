# Backlog — SA-02m web interface

Recorded findings and deferred work (`.ai-dev/procedures/backlog.md` owns the
format). One status per finding: `- [OPEN|RESOLVED] <date> <item>`. Resolved
entries are pruned (history lives in git); last prune 2026-07-17 (whole-project
audit).

## Open

- [OPEN] 2026-07-17 **[LOW] `test_agent.py` gated by no quality row.**
  `opt/sa02m-cloud-agent/tests/test_agent.py` (the send-only / no-command-channel
  / no-wireguard guards — the F1-removal net) is pytest-style, so the new
  `py-unit-cloud` row (`-p test_render*.py`, stdlib unittest) deliberately
  excludes it and pytest is not a device/CI dep. It never runs in CI. Convert it
  to `unittest`-style (or add a pytest CI dep) so the F1-removal guard is
  enforced, not just present. Surfaced by the 1.0.5.15 build + reviewer.
- [OPEN] 2026-07-17 **[MED→Phase-4, cross-repo] Cloud identity: fleet-shared FRP
  token → per-device mTLS.** v1 uses one shared `FRP_TOKEN` across the fleet
  (`CYNTRON-git/cloud` `config/frps.toml.template`); extract it from one device's
  `0600 /etc/sa02m-cloud/frpc.toml` → present as any device (subdomain authz
  limits squatting, but the transport secret is shared). Fix = per-device mTLS
  client certs issued at enrollment (cloud issues + device stores 0600, no
  hardware root of trust on the A40i). Co-owned with the cloud repo — not
  buildable device-side alone. Plan §STATUS O2 / `threat-model.md §6` identity.
- [OPEN] 2026-07-17 **[LOW→`[?]`] Cloud tunnel transport-TLS not enforced
  server-side (verify).** 1.0.5.15 pinned `transport.tls.enable = true` on the
  device (frpc); confirm the cloud frps side enforces/accepts TLS on the control
  leg (`CYNTRON-git/cloud` `config/frps.toml.template` has no explicit
  `transport.tls.force`). Cloud-repo verification item.
- [OPEN] 2026-07-17 **[LOW] Cloud contracts — residual.** 1.0.5.15 landed
  `docs/contracts/cloud-enrollment.md` (device mirror of the claim/enroll/
  heartbeat + frpc-profile seam). Residual: the `cloud.cgi` status JSON shape
  (frontend `#cloud-card` consumer) is still uncontracted — freeze its field
  names in a small contract entry on the next cloud UI touch. (Threat-model
  cloud section shipped 1.0.5.14; the audit-F1/F2 items are resolved.)
- [OPEN] 2026-07-17 **[LOW] Decompose worklist (module-size sweep).**
  `main.css` 4166 · `modbus_mqtt_bridge.py` **3179** (fastest grower again:
  +405 in the 1.0.5.46 window on top of +436 in 1.0.5.9–12; **first
  priority** — audit 2026-07-22 B7; the FMB event/insurance unit tests
  seed the behaviour net; clean up legacy `except Exception: pass`
  clusters during the split) · `status.cgi` 2507 · `mqtt.js` **2441** ·
  `flash_protocol.py` 2418 · `app/status.js` 1546. Start with the bridge.
  `flasher.js` deliberately excluded (see F10 below). Audits 2026-07-17
  (F6, evening F3), refreshed 2026-07-22.
- [OPEN] 2026-07-22 **[LOW] Bridge `PortCycleScheduler` loop untested.**
  The per-port scheduling loop (classic/event balancing, warmup gate, the
  A1 reconfigure-backoff *path selection*) has no direct unit coverage —
  the 1.0.5.46 tests pin backoff/insurance behaviour but not the loop
  itself. Seed for the bridge decompose above. Audit 2026-07-22 (A9).
- [OPEN] 2026-07-22 **[LOW, vendored→downstream-feedback] `run.mjs
  --touched` vacuous on this repo.** Registry `covers` are bare path
  prefixes but `coversToRegex` (`.ai-dev/quality/run.mjs:48`) anchors
  `^…$` with no prefix semantics — no touched file ever matches, every
  scoped row silently skips (false green); plus the `git status --short`
  fallback (:147) trims the whole output before `slice(3)`, mangling the
  first filename. Vendored framework file — do NOT patch locally (D8/D9
  precedent); route as downstream-feedback on the next protocol upgrade.
  Until then run the FULL beat, never `--touched`. Builder 1.0.5.46.
- [OPEN] 2026-07-22 **[LOW] HIG font-floor gate residual.** The ui-layout
  driver measures the 11px HIG font floor report-only; gating it is a
  one-line change + a seeded `FONT_WHITELIST` (mirrors the touch/contrast
  ledger discipline). Salvaged from the pruned `responsive-hig-rework`
  plan (shipped through 1.0.5.39).
- [OPEN] 2026-07-14 **[MED→product decision] TLS + single-credential exposure gap
  (from threat-discovery §5).** `docs/threat-model.md` names the strongest
  unmitigated threat: HTTP-without-TLS + one shared password + internet/VPN &
  shared-LAN exposure + root-capable CGI/flasher behind that single door.
  Measures that hold (allow-list input, auth-before-mutation, sudo pinning) don't
  close it. Candidate product features (Operator decision, NOT started): TLS out
  of the box, forced default-password change on first login, per-user accounts +
  access audit log, an explicit "VPN-only" policy in `docs/deployment.md`.
- [OPEN] 2026-07-14 **[LOW] MR-02m firmware trust chain unknown (threat §6).** Is
  the `.fw` cryptographically signed/validated, or format-only? Owner: the MR-02m
  repo. Confirm before treating flasher supply-chain (S3) as mitigated.
- [OPEN] 2026-07-14 **[LOW] CTL-list resolution phase still per-unit (perf, optional).**
  1.0.5.2 batched the `systemctl show`/`is-enabled` calls in `cmd_list` (~28→9
  forks, 3.9→3.0 s, byte-identical). The residual ~3 s is the unit-RESOLUTION phase
  (per-candidate `service_present`/`unit_exists` systemctl) — batchable too but the
  resolution logic (candidates/masked/init.d) is entangled; higher risk for ~1 s.
  Defer unless the full services path needs to be faster still.
- [OPEN] 2026-07-13 **[MED] eth0-hardcoding in web/net consumers (end-board gap).**
  After the installer's `02-network.sh` was made interface-name-aware, other
  consumers still hardcode `eth0.conf`/`eth1.conf` and won't work on an
  `end0`/`end1` board: `www/network_config/cgi-bin/config.cgi:63-64`,
  `apply.cgi:80-98` (web "network apply" writes `interfaces.d/eth0.conf`),
  `etc/fix-eth1-internet.sh:319`, `etc/systemd/sa02m-eth1-coldboot.service:6`.
  Route each through the same `first_existing_iface` detection. Surfaced by the
  installer-end0 fix reviewer (A2). No SSH-safety regression; a real gap for
  end-name boards.
- [OPEN] 2026-07-13 **[LOW] hw-backend-guard static session cookie.**
  `etc/sa02m-hw-backend-guard.sh:11,68-69` still probes `status.cgi` with the
  legacy static `session_token=cyntron_session` cookie instead of the
  per-session sha256 model. Benign today — it is a liveness check and
  `status.cgi` returns HTTP 200 (with an error body) so `curl -fsS` still
  succeeds; unchanged from `main`, NOT introduced by the 1.0.5.0 flasher-auth
  work. Align it to a dedicated internal probe (or the per-session model) so no
  static-token assumption lingers. Surfaced by the 1.0.5.0 ship reviewer.
- [OPEN] 2026-07-13 **[LOW] F10 — decompose the god-files.** `app.js` DONE:
  a UI characterization harness (`scripts/dev/`, headless Chromium over every
  tab×theme×variant; globals + new-errors + DOM-skeleton gate) captured a
  baseline, then app.js was peeled section-by-section into 7 plain global-scope
  scripts `static/js/app/*.js` (app.js is now the ~389-line core) — one file per
  commit, oracle PASS after each. `flasher.js` DEFERRED (follow-up): it is a
  single cohesive IIFE — 230 closure-private functions threaded through one
  shared mutable `state` (260 refs), only 5 `window.*` exports. Splitting it into
  plain global scripts (ES modules forbidden) would mean promoting `state` + the
  fn set to global scope and rewriting hundreds of internal references — a
  semantic rewrite, not a behaviour-preserving move, and it would recreate the
  god-object in global scope. Not worth the risk vs the [LOW] payoff; revisit
  only if the no-modules constraint is lifted. Still OPEN for app.js:
  on-device click-through before deploy (headless is necessary, not sufficient
  for a global-scope reorg).
- [OPEN] 2026-07-12 **[LOW] Y7-b — `set -u` in installer modules.** `set -o pipefail`
  landed; bare `set -u` deferred — an unset-var abort mid-provision could brick a
  fresh install. Add only with an on-device install run.
- [OPEN] 2026-07-12 **[LOW] D8/D9 — vendored ai-dev doc pointers.**
  `.ai-dev/quality/run.mjs` usage line + `.ai-dev/procedures/backlog.md` cite
  paths that don't exist in this repo. These are vendored framework files — do
  NOT edit locally (upstream drift); route as downstream-feedback on the next
  protocol upgrade.
- [OPEN] 2026-07-12 **[task] On-device verification (pre-deploy).** All device-
  side changes tested only locally/logically. Verify on a real SA-02m before
  deploy: login (hashed + legacy plaintext), password change, network apply +
  re-run `install.sh` preserves the static IP, cloud activation, storage
  autoformat off by default. A www-only OTA needs `/etc/sa02m_web.env` present
  (login now fails closed).
- [OPEN] 2026-07-29 **[LOW] F9 — over-broad "covers all record counts" wording
  (fmb event wire).** `test_no_configured_record_can_satisfy_the_wb_gate` builds
  its `(type, reg_high)` corpus from `all_event_ranges()` (types 0, 2, 3), so a
  legacy `FMB_EVT_REBOOT` (0x0F) record standing FIRST in a frame is outside it —
  while the surrounding sentence claims the general all-n case. Same wording at
  `docs/contracts/fmb-event-wire.md` (~:111, ~:133) and the test module docstring.
  The property itself was verified to HOLD (no wire code carries a 15-byte
  payload; 65 536 reboot-first legacy frames rejected by `_parse_events_wb`) — so
  this is a claim-precision fix, not a defect: narrow the sentence, or extend the
  corpus with the reboot type and keep the broad claim. Third instance of the same
  over-claim pattern in this feature; found by the round-3 reviewer, shipped
  knowingly in 1.0.5.50 on the Operator's "push" word.
