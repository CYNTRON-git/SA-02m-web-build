# Backlog — SA-02m web interface

Recorded findings and deferred work (`.ai-dev/procedures/backlog.md` owns the
format). One status per finding: `- [OPEN|RESOLVED] <date> <item>`. Resolved
entries are pruned (history lives in git); last prune 2026-08-17 (whole-project
audit 1.0.5.71).

## Open

- [OPEN] 2026-08-18 **[LOW] Two pre-existing `opt/sa02m-devices` test failures**
  (unrelated to any recent change; reproduced on `main` via worktree by the
  MR-widget Reviewer): `test_stand_storage_path` and `test_promote_copy_and_merge`.
  Investigate + fix or delete-if-obsolete. Would be caught earlier once the
  `py-unit-devices` gate row (below) exists.
- [OPEN] 2026-08-18 **[LOW] No quality-registry row covers `opt/sa02m-devices/tests`.**
  The devices-API tests (`test_stand_devices.py`, `test_devices_widgets.py`) are
  pytest/unittest and gate nothing — `build --touched` runs only `py-syntax`
  (compileall). The dtv/ce tests have the same gap; the MR-widget 1.0.5.83 tests
  (26, incl. the non-AI-skip + online=None cases) were run by hand. Fix: a
  `py-unit-devices` registry row (stdlib-unittest to match the repo's no-pytest
  convention, or add pytest). Surfaced by the MR-widget build.
- [OPEN] 2026-08-18 **[MED] Alice enrollment through the web UI likely fails as
  www-data (PermissionError writing into the root-only cert dir).**
  `scripts/06-alice.sh` makes `/var/lib/sa02m-alice` `0750 root:root`, yet
  `config/api.py` `start_link`/`complete_link` (writing `pending_claim.json` and
  the issued PEMs there) run as **www-data** inside `sa02m_alice_api.cgi` (no sudo
  on that path) → on-device enrollment via the card probably surfaces as
  `enrollment_failed`/`alice_api_failed`. The bench device evidently got its certs
  another way. Surfaced by the 1.0.5.80 cert-status Builder; deliberately NOT
  changed there (out of scope). Fix direction: route the enrollment writes through
  the privileged trigger (`sa02m-alice-web-trigger.sh` sudoers) or a root helper —
  never chmod the cert dir open. Verify on 1.136 by attempting a link from the UI.
- [OPEN] 2026-08-18 **[LOW] Bridge module deploy list lives in 3 manually-synced
  homes with only 1 gate (audit 2026-08-18 LOW-2).** `scripts/05-mqtt.sh:137-138`
  and `scripts/update-www-only.sh:378-379` each list the `bridge_*.py` modules to
  install; `opt/sa02m-modbus-mqtt/tests/test_entry_surface.py:54` freezes the
  import SET (asserted `:135`) but nothing enforces the two shell deploy lists
  match it. A future module added to the test + one script only would ship a board
  missing a module with no gate firing. Fix: a quality-registry check that both
  shell lists equal the frozen import set.
- [OPEN] 2026-08-18 **[LOW] Decompose worklist refresh (audit 2026-08-18 LOW-3).**
  Via the `decompose` side-tool, by cohesion: `www/network_config/static/js/mqtt.js`
  (2764 lines) — split device-add modal/per-family builders · the `type:template`
  picker (`_templateCatalog`/`fillTemplateSelect`/`refreshTemplateCatalog`) · MQTT
  broker/credential settings · config model. `opt/sa02m-modbus-mqtt/bridge_mqtt.py`
  (507) — extract the `/meta` blob cluster (`_num_or_str`/`_obj_or_str`/
  `_control_meta_blob`/`_device_meta_blob`/the two `_publish_*_meta_blob`) →
  `bridge_meta.py` (self-contained, covered by `test_meta_blob.py`).
  `etc/sa02m-web-service-ctl.sh` (1518, standing — not this cycle's growth).
- [OPEN] 2026-08-18 **[LOW→follow-up] Configurable serial parity/stopbits for
  `type:template` (8N2 support).** The honesty/doc half of audit-2026-08-18 MED-1
  is DONE in **1.0.5.78** (corrected the wrong "9600 8N2" comment → 8N1-only;
  documented in `docs/contracts/template-device.md §8` + `templates/README.md` +
  the YAML example). RESIDUAL: `bridge_serial.py:218-219,445` still hard-codes
  8N1 (`parity=NONE, stopbits=ONE`), no config field — a WB device on 9600 **8N2**
  cannot be polled. Follow-up: add YAML parity/stopbits and thread them through
  `get_port`/`_ensure_open`. (Same as the "serial 8N2" deferred item.)

- [RESOLVED-superseded 2026-08-18] **[LOW] Decompose worklist — oversized/incohesive
  files (audit 1.0.5.71 F2).** Superseded by the 2026-08-18 decompose worklist
  above (current line counts + the meta-blob/template growth). Split by cohesion via the
  `decompose` side-tool: `www/network_config/static/js/flasher.js` (~5130 lines)
  · `www/network_config/static/css/main.css` (~5249) ·
  `www/network_config/cgi-bin/status.cgi` (~2508, Bash on the status-poll hot
  path) · `www/network_config/static/js/mqtt.js` (~2700) ·
  `www/network_config/static/js/app/status.js` (~2449) ·
  `opt/sa02m-flasher/.../flash_protocol.py` (~2517). Advisory; refresh of the
  earlier module-size sweeps. Source: audit 1.0.5.71 F2.
- [OPEN] 2026-08-17 **[LOW] Cloud deploy/enrollment path absent from
  `docs/deployment.md` (audit 1.0.5.71 F4).** The cloud enrollment/deploy flow
  is undocumented in the deployment runbook, already flagged `[?]` in
  `docs/threat-model.md §7`; document it there. Source: audit 1.0.5.71 F4.
- [OPEN] 2026-08-06 **[MED] Nothing cross-checks that a canonical `zImage` and
  the module tree it is paired with are the same build — and the panel writes
  that `zImage` to the boot partition.** `zimage_ok()`
  (`etc/sa02m-kernel-select.sh:139-145`) validates **existence and size only**
  (`ZIMAGE_MIN`..`ZIMAGE_MAX` = 5–12 MB); it never reads the image's identity or
  version. `cmd_set` gates the switch on `zimage_ok "$CANON_DIR/zImage.$target"`
  **and** `modules_ok "$mod_ver"`, but those two checks are independent: any
  5–12 MB file named `zImage.rt` satisfies the first, whatever kernel it is, and
  `/lib/modules/6.1.0-rc6-rt4` satisfies the second regardless of what
  `zImage.rt` contains. The FAT write itself is careful (atomic temp→rename, the
  active slot never left truncated) — the gap is **identity, not atomicity**.

  **Blast radius:** a mismatched pair boots a kernel whose modules are absent →
  no network, no panel. Recovery is an SD card or a serial console, **not the
  web UI** — the same recovery path `tools/buildroot/README.md` already
  documents for the failed RT deploy of 2026-06-23.

  **Bounded, not theoretical:** a board that has never run RT has no
  `zImage.rt` and still gets a clean `zimage_missing` refusal, so this needs a
  seeded-or-deployed artifact to bite. The seeding paths are `cmd_init` (copies
  the *running* FAT zImage into the canonical slot for the running profile) and
  `tools/buildroot/sa02m-kernel-deploy.sh install-rt|install-smp`, which takes
  the zImage and the modules tarball as two **independent** arguments — nothing
  checks they came from the same build.

  **Predates the 2026-08-06 kernel-line change** and is not caused by it; that
  change is what makes the RT switch reachable on a conf-less 6.1 board, which
  is why it surfaced here. Fix direction (not chosen): record the built version
  alongside each canonical zImage at deploy/seed time and compare it against
  `mod_ver` in `cmd_set`/`cmd_refresh` before the FAT write. Found by the
  Reviewer of the kernel-port deletion.
- [OPEN] 2026-08-06 **[MED] No executable test covers `sa02m-kernel-select.sh`'s
  self-heal, which is exactly where the kernel-version bug lived.** The
  2026-08-06 fix is pinned only by `kernel-policy-contract` pin 8, which is a
  **static** consistency check: the defaults must name the contract's kernel
  pair and the detector's `case` arm must match both. It does not execute
  `load_conf()`, so the actual failing behaviour — an unmatched default surviving
  into `/etc/sa02m_kernel.conf` via `write_conf()`, after which the panel refuses
  a profile the board has — has no regression test. The Reviewer exercised the
  state machine against nine synthetic `/lib/modules` fixtures by hand; that
  evidence is not in the repo and does not re-run.

  **The mould already exists:** `scripts/dev/test-iface-canonical-gate.sh` and
  its siblings (`test-iface-migration.sh`, `test-iface-gw-repair.sh`,
  `test-nodered-ctl.sh`) — each a `scripts/dev/test-*.sh` harness with a
  `build`-beat row in `.ai-dev/quality/tools.json` and a `covers` entry naming
  the script it guards. Cost note for whoever picks this up: the script hardcodes
  `/lib/modules` and `/etc/sa02m_kernel.conf` as absolute paths, so it needs a
  small injectable seam (e.g. `${SA02M_MODULES_DIR:-/lib/modules}`) before a
  harness can drive it — that is a device-code change, which is why this is its
  own change and not a one-liner. Deliberately **not** built into the deletion
  change.
- [OPEN] 2026-08-06 **[MED] Stale 5.10.35 assumptions survive in live code after
  the 6.1 migration.** Narrowed 2026-08-06: the two sites this entry named —
  `etc/sa02m-iface-canonical.sh:190` and `install.sh:142-145` — are **fixed**;
  both were stale *text*, and in both the *logic* was already version-independent
  (the altname guard reads `ip -d link show`; the Docker mode greps
  `/boot/config-$(uname -r)` for capabilities). Neither predicate was touched.
  **Still open — sites found by the wider sweep, not yet traced:**
  `scripts/01-system.sh:93` (USB gadget configfs), `scripts/02-network.sh:288-289`
  (nftables availability), `scripts/lib.sh:55` (eth naming default),
  `tools/debian-rootfs/create-sa02m-rootfs.sh:146` (nftables masking), and
  `docs/contracts/ethernet-iface-naming.md:210-222` (udev-247 evidence, likely
  genuine history rather than a stale premise). Each asserts a 5.10 kernel
  capability as a premise; each needs its logic traced before the comment is
  rewritten, which is why they were left out of the deletion change. Root fact:
  `.ai-dev/notes/kernel-line.md`.
- [OPEN] 2026-08-06 **[LOW] `run.test.mjs` does not pin the regex-escaping in
  `coversToRegex`.** Dropping the escape at `.ai-dev/quality/run.mjs:64` leaves
  all 15 table assertions passing while behaviour really changes — `install.sh`
  would then also match `installXsh`, because the `.` stops being literal.
  Fail-safe in both directions (an unescaped metachar only widens the match, and
  widening only makes MORE rows run), so it is low severity — but it is a hole
  in a suite whose whole job is to pin this function. One table row closes it.
  Found by the reviewer as a fourth mutation during the backlog sweep, after the
  builder's own three were confirmed.

- [OPEN] 2026-08-06 **[LOW] «Время с RTC» in the panel now reads 3 hours behind
  «Текущее время», and both are unlabelled.** `index.html:429-438` shows system
  time (local) directly above chip time (UTC, since `fbeac21`). On MSK the RTC
  row is correct but looks like a defect and will generate support reports —
  this may well be what the original 3-hour report was actually seeing. Fixing
  it is a label or a conversion in `status.js`/`forms.js` plus an i18n DICT
  entry plus `index.html`: a `www/` change with its own `?v=`/`APP_VERSION`
  bump flow, so it is a separate decision, not a slip-in. Surfaced by the
  builder during the rtc-utc-convention work.
- [OPEN] 2026-08-06 **[LOW] `/etc/adjtime` is an inherited default this repo
  never writes.** A repo-wide sweep of `etc/ scripts/ tools/ opt/ www/
  install.sh` finds zero writes to `/etc/adjtime` and no
  `timedatectl set-local-rtc`. The UTC convention every RTC path now hard-codes
  therefore rests on a base-image default we neither create nor control — one
  `timedatectl set-local-rtc 1`, a different Armbian build, or a restored
  backup flips it. The new `rtc-utc-convention` gate pins our side; owning the
  file (writing it at install time) would close the other half.
- [OPEN] 2026-08-06 **[LOW] `lib_rtc.sh:198,282` weekday fallback mixes
  frames.** `wday=$(date -d "$dt" +%w) || wday=$(date +%w)` — the fallback
  computes a LOCAL weekday from a UTC timestamp, so it can be off by one near
  midnight. Only reachable when `date -d` is unavailable (busybox), and the
  DS3231 day-of-week register is never read back by our read path or the kernel
  driver. Cosmetic.
- [OPEN] 2026-08-06 **[LOW] `docs/threat-model.md` has no entry for port 1880.**
  The offline Node-RED work moves a Node-RED footprint onto air-gapped and
  imaged boards, and it composes with the model's existing "no device-side port
  allow-list" note for the cloud tunnel. Plan `nodered-offline.md` §11 S5
  committed to filing this. Reviewer M-B. **Deferred by the 2026-08-06 backlog
  sweep, deliberately:** a 1880 row is threat *reasoning*, not a mechanical edit
  — Node-RED ships without `adminAuth` and it composes with the already-partial
  cloud-tunnel allow-list gap — so it belongs to the `threat-discovery`
  side-tool, not a text sweep.
- [OPEN] 2026-08-06 **[LOW] Three nits in the nodered-offline change.**
  **(a) and (c) fixed 2026-08-06; (b) is the only residual.**
  (a) DONE — `etc/sa02m-web-service-ctl.sh:942` now says a healthy non-en/ru
  board IS judged wrongly, one-directionally (false failure, never false
  success). (c) DONE — `scripts/dev/test-nodered-ctl.sh` gained a de/ja fixture
  pinning exactly that (strings verbatim from node-red 4.1.13
  `@node-red/runtime/locales/{de,ja}/runtime.json`); it is non-vacuous — adding
  the de spelling to the ctl matcher makes the suite exit 1.
  (b) OPEN — `scripts/07-nodered.sh:177,294` still hardcode
  `/usr/lib/node_modules` where the ctl now weighs all global roots — fail-closed
  and install-time only, so narrowing rather than a defect.
  Reviewer L-A/L-B/L-C.
- [OPEN] 2026-08-06 **[LOW] `etc/sa02m-web-service-ctl.sh` is now 1414 lines.**
  +174 in the nodered work. The reviewer names a decompose seam at `:782-1247`
  (the Node-RED block). Candidate for the `decompose` side-tool on the next
  module-size sweep.
- [OPEN] 2026-08-06 **[LOW] Panel shows a raw error code for a refused
  cross-major Node-RED upgrade.** `svcCtlErrorMessage` in
  `www/network_config/static/js/app/services.js` falls back to `map[c] || c`, so
  an operator sees the literal `major_upgrade_refused` instead of a sentence.
  The human explanation exists only in the install log and the runbook. One line
  to fix; `www/` was outside the nodered-offline fence. Note the OTA path
  (`etc/sa02m-web-update-apply.sh:84`) does not ship the ctl script, so the gap
  is narrower than it looks.

- [OPEN] 2026-08-06 **[LOW] `netmask 24` (bare prefix in the netmask field) is
  refused as unparseable by `ensure_gw_dns`.** ifupdown accepts a bare prefix
  there; `scripts/02-network.sh` parses only dotted-quad in `netmask` and CIDR
  in `address`. Fails CLOSED with an accurate WARN, so a board is never
  endangered — the repair simply does not happen on such a conf. The mirror of
  the CIDR-in-`address` gap already closed. Reviewer N4 on the net-and-log
  hygiene change.
- [OPEN] 2026-08-05 **[MED — accepted 2026-08-06: KEEP, not to be pruned] Default device password
  hard-coded in the imaging fallback.** `tools/imaging/make-image.sh:20` defaults
  `SSH_PASS` to `cyntron` when `SA02M_PASS` is unset, so a build host without the
  key silently authenticates with a well-known credential. Untouched by the
  boot.scr format fix (surfaced by its review, out of that diff's scope).
  **Re-rated [LOW] → [MED] on 2026-08-06** (sweep reviewer, advisory A6): it is
  a literal default credential in a committed artifact — the one swept item
  carrying a hard security floor. **Operator decided KEEP on 2026-08-06**: the build-host
  fallback is relied upon, and dropping it would break any host without
  `SA02M_PASS` set. Kept OPEN deliberately — the code still ships the default,
  so a RESOLVED status would let the next prune erase a live accepted risk.
  Revisit if imaging ever runs where the build host is not trusted. The remedy
  then is one line (drop the
  default, require an explicit `SA02M_PASS`) but it breaks any build host
  relying on the fallback — which is why it was escalated rather than swept, and
  it has now been decided (above). Code
  deliberately unchanged.
- [OPEN] 2026-08-06 **[LOW] Bulk Russian code comments in shell scripts, against
  invariant 5.** `PROTOCOL.md` invariant 5 puts code comments on the
  machine-facing axis — always English; `docLanguage: ru` reaches only `docs/`
  and `README.md`. Measured over tracked non-`www/` `*.sh` (116 files, whole-line
  comments, shebangs excluded): **1273 Cyrillic comment lines of 3464**. Worst:
  `scripts/01-system.sh` 159/198, `scripts/02-network.sh` 115/221,
  `etc/fix-eth.sh` and its `firstboot-overlay` twin 71/81 each,
  `scripts/07-nodered.sh` 62/70. Pre-existing debt, not introduced by any recent
  change. Surfaced during the 2026-08-06 backlog sweep, whose own two new blocks
  were written in English after the reviewer's ruling — the rest is untouched
  because translating it is a different change. Same shape as the nine-`./…`
  entry below: mechanically checkable, so a gate could pin it (new/changed
  comment lines must be Latin) without a boil-the-ocean translation.

- [OPEN] 2026-08-06 **[LOW] Nine more scripts documented with an x-bit-dependent
  `./` invocation.** Same class as the x-bit-dependent rootfs-builder
  invocation, quantified while fixing it: `etc/storage-mount.sh`, `scripts/03-webserver.sh`,
  `scripts/update-www-only.sh`, `tools/debian-rootfs/prepare-sa02m-flash-usb.sh`,
  and under `tools/imaging/`: `cleanup-donor.sh`, `flash-receiver.sh`,
  `make-image.sh`, `prepare-flash-media.sh`, `restore-donor-ssh.sh` — all
  `100644` in git yet documented as `./…` in markdown. (The counter-example this
  entry cited, `tools/kernel-wb/*` at 100755, was deleted 2026-08-06 with the
  dead kernel pipeline. The only `100755` files left under `.sh` are
  `etc/sa02m-web-root-cmd.sh` and `etc/sa02m-web-update-check.sh`, neither of
  which is documented with a `./` invocation — so the gate below no longer needs
  a whitelist.)
  Left out of the backlog sweep deliberately: it is a ten-file doc edit across
  unrelated runbooks, better done as one scoped change with a gate pinning it
  (mode-vs-invocation is mechanically checkable).
- [OPEN] 2026-08-05 **[LOW] `compress-bin.sh` patch step swallows failures.**
  `tools/imaging/compress-bin.sh:177-210` defines a watchdog-only `patch_image()`
  that returns 0 on failure. Verified during the boot.scr review NOT to carry the
  boot.scr fail-open class (it never generated `boot.scr`), but the same
  swallow-the-error style remains.
- [OPEN] 2026-08-05 **[LOW] `watchdog-cap` gate has no meta-test and a loose
  non-vacuity floor.** **Floor half fixed 2026-08-06; the meta-test half is the
  residual, and the original premise needed correcting.** The sweep floor is now
  DERIVED as well as counted: every enumerated writer that actually sets one of
  the keys must also be REACHED by the sweep, so a dropped/renamed root fails by
  name. Correction to the premise: "dropping one sweep root still passes" is
  **not** reproducible on today's tree — only `etc` and `tools` contribute swept
  files, and pin 8's `WD_SYSFS_MIN_SITES=2` incidentally covers both, so a drop
  today fails with pin 8's *misleading* message ("the writers moved") rather
  than silently. What was really wrong is that coupling: lower
  `WD_SYSFS_MIN_SITES` (the feeder unit is a delete candidate — it is masked and
  disabled on a correctly-installed device) and the hole becomes real. Demonstrated: with `etc` dropped
  AND `WD_SYSFS_MIN_SITES=1` the old gate exits 0, the new one exits 1 naming
  `etc/systemd/sa02m-watchdog.conf`. Still OPEN: the widened
  value regex can absorb a following English word on a prose line (fail-closed
  cry-wolf, reachable via the swept `tools/system-hardening/README.md`), and the
  gate has no meta-test — matching its two sibling gates
  (`iface-naming-contract`, `kernel-policy-contract`), so this is a shared shape,
  not a regression. Reviewer advisories A5–A7 on the watchdog-cap change.
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
- [OPEN] 2026-08-06 **[LOW] `sa02m-failure-monitor.sh` probe cannot fail.**
  `etc/sa02m-failure-monitor.sh:18,127` probes `status.cgi?part=priority` with
  `curl -fsS` and no cookie. Same class as the guards above, but this one feeds
  a **monitoring/alerting verdict**, so fixing it changes a signal rather than
  a gate: it needs a decision about intent first — "is the CGI stack alive?"
  (for which an `unauthorized` body IS legitimate evidence, and today's
  behaviour is correct) versus "is the dashboard producing data?" (which needs
  a body assertion on real fields). Deliberately NOT folded into the port-lease
  change. Surfaced by the 2026-08-06 port-lease work.
- [OPEN] 2026-08-06 **[LOW] distinct `flasher_unknown` refusal code.** The
  port-lease probe now fails CLOSED when the daemon is running but unreadable,
  reusing the existing `flasher_busy` error code — so a wedged
  `sa02m-flasher.service` greys out the MPLC4 / MQTT-мост «Пуск» buttons under
  the message «идёт прошивка или сканирование RS-485», which is then
  inaccurate. The accurate fix needs a distinct code plus its `i18n.js` DICT
  entry, i.e. a `www/` touch and the `?v=`/`APP_VERSION` flow — fenced out of
  the port-lease change by scope. Land it with the next `www/` touch. Operator
  workaround meanwhile: stopping `sa02m-flasher.service` removes the socket,
  which reads as "not busy". Reason is logged to `/var/log/sa02m_install.log`.
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
