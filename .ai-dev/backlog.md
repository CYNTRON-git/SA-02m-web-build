# Backlog — SA-02m web interface

Recorded findings and deferred work (`.ai-dev/procedures/backlog.md` owns the
format). One status per finding: `- [OPEN|RESOLVED] <date> <item>`. Resolved
entries are pruned (history lives in git); last prune 2026-07-17 (whole-project
audit).

## Open

- [OPEN] 2026-07-17 **[MED] Threat model blind to the cloud surface.**
  `docs/threat-model.md` (2026-07-14) predates 1.0.5.5's frpc tunnel
  (`cloud.cyntron.ru` proxying `:80` and `:9999`) and never mentions
  cloud/frpc — exactly the "strongest unmitigated gap" (§5: shared password +
  HTTP) the model itself names, now reachable from outside the LAN. Add a
  cloud-boundary section + abuse cases through the normal loop. Surfaced by
  the 2026-07-17 audit (F1).
- [OPEN] 2026-07-17 **[MED] Cloud surfaces have no contract entries.**
  `docs/contracts/` holds only `rs485-roster.md`; shipped `cloud.cgi`, the
  cloud pairing/heartbeat protocol, and the `frpc.toml` format are
  uncontracted. Close with the next cloud batch. Audit 2026-07-17 (F2).
- [OPEN] 2026-07-17 **[LOW-MED] Quality registry blind to Python.**
  ~10k+ lines of Python daemons (`opt/sa02m-flasher/`, the MQTT bridge,
  `opt/sa02m-cloud-agent/`) have no row in `.ai-dev/quality/tools.json`;
  existing pytest tests (`opt/sa02m-cloud-agent/tests/test_agent.py`) are
  never run by any gate. Add `py-syntax` + `pytest` rows. Audit 2026-07-17 (F3).
- [OPEN] 2026-07-17 **[LOW] Decompose worklist (module-size sweep).**
  `main.css` 3578 · `modbus_mqtt_bridge.py` **2774** (fastest grower:
  +436 in the 1.0.5.9–12 window; its new unit tests seed the behaviour
  net; clean up legacy `except Exception: pass` clusters during the
  split — lines ~358, 525, 1243, 1977–2009) · `status.cgi` 2494 ·
  `flash_protocol.py` 2347 · `mqtt.js` **2397** · `app/status.js` 1481.
  Start with the bridge or `mqtt.js`. `flasher.js` deliberately excluded
  (see F10 below). Audits 2026-07-17 (F6, evening F3).
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
