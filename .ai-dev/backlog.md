# Backlog — SA-02m web interface

Recorded findings and deferred work (`.ai-dev/procedures/backlog.md` owns the
format). One status per finding: `- [OPEN|RESOLVED] <date> <item>`. Resolved
entries are pruned (history lives in git); last prune 2026-08-05 (whole-project
audit).

## Open

- [OPEN] 2026-08-05 **[LOW] Default device password hard-coded in the imaging
  fallback.** `tools/imaging/make-image.sh:20` defaults `SSH_PASS` to `cyntron`
  when `SA02M_PASS` is unset, so a build host without the key silently
  authenticates with a well-known credential. Untouched by the boot.scr format
  fix (surfaced by its review, out of that diff's scope); decide whether to drop
  the default and require an explicit `SA02M_PASS`.
- [OPEN] 2026-08-05 **[LOW] x-bit-dependent invocation documented for the rootfs
  builder.** `tools/debian-rootfs/README.md:31` documents
  `sudo ./tools/debian-rootfs/create-sa02m-rootfs.sh`, which fails on a fresh
  clone because tracked `*.sh` are `100644` (110 of 115). Same class as the
  blocking finding fixed in the boot.scr format work, on a script that change
  does not touch — invoke via `bash` there too.
- [OPEN] 2026-08-05 **[LOW] `compress-bin.sh` patch step swallows failures.**
  `tools/imaging/compress-bin.sh:177-210` defines a watchdog-only `patch_image()`
  that returns 0 on failure. Verified during the boot.scr review NOT to carry the
  boot.scr fail-open class (it never generated `boot.scr`), but the same
  swallow-the-error style remains.
- [OPEN] 2026-08-05 **[LOW] Stale bootargs comments after the payload default
  change.** `kernel-port/overlay/arch/arm/boot/dts/sun8i-r40-sa02m.dts:77` and
  `scripts/01-system.sh:112` both describe bootargs as carrying `console=tty1`
  "generated from etc/boot.cmd.sa02m" — no longer true of the default payload
  (now `etc/boot.cmd.sa02m.min`). Outside the format fix's scope.
- [OPEN] 2026-08-05 **[MED] Direct sysfs writes of a watchdog timeout above the
  16s cap — same defect class, different mechanism.**
  `etc/sa02m-watchdog-feed.sh:45-46` and `tools/imaging/ssh-flash-safe.sh:150` do
  `echo 30 > /sys/class/watchdog/watchdog0/timeout` — 30s against a 16s-capped
  sun4i-wdt, swallowed by `|| true`, and the feeder's own comment acknowledges
  the 16s Allwinner default. Not a systemd directive, so the `watchdog-cap` gate
  does not see it — a named limit of that guard, not an oversight. Mitigating
  context: the feeder is the very unit the `sa02m-watchdog.conf` policy masks and
  disables, so this is likely dead on a correctly-installed device — verify that
  before deciding fix-vs-delete. Surfaced by the builder during the watchdog-cap
  change; deliberately left out of that diff (plan §14 scope).
- [OPEN] 2026-08-05 **[LOW] `watchdog-cap` gate has no meta-test and a loose
  non-vacuity floor.** Its sweep non-vacuity check is a fixed `>=10` against a
  live count of 15, so dropping one sweep root still passes. Also the widened
  value regex can absorb a following English word on a prose line (fail-closed
  cry-wolf, reachable via the swept `tools/system-hardening/README.md`), and the
  gate has no meta-test — matching its two sibling gates
  (`iface-naming-contract`, `kernel-policy-contract`), so this is a shared shape,
  not a regression. Reviewer advisories A5–A7 on the watchdog-cap change.
- [OPEN] 2026-08-05 **[MED] `covers` directory-prefix entries match nothing under
  `--touched`.** `.ai-dev/quality/run.mjs` `coversToRegex("etc/")` compiles to
  `/^etc\/$/`, so a bare directory prefix matches the directory string itself and
  never a file inside it — `fileMatchesCovers(["etc/systemd/x.conf"], ["etc/"])`
  is `false`. Repo-wide: `etc/`, `scripts/`, `tools/imaging/`,
  `www/network_config/static/js/`, `opt/sa02m-flasher/` all behave this way, so
  the Builder's `--touched` subset silently under-runs (observed: 2 rows instead
  of the 4 the touched set implied). `"tools/imaging/**"` matches correctly. Ship
  is unaffected — the full `build`/`review` beats run everything. Fix needs a
  `run.test.mjs` case pinning both the prefix and `**` forms. Found by the
  builder + reviewer during the watchdog-cap work.
- [OPEN] 2026-08-05 **[LOW] `autorun.sh` / `autorun-fel.sh` twin drift is
  unpinned.** The two files are byte-identical today and every change so far has
  carried the same hunk to both, but nothing enforces it — unlike the
  overlay↔policy-home `cmp` in `watchdog-cap.sh` pin 5. A non-watchdog divergence
  between the USB and FEL flashing paths would pass silently. Pre-existing shape;
  fix = one `cmp` pin, or a shared sourced fragment if the two must legitimately
  diverge. Surfaced by the watchdog-cap reviewer (A2).
- [OPEN] 2026-08-05 **[LOW] `tools/imaging/ssh-flash-safe.sh` writes
  `system.conf` directly instead of the drop-in.** It is the only watchdog writer
  patching `/etc/systemd/system.conf` itself; every other path *comments out*
  that file's `RuntimeWatchdogSec` precisely so `system.conf.d/` wins. So its
  value can be silently overridden by any drop-in, or override nothing.
  Deliberately left alone in the watchdog-cap change (value unified to 15s + an
  explanatory comment); restructuring it to the drop-in convention is the real
  fix. Plan `watchdog-cap.md` §6.4.
- [OPEN] 2026-08-05 **[LOW] `firstboot-overlay` watchdog conf has no packer
  step.** `tools/imaging/firstboot-overlay/etc/systemd/system.conf.d/sa02m-watchdog.conf`
  is a shipped media artifact kept byte-identical to `etc/systemd/sa02m-watchdog.conf`
  by a `cmp` pin, not generated from it. True dedup = a packer step copying from
  `etc/` at image-build time; deferred because it changes the imaging flow. Plan
  `watchdog-cap.md` §11.3 option B.
- [OPEN] 2026-07-28 **[LOW] `read_iface_conf` reports `enabled:false` for a DHCP
  interface.** `config.cgi:51` sets `enabled=true` only for `inet static`, so a
  DHCP-configured Ethernet shows the toggle off with empty fields. Pre-existing,
  unrelated to the 1.0.5.49 naming work (F8) — recorded so it is not attributed
  to that diff.
- [OPEN] 2026-07-28 **[LOW] KLogic coexistence — deliberately deferred pieces.**
  From `docs/contracts/ethernet-iface-naming.md §5.5`: the panel does not
  *repair* a stripped vendor hook (writing vendor code into a root-executed conf
  from a web form is a refused capability — manual procedure is in the contract);
  KLogic's `set-route` does `route del default` and can fight the panel's
  `gateway` and `inet-failover.sh` metrics; the live panel↔KLogic write race is
  inherent to two writers and is not closed from our side; VLAN sub-interfaces
  (`eth0.1`/`eth0.2` in KLogic's table) are neither created nor migrated.
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
  Refreshed by audit 2026-08-05: `flasher.js` **4428** (see F10 below —
  deferred, cohesive IIFE) · `main.css` **4424** · `modbus_mqtt_bridge.py`
  **3422** (fastest grower; **first priority** — an Operator-started decompose
  session is already running on it, coordinate; the FMB event/insurance unit
  tests seed the behaviour net; clean up legacy `except Exception: pass`
  clusters during the split) · `status.cgi` **2507** · `mqtt.js` **2441** ·
  `flash_protocol.py` **2418** · `app/status.js` **1553**. Start with the
  bridge. Audits 2026-07-17 (F6, evening F3), refreshed 2026-07-22 and
  2026-08-05.
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
- [OPEN] 2026-08-05 **[LOW] `cmd_exec.cgi` uncontracted.** The 1.0.5.64 command
  line (`www/network_config/cgi-bin/cmd_exec.cgi`) is a request/response CGI
  surface with no `docs/contracts/` entry; today it is UI-only (no external
  client) and its security posture is homed in `docs/threat-model.md §S2/§4`,
  so this is optional — freeze its POST fields (`cmd`/`mode`/`root_password`)
  and JSON shape (`ok`/`rc`/`output`/`mode`/`truncated`) in a small contract
  entry on the next command-line touch. Audit 2026-08-05 (LOW-7).
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
