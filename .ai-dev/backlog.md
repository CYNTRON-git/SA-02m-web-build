# Backlog — SA-02m web interface

Recorded findings and deferred work (`.ai-dev/procedures/backlog.md` owns the
format). One status per finding: `- [OPEN|RESOLVED] <date> <item>`. Resolved
entries are pruned (history lives in git); last prune 2026-08-28 (1.0.6.24 P6 —
shipped Alice items and a duplicated deploy finding removed, the decompose
worklist collapsed into one home).

## Open

- [OPEN] 2026-08-27 **[MED] The action path writes the commanded value into our own
  state cache, so a failed command is indistinguishable from a successful one.**
  `device_registry.apply_actions` does `self._mqtt_cache[topic] = payload` as it builds
  the publish, so a later `query` echoes what we ASKED for, not what the device did.
  This is why the 2026-08-27 "control does not work" investigation took four steps: the
  cloud, the app and our own status all reported success while the bus read 0. Predates
  1.0.6.19. **Do NOT fix this by gating on "the publish left"** — in that incident the
  publish DID leave and the module reverted afterwards, so that version still reports
  success. The fix is to let the cache follow the reading that comes back from the bus.
  Known trade to design for: a slow device would briefly report the previous value after
  a successful command, so the optimistic write exists for a reason and its removal
  needs a real pass (how long to wait, what to report meanwhile) rather than a patch.
  Direction agreed with the cloud side (2026-09-02): while a command is pending the device
  reports the PREVIOUS bus value, never the commanded one, in `query`/`device_state`; the cloud's
  three-condition confirm (`live_ts` newer than the command AND live value == target AND current
  value == target) then works without lying, and a module that reverts simply never confirms.
- [OPEN] 2026-08-27 **[MED, NOT OURS — route to the MR-02m project] The 16DO module at
  COM4 addr=11 drops its own output seconds after it is set.** Bench 1.135, verified
  from both sides: the command reaches the bus, the bridge writes it
  (`writeback DO1=1`, 33-43 ms), the output really does go to 1 — and then returns to 0
  with nothing re-commanding it (no second writeback; the bridge only publishes what it
  reads). **The identical module at addr=14 holds** under the identical command, and the
  board's local I²C output holds too, so it is that module, not the bus/bridge/Alice/
  cloud. Hold time measured twice with different sampling: ours (2 s interval) showed
  the drop within 2-4 s; the cloud session's (1 s interval) showed a hold of 8 s then a
  drop between t+8 and t+10 — a ~10 s round number looks like a configured comms/
  safe-state watchdog. Next step is a register comparison between addr=11 and addr=14
  in the MR-02m firmware project; do not guess at the register map from this repo.

- [OPEN] 2026-08-27 **[MED] `ssh-flash-safe.sh` resets neither cloud nor Alice identity
  (cloud-parity gap).** It loop-mounts a freshly written rootfs (`:119-179`) — the same
  moment `patch-firstboot-image.sh` uses to clear enrollment — but performs no identity
  reset at all. So a board flashed through THAT path inherits whatever the source image
  carried, bypassing both the cloud twin's fix (2026-07-31) and the Alice one (1.0.6.20).
  Deliberately left outside 1.0.6.20's fence (different script, different acceptance);
  named by that build. Fix direction: call the same two wipes at the existing mount, or
  state in the script header why the path is exempt.
- [OPEN] 2026-08-27 **[LOW] `cleanup-donor.sh:140` DENY bypass pattern.** The
  `--purge-update-state` branch carries a `case` arm `/etc/sa02m-update/trusted-keys|/*)`
  whose `|/*` alternative matches ANY absolute path, so the arm is far wider than its
  apparent intent. It currently only *returns 1* (protects), so the effect is
  conservative — but the pattern is almost certainly a typo for a second literal, and a
  future edit that flips the branch's polarity would turn it into a hole. Found by the
  1.0.6.20 build while reading the DENY model.

- [OPEN] 2026-08-27 **[LOW] A binding created outside the UI can silently lose a unit
  conversion.** The UI attaches `scale` from `ALICE_KINDS` (tvoc ×1000 mg/m³→µg/m³,
  pressure ×7.50062 kPa→mmHg), but `validate_device` accepts a float property with no
  `scale` at all, so a hand-made API call — or an older document — sends a value three
  orders out and the Alice app renders «0 мкг/м³». Hit for real on 2026-08-27: a test
  binding made via curl without `scale` read 0.27 instead of 270; the cloud session
  spotted it in the app. The UI path was correct throughout — this is about the API
  path having no guard. Fix direction (needs thought, not a reflex): the backend cannot
  simply inject a default, because a topic already publishing µg/m³ would then be
  scaled wrongly — so either warn when a known-conversion instance arrives without a
  scale, or carry the source unit explicitly. Do not silently coerce.
- [OPEN] 2026-08-27 **[MED] Telemetry self-evicts in a ~1 Hz reconnect loop.** Broker log
  on 1.135 (cloud session, measured): `Client sa02m-SA-02m-telemetry already connected,
  closing old connection` repeating at ~1 Hz for 2.5 hours, journal alternating
  `MQTT connected` / `MQTT disconnected: Unspecified error`, 31 s of CPU burned — from a
  SINGLE process holding one ESTAB socket. Cause: `sa02m_telemetry.py`'s reconnect path
  builds a NEW paho client with the same FIXED client id (`<device_id>-telemetry`)
  without stopping or disconnecting the old one, so each CONNECT evicts its predecessor
  and the survivor's disconnect drives the next reconnect — self-sustaining once
  triggered. A clean `systemctl stop` + `start` breaks it. MQTT is effectively down for
  the duration, which is worse than the CPU. Deliberately NOT folded into 1.0.6.22: that
  branch's production code is byte-stable after three review rounds and the id fix should
  not wait. Fix shape: stop/disconnect the previous client before building a new one (and
  check whether a new client is needed at all — paho reconnects on its own). Acceptance:
  restart mosquitto, watch exactly one clean reconnect in the journal and no eviction line
  in the broker log.

- [OPEN] 2026-08-27 **[LOW] A contract sentence is hostage to an unpinned JS line.**
  `docs/contracts/alice-mqtt-mapping.md` states that a `complete_link` over an already
  connected client is unreachable from the panel; that is true only because
  `www/network_config/static/js/app/alice.js` computes `linked` as
  `st === 'connected' || certOk` and renders «Завершить привязку» in a later branch.
  Nothing asserts it — edit that line and the contract silently becomes false. Same
  seam class the 1.0.6.19 gate pins on the helper↔client side, left unpinned on the
  JS↔contract side. Fix direction: a small assertion in the headless driver (the
  linked-state card offers «Отвязать», never «Завершить привязку»).

- [OPEN] 2026-08-27 **[MED] `run.mjs --touched` is blind to uncommitted work on a
  branch that already has a commit — a false-green shape.** It resolves the touched set
  from `origin/main..HEAD` and only falls back to the unstaged working tree when that
  diff comes up EMPTY. A Builder handing back uncommitted work on a branch carrying at
  least the version-bump commit therefore gets a subset scoped to the committed diff
  only: on 1.0.6.19 it selected 4 rows and saw none of the new alice work. Found by the
  1.0.6.19 Builder (which relied on the full beat throughout, so nothing shipped
  unchecked). Fix direction: union the committed diff with the working-tree diff, or
  make the fallback additive rather than conditional.

- [OPEN] 2026-08-27 **[MED] Alice client never resubscribes MQTT after a broker
  reconnect.** Subscriptions are taken once, right after `mqtt.connect()`
  (`client/main.py`, the connect path); `loop_start()` reconnects the socket but
  paho does NOT restore subscriptions — the documented idiom is to subscribe
  inside `on_connect` precisely so they are renewed. A mosquitto restart
  therefore leaves the client Socket.IO-connected and MQTT-deaf: values freeze
  at their last cached reading and `query` keeps serving them as current, until
  the SIO session happens to drop and the outer loop rebuilds everything.
  **Honesty label: derived from paho's documented reconnect/`on_connect`
  behaviour and this code's structure — NOT reproduced on hardware.**
  Acceptance: `systemctl restart mosquitto` on bench 1.135, then watch state
  keep flowing. Fix shape: subscribe inside an `on_connect` handler, arming
  `RetainedGrace` for ALL topics first — the 1.0.6.19 grace mechanism
  generalises to it directly (~10 lines). Deliberately NOT folded into
  1.0.6.19: a different guarantee ("survive a broker restart") with a different
  acceptance test, and it would re-time the connect settle window that shipped
  in 1.0.6.16. Fork 2 of plan `alice-registry-reload.md`.
- [OPEN] 2026-08-27 **[MED → cross-repo, `CYNTRON-git/cloud`] Overlapping
  controller sessions across a restart: the hub's `sn → sid` map is overwritten
  silently.** Bench 1.135, 2026-08-27 09:31–09:34: connect 09:31:47 → unprompted
  disconnect 09:32:03 (16 s into a healthy session) while `/v1.0/ping` answered
  200 from the same board (RTT ~270 ms, 0 % loss). The cloud session then found
  the hub sets `sn → sid` unconditionally on connect, so a second session for
  the same serial overwrites the first and the old socket's later disconnect is
  a no-op there — their log shows a connect and a disconnect for the same SN
  1 ms apart. Most probable reading: OUR old client process had not fully
  released its socket when the new one connected (two overlapping sessions from
  the device side), not a server timeout — so there is nothing to patch on their
  side and no timeout was changed. Evidence channel now exists: since 1.0.6.19
  every ended session logs `sid`, both timestamps, the monotonic duration and
  the disconnect source (`local_shutdown` / `lib:<reason>` / `unknown` — never a
  guess). Next step is to read those lines after a restart on 1.135 and decide
  whether the client must close the old session before opening a new one.
- [OPEN] 2026-08-27 **[LOW → cross-repo, `CYNTRON-git/cloud`] No
  controller→gateway "devices changed" event exists**, so a new binding reaches
  Alice only when the user runs «Обновить список устройств». A push would be a
  gateway-side call to the skill's discovery callback, which needs the SKILL's
  credentials — the controller does not have them and must not. Needs a new C→G
  event first. Owner: the `cloud` repo. Fork of plan
  `alice-registry-reload.md` §5.4.
- [OPEN] 2026-08-27 **[MED] Client restart after a binding mutation costs a
  ~60 s window in which the account shows ZERO devices.** Cloud-side hardware
  verification on the linked 1.135 (cloud session, 2026-08-27): after a
  mutation the auto-restart reconnected, the gateway logged connect→disconnect
  within 2 ms, the board logged `One or more namespaces failed to connect`, and
  the next successful connect came ~60 s later; `/v1.0/user/devices` returned an
  empty list for the whole account during that window — an Alice discovery
  landing there shows the user an empty house. Self-heals, so not a blocker.
  Fix directions: reload the DeviceRegistry in place (SIGHUP / config-watch)
  instead of restarting the unit, or make the socket.io reconnect prompt after
  a restart (backoff//jitter review in `client/sio_connection.py`).
  **Both directions built in 1.0.6.19** (in-place reload + a bounded jittered
  reconnect ladder); the entry stays OPEN until the bench acceptance on 1.135
  confirms an edit leaves the session up — it is a hardware-verified finding
  and closing it on code alone would over-claim.
- [OPEN] 2026-08-27 **[LOW] Device-name validator rejects ordinary punctuation
  and the error says nothing useful.** `models.py` `^[\w \-./+]{1,64}$` excludes
  parentheses, commas, «№» — «Проверка облака (изменено)» fails with a bare
  `invalid device name`. **There is no upstream rule to adopt** (cloud session
  checked the source, 2026-08-27): Yandex's Discovery reference documents `name`
  only as required — no length cap, no charset; their support page gives only
  semantic advice (unique within a room, no room name embedded). The gateway
  forwards the payload verbatim, and Cyrillic + spaces + digits are verified
  end-to-end. So this regex is OUR constraint: keep a validator (control
  characters + a length cap are genuinely useful), widen the printable set,
  state the rule in the field help AND the error text, and pin a test with a
  punctuation-bearing name so a future narrowing is caught.
  Cloud-side note (2026-09-02, its `docs/yandex-smart-home-rules.md` §4.1-4.3): the API sets no
  limit; the «Дом с Алисой» web INPUT field is stricter (25-char counter, «без пунктуации и
  спецсимволов»); so the field help also says: a name meant to survive auto-add in the app is
  best kept punctuation-free and ≤ 25 chars — that limit lives in Yandex's UI, not its API.

- [OPEN] 2026-08-26 **[HIGH] B2 honesty gap: the committed default password `cyntron`
  is a threat-model omission (audit H1).** The constant lives in `install.sh:28`,
  `create-sa02m-rootfs.sh:35`, `serial-restore-ssh.py:24`, `sa02m-check-perms.py:22`,
  `pack-factory-defaults.py:228`; `docs/threat-model.md` names password-change as the
  mitigation but never states the default is public. Amend the threat model (the
  forced-change product feature is the separate 2026-07-14 entry).
- [OPEN] 2026-08-26 **[MED] Threat model has no Alice section (audit M1).** The
  1.0.6.14/15 surface — `/var/lib/sa02m-alice` www-data 0700 (device private key written
  by the CGI), `/etc/sa02m-alice` 0770 group-write, the argument-unrestricted sudoers
  trigger with enable/disable/restart verbs, the CGI nudges — is homed only in review
  stamps that ship-beat deletion removes. Give it a durable home.
- [OPEN] 2026-08-26 **[MED] Prior-audit coverage findings never dispatched (audit M3):**
  C-2/C-3/C-5 from 2026-08-21 (their run-notes are now deleted; recover detail from git
  history of `.ai-dev/audit/`).
- [OPEN] 2026-08-26 **[LOW] `reachable` is never derived for sensor-only devices (audit
  L1).** `opt/sa02m-alice/sa02m_alice/client/device_registry.py:127-136` sets it in the
  capabilities loop only, so a property-only device reads reachable regardless of
  freshness. One-line follow-up (reviewer advisory, 1.0.6.15).
- [OPEN] 2026-08-26 **[LOW] `cleanup_b1_deploy_artifacts` skips `sa02m-alice` sudoers
  0440-hardening (audit L5).** Low impact today — that file is install-only, not
  OTA-deployed; act if alice sudoers ever enters the OTA set.
- [OPEN] 2026-08-26 **[LOW] GitHub Actions did not fire the `pull_request` event on
  first PR open** for #153 AND #154 (close/reopen re-armed it both times). Workflow
  triggers verified correct in-repo; recorded as observed-unexplained (delivery-side).
  Watch on the next ship; escalate to GitHub support if it recurs.

- [OPEN] 2026-08-20 **[LOW] Functional test for the update-runner health-gate operator-disabled
  skip (deferred).** The skip logic (masked/masked-runtime/disabled required units are skipped,
  enabled-but-down still fails — `etc/sa02m-update-runner.sh` restart_services_and_health) is
  covered only by the STATIC gate `health-gate-operator-disabled` (structure, non-vacuous), below
  the repo's functional-extraction idiom (service-ctl-policy-write, port-lease). A functional
  harness extracting `restart_services_and_health` + a systemctl shim was attempted but hit a
  Git-Bash tmp-file gremlin (the shim's per-unit state files read empty inside the sourced
  function despite working standalone) and was dropped for the static gate. Follow-up: drive the
  four is-enabled branches through a stubbed systemctl (masked/disabled → rc0 skip, enabled-down
  → rc1 fail) — likely needs a Linux/WSL runner, not Git-Bash. Surfaced by the health-gate
  Reviewer (F1 advisory).
- [OPEN] 2026-08-19 **[MED] Gateway mode for RS-485 flashing/scan — UI must steer to
  transparent.** Fixed in 1.0.5.86: WB/fast-modbus replies (`0xFF` arbitration) are no
  longer truncated, so scan works in `rtu_over_tcp`. BUT the PC flasher / a fast-modbus
  device scan through the gateway works ONLY in `transparent` (best) or `rtu_over_tcp`;
  `modbus_tcp` cannot carry it (MBAP can't wrap the `FD 46` fast-modbus / raw-RTU frames,
  and `fast_modbus_probe` answers the WB probe locally). Verified live on 1.135 (DTV+MR
  on COM4, СЭ on COM2): transparent + rtu_over_tcp scan all devices; modbus_tcp drops raw
  RTU. Follow-up: the «Шлюз RS-485» / flasher UI should warn (or auto-set) transparent @
  the device baud when a port is used for flashing, instead of leaving the modbus_tcp
  default as a trap; document in `docs/deployment.md` gateway section + the flasher hint.
- [OPEN] 2026-08-19 **[LOW] `tools/update-bridge/` is deprecated (unused) — remove at
  next cleanup.** The self-upgrade bridge (force-push a launcher onto fielded version
  branches) was REJECTED — see `docs/decisions/no-force-push-version-branches.md`. Old
  boards update via the offline full-tree archive (`scripts/offline-full-update.sh`),
  which stays. `tools/update-bridge/{repair-web-env-launcher,publish-bridge}.sh` are
  harmless (publish-bridge is dry-run by default) but no longer used; delete the dir at
  the next `tools/` tidy. The `--unattended`/`--status-file`/`--log` flags in
  offline-full-update.sh may stay (generic) or be trimmed with it.

- [OPEN] 2026-08-19 **[MED] Imaging pipeline: two recurring traps that each cost a
  failed capture run on 2026-08-19 (golden-image 1.136).** (a) **Stale known_hosts
  after the donor regenerates its host keys.** The id-reset before dd deletes
  `/etc/ssh/ssh_host_*`; on the donor's next boot `regen-ssh-host-keys` issues NEW
  keys, but `wait-donor.sh`/`make-image.sh` use `StrictHostKeyChecking=accept-new`,
  which accepts only UNKNOWN hosts and REJECTS a changed key → "ssh not ready yet"
  for 5 min → capture dies; on BOTH `/root/.ssh/known_hosts` and the repo
  `private/.ssh/known_hosts`. Fix: `ssh-keygen -R "$IP"` on both at the start of
  `capture-image.sh` (the donor's host key is disposable by design), or
  `-o UserKnownHostsFile=/dev/null`. (b) **Final `cp` to a drvfs OUT_DIR fails
  "are the same file"** (`make-image.sh` `FINAL_IMG`/`FINAL_IMG_KEEP` resolve to the
  same WORK path when OUT_DIR is `/mnt/d`) → exit 1 AFTER a fully successful
  dd/PiShrink/xz; artifacts rescued under `/tmp/sa02m-image-rescue-*` but the run
  reports failure and the `.img` never reaches OUT_DIR. Fix: `[ "$src" -ef "$dst" ]
  || cp …`. Also (c) **`docs/deployment.md` golden-image runbook §10 must add:**
  `sa02m-rootfs-expand` self-disables after running on the donor — before capture
  `rm /var/lib/sa02m-rootfs-expand.done` + re-enable, else clones boot with an
  un-expanded rootfs (imaging guide §941 trap); **never leave an `authorized_keys`
  on the donor for the capture's key-auth** — `cleanup-donor.sh` keeps `/root/.ssh`
  in its DENY (protected) list, so it SHIPS into the image (the 2026-08-19 run #1
  was rejected for exactly this; the password/sshpass path is the safe one); and the
  Alice check names `agent.conf` — current builds have
  `/etc/sa02m-alice/sa02m-alice-*.conf` (no agent.conf).
- [OPEN] 2026-08-18 **[MED] STAND 1.135 serves `/api/devices` from `sa02m-stand-api`
  (gunicorn `/opt/hardpy_tests/services/stand_web_api.py`), which a www-only deploy
  does NOT restart** — so the stand runs stale imported `sa02m_devices` code until
  `systemctl restart sa02m-stand-api`. `scripts/11-devices.sh` restarts
  `sa02m-devices-api` (the standard-board unit, present but NOT the :8765 owner on
  the stand → gunicorn is). Cost this session: the MR card looked "missing" on 1.135
  after deploy until a manual stand-api restart (code was correct all along; the
  running process was old). Fix direction: teach `update-www-only.sh` / `11-devices.sh`
  to detect + restart whichever unit owns :8765 (check `sa02m-stand-api` presence),
  OR document the stand's extra restart step in `docs/deployment.md`. Note in the
  deploy runbook that the stand is a hardpy_tests host with its own API service.
- [OPEN] 2026-08-18 **[LOW] Bridge module deploy list lives in 3 manually-synced
  homes with only 1 gate (audit 2026-08-18 LOW-2).** `scripts/05-mqtt.sh:137-138`
  and `scripts/update-www-only.sh:378-379` each list the `bridge_*.py` modules to
  install; `opt/sa02m-modbus-mqtt/tests/test_entry_surface.py:54` freezes the
  import SET (asserted `:135`) but nothing enforces the two shell deploy lists
  match it. A future module added to the test + one script only would ship a board
  missing a module with no gate firing. Fix: a quality-registry check that both
  shell lists equal the frozen import set.
- [OPEN] 2026-08-18 **[LOW→follow-up] Configurable serial parity/stopbits for
  `type:template` (8N2 support).** The honesty/doc half of audit-2026-08-18 MED-1
  is DONE in **1.0.5.78** (corrected the wrong "9600 8N2" comment → 8N1-only;
  documented in `docs/contracts/template-device.md §8` + `templates/README.md` +
  the YAML example). RESIDUAL: `bridge_serial.py:218-219,445` still hard-codes
  8N1 (`parity=NONE, stopbits=ONE`), no config field — a WB device on 9600 **8N2**
  cannot be polled. Follow-up: add YAML parity/stopbits and thread them through
  `get_port`/`_ensure_open`. (Same as the "serial 8N2" deferred item.)
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
  shared mutable `state` (260 refs), only 5 `window.*` exports. The 2026-07 reason
  for deferring it (no ES modules ⇒ the split would mean promoting `state` and the
  whole fn set to global scope — a semantic rewrite that recreates the god-object)
  is **VOID since 2026-08-18**: `docs/decisions/es-modules.md` lifted the ban and
  names this split as its motivation, and `mqtt.js`/`devices.js` already ship as
  modules. The live entry is the 2026-08-28 decomposition worklist (item 1) — do
  not re-argue it here. Still OPEN for app.js:
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
- [OPEN] 2026-08-21 **[MED] `cleanup-donor.sh` DENY list is disabled by a `/*`
  alternative.** `tools/imaging/cleanup-donor.sh:140` — the case pattern reads
  `/etc/sa02m-update/trusted-keys|/*) return 1 ;;`, and the `/*` alternative
  matches EVERY absolute path, so under `--purge-update-state` the whole DENY
  list (`/var/www/network_config`, `/opt/mplc4`, `/boot`, …) silently stops
  protecting anything. Found by the #140 reviewer, verified in a shell;
  pre-existing on main and out of that PR's scope. Fix: drop the stray `/*`
  alternative (it was almost certainly meant to be
  `/etc/sa02m-update/trusted-keys/*`) and add a regression test that asserts a
  DENY-listed path is still refused with `--purge-update-state` on.
- [OPEN] 2026-08-21 **[LOW] EN tooltips go stale after a re-poll (`i18n.js`).**
  `translateAttr(el, attr, refreshOriginal)` is never called with
  `refreshOriginal` — `i18n.js:1483` (the MutationObserver's `attributes`
  branch) omits it, so `:1330` pins the FIRST recorded original and re-applies
  it over any later JS-set `title` / `aria-label` / `placeholder`. Reproduced on
  the MPLC licence tooltip: change the licence with the page open in EN and the
  visible numbers update while the tooltip still describes the previous licence;
  RU is unaffected, and the text path is immune (`textContent =` replaces the
  node). Fix is one line — pass `true` — but it changes behaviour for EVERY
  dynamic attribute in the app, so it needs its own pass over the attribute
  consumers plus a re-run of the headless harness. Found by the 1.0.6.4 builder,
  root cause confirmed independently by its reviewer; deliberately left outside
  that branch's fence.
- [OPEN] 2026-08-24 **[MED] carrier-wait drop-ins are not OTA-reachable — the DNS boot-race fix reaches field boards only half-way.** `etc/systemd/system/ifup@.service.d/` + `networking.service.d/` drop-ins (1.0.6.6) install only via `scripts/02-network.sh` / a fresh image, not web update. HARDWARE-CONFIRMED 2026-08-24 (FR-CABLE via Keenetic on the OTA'd bench board): the belt restores DNS, but with no carrier-wait a no-carrier boot did NOT recover the interface until a reboot. Field boards need a NEW image built from ≥1.0.6.8 (or a reinstall) to get carrier-wait. The current golden image is 1.0.6.4 (pre-fix).
- [OPEN] 2026-08-24 **[MED] firstboot-overlay carries stale twins of two ladder scripts (audit A1).** `tools/imaging/firstboot-overlay/usr/local/sbin/sa02m-eth-coldboot.sh` and `.../fix-eth.sh` were byte-identical to `main` before 1.0.6.6 and were NOT updated by that branch. `autorun.sh` mirrors that dir onto a freshly flashed rootfs, so a FEL/USB-imaged board silently reverts the 1.0.6.6 ladder call sites. Sync the overlay twins (or replace with a symlink/build step) before the next image capture.
- [OPEN] 2026-08-24 **[LOW] HW-variant auto-detect flake (1eth ↔ 2eth).** The 2-eth bench board reported `SA02M_HW_VARIANT=sa02m-1eth` on one boot and `sa02m-2eth` on the next (both eth0+eth1 present physically). Detect-by-physical-eth-count is racing something at boot. Investigate the detection ordering vs PHY/link readiness.
- [OPEN] 2026-08-24 **[MED] make-image.sh strands the donor after capture.** It strips SSH host keys as its last pre-`dd` step but never reboots the donor in the still-live session, so post-capture the board is unreachable (sshd has no host keys, panel stopped) until a manual power cycle — a problem on a remote bench. One line: schedule a detached reboot at the end of the stream session (rc.local already regenerates keys at boot).
- [OPEN] 2026-08-24 **[BLOCKER→in-progress 1.0.6.11] B1 (1.0.6.10) is a no-op on OTA/upgraded boards.** Hardware-verified on the 2-eth bench (OTA'd to 1.0.6.10): (1) a legacy `/etc/sudoers.d/www-data` file (pre-rename name, raw `tee`/`ifup`/`reboot` grant) survives — nothing in the current repo writes or removes it — so `sudo -n tee /etc/sudoers.d/zz`→root is STILL open (PoC created `/etc/sudoers.d/zzhack`). (2) OTA `map_dst` strips `.sh` from the two new B1 helpers, so they land as `/usr/local/sbin/sa02m-{iface-conf-write,usb-power}` while the sudoers grants the `.sh` path → network-apply + USB-power break once the legacy grant is removed. Also the stale `sa02m-www.fragment` survived OTA. B1's validator/pinning LOGIC is correct (reviewer-confirmed); this is purely the deploy/cleanup layer. Fix = installer+updater+OTA+offline remove the known-obsolete legacy sudoers names (allow-list, never blanket) + deploy the helpers WITH `.sh` on all three paths + a RED→GREEN gate. Plan `.ai-dev/plans/b1-deploy-gap.md`. **FIX IN BRANCH 1.0.6.11** — hardware acceptance pending.
- [OPEN] 2026-08-27 **[MED] «Время без опроса» accepts a value that silently breaks outputs, with no warning.** MR-02m holding 134 clears ALL of a module's outputs after N seconds without a frame that resets its inactivity counter (five reset sites, only one of them address-matched — the table is in the note named below). The module-config window (`flasher.js`, `saveMrGlobalInactivity` + the per-AO field + the bulk template-apply path at ~4322) offers the raw range 0–255 with no guidance, so a value shorter than one bus sweep — 1 s on a line the bridge polls round-robin — makes every output fall by itself during normal operation. Cost a full firmware-update cycle and hours of bus tracing on bench 1.135 before the register was read (root cause + the A/B/A proof: `.ai-dev/notes/mr02m-inactivity-timeout.md`). Fix candidates: warn (do not block) below a threshold derived from the port's device count and poll period; surface the current value in the module card next to the DO states; make the template-apply path name this field explicitly in its confirmation, since it copies it onto other modules. Fold in round-1 finding 10 while there: neither `docs/agent-rules/web-diagnostic-tools.md` nor `docs/agent-rules/sa02m-domain.md` points at the note, so the symptom->tool dispatch still cannot route "an output falls by itself".

<!-- Whole-project audit 2026-08-28 at 1.0.6.23 (3 parallel auditors: contracts,
     security, docs). Suite was GREEN (build 47/47, review 6/6) and branch
     protection verified live — every finding below is what green does NOT cover. -->

- [OPEN] 2026-08-28 **[BLOCKER] Root code injection in the gateway config helper.**
  `etc/sa02m-gateway-config-apply.sh:16` opens the Python heredoc UNQUOTED and
  interpolates `open("$TMP_SRC")` into the Python source; `scripts/06-gateway.sh:47`
  grants `www-data` NOPASSWD on that helper with NO argument pinning. A logged-in panel
  user reaches root (`cmd_exec.cgi` is an authenticated `www-data` shell by design).
  Orchestrator-verified at both lines. Fix: quote the heredoc delimiter + pass the path as
  argv, and pin the grant. Every other privileged helper already uses the quoted form —
  this is the lone exception.
- [OPEN] 2026-08-28 **[BLOCKER] Root sed injection in the cloud pairing helper.**
  `usr/local/sbin/sa02m-cloud-web-trigger.sh:48` interpolates `$SERVER` into a `sed -i`
  substitution under a comment asserting the CALLER validated it; `etc/sudoers.d/sa02m-cloud:2-3`
  is unpinned, so the caller is bypassable. A pipe character terminates the expression; GNU
  sed's `e`/`w` flags then execute/write as root. Precondition: `/etc/sa02m-cloud/agent.conf`
  exists (post-enrolment). Orchestrator-verified. Fix: validate INSIDE the helper (never trust
  the caller) + pin the grant.
- [OPEN] 2026-08-28 **[HIGH] The B1 escalation gate reads 1 of the 6 homes that grant
  www-data root — hollow ratchet #9, on a security-load-bearing claim.**
  `.ai-dev/quality/checks/sudoers-pin-contract.sh:29` reads only `etc/sudoers.d/sa02m-www`.
  The other homes: `etc/sudoers.d/sa02m-cloud`, `etc/sudoers.d/sa02m-mqtt`,
  `scripts/06-alice.sh:105`, `scripts/06-gateway.sh:47-51`, and a RUNTIME APPEND at
  `etc/sa02m-web-update-apply.sh:308`. The `sa02m-www` header claims to be "the COMPLETE,
  single-home" grant list — false. This is why the two BLOCKERs above are green today, and why
  `docs/threat-model.md` still says the escalation class is closed (B1). Fix: extend the gate to
  all six homes and prove it RED against the two injections BEFORE fixing them.
- [OPEN] 2026-08-28 **[HIGH] Two shipped daemons put process control on the LAN with no auth
  at all, and neither is in the threat model.** `opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua.py:180`
  sets the NoSecurity policy, bound `0.0.0.0:4841` (`:157`), with WRITABLE nodes (`:284`) polled
  back into MQTT (`:309-319`); `UserManager` is imported at `:33` and never used.
  `opt/sa02m-serial-gateway/serial_gateway.py:413,564,642` — all three modes bind hardcoded
  `0.0.0.0` (ports 502-506/8502-8506/9502-9506) with no bind-address, IP allow-list or key in the
  config schema. Both ship DISABLED and are Operator-enabled per port — but the Operator enables
  them without being told what they expose. Fix: bind-address + allow-list options, and a
  threat-model row so the trade is visible at the moment of enabling.
- [OPEN] 2026-08-28 **[HIGH] Five safety gates are defeated by putting a comment mark in front
  of a line.** Mutation-proven GREEN on comment-out (16 of 22 mutations correctly went RED;
  these 5 did not): `mplc-ota-deploy-contract.sh:20`, `mplc-project-deploy-contract.sh:36`,
  `kernel-policy-contract.sh:177`, `health-gate-operator-disabled.sh:28`,
  `sudoers-pin-contract.sh:76`. All five DO go red on deletion — only the comment form slips.
  `tools.json:159` and `:303` explicitly promise these cases fail. 8 of 16 check scripts have no
  comment handling, though `no-retired-session-token.sh` already solved it in-repo.
  Fix: reuse that pattern across the 8; add a comment-out mutation to each row's own proof.
- [OPEN] 2026-08-28 **[HIGH] The one endpoint that switches real relay outputs is guarded only
  by a comment.** No registry row touches `www/network_config/cgi-bin/mqtt_set.cgi` beyond
  `bash -n`; `docs/contracts/mqtt-set-endpoint.md:74-96` is a MANUAL recipe, and `mqtt_set.cgi:88`
  claims "Hard floor of this endpoint; asserted by the contract check" — no such check exists.
  Re-adding the MQTT retain flag would re-fire every output on the next bridge restart with
  nothing in the pipeline noticing. Fix: a real row + delete the false comment.
- [OPEN] 2026-08-28 **[HIGH] The panel's login/CSRF core has zero functional tests.**
  `www/network_config/cgi-bin/lib_web_auth.sh` — 354 lines, 27 functions (session tokens, CSRF
  minting/validation, password hashing, credential-file repair), sourced by every mutating
  endpoint — is reached only by `bash -n` and CI shellcheck. A mistake in the code deciding
  "is this person logged in" ships green. Harness pattern already in-repo: `test-subnet-validate.sh`.
- [OPEN] 2026-08-28 **[HIGH] README is 2-3 subsystems behind while its version badge reads
  current.** Zero mentions of Alice (6 of the last 9 releases) or the devices tab;
  `README.md:392-474` lists `scripts/` 01-07 (tree has 01-12+85), `opt/` 5 (tree 11), 4 JS
  bundles (index.html loads 16); `:1456` claims a complete CGI list — 26 of 42 absent.
- [OPEN] 2026-08-28 **[HIGH] The deploy runbook documents a step that aborts, and one deploy
  rule lives only in a gitignored file.** `docs/deployment.md:78-80` says the www-only path
  deliberately omits `etc/`, but `scripts/lib.sh:500` sources `../etc/sa02m-stacks-policy.sh`
  unconditionally, so that path fails (hit live 2026-08-26, backlog:234) with no warning in the
  runbook. And the rule "Alice reaches a board ONLY via install.sh/06-alice.sh — never OTA"
  exists ONLY in `.ai-dev/state/current.md:58-60` (gitignored, dies on a state reset); its home
  is `docs/deployment.md:24-38`.
- [OPEN] 2026-08-28 **[MED-HIGH] `headless-smoke` is a dormant row over a stale baseline.**
  `headless-smoke.sh:12` skips unless playwright AND `SA02M_WEB_PASS` are present; CI provides
  neither, so it has no environment where it runs. Its committed baseline
  `scripts/dev/baseline/manifest.json` was last touched at 1.0.5.81 while the JS is at 1.0.6.23.
- [OPEN] 2026-08-28 **[MED-HIGH] The `rs485-roster` contract is validated producer-side only.**
  `py-unit-roster` covers `opt/sa02m-rs485-roster/` (`tools.json:127`); the contracted `modules`
  field is emitted from `status.cgi:1116-1124`, which NO row covers. Edit `status.cgi`, lose the
  RS-485 module list from the dashboard, every gate stays green — the exact blind spot this audit
  dimension exists for.
- [OPEN] 2026-08-28 **[MED-HIGH] Four always-loaded rule docs say the quality registry has 3
  rows; it has 53.** `web-code-rigor.md:142`, `web-diagnostic-tools.md:96-97`,
  `web-workflow.md:78-79`, `sa02m-web-testing/SKILL.md:64-65`. The registry is the one home —
  the prose should point at it, not enumerate.
- [OPEN] 2026-08-28 **[MED] MQTT password written to a web-served log.** `scripts/05-mqtt.sh:55`
  logs the generated password via `scripts/lib.sh:20` into `/var/log/sa02m_install.log`, served
  verbatim by `log_export.cgi:18` and `log.cgi:21`. The same secret is 0600 in
  `/etc/sa02m_mqtt.env`; the log is never chmod'd. It opens :1884 with readwrite on all device
  topics. Related: `scripts/05-mqtt.sh:52` fallback password is a timestamp-derived string —
  predictable.
- [OPEN] 2026-08-28 **[MED] `session_token` is not HttpOnly, and login has no brute-force
  protection.** `login.cgi:54` (deliberate — the `app.js` guard reads `document.cookie`), but the
  JS-readable `sa02m_csrf` mirror at `:58` shows the guard could key off a non-session cookie
  instead. Any XSS then reaches `cmd_exec.cgi` and (via the two BLOCKERs) root. Separately
  `login.cgi` has no counter, lockout or delay against a single shared password over plain HTTP.
- [OPEN] 2026-08-28 **[MED] The Alice config API is an unauthenticated root API, and its
  function name lies.** `opt/sa02m-alice/sa02m_alice/config/api.py:640-653` — `serve_unix` binds
  TCP, not a unix socket — serves enable/disable/link/unlink/upsert_device/delete_device
  (`:580-603`) with no authn under `User=root`. Loopback + opt-in, so reach is any local process
  including `www-data` (no session, no CSRF).
- [OPEN] 2026-08-28 **[MED] CSRF gap on the legacy OTA endpoint contradicts our own canon.**
  `web_update_apply.cgi:318-330` runs the update helper marked "no CSRF", while
  `docs/decisions/selective-csrf-policy.md` names only `logout` as an exception and the threat
  model requires CSRF on ALL mutating endpoints. SameSite=Lax still holds, so this is broken
  defence-in-depth plus false canon — fix the endpoint or amend the decision, not neither.
- [OPEN] 2026-08-28 **[MED] Default credential `cyntron` is committed in 8 places and nothing
  forces a change.** `install.sh:28`, `scripts/03-webserver.sh:15`,
  `tools/debian-rootfs/create-sa02m-rootfs.sh:20,35`, `tools/imaging/make-image.sh:20`,
  `tools/ssh/sa02m-check-perms.py:22`, `tools/ssh/sa02m_remote.py:16,56`; the password hash in
  `etc/sa02m-factory-defaults/templates/etc/sa02m_web.env:2`; and in clear as a password hint in
  the factory manifest. `etc/ssh/sshd_config.d/10-sa02m.conf:30-32` leaves password SSH enabled
  with that note.
- [OPEN] 2026-08-28 **[MED] GitHub OTA installs unsigned code as root.**
  `etc/sa02m-update-runner.sh:509-511` allows unsigned artifacts for the github source, skipping
  the Ed25519 check the upload path requires (`:657-664`). Deliberate — but the threat model
  carries no supply-chain row for it, so the trade is invisible.
- [OPEN] 2026-08-28 **[MED] Two shipped daemons have no tests at all.** `opt/sa02m-mqtt-opcua`
  (451 L, OPC UA server on 4841) and `opt/sa02m-mqtt-snmp` (427 L) have no `tests/` and no
  `py-unit-*` row; every other `opt/` package has one.
- [OPEN] 2026-08-28 **[MED] `ui-layout` never runs in CI** — the workflow installs Python deps
  only, no playwright, so layout / mobile KPI centring / clipping / the WCAG contrast ledger are
  dev-box-only. Honestly labelled (`tools.json:482`, `.ai-dev/notes/quality-gate-environment.md`),
  so this is coverage, not dishonesty.
- [OPEN] 2026-08-28 **[MED] Backlog and pointer carry shipped work as open, and the decompose
  worklist is stale in 4 homes.** backlog:88 (golden-image Alice identity) shipped as 1.0.6.20 —
  and `current.md:36-40` still ASKS the Operator whether to take it; backlog:240 shipped as
  1.0.6.15/18; `current.md:19-20` contradicts `current.md:66-67` on the audit-cadence count.
  Worklist: backlog:604 names `modbus_mqtt_bridge.py` at 3422 L (now 298 — already split);
  backlog:461 says service-ctl is 1414 L (now 1582); backlog:667 defers `flasher.js` because
  "ES modules forbidden" — lifted by `docs/decisions/es-modules.md` on 2026-08-18, whose stated
  motivation IS that split.
- [OPEN] 2026-08-28 **[MED] `docs/architecture.md` does not exist yet is cited 12x in
  always-loaded files** (`PROTOCOL.md` 6x, `.claude/ai-dev.md` 3x, `.ai-dev/notes/README.md:4,9,20`).
  Every session is pointed at a missing home.
- [OPEN] 2026-08-28 **[MED] Two homes for the polling architecture, one of them declaring itself
  the single home.** `sa02m-web-architecture/SKILL.md:25-51` restates cadences, `statusPauseUntil`,
  warmup and renderer-owned DOM from `sa02m-domain.md:56-72` while `:7` claims "One home". Also
  `sa02m-domain.md:28` names 5 JS bundles (tree has 8 + a 9-file `app/` cluster) and contradicts
  its own tab table at `:79`.
- [OPEN] 2026-08-28 **[MED-LOW] Two declared reviewer floors have no mechanical row** — i18n
  completeness and the HTML-id contract are named as floors in `web-code-rigor.md` but nothing
  implements them. Concrete: a DICT-completeness script (RU strings in markup/JS vs `i18n.js`
  keys) and an id-contract grep (getElementById against `index.html`).
- [OPEN] 2026-08-28 **[LOW] Security long tail.** Response-header injection in the devices export
  (`device_history_db.py:1788-1789` raw metric/group into `api.py:59-66`; the ascii filter keeps
  CR/LF, `_q1` strips only edges) — authenticated · no dependency-CVE and no secret-scanner row in
  the 55-row registry, deps floor-pinned with no lock · 42 of 48 systemd units run root with zero
  hardening, including the four that parse hostile input · `docs/threat-model.md` says the frpc
  port allow-list is absent — it exists (`sa02m-cloud-agent.py:66`).
- [OPEN] 2026-08-28 **[LOW] Docs long tail.** Durable docs citing deleted transient plans
  (`selective-csrf-policy.md:48`, `web-bus-mode-bacnet.md:24`, `mplc-driver-build.md:233`) · an
  unbuilt 2026-07 plan with no status marker (`storage-benchmark-plan.md`) · `CLAUDE.md:5` says
  "six docs", seven are imported · superseded wording kept as archaeology
  (`TZ_PRE_PRODUCTION_DONOR_CLEANUP.md:180-185`) · `docs/audits/AUDIT_1.0.4.0.md` not marked
  historical · `docs/contracts/web-bus-mode-bacnet.md` names no validating test though coverage
  exists (`test_bus_mode.py`, `test_bacnet_mstp.py` under `py-unit-flasher`).
- [OPEN] 2026-08-28 **[MED] `docs/architecture.md` is a pointer stub — the real doc-bootstrap
  pass is still owed.** 1.0.6.24 created the file so no session is sent to a missing home, but
  deliberately only as a map ("куда идти за чем"); it says so in its own text. A real
  architecture document (layers, data flow, boundaries, the deploy model of the whole system)
  wants a doc-bootstrap pass over the tree and would have swamped the review of a security
  branch. Do it on its own branch; its natural seed is §3 of the 1.0.6.24 plan (the deploy
  reality tables) plus `docs/agent-rules/sa02m-domain.md`.
- [OPEN] 2026-08-28 **[LOW] `docs/storage-benchmark-plan.md` — build it or drop it.**
  A 2026-07-04 design for «Управление → Тест накопителя» (`etc/sa02m-storage-bench.sh` +
  `storage_bench.cgi`), never implemented, 19 versions on. 1.0.6.24 marked it
  «Статус: НЕ РЕАЛИЗОВАН» so it stops reading as a description of shipped behaviour. The
  Operator decision it needs: is disk diagnostics from the panel still wanted? If no, delete
  the file (git keeps it); if yes, it is a ready-made plan.
- [OPEN] 2026-08-28 **[SUSPECTED — Operator to settle] Three open questions from the audit.**
  (1) `etc/nginx/network_config.conf` carries ZERO assertions though `flasher-health.md` names its
  `auth_request` lines (165,180,195,204) as load-bearing; the counter-position "nginx conf is
  device config, verified at deploy" is defensible. (2) Frontend XSS was spot-checked (~6000 lines,
  escaping held everywhere inspected) but NOT exhaustively swept — MQTT device payloads do reach
  the UI, so a dedicated pass is warranted given the non-HttpOnly cookie. (3) Git history was never
  scanned for secrets (`private/`, `.tmp/` are correctly gitignored).
- [OPEN] 2026-08-28 **[MED] Decomposition worklist (audit-derived, by cohesion not line count).**
  **THE one home for this worklist** — the 2026-07-17 / 2026-08-06 / 2026-08-18 entries were
  collapsed into it on 2026-08-28 (their numbers were stale, one on a void rationale). Line
  counts re-measured in-tree 2026-08-28 on branch 1.0.6.24.
  1. `flasher.js` 5189 L — ~10 responsibilities in one IIFE; the ES-module blocker is void since
  2026-08-18 (`docs/decisions/es-modules.md`, whose stated motivation IS this split).
  2. `app/status.js` 2509 L — the update flow (`:1294-2030`) and MPLC deploy
  (`:2030-2300`) are not status. 3. `device_history_db.py` 1918 L — ranges/schema/write/query.
  4. `devices.js` 2406 L — extract the canvas chart engine (`:1068-2008`). 5. `flash_protocol.py`
  2517 L — split the three flash-sequence drivers. 6. `etc/sa02m-web-service-ctl.sh` 1582 L —
  Node-RED block (`:955-1491`); it grew 1414 → 1518 → 1582 across three sweeps, so it is a
  standing item, not this cycle's growth. 7. `mqtt.js` 2721 L — seams already named: device-add
  modal / per-family builders · the `type:template` picker (`_templateCatalog` /
  `fillTemplateSelect` / `refreshTemplateCatalog`) · broker+credential settings · config model;
  already an ES module. 8. `status.cgi` ~2530 L (size relief, not cohesion) ·
  9. `sa02m-update-runner.sh` 1479 L. Smaller, self-contained and still worth doing:
  `opt/sa02m-modbus-mqtt/bridge_mqtt.py` (444 L) — extract the `/meta` blob cluster
  (`_num_or_str`/`_obj_or_str`/`_control_meta_blob`/`_device_meta_blob`/the two
  `_publish_*_meta_blob`) → `bridge_meta.py`, covered by `test_meta_blob.py`.
  NOT a finding: `main.css` 5466 L — sectioned, one token root, no-build stack.
  Already done, do not re-raise: `modbus_mqtt_bridge.py` (was 3422 L, now 298 — split into
  `bridge_*.py`) and `app.js` (F10, now a ~389 L core + the `app/` cluster).
- [OPEN] 2026-08-28 **[MED] Empty `FSTYPE` conflates "blank" with "probe failed", and
  auto-format cannot tell them apart.** `etc/storage-mount.sh` `probe_fstype` returns the
  same empty value for a genuinely blank partition and for one udev+blkid could not read,
  so with the flag on, an unreadable-but-populated partition is formatted. This is the
  feature's original 1.0.3 semantics, mitigated three ways (5 probe retries over ~5 s of
  backoff — 1.2 s and non-monotonic until 1.0.6.24 made the code match that figure,
  try-mount-before-mkfs ordering, and the flag shipping OFF) — and it is the plausible
  reason someone might mistake the `-z` clause removed in 71e92ba for an intentional
  guard. The honest fix is a DISTINCT "probe failed" outcome in `probe_fstype` that never
  reaches mkfs; that is a separate planned change, not a one-liner. Found by the 1.0.6.24
  builder while fixing the regression, deliberately left outside that commit's fence.
- [OPEN] 2026-08-29 **[MED] codesys presence still trusts wrappers — same class the
  mplc4 fix (c7a4443) closed for mplc4 only.** On bench 1.135 a leftover
  `/etc/systemd/system/codesyscontrol.service` (июн 23) with no runtime, no
  `/etc/init.d/codesyscontrol` and no dpkg package makes `service_present codesys`
  (generic unit-file candidate loop in `etc/sa02m-web-service-ctl.sh`) and the
  stacks-policy CODESYS probe read "installed" — the panel shows a dead Пуск/Стоп
  pair instead of «Установить». Fix mirrors c7a4443: presence = the runtime
  (`/opt/codesys`/dpkg/init.d), not unit-file remnants; codesys_uninstall should
  also remove the leftover unit it currently strands (it rm's the drop-in dir but
  not `/etc/systemd/system/codesyscontrol.service` when dpkg is already gone).
- [OPEN] 2026-08-29 **[LOW] installer WARN «sa02m-userspace-watchdog.service: не
  удалось включить автозапуск» on every run — the unit exists nowhere.** Observed
  on the 1.135 refresh (1.0.6.24, c7a4443): the enable site targets a unit that is
  neither on the board (`is-enabled` → not-found) nor in the deployed tree
  (`etc/systemd/system/` ships no watchdog unit). Either the unit was renamed/
  retired and the enable site is stale, or the unit file was never added — find
  the enable call in `scripts/` and make it match reality (drop it or ship the
  unit). Every refresh currently logs a WARN the runbook tells the operator to
  review by hand.
- [OPEN] 2026-09-02 **[LOW] The image-identity reset leaves the cloud-control profile's
  traces on a cloned board.** `tools/imaging/*` and `docs/contracts/image-identity-reset.md`
  clear the Alice identity but neither remove `/run/sa02m-alice/status-cloud.json` nor
  disable `sa02m-cloud-control.service` (1.0.6.26, the second profile of the same
  package). Harmless today: the status file is tmpfs and, with the cloud agent's
  identity wiped, the unit lands in `missing_identity` standby — but a clone that is
  later re-enrolled starts cloud control without an explicit operator opt-in. Left out
  of 1.0.6.26 deliberately so the `alice-image-identity` gate's mutation set stayed
  untouched; the parity fix is a planned change that extends that mutation set and the
  contract together, not a one-liner.
- [OPEN] 2026-09-02 **[MED] CI runs the `ui-layout` review gate VACUOUSLY — Playwright is never
  installed in `.github/workflows/web-quality.yml`, so the row self-skips and reports PASS.**
  Exactly the class of defect caught locally during the 1.0.6.26 review: the driver "passed"
  on the developer host until `npm run ui-layout:install` was run, after which it found three
  real violations (tap target, two contrast pairs) in the new «Умный дом» UI. Until CI runs
  `npm run ui-layout:install` before the review beat (chromium download + cache step), the
  remote floor does not cover geometry/contrast at all and the gate is honest only on a
  prepared workstation. Separate CI change, deliberately outside the 1.0.6.26 PR.
- [OPEN] 2026-09-02 **[INFO — bench reference, not a defect] Three paths where a cloud tap is
  NOT confirmed by design (1.0.6.26 cloud control).** Recorded so the bench run on 192.168.1.135
  does not book them as regressions: (1) the target already equals the actual state — the cloud
  tile never enters the pending state (cloud `ui_pages.py`, target==actual short-circuit);
  (2) the ~1 s window right after a reconnect, while the client re-offers the snapshot before
  the first live echo can be attributed (`client/main.py` reconnect path); (3) the retained-
  grace window after an in-place document reload, when retained MQTT values are deliberately
  not reported as live; (4) a held live frame is LOST, not delayed, when the session drops
  inside the hold window (≤ 1 s during an event burst, 1.0.6.26 B6 ceiling) — `_last_value`
  already counts it as sent, so the reconnect snapshot restores the value but never confirms
  the tap (found by the 1.0.6.26 review's loss probe; same class as the three above). Each is a
  designed non-confirmation; a tap in those windows shows the cloud's "no confirmation"
  outcome without a device fault. Resolve by folding the four into
  `docs/contracts/alice-mqtt-mapping.md` (device side) once the bench confirms the timings.
  Notes from the same review, not defects: a dead defensive branch in `state_sender.py`
  (`_split_fast_lane` fallback) and the one literal exception to "a snapshot never holds".
- [OPEN] 2026-09-02 **[MED, cross-repo with the cloud] The controller's `action` result shape
  does not match Yandex.** The client answers `alice_devices_action` with `status`/`error_code`
  one level ABOVE `state.action_result`; the platform reads the result from exactly two
  alternative places — `capabilities[].state.action_result` or `action_result` beside `id` — with
  `status` = `DONE|ERROR` and `error_code`/`error_message` INSIDE `action_result`. Today the
  cloud gateway normalises the shape (a load-bearing fix-up); the device should emit the Yandex
  shape itself so the gateway's normalisation becomes a safety net. Homes of the expected shape
  (cloud repo): `docs/yandex-smart-home-rules.md` §1 (platform requirement, source-linked,
  verified 2026-08-27) and `docs/contracts/alice-gateway.md` «`action` result shape (gateway
  guarantee)» (what the controller sends today and how the gateway rewrites it). Touches
  `client/sio_handlers.py` / `device_registry.apply_actions` — schedule after the 1.0.6.26
  revoke stand-down round, together with the optimistic-cache item above.
- [OPEN] 2026-09-03 **[LOW] `py-unit-devices` is a clock-of-day flake: `tests/test_device_events.py::
  test_voltage_jump_and_current_spike` fails in the first hour after local midnight.** The test
  seeds its baseline at `now − 3600 s` (yesterday) while `detect_ce_events` averages over the
  CALENDAR day (`_day_avg_current` → `_day_start_ts(ts)`), so between 00:00 and 01:00 local
  there is no same-day baseline and no `current_spike` fires. Reproduced 3/3 at 00:23 on
  2026-09-03 with `opt/sa02m-devices` untouched (last change b9f3ad4); green at any other hour.
  Fix: seed the baseline inside the same calendar day (or inject the clock) — untouched code,
  out of the 1.0.6.26 fence. Until then a midnight-hour `build` run is a false red.
- [RESOLVED] 2026-09-03 **The «Облако» card's CSS/JS half has no in-tree test** → closed by the `cloud-card-smoke` review row (`scripts/dev/cloud-card-smoke.mjs`, 1.0.6.28): renders every contract state of the card in headless Chromium with in-page transitions from `active`; RED without chromium by design (exit 2), never a vacuous green.
- [OPEN] 2026-09-03 **[MED] Neither cloud client logs a line when it executes an action, so the
  source of a command cannot be established after the fact.** On the 1.0.6.26 bench run
  (192.168.1.135) a second write to `/devices/SA-02m/controls/alarm_led/on` arrived 6 s after the
  first; `journalctl -u sa02m-cloud-control` and `-u sa02m-alice-client` had NO entries at all in
  that minute, although both were connected. Distinguishing "the operator tapped twice" from "the
  chain generated a command" was impossible from the board and had to be answered from the cloud
  hub's own log (it was ten taps from the control page, no Alice path). A device that executes a
  remote command must record who asked and what it did: one INFO line per executed action
  (channel/profile, device id, capability+instance, requested value, resulting publish) in both
  profiles. Cheap, and it is the difference between a diagnosis and a guess.
- [OPEN] 2026-09-03 **[LOW] In the stand-down state the cloud-control profile logs an ERROR every
  ~50 s: `cloud control token: cloud identity missing`.** Observed on the bench between 10:35 and
  10:59 while the board was revoked. The behaviour is correct — with the identity erased no token
  can be minted — but "revoked" is an EXPECTED state, not an error: the profile should back off
  and log at INFO/DEBUG (or once per transition), not raise a recurring ERROR that pollutes the
  journal exactly while an operator is diagnosing a revocation.
- [OPEN] 2026-09-03 **[LOW] `sa02m-alice-client` dropped its Socket.IO session twice in one hour
  with `reason=lib:transport error`** (10:45:04 after 303 s, 11:02:51 after 1062 s), each time
  reconnecting on its own (10:45:09, 11:03:08 — once via `[ERROR] yandex client error: One or more
  namespaces failed to connect`). Self-healing, so not a defect, but the cadence is worth a look:
  if the transport drops on a ~5-17 min cycle, every drop is a window in which a tap is not
  confirmed. Bench 192.168.1.135, 1.0.6.26.

- [RESOLVED] 2026-09-03 **Two MQTT overlays were not viewport-anchored** — `#mqtt-scan-modal`
  and `#mqtt-add-modal` lived inside `#tab-mqtt`, whose entry animation leaves a `transform` on
  the ancestor for ~200 ms, and a transformed ancestor becomes the containing block for
  `position: fixed`. Fixed in 1.0.6.29 in the same change that found it (per the standing rule
  «найденный дефект назначается, а не заносится»): both overlays moved to the document root,
  `mqttTabDestroy` hides them on leaving the tab, and `sh-modal-layout-smoke` asserts both stay
  inside the viewport with the ancestor deliberately transformed.
### [LOW] Тексты входов/выходов Carel не переведены на английский

Строки таблиц «Входы/выходы» и расшифровки цифровых состояний живут в
`opt/sa02m-carel/sa02m_carel/carel_ahu_map.py` только по-русски — у тревог там
есть `text_en`, у остальных строк нет. В английском интерфейсе окна настройки
контроллера эти две таблицы остаются русскими. Заметно только при переключении
языка; чинится добавлением `text_en` в карту (её единственный дом) и
использованием его в рендерере, как уже сделано для тревог.
Найдено при сборке окна 1.0.6.31. OPEN.
