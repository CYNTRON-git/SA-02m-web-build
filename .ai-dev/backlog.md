# Backlog — SA-02m web interface

Recorded findings and deferred work (`.ai-dev/procedures/backlog.md` owns the
format). One status per finding: `- [OPEN|RESOLVED] <date> <item>`. Resolved
entries are pruned (history lives in git); last prune 2026-09-24 (1.0.6.54 — audit 2026-09-24 M5:
24 RESOLVED entries and one OPEN entry shipped in 1.0.6.51 removed; the removed text is that
commit's diff of this file).

## Open

- [OPEN] 2026-09-30 **[MED] Sweep the other harnesses for host-binary / host-root side effects (ci-linux-green
  finding).** Two harnesses could, on a ROOT host, reach real system actions: firstboot-sb-csum a real
  `fsfreeze -f /`, web-update-apply-guard reboot.cgi's real `sudo … reboot -f` chain. Both fixed on
  ci-linux-green (PATH masking via scripts/dev/lib_path_mask.sh; sandboxed CGI copy). Other `scripts/dev/`
  harnesses that remove a stand-in and rely on `command -v` failing, or run a shipped CGI whose side
  effects are real on a writable host, were not swept. The CI runner has passwordless sudo.
- [OPEN] 2026-09-30 **[LOW] `devices-api-upstream` passes on two identical socket lines (1.0.6.65 review R3-A1).**
  `.ai-dev/quality/checks/devices-api-upstream.sh:95-99` has no branch for more than one socket line although its
  header says «EXACTLY one». Add the else branch and a mutation case.
- [OPEN] 2026-09-30 **[LOW] INTERNAL_TOKEN reasoning restated in four places (1.0.6.65 review R4-A1).**
  `opt/sa02m-flasher/sa02m_flasher/config.py:52`, `service.py:331-336`, `service.py:345-351`,
  `etc/sa02m_flasher.conf:34-35` (the last still states the nginx overwrite unconditionally). Point them at
  `docs/decisions/selective-csrf-policy.md` «Демоны» instead of restating it.
- [OPEN] 2026-09-29 **[LOW] `test-web-auth.sh` section 8 comment still says «tiny window» (1.0.6.63 review A12).**
  Cases 57–61 now run with a 30 s window and 62 judges expiry with a 1 s window at check time; the
  section header above them should say so. Comment only.
- [OPEN] 2026-09-29 **[LOW] A partial section skip reads as a row PASS (1.0.6.62 review A3).**
  `test-web-update-launcher-guard.sh` runs L1–L7 everywhere but L8–L11 only where `flock(1)` exists; on
  git-bash the row prints «PASS (all checks; SKIPPED here: L8–L11 (no flock))» and the runner counts it as
  a PASS. The runner has two verdicts for a skip (whole-row exit 77) and none for a partial one. Fix
  direction: a third runner verdict (e.g. exit 78 = PASS-with-skips, printed and summarised apart), or
  split L8–L11 into its own row that exits 77 where `flock` is absent.
- [OPEN] 2026-09-29 **[LOW] Children spawned while the update lock fd 9 is held inherit it (1.0.6.62
  review A7).** In practice they cannot outlive the runner's re-take of the lock after `exec`; recorded so
  a later long-lived child (a background helper started from the launcher) is checked against it.
- [OPEN] 2026-09-29 **[LOW] Launcher-guard harness prose (1.0.6.62 review A8).**
  `scripts/dev/test-web-update-launcher-guard.sh` L9 comment: two lines over the ~125-char wrap, and a
  history paragraph («L9 was first built with the holder's pid…») that amends the «6 FAIL» record to 7
  instead of superseding it; the RED record already lives in commit `58306526`'s body. Fold into one
  current paragraph.
- [OPEN] 2026-09-29 **[LOW] `run.mjs --touched main` diffs two-dot `main..HEAD` (1.0.6.62 review A9,
  1.0.6.64 builder).** Once `main` moves past a branch's base, the selection includes main's own changes:
  it errs to more rows but misreports the branch's scope (81 files / 68 rows vs the true 6 files). Fix:
  resolve `--touched <ref>` against `git merge-base <ref> HEAD` (the `...` set) and print the base it used.
- [OPEN] 2026-09-29 **[LOW] One transient `comment-mutation-proof` baseline failure (1.0.6.62 review
  A10).** Under host load from parallel agents the review beat once printed «mqtt-set-contract is not green
  on an unmutated tree»; three direct runs and a full re-run («ALL OK — 68 comment-out mutation(s)») were
  green. Watch for a recurrence; then read the baseline run's time budget like the 2026-09-24 flakes.
- [OPEN] 2026-09-29 **[LOW] The offline update's `confirm_version` stage check still lists six stages
  (1.0.6.62).** Online «Применить» now refuses on every non-terminal stage (G7); the offline path's own
  stage check was not widened, because the launcher's lock already covers it. Recorded so a change to the
  offline launcher keeps that lock as its guard, or widens the check to match G7.
- [OPEN] 2026-09-28 **[LOW] Hardware watchdog held off from boot recover until verify completes
  (audit 2026-09-28 L5).** `recover_transaction` `verifying|committing` → `install_imaging_lock`
  (`etc/sa02m-update-runner.sh:2522`), then `schedule_boot_verify` returns with the lock kept
  (`:2582-2600`): the manager watchdog stays at 0 from early boot until `sa02m-update-verify` finishes
  (1–3 min on every recovered boot) — and open-ended if the verify unit is accepted (`--no-block`) but its
  ExecStart fails, since `cleanup_imaging_lock` runs only on the «cannot schedule» path (`:2597`). Fix
  direction: take the lock inside `cmd_verify` only (it already handles an absent lock, `:2631-2635`), or a
  bounded fallback; harness case in `test-update-recover-boot.sh`. Queued behind the 1.0.6.60–.68 train
  (2-agent cap, Operator 2026-09-28).
- [OPEN] 2026-09-28 **[LOW] Two layout nits seen in the 1.0.6.63 headless shots (pre-existing, not that
  change's).** (1) The E_CSRF widget line «Ошибка защиты сессии — повторите действие» wraps to two lines
  in the narrow «Обновление веб» card (the same string 1.0.6.53 G8 already ships there) — against the
  one-line-label rule (`web-code-rigor.md ## CSS / UI floors`). (2) Light theme at 1280 px: the «Создать
  резервную копию и установить» button text is clipped in the three-column layout. Evidence: session
  shots `A-proxy-toast-*.png`; `ui-layout` did not flag (2) — check why its intra-box clipping pass misses
  a button label before fixing. Queued behind the 1.0.6.60–.68 train.

- [OPEN] 2026-09-24 **[MED] `install.sh --port N` is not persisted — the next re-render resets nginx to
  9999.** `scripts/03-webserver.sh:12` and `scripts/11-devices.sh:18` default `PORT` to 9999 and render
  `etc/nginx/network_config.conf` with it; `scripts/update-www-only.sh` never sets it, and nothing saves
  the port chosen at install time. A board installed with `--port N` is switched back to 9999 by the
  next www-only deploy (or refresh without `--port`). Found by the 1.0.6.53 fixup review (round 5),
  verified by grep. Fix direction: persist the port (e.g. `/etc/sa02m_web.env` `SA02M_WEB_PORT`) at
  install and read it in both renderers; harness case. OTA consequence (audit 2026-09-28 L4): the
  runner's health gate hard-codes `http://127.0.0.1:9999/login.html` (`etc/sa02m-update-runner.sh:857`),
  so a board installed with `--port N` fails the gate and rolls back EVERY OTA — the persisted port must
  feed the health probe too.
- [OPEN] 2026-09-24 **[LOW] 1.0.6.53 review advisories left open.** A8: `docs/deployment.md` (nginx
  lanes bullet) cites `11-devices.sh:94-101` for the `nginx -t` + reload, which sit at `:102-108` — cite
  `:94-108`. A10: the backlog entry «The GitHub-OTA runner never deploys the nginx site config» mixes
  English and Russian (options (a)/(b)); make it one language (English, machine-facing backlog).

- [OPEN] 2026-09-16 **[HIGH] Audit 2026-09-16 (whole tree at 1.0.6.48) — H1: branch-protection floor half-wired and already bypassed.** Live `gh api …/branches/main/protection`: `quality` is required but `enforce_admins:false`, `strict:false`, no review rule; `1dfa503` (2026-09-10, «1.0.6.46», 13 files incl. `bridge_fmb.py`/`bridge_mr02m.py`) sits on `main` with NO PR (direct push, no review stamp evidence). `.ai-dev/notes/ci-budget.md:32` claims `enforce_admins: true` — false. Fix: set `enforce_admins=true` + `strict=true` on the forge (Operator's word), correct the note, retroactive Reviewer pass over 1.0.6.46's diff. Status 2026-09-24 (audit H1): unchanged — `contexts:["quality"]` present, `strict:false`, `enforce_admins:false`, no review rule; the 1.0.6.46 retro-review still owed.
- [OPEN] 2026-09-16 **[HIGH] Audit H2: GitHub Actions billing-locked since 1.0.6.24 (2026-08-28 20:47, run 33209713129), not 1.0.6.40.** Last green = 1.0.6.23. 25 releases + 3 side branches merged with the required check never executed; the substitute (local suite) runs on Windows where `shellcheck`, `ui-layout`, two `web-auth-behaviour` asserts and `install-atomic` 8b skip. Not recorded durably until now. Operator action: unlock billing; until then run the substitute under WSL/Linux where possible (node is absent in WSL today). Status 2026-09-24 (audit H1): still locked — the five PR runs #182–#186 each failed in 4–10 s and all five releases (1.0.6.49–.53) were admin-merged over the red required check.
- [OPEN] 2026-09-16 **[MED] Audit M7: verification coverage** — no dependency-CVE row (pip deps installed unpinned `scripts/lib.sh:739-763`; Node-RED payload; frpc) — re-rank the 08-28 LOW item; 16 mutating/root-capable CGIs (cmd_exec, hw_set, kernel_ctrl, reboot, restart, web_creds, web_factory_reset, storage_format_set, mqtt_config, mqtt_ctrl, cpu_profile, gateway_ctrl, web_update_upload/cancel, ssh_debug, web_backup) have zero behavioural test references. **Operator decision owed:** add `pip-audit` / `npm audit --package-lock-only` rows, or record the accepted risk (1.0.6.49 plan: no row until decided). The CGI half now has a shape to reuse — `cgi-csrf-behaviour` covers two of the 16; the rest stay uncovered.
- [OPEN] 2026-09-16 **[LOW] Audit M8 (advisory): module size** — 47 files > 800 lines (40 on 08-28): main.css 6055, flasher.js 5863, status.js 2584, smarthome.js 1873, config/api.py 1411, engine.py 1117; gate files too (test_binding_reset.py 1735, sh-modal-layout-smoke.mjs 1209, sudoers-pin-contract.sh 1148, ui-layout.mjs 1062). Decompose worklist (`.ai-dev/procedures/decompose.md`), by cohesion. Re-measured 2026-09-24: 56 files; the order lives in the «Decomposition worklist» entry (its one home).
- [OPEN] 2026-09-16 **[LOW] `tools/imaging/_run_reset_cloud.py` — keep or retire (Operator).** Tracked via the `.gitignore` exception (`:89-93`) since 1.0.5.64; used only by hand against the bench (audit L2).
- [OPEN] 2026-09-16 **[LOW] The firstboot overlay carries the DNS-belt callers but not the belt.** `tools/imaging/firstboot-overlay/` has `sa02m-eth-coldboot.sh` and `fix-eth.sh` with `dns_ensure` (synced 1.0.6.49) but no `usr/local/sbin/sa02m-dns-ensure.sh`; `dns_ensure` guards on `-x`, so a clone flashed from a pre-1.0.6.6 image gets the guard, not the belt. Fix: add the helper to the overlay + its PAIRS row in `firstboot-overlay-parity` (audit M1 follow-up).
- [OPEN] 2026-09-16 **[HIGH] Rebuild the board kernel `6.1.0-rc6` with the upstream ext4 patch
  «ext4: fix bad checksum after online resize»** (Baokun Li, 2022-11-16, `fs/ext4/resize.c`
  `ext4_update_super`; Fixes: de394a86658f). The 1.0.6.47 `ensure_primary_sb_checksum()`
  freeze/verify in `etc/sa02m-rootfs-expand.sh` is a userspace workaround for the first-boot
  case only; ANY future online resize on the board (a manual `resize2fs`, a re-imaged larger
  eMMC) re-opens the power-cut window until the kernel carries the fix. Durable fix = the
  kernel line (RT and SMP zImages both, `sa02m-kernel-select.sh` swaps them). Evidence and
  the field symptom: `docs/bugs/BUGLOG.md` 2026-09-16.
- [OPEN] 2026-09-16 **[HIGH] Root fs `commit=600` + superblock default `journal_data_writeback`:
  up to ~10 min of unsynced writes are lost on a power cut.** Bench 2026-09-16 (reflashed
  clone, cut at 15:21): `/var/log/sa02m-rootfs-expand.log`, `/var/log/sa02m-reboot-reason.log`
  and the persistent journal files under `/var/log.hdd/journal/<machine-id>/` all came back
  0 bytes («truncated, ignoring file»), journald fell back to `/run/log/journal`. Not
  logrotate (no rule for these files). Web-saved configs (`/etc/sa02m_*.conf`, MQTT/gateway
  YAML, Alice/cloud state) are exposed the same way unless their writers fsync — a config
  saved and power-cut inside the window silently reverts or truncates. 1.0.6.48 made the
  first-boot verdict durable by targeted `sync FILE`; the policy itself is the Operator's
  decision: wear vs durability — `commit=5..30` and/or `data=ordered` in the image's fstab /
  `tune2fs -o` defaults, or targeted fsync in every config writer (the
  `sa02m_atomic_install` shape, BUGLOG 2026-09-08). Record: `docs/bugs/BUGLOG.md` 2026-09-16 16:40.
- [OPEN] 2026-09-23 **[LOW] Follow-ups found while shipping 1.0.6.50 (Carel fan) — none is in that
  release, each is its own change.**
  1. **Observability: the Alice client logs no POSITIVE line when it builds a catalogue.** After an
     install, the only board-side evidence that the new code runs is indirect: the client's
     restart time, the ABSENCE of the fail-soft «sa02m_carel not importable» warning, and re-running
     the Discovery generator against the installed /opt trees. Those prove the running process
     loads the right code, not that it emitted the control. A one-line INFO on catalogue build
     (device count, capability types per device) would make post-install verification direct.
  2. **The «Умный дом» window writes an undocumented `parameters: {"instance": "on"}` on every
     `on_off`** (9 of 12 capabilities on bench 1.135). Yandex's Discovery schema defines one
     optional `on_off` parameter, `split`; `instance` belongs in the STATE object. The Operator's
     app check on 2026-09-23 shows Yandex tolerates it silently (15 of 15 devices visible,
     control works), so this is cleanup, not an outage. Fix centrally in `discovery_devices` so
     documents already saved on boards are covered too.
  3. **Setpoint `range` is 0..99 for both Carel families; uAria's real ceiling is 50 °C.** The
     bridge clamps at write time (70 is written as 50), so this is presentational: the top of the
     slider maps to one setpoint. It was unfixable while the window could not tell the family;
     1.0.6.50 introduces the family resolver, so the window can now narrow it. Same window as 2 —
     one branch. `random_access` is the documented key if the setpoint should step, not jump.
  4. **`ui-layout` binds a fixed port (8902, `UI_LAYOUT_PORT`).** Two concurrent quality runs on
     one machine make the second fail with EADDRINUSE, reported as a red `ui-layout` row that is
     indistinguishable at a glance from a layout regression (hit 2026-09-20). Either pick a free
     port or report a bind failure as an infrastructure error, not a gate FAIL.
  5. **«LED лента» brightness and colour are `cloud_only`**, so Alice can only switch it on/off.
     Likely deliberate, but brightness is bound as `range` 0..255 with `unit.percent`, which
     Yandex would not accept as-is (percent is 0..100). Operator's call whether Alice should dim it.
  6. **Wording advisories from the 1.0.6.50 final review** (`.ai-dev/reviews/1.0.6.50_review.md`,
     transient): A1 `carel-ahu.md` §6 «установлено независимо» carries a recovery clause its cited
     page does not support — end the sentence earlier or point it at the cloud conclusion; A2 the
     stated reason for keeping the §6 retraction is weak — the strong reason is that the claim lives
     in the cloud team's repo, where readers of this contract may have met it; A3 two lines over
     80 columns (cosmetic, no limit configured). Also carried: `test_device_registry.py` is ~1000
     lines, a split candidate.
- [OPEN] 2026-09-23 **[FEATURE, Operator-approved 2026-09-23] Alice dims the «LED лента» —
  its own release, after the 1.0.6.52+ fix series.** Today brightness and colour are bound
  `cloud_only`, so Alice only switches the strip on/off. The brightness row on bench 1.135 is
  `range` 0..255 with `unit.percent`, which is board data, not repo code, and Yandex would not
  accept it (percent is 0..100). Needs a percent↔raw scale on `range` bindings: validator,
  converters, the «Умный дом» window, `docs/contracts/alice-mqtt-mapping.md`, and
  `led-mb2ws.md` where it applies. Design and evidence: the 1.0.6.51 plan's F-C5(b); the plan
  is transient, so re-derive.
- [RESOLVED 1.0.6.60] 2026-09-23 **[LOW] `scripts/sync-app-version.py` writes CRLF on Windows.** It calls
  `write_text` without `newline="\n"`, so a version sync run from a Windows checkout leaves the
  version homes (VERSION, index.html, login.html, three bundles) CRLF in the working tree. On
  1.0.6.51 the Builder converted them back to LF by hand. Git normalises on commit, but any local
  gate that reads the working tree, and any pscp-style delivery, sees CRLF. Fix: pass
  `newline="\n"` on every write, and add a harness case that runs the syncer and asserts LF.
  Queued for R58 (gates and tools).
- [OPEN] 2026-09-23 **[MED] Armbian's log hooks move the journal to RAM every 15 min and
  truncate it — the repo's `Storage=persistent` promise does not hold for `journalctl -b -1`.**
  `etc/systemd/sa02m-journald.conf` (installed by `scripts/01-system.sh`) promises the journal
  survives a reboot. Measured on 1.135: `armbian-ramlog.service` is DISABLED and `/var/log` is on
  the eMMC (ext4), but the cron hook `armbian-truncate-logs` still runs every 15 min, calls
  `journalctl --relinquish-var` (journal goes volatile to `/run/log/journal`, `RuntimeMaxUse=16M`);
  it truncates and vacuums only at ≥75 % disk use (1.135 is at 64 %). The persistent journal is
  written back only at the midnight flush (logrotate's `armbian-ramlog write`). Result: the
  journal loses the middle of the day under a noisy bus (gaps 22 Sep 00:00→18:37, 23 Sep
  00:00→06:00), and a power cut loses the day FROM THE JOURNAL VIEW. The text `/var/log/syslog`
  on the eMMC still has every line (5,196 lines for 22 Sep 12:00), so the data is not lost. Only
  the `journalctl -b -1` post-mortem the drop-in advertises is. (First filed 2026-09-23 as [HIGH]
  «RAM-only until midnight, a power cut loses the day»; that premise was wrong. Corrected the
  same day by the 1.0.6.51 planner's measurement and re-checked by the orchestrator.) Fix in
  1.0.6.51 (Operator decision A-1: Armbian hooks off, journal persistent, 1-min sync; reaches a
  board only via a full install or the next golden image, not OTA).
- [RESOLVED 2026-09-29 — wiring: the CE-02m3's second RS-485 port; see DATA 2026-09-29] 2026-09-23 **[MED] Bench 1.135 COM3 answers ~500 short/CRC polls per hour, bus-wide, and
  the flood caps journal retention at ~1.5 days.** Measured after the 1.0.6.50 deploy, rate unchanged
  across it (not caused by it): carel-COM3-1/-2, mr02m-COM3-10, led-COM3-13 all log `Short response`
  / `CRC mismatch`; COM1/2/4/5 together log ~11/h. Only the bridge (PID of `modbus_mqtt_bridge.py`)
  holds `/dev/ttyS4`; mplc4 holds no tty, the flasher is idle — so no second master ON THE BOARD.
  Open: a master elsewhere on the wire, termination/wiring, or one babbling device (unplug-one-at-a-
  time settles it). Consequence for 1.0.6.50: the write-back audit line lives in a journal that
  loses the middle of each day — the mechanism and its correction are in the journal entry above
  (armbian-ramlog is DISABLED; the 15-min `armbian-truncate-logs` hook relinquishes the journal to
  `/run`, `RuntimeMaxUse=16M`; midnight flush). Inferred, fits both observed gaps (22 Sep
  00:00:18→18:37, 23 Sep 00:00→06:00): at ~6,460 lines/h the 16M runtime journal holds ~5–6 h, so
  a write-back audit line survives ~5–6 h under this flood, not a day. Journal side fixed in
  1.0.6.51 (A-1); the bus itself: Operator decision «data first» — B3 diagnostics ship in
  1.0.6.51, the COM3 fix follows in 1.0.6.52 on 1 h of bench data. «22 Sep 18:00» is a retention
  edge, not the onset (cloud session + re-checked here).
  DATA 2026-09-24 10:54 (B3 capture, bench on 1.0.6.53): UART framing errors 20–51/min on COM3 only
  (the other four ports ~0); errors per hour by device: mr02m-COM3-10 652, carel-COM3-1 368,
  carel-COM3-2 336, led-COM3-13 123. Framing errors are a line-level symptom (baud, termination,
  wiring, a talker off-board), not a software one — no code change is indicated. RE-TARGETED: the next
  step is a person at the stand — unplug one device at a time and re-sample the framing-error rate;
  no release carries this until that data exists. The Carel in-reply-pause loss (its own entry) is a
  separate, software cause.
  DATA 2026-09-29 — ROOT CAUSE FOUND. A line scan (all five ports × 2400–115200 × N/E × addr 1–15) found
  TWO device groups on the COM3 wire: 19200 (Carel 1/2, MR-02m 6AI6AO @6 — then not in the bridge
  config, MR-02m 6DO8DI @10, LED @13) and 115200 (16DO @2, 6DO @4, 14DI @5, CE-02m3 @14, SENSOR @15 — none
  configured on COM3). The CE-02m3 has two RS-485 ports: port 1 on COM2 (polled by the bridge at 115200),
  port 2 on COM3 (the Operator's hypothesis). Controlled test, bridge stopped, clean 19200 polling on COM3
  in 45 s phases: COM2 silent → 1527 clean / 0 bad, fe +0; the CE-02m3 polled on COM2 → 325/366, fe +179;
  silent → 1530/0; polled → 290/384, fe +161 — answering on port 1 drives the COM3 line through port 2.
  The Operator disconnected port 2 and wired the 19200 chain to COM3 directly: the same test 1524/0 ·
  1490/0 · 1540/0 · 1498/0, fe +0 in every phase; real bridge 3 min: Short/CRC 0 on all COM3 devices and
  on the CE, COM3 fe 0/min (was 20–92/min), poll cycle ~270 ms (was ~290). Rejected on the way: a foreign
  master (passive sniff: 0 bytes in 32 s), Fast Modbus scanning on COM3 (off 5 min: fe unchanged), the
  devices themselves (600 clean exchanges each). Follow-ups: the defect report went to the CE-02m-3
  firmware session (2026-09-29); the wiring warning is in README («Modbus→MQTT мост»); the 6AI6AO @6 was
  added to the bench bridge config the same day.
- [OPEN] 2026-09-09 **[MED] An Alice «включи» is answered DONE while `sa02m-rules` is down.**
  The registry publishes `/devices/sa02m-rules-<sid>/controls/run/on` and reports success;
  with the engine stopped the publish is simply lost and the user gets «сделано» for a
  scene that never ran (1.0.6.41, scenes as devices). Fix direction: an LWT/availability
  topic for the engine that the registry reads before answering.
- [OPEN] 2026-09-09 **[LOW] `upsert_room` with an unknown `id` CREATES that room**, while
  `apply_rooms` answers `not_found` for the same id — an asymmetry between the two room
  writers (found while fixing the rename wipe, 1.0.6.41). Deliberately unchanged: which
  one is right is a contract call (does the cloud hub rely on create-by-id?), not a
  defect fix. `docs/contracts/alice-mqtt-mapping.md` §Room membership is the home.
- [OPEN] 2026-09-10 **[MED] A machine-rate `beeper` spawns one override worker per accepted
  command, with nothing collapsing them.** Each accepted command on a held bus starts
  `sa02m-beeper-override.sh` detached; N commands inside one 7 s TTL leave N concurrent shell
  loops polling i2c every 0.2 s on a shared ARM target that also runs MPLC4 and CODESYS. It
  MIRRORS the reference — `sa02m_hw_beeper_override_start_worker` does the same — but the
  reference is driven by a human clicking a button, and this path is driven by the cloud, Alice
  and scenarios, i.e. machine-rate. Not anonymously reachable (1883 is loopback-only, 1884 needs
  auth), so this is load, not a security hole. **The obvious fix is forbidden by the accepted
  design:** the daemon must not assume it is the only producer, so it cannot track «my worker»
  and skip. Any real fix is a change to the worker's own contract — e.g. it takes a lock and a
  second instance exits — which is `etc/sa02m-beeper-override.sh`'s to make, not the daemon's.
  Recorded because it is an ACCEPTANCE nobody had written down. Found by the 1.0.6.43 ship review.
- [OPEN] 2026-09-10 **[LOW] The daemon's `makedirs` fallback could root-own the shared override
  directory.** If `/run/sa02m-hw-override` is ever missing when the telemetry daemon writes the
  beeper override, root creates it and the www-data CGI can no longer stage its temp file there —
  the PANEL's own override would start failing, having been broken by the daemon. Unreachable on
  any board that has the feature: `scripts/03-webserver.sh` installs a tmpfiles.d entry
  `d /run/sa02m-hw-override 0775 www-data www-data`, recreated every boot. The Builder mirrored
  the CGI rather than hard-coding a second home for that ownership and recorded the consequence
  at the site. Found while building 1.0.6.43. **The 1.0.6.43 review sharpened this:** the
  «unreachable» reasoning is true today but NOTHING KEEPS IT TRUE — delete that tmpfiles.d line
  and this finding goes live with every quality row still green. Whoever closes this should
  either pin the tmpfiles.d entry or stop depending on it.
- [OPEN] 2026-09-10 **[LOW, Operator's call] After taking the override path the daemon publishes
  the COMMANDED `controls/beeper` value, not a measured one.** It did not drive the pin — the
  worker does, later — so the retained value is a claim about a byte this process never wrote.
  Mitigations already in place: the journal line is deliberately distinct (`via the override
  file … holds the expander`), the CGI returns success on the same path, and the ≤30 s poll
  republishes the measured level. Publishing nothing instead is a one-line change; which is
  right is a product question, not a defect. Found while building 1.0.6.43.
- [OPEN] 2026-09-10 **[LOW] An explicitly BLANK `SA02M_I2C_EXTRA_OUTPUT_MASK` resolves to 0 in
  the CGI and 0x08 in the daemon** — the 1.0.6.42 fight in reverse, at a different boundary.
  `lib_hw.sh:34` applies its `:-0x08` and `:36` then SOURCES the conf, so an empty assignment
  overwrites the default and `sa02m_hw_i2c_extra_output_mask_dec`'s `${…:-0}` yields 0; the
  daemon's `_hw_extra_output_mask` returns the 0x08 default for blank as well as absent. Nothing
  shipped writes an empty value, so no board is in that state — which is why it was recorded
  rather than silently made to match after the release was stamped. Resolving it is a choice
  about which consumer moves: matching the CGI drops bit3 (KLogic's blue LED) on both when a
  conf carries a blank line, matching the daemon keeps it. Found by the 1.0.6.42 round-2 review.
- [OPEN] 2026-09-10 **[MED] The daemon's conf-key ledger is a TEXT SCAN for one idiom, not an
  enumeration — and the next queued fix walks straight into its blind spot.** The pin added in
  `680ebe7` finds keys with `re.findall(r'val\("(SA02M_[A-Z0-9_]*)"', src)`. The 1.0.6.42 round-2
  reviewer defeated it three ways, each leaving the ledger GREEN: `_read_conf_value(path,
  "SA02M_…")` directly, `val('…')` with single quotes, and a key assembled in a variable. **No
  key is unpinned today** — the only `_read_conf_value` call sites are the two the `val` closure
  wraps — which is why this did not block 1.0.6.42. The trap is the entry below: `_i2cget` and
  `_i2cset` are module-level and cannot see the `val` closure, so reading
  `SA02M_I2C_TIMEOUT_SEC` there naturally uses `_read_conf_value` — a seventh mirrored default
  that the ledger would not see while the registry still promises coverage. **Fix the two
  together, on one branch**, and make the scan see every read idiom (or make the code use one).
- [OPEN] 2026-09-09 **[LOW] The telemetry daemon hard-codes its I2C subprocess timeout instead
  of reading `SA02M_I2C_TIMEOUT_SEC`** — `_i2cget`/`_i2cset` pass `timeout=1`, a second copy of a
  conf value that happens to equal the shipped default. A board that raised it would have the
  CGI waiting 3 s and the daemon 1 s on the same bus. Found while fixing the channel map
  (1.0.6.42); it is the same one-home defect class as the map itself, one layer down. Fix is to
  read it where the rest of the profile is read.
- [OPEN] 2026-09-09 **[LOW] The telemetry daemon does not read back after a hardware write.**
  `lib_hw.sh` verifies the output register after writing it; the daemon publishes success on the
  `i2cset` return code alone. On the byte that carries the discrete output, «the write returned
  0» and «the pin moved» are not the same claim — this release's whole subject is the gap
  between them. Found while fixing the channel map (1.0.6.42).
- [OPEN] 2026-09-09 **[MED, peer observation — not verified by me] `sa02m-cloud-control` on
  bench 1.135 drops its connection with «lib:transport error» every 10-70 min all day, and at
  13:04:47 systemd killed it on its stop timeout; load average ~6.** Reported by the peer session
  «lighting-module-diagnostics» (cloud repo) while working read-only on 1.135. Recorded as
  theirs, not re-derived here. Two notes: the 13:04:47 kill is the same window in which bench
  1.136 lost power, and 1.135's install was restarting `sa02m-modbus-mqtt`, `sa02m-telemetry`,
  `sa02m-alice-client` and `sa02m-cloud-control` between 13:03:44 and 13:04:22 — so the kill is
  plausibly the install's own restart hitting a stop timeout rather than a standing defect. A
  load average of 6 on a 491 MB board with no swap and no zram is worth its own look; part of
  today's was mine (the Carel pivot measurements).
- [OPEN] 2026-09-09 **[MED] The Carel long→wide pivot holds a write lock longer than the
  logger's 30 s timeout on a multi-million-row archive** — measured on bench 1.135 with its
  real archive (2026-09-09): the cost is linear at ~23 µs/row (125k → 2.8 s, 250k → 6.9 s,
  504 903 → 11.5 s), so `sqlite3.connect(timeout=30)` in `history_store._connect` covers an
  archive up to roughly 1.3M rows. A full 30-day Carel archive is several million rows — the
  figure this release's own CHANGELOG named — where the one-time migration would hold
  `BEGIN IMMEDIATE` for ~1–1.5 min: the 1 Hz logger's writes raise `database is locked` and
  those ticks are lost, and an archive read from the web UI fails in the same window. Not a
  data-integrity risk: the pivot is atomic, `carel_samples_v1` is intact, and what is lost is
  individual 10 s samples. Fix shape: migrate in bounded chunks (commit per N ticks, the
  `metric` column staying the resume marker) so no single lock is long, or give the migration
  window its own longer busy timeout. The measured bound is now stated in
  `docs/contracts/carel-ahu.md` and `CHANGELOG.md` rather than promised away.
- [RESOLVED 1.0.6.60 — both sites atomic; the codemod docstring is the record] 2026-09-09 **[MED] Two live-path `install -m` sites under `etc/` remain**, and
  neither is blocked by a missing helper — **my earlier record here was false**: it claimed
  closing them «needs the helper duplicated into a device-side lib», but `atomic_install_file()`
  already existed at `etc/sa02m-update-runner.sh `atomic_install_file()`` (used its two callers) and
  `etc/sa02m-factory-reset-runner.sh `atomic_install_file()`` (one caller), and `etc/sa02m-web-update-apply.sh:59`
  now carries a third. The three are NOT byte-identical and carry no `cmp` pin: each is scoped
  to its own caller's duties (the factory runner adds a destination allow-list and a rollback
  journal; the OTA one adds CRLF normalisation), which is why a further copy is a decision, not
  a formality.
  - `etc/sa02m-web-service-ctl.sh:1359` → `/etc/systemd/system/nodered.service` — the
    incident's own shape. Survives because this file carries no atomic helper yet.
  - `etc/sa02m-update-runner.sh `rollback_from_journal()`` → `"$rel"`, an absolute path replayed from the
    pre-update rollback archive, whose members are the manifest's `deploy[].dst` entries
    (`build_rollback_archive`, `:968-985`) — so `/usr/local/**` and `/etc/systemd/system/**`
    are exactly what it restores. **Survives only because nobody looked:** this file DEFINES
    `atomic_install_file` 283 lines above, so the conversion needs no new helper — and this is
    the site that runs when the board is already mid-failure. Highest-value of the two.
  My earlier list was wrong in both directions. Not live paths, so outside the rule rather than
  exceptions to it: `sa02m-web-service-ctl.sh:895,898` → `/opt/mplc4/*.so`;
  `sa02m-commit-web-env.sh:14` → `/etc/sa02m_web.env`; `sa02m-web-update-apply.sh:396,432`
  (I recorded `:316,352`) → `/etc/tmpfiles.d/*` and `/etc/sudoers.d/sa02m-www`. And
  I also recorded `sa02m-update-runner.sh:394` as a live-path site; it was wrong on both axes.
  The `install -m` is in `self_reexec_before_deploy()`, and its destination is
  `"$STATEDIR/runner/$txn/runner"`, a per-transaction scratch self-copy exec'd immediately —
  not a live path at all. **Cite the symbol, not the line:** every line number in this entry
  went stale at least once while the entry was being corrected, twice inside the commit that
  corrected it. Full enumeration, including the sites that ARE the atomic staging
  write: the docstring of `scripts/dev/codemod-install-atomic.py`, the one home of «which
  install sites are live-path».
- [OPEN] 2026-09-09 **[LOW] `scripts/update-www-only.sh`: the non-unit, non-`/usr/local`
  `install -m` sites are still non-atomic** — widen the codemod's `LIVE_PREFIXES` or
  record why those paths are not live-path.
- [OPEN] 2026-09-09 **[LOW] The 1.136 `busctl` write is unverified.** 8D step G reports
  the value in force rather than assuming it, so it is safe either way; read 6 of the
  8D (does the manager accept `RuntimeWatchdogUSec`) is the only unconfirmed half.
- [OPEN] 2026-09-09 **[LOW] `carel_samples_v1` is dropped in 1.0.6.42.** The wide-table
  pivot of 1.0.6.41 keeps the old long table as a one-release rollback path; the drop
  (plus the `CAREL_METRIC_AGG` vocabulary constant if it still has no reader) belongs to
  the next release. Bench 1.135 carries **504 903** rows of it (measured 2026-09-09,
  `a88190e`); the earlier «~75k» here was a dev-host fixture figure, not the board.
- [OPEN] 2026-09-09 **[MED] The scenario sandbox is an AST denylist in front of a real
  CPython interpreter, not isolation.** Every known escape is closed (1.0.6.39 banned
  `.format`/`format_map` — the reproduction `'{0.text.__globals__}'.format(Notify)` now
  answers `banned attr`; 1.0.6.41 added the per-run HTTP cap, the `pub` fence and one
  namespace), but a new introspection route without `_`, `format` or `getattr` is not
  excluded: the body still runs as root in the daemon's own process. Deep options, both
  the Operator's call: run `type=code` in a bounded child process (seccomp/`setrlimit`,
  no network, IPC to the engine), or drop `type=code` in favour of the block/logic
  templates the cloud editor already builds. Found with the cloud session, 2026-09-09.
- [OPEN] 2026-09-08 **[LOW] LED window: PWM safe-state 503..506 not exposed.**
  The daemon has no read/write path for the family safe-state AO block (plan
  led-window-1.0.6.40 F4); the desktop page shows it. Add an FC03 of 4 to the PWM
  poll + `pwm {channel, safe}` + tests when wanted.
- [OPEN] 2026-09-08 **[LOW, DEFERRED by the Operator 2026-09-23 until a customer needs it] LED window: EXFX upload has no tab.** Needs the
  holding-4000+ upload protocol, a file path through nginx and a daemon job with
  progress (F2) — a release of its own; the desktop's «Загрузить и пуск» is the
  target.
- [OPEN] 2026-09-08 **[LOW] LED window: weather-listen binds only in the spy
  card.** The daemon reads 696..709 for effect 81 alone (F7); the desktop also shows
  them on the weather card (fx 64). Add a read for fx 64 if operators ask.
- [OPEN] 2026-09-08 **[LOW] Config windows: an F5 mid-write leaves the port's
  pollers released.** The daemon finishes the write under the lease, but the reload
  drops `configPortReleased`, so MPLC4/bridge stay stopped until the next
  open/close of any config window (pre-existing class for every kind; noted in plan
  led-window-1.0.6.40 §3).
- [OPEN] 2026-09-08 **[LOW] `.flasher-config-form input { width: 100% }` stretches
  checkbox/radio boxes.** Seen on the LED window's first render (label text pushed
  off the card); the LED rows now use `.cfg-led-check`. The Carel/MR windows'
  `.checkbox-line` rows sit under the same rule — check their screenshots.
- [OPEN] 2026-09-08 **[LOW] `#web-upd-apply-btn[hidden] { display:none !important }` has no
  driver** (audit C14): the CSS guard for the reported 1.0.6.37 bug is defence-in-depth
  nothing exercises; `test-web-update-semver.mjs` covers the JS half only. Also
  pre-existing: light `.btn-warn:hover` = 4.44:1 (`#b45309` on `#fff0cc`), just under AA;
  `ui-layout` never measures hover.

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
- [OPEN] 2026-09-24 **[MED, Operator decision] The GitHub-OTA runner never deploys the nginx site
  config.** `etc/sa02m-update-runner.sh` `prepare_github_overlay` → `map_dst` has no branch for
  `etc/nginx/` (`DST_RE` admits the destination, the filter is map_dst AND DST_RE). The site file
  reaches a board by every OTHER lane — the one-home list is `docs/deployment.md` «Чего
  OTA/офлайн-пакет не делает никогда» (full install/refresh via `03-webserver.sh`; www-only from a
  checkout with `etc/` + `opt/sa02m-devices/` via `update-www-only.sh` → `11-devices.sh`; the offline
  `.sa02m` package; the golden image) — never GitHub-OTA. Measured 2026-09-24 on 1.135: after the
  panel OTA 1.0.6.52 → 1.0.6.53 the `X-SA02M-Auth` strip is absent under /etc/nginx while the repo
  gate is green. Consequence: every nginx-level hardening is invisible to boards updated only through
  the panel. Fork for the Operator:
  (a) Расширить `map_dst` раннера веткой для `etc/nginx/network_config.conf` **с шагом рендера**: файл в
  репо — шаблон с `__PORT__`/`__WEB_ROOT__` (рендерят `scripts/03-webserver.sh` и
  `scripts/11-devices.sh:94-97` через `sed`), голая ветка положила бы литеральные плейсхолдеры в
  `/etc/nginx/sites-available/network_config`; `nginx -t` + `reload` раннер уже выполняет в health-gate
  и rollback, так что добавляется только рендер — и решение Оператора, что GitHub-OTA получает право
  переписывать конфиг edge (security surface).
  Cloud team's note (2026-09-24): with the cloud forwarding the whole `X-SA02M-` family, the one case
  where that forwarding stops being harmless is a board updated ONLY through the panel OTA (no nginx
  strip) on which someone set a non-empty `INTERNAL_TOKEN` for the flasher — weigh it in this fork.
  (b) Оставить как есть и держать одним домом в `docs/deployment.md`: site-файл nginx приезжает полной
  установкой (`03-webserver.sh`), www-only из чекаута с `etc/` + `opt/sa02m-devices/`
  (`update-www-only.sh` → `11-devices.sh`), офлайн-пакетом и образом — никогда GitHub-OTA; правка
  nginx, которая должна дойти до флота, едет одним из этих путей. Until decided, the texts say (b).
- [RESOLVED ci-linux-green] 2026-09-24 **[LOW] Two load-induced harness flakes.** (1) `test-web-update-apply-guard.sh`
  R1: the live-runner fixture was `sleep 30`; under quality-runner load section R reached it 44–50 s
  later → false RED. FIXED in 1.0.6.52 (`sleep 900`). (2) `test-web-auth.sh` case 59 «NOT locked
  after MAXFAIL failures — brute force is unthrottled» FAILED once inside a full `build` beat on
  2026-09-24 (87/88) and passed on the immediate re-run and every later run — a timing window of
  the throttle test under load, class (1); not reproduced, not investigated. When it recurs: read the
  case's time budget against the throttle's window and pin the fixture like R1. RECURRED 2026-09-28
  three times under load (1.0.6.58 builder; 1.0.6.60 reviews rounds 2 and 3; standalone re-run green each
  time) — the 2 s lockout window is the suspect. FIXED 1.0.6.63: the window was the cause (three
  failures + the check outran 2 s under a loaded build; 3 of 4 standalone runs red on 2026-09-29) — the
  harness now uses a 30 s window for 57–61 and judges 62 with a 1 s window at check time, after an
  explicit «locked» precondition. (3) `test-web-update-apply-guard.sh` rows 7, R4 and H1/H1b
  fail under WSL on `d66d7b6` as well (1.0.6.62 builder + reviewer, twice) — the ~1.2 s sudo shim appears to
  leak into the next row. Item (3) is still queued behind the 1.0.6.63–.68 train (2-agent cap).
  RESOLVED 2026-09-30 (branch ci-linux-green): (1) and (2) were already fixed (1.0.6.52, 1.0.6.63); (3) was
  case 5's launch stand-in still holding the lock on fast Linux hosts and reboot.cgi's real background
  chain firing on root hosts — apply-guard now waits for its stand-ins and drives a sandboxed reboot.cgi.
- [RESOLVED 1.0.6.65] 2026-09-23 **[MED] `sa02m-devices-api` listens on `127.0.0.1:8765` with no auth of its
  own.** `opt/sa02m-devices/sa02m_devices/api.py` reads only Content-Length and relies entirely on
  nginx's `auth_request` in front of `/api/devices*`; any local process or user on the board can
  open the loopback port and bypass the panel's session. Found while sweeping the `X-SA02M-Auth`
  class (1.0.6.53): a different class (unauthenticated loopback TCP), the same shape the Alice config
  API had before it moved to a root-only unix socket (1.0.6.24, `88032f4`). Fix direction: the same
  move (AF_UNIX socket, 0660 root:www-data) or a shared local secret set by nginx only. Threat model
  row to add with the fix.
- [RESOLVED 1.0.6.63] 2026-09-23 **[LOW] XHR upload paths have no CSRF refresh-and-retry of their own.** The
  panel's two XMLHttpRequest uploads (`status.js` ~:1756 and ~:2318, offline package / MPLC project)
  read the refreshed token but bypass the fetch wrapper, so an E_CSRF there still ends the upload
  with a plain error instead of the 1.0.6.53 refresh-once-retry-once path. Route them through the
  same reaction or document the difference in `docs/contracts/cloud-panel-proxy.md`.
- [OPEN] 2026-09-23 **[LOW] Known limit: the delivering GitHub OTA on a ≤1.0.6.51 board still
  freezes at 85 %.** `self_reexec_before_deploy` copies the INSTALLED runner and execs the copy,
  so the whole delivering apply (health gate included) runs under the OLD code and dies at
  `restart fcgiwrap`; the NEW runner, verify unit and CGI are on disk, so the panel says
  «Обновление прервано … перезагрузите плату» after 120 s and the next boot (or
  `scripts/sa02m-update-remedy.sh`) completes it — every update after that runs whole. No
  lever in the old code (no fcgiwrap drop-in route in the old map, empty migrations, fixed
  restart[]). Named in `docs/deployment.md` «Пути деплоя»; closes itself once every board
  runs ≥ 1.0.6.52. Alternative for a visited board: offline full update or a `.sa02m` package.
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
  **Widened 2026-09-08 (audit F13, incomplete-enumeration shape):** the same
  factory default is also spelled out in `docs/AGENTS_SSH_AND_DEVICE_ACCESS.md:14,15,62`
  and `docs/OFFLINE_UPDATE_HW_TEST.md:58` (bench-access docs; the value is the
  product's published default, `README.md:211`) — this accepted-risk record now
  names those homes too. The fourth copy, `.cursor/rules/sa02m_agent_ssh.mdc`,
  is cut in 1.0.6.39 (pointer only).
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
- [OPEN] 2026-08-24 **[LOW] HW-variant auto-detect flake (1eth ↔ 2eth).** The 2-eth bench board reported `SA02M_HW_VARIANT=sa02m-1eth` on one boot and `sa02m-2eth` on the next (both eth0+eth1 present physically). Detect-by-physical-eth-count is racing something at boot. Investigate the detection ordering vs PHY/link readiness.
- [OPEN] 2026-08-24 **[MED] make-image.sh strands the donor after capture.** It strips SSH host keys as its last pre-`dd` step but never reboots the donor in the still-live session, so post-capture the board is unreachable (sshd has no host keys, panel stopped) until a manual power cycle — a problem on a remote bench. One line: schedule a detached reboot at the end of the stream session (rc.local already regenerates keys at boot).
- [OPEN] 2026-08-27 **[MED] «Время без опроса» accepts a value that silently breaks outputs, with no warning.** MR-02m holding 134 clears ALL of a module's outputs after N seconds without a frame that resets its inactivity counter (five reset sites, only one of them address-matched — the table is in the note named below). The module-config window (`flasher.js`, `saveMrGlobalInactivity` + the per-AO field + the bulk template-apply path at ~4322) offers the raw range 0–255 with no guidance, so a value shorter than one bus sweep — 1 s on a line the bridge polls round-robin — makes every output fall by itself during normal operation. Cost a full firmware-update cycle and hours of bus tracing on bench 1.135 before the register was read (root cause + the A/B/A proof: `.ai-dev/notes/mr02m-inactivity-timeout.md`). Fix candidates: warn (do not block) below a threshold derived from the port's device count and poll period; surface the current value in the module card next to the DO states; make the template-apply path name this field explicitly in its confirmation, since it copies it onto other modules. Fold in round-1 finding 10 while there: neither `docs/agent-rules/web-diagnostic-tools.md` nor `docs/agent-rules/sa02m-domain.md` points at the note, so the symptom->tool dispatch still cannot route "an output falls by itself".

- [OPEN] 2026-09-24 **[MED, Operator decision] The two HTTP daemons are outside the CSRF policy
  (audit 2026-09-24 M3).** `docs/threat-model.md:291` says `X-SA02M-CSRF` covers «ВСЕХ» session-authed
  mutating endpoints; the flasher daemon (`service.py:345-365`: `/flash`, `/flash_batch`,
  `/ports/release|restore`, `/firmware/*`, `/device_config/network`) and devices-api (`api.py:311-323`:
  `widgets/add|remove`) are session-authed behind `auth_request`, mutating, and carry no token and no
  recorded exemption; the frontend sends none (`flasher.js`, `flasher/*.js`, `devices.js`).
  `_read_json_body` parses any Content-Type, so a `text/plain` form POST needs no preflight. What
  holds: `SameSite=Lax` blocks cross-SITE POSTs; the residue is same-site origins on the board's IP
  (other ports, e.g. Node-RED `:1880`). `/ports/release` stops field polling; `/flash` is
  irreversible. Options: (a) extend the token to both daemons (they already read the session file; the
  `<hash>.csrf` sits next to it), or (b) record the exemption with its reason in
  `docs/decisions/selective-csrf-policy.md` and correct the threat-model line. Not the devices-api
  loopback entry (a different class).
- [OPEN] 2026-09-24 **[LOW, advisory] CHANGELOG release prose is heavy (audit L8).** 1.0.6.52 is 176
  lines, .51 115, .53 92; the file is 6,128 lines. A release entry states user impact; the mechanism
  lives in the contract or the commit.

- [OPEN] 2026-09-27 **[LOW] Modbus TCP follow-ups from the 1.0.6.56 review.** A3: the add-dialog TCP
  logic (`mqtt.js` connection selector, narrowing, ids, refusal toast) has no JS unit test — only headless
  runs. A4: no «проверить связь» action — a wrong IP shows only after «Сохранить и применить». A7: devices
  sharing one `host:tcp_port` use the first entry's `tcp_timeout_s` (documented in
  `docs/contracts/bridge-modbus-tcp.md`) but nothing warns when entries disagree. A8: `mqtt.js` 2937 lines,
  `stand_devices.py` 750, `bridge_serial.py` 699 — decompose worklist.
- [OPEN] 2026-09-27 **[LOW] No direct unit test of `ModbusSerial` bit parsing** (FC01/FC02 are covered only
  through the shared helper by the TCP round-trips since 1.0.6.56).

- [OPEN] 2026-09-28 **[MED] Cause of the bench 1.136 resets is still unknown (power or an external
  reset).** Closed parts of the 2026-09-09 8D are recorded in `docs/bugs/bench-136-reset.md` (D5 step F —
  the install lock — shipped in 1.0.6.51). Standing rule: no further install on 1.136 without a serial
  console attached; the next step is a console capture across one reset.
- [OPEN] 2026-09-28 **[LOW] Quality follow-ups from 1.0.6.58.** (1) A CI strict mode: a SKIP should be a
  FAIL where the environment is supposed to have the tool (e.g. an env var set in `web-quality.yml`).
  (2) A gate requiring every row a contract cites to carry that contract in its `covers` — stops the audit
  L7 class recurring. (3) Upgrade risk: the ai-dev installer ships its own template of
  `.ai-dev/quality/run.mjs`; an upgrade that overwrites it would drop this project's runner fixes (the
  SKIP verdict, the `--touched` union) — check `.ai-dev/procedures/upgrade.md` handling before the next
  tooling bump.
- [OPEN] 2026-09-29 **[LOW] Review advisories left open at the 1.0.6.61 ship.** MP-02 template
  (`opt/sa02m-modbus-mqtt/templates/`): (1) the fan_mode / operator-window semantics are told in three
  places (`config-mp02-ahu.json` `_comment`, `templates/README.md`, the test comment) and already word the
  window differently — keep the firmware requirement in `_comment`, the semantics in README, the local
  why in the test; (2) the read side of `fan_mode` (raw 10 → «1.0») is not pinned directly — seed
  `193: 10` in the MP-02 read test. Queued behind the 1.0.6.62–.68 train.
- [OPEN] 2026-09-29 **[LOW] Review advisories left open at the 1.0.6.60 ship (three review rounds).**
  Update runner (`etc/sa02m-update-runner.sh`): (A14) `docs/deployment.md` gives the operator no next step
  after «rollback incomplete» (journal 15d, archive (h)/(h2)/(i)) — and if the new VERSION already landed,
  the update check may not offer that version again; write the remedy (re-run the delivering update /
  `sa02m-update-remedy.sh`) and check the retry path. (A9) FIFO retention of rollback archives
  (`build_rollback_archive`, `ls -1t | tail -n +3`) can prune the CURRENT archive after a backward clock
  step — exclude `$archive` from the prune set. (A13) harness case (j) does not pin a refused DIRECTORY
  fsync (`sync -- "$STATEDIR/rollback" || true` survives). (A5) a newline in an archive member name splits
  the `read -r` walk (fails safe to «rollback incomplete»; `-print0` is stricter). (A15) the runner is
  ~2745 lines — the decomposition worklist entry owns the split. Harnesses: (A3)
  `scripts/dev/test-nodered-ctl.sh` `svc_dst_is_live()` duplicates the codemod's `LIVE_PREFIXES` with no
  sync pin; (A4) style-level shellcheck notes grew in the two harnesses. (A10) is the `web-auth-behaviour`
  case 59 flake — see «Two load-induced harness flakes». (A11, the shellcheck row printing PASS when absent)
  is closed by 1.0.6.58's SKIP verdict. Queued behind the 1.0.6.63–.68 train.
- [OPEN] 2026-09-28 **[LOW] Review advisories left open at the 1.0.6.58 ship (four review rounds).**
  Alice gateway probe (`opt/sa02m-alice/sa02m_alice/config/api.py`, `_read_probe_cache` and the detached
  refresher): (A1) the refresher has no hard runtime bound — `setsid` takes it out from under the CGI's
  timeout and urllib's timeout does not cover DNS; past the 30 s stale-lock window a second refresher
  breaks the lock and the first one's final unlink-by-path may delete the new holder's lock; (A2) the
  stale-lock break is check-then-act (worst case: one duplicate probe); (A7) the «Проверка шлюза…» string
  and its DICT entry are never rendered (only `kind:'err'` text shows) — drop or render it. (A4) `api.py`
  is 1,621 lines — a `decompose` candidate (worklist entry). Quality runner: (A8) section E empties PATH, so
  the skipper harnesses `cd /` before reaching their skip line — the skip is reached by accident, not by
  design; (A12) the section F scanner reads a heredoc inside `$(…)` as code (no live script affected);
  (A13) contrived exit shapes neither scanned nor named in the scope sentence (`(exit "$fails")`,
  `trap 'exit "$fails"'`, `exit $(( f>0 ? f : 0 ))`, `process.exitCode +=`). Flasher: (A9) the multipart
  parser holds the whole upload in RAM (nginx caps it at 8 MB). Queued behind the 1.0.6.60–.68 train
  (2-agent cap, Operator 2026-09-28); verdict text: the 1.0.6.58 PR's review stamp history.

<!-- Whole-project audit 2026-08-28 at 1.0.6.23 (3 parallel auditors: contracts,
     security, docs). Suite was GREEN (build 47/47, review 6/6) and branch
     protection verified live — every finding below is what green does NOT cover. -->

- [OPEN] 2026-08-28 **[LOW] Security long tail.** Response-header injection in the devices export
  (`device_history_db.py:1788-1789` raw metric/group into `api.py:59-66`; the ascii filter keeps
  CR/LF, `_q1` strips only edges) — authenticated · no dependency-CVE and no secret-scanner row in
  the 55-row registry, deps floor-pinned with no lock · 42 of 48 systemd units run root with zero
  hardening, including the four that parse hostile input · `docs/threat-model.md` says the frpc
  port allow-list is absent — it exists (`sa02m-cloud-agent.py:66`).
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
  collapsed into it on 2026-08-28 (their numbers were stale, one on a void rationale); re-measured
  and re-ordered by audit 2026-09-24 at 1.0.6.53 (56 tracked text files over 800 lines).
  1. `etc/sa02m-update-runner.sh` 2308 L on main (≈2,660 on 1.0.6.54), 58 functions; largest
  `run_validate_and_extract` 292 and `prepare_github_overlay` 254 (mostly embedded Python). Seams:
  (a) the GitHub manifest builder (`map_dst` + `DST_RE`) and the validator's second `DST_RE`/`DEL_RE`/
  `PRESERVE` → one Python module in `opt/sa02m-update/lib/` next to `validate_package.py` (closes the
  four-home allow-list entry; also fix there audit 2026-09-28 L3 — `map_dst` sends the non-unit drop-ins
  `etc/systemd/sa02m-journald.conf` / `sa02m-watchdog.conf` to `/etc/systemd/system/` on every OTA,
  harmless to systemd but a misleading stray file on the board); (b) deploy/journal/rollback core; (c) services + health gate;
  (d) recover/reclaim/verify state machine; (e) watchdog/imaging lock. Constraints: the delivering
  update runs the INSTALLED runner (`self_reexec_before_deploy` copies one file), so a sourced-lib
  split gives old-runner + new-lib skew — keep the bash single-file, move only Python out; three
  harnesses extract single functions by awk marker (`test-update-recover-rollback.sh:46`). After
  1.0.6.54 lands.
  2. `flasher.js` 5863 L — ~10 responsibilities; `flasher/led.js` (1427) proved the ES-module split.
  Next: the Carel window, the MR module-config window, scan/flash job UI vs port lease.
  3. `app/status.js` 2640 L — the web-update UI (panel belt, semver, XHR upload) and MPLC deploy are
  not status → `app/webupdate.js`, `app/mplcdeploy.js` (the XHR CSRF entry lands there naturally).
  4. `scripts/lib.sh` 1322 L — sections exist (net/subnet, resolvconf, apt/pip, sudoers, atomic
  install, the byte-identical watchdog block, service capture/apply) → `lib-net.sh` / `lib-pkg.sh` /
  `lib-install.sh` / `lib-svc.sh`; mind the 7 rows whose `covers` name `scripts/lib.sh` and the
  runner twins (`cleanup_b1_deploy_artifacts` vs `sa02m_cleanup_b1_deploy_artifacts`).
  5. `devices.js` 2618 L — the canvas chart engine. `mqtt.js` 2775 L — seams: device-add modal /
  per-family builders · the `type:template` picker (`_templateCatalog` / `fillTemplateSelect` /
  `refreshTemplateCatalog`) · broker+credential settings · config model. `status.cgi` 2538 L (size
  relief). `flash_protocol.py` 2517 L — the three drivers. `etc/sa02m-web-service-ctl.sh` 1590 L —
  the Node-RED block. `device_history_db.py` — ranges/schema/write/query. Smaller:
  `opt/sa02m-modbus-mqtt/bridge_mqtt.py` — the `/meta` blob cluster → `bridge_meta.py`, covered by
  `test_meta_blob.py`.
  NOT a split target: `main.css` 6069 L — sectioned, one token root; a split costs link tags plus
  `?v=`/`&r=` busts per file on a no-build stack. Re-judge at 7k.
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
  **Premise corrected 2026-09-23 (1.0.6.50 refresh of 1.135, same WARN):** the unit
  DOES exist — `etc/systemd/sa02m-userspace-watchdog.service`, installed by the imaging
  path (`patch-firstboot-image.sh`, `compress-bin.sh`, `tools/system-hardening/
  install.sh`) — but `install.sh` never installs it, so `scripts/01-system.sh:622`
  enables a unit only image-born boards have. Refresh-updated boards run without this
  reboot-watchdog. Installing it changes board behaviour (forces reboots) — Operator's call.
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

- [OPEN] 2026-09-03 **[LOW] Carel I/O texts have no English.** The «Входы/выходы» rows and the
  digital-state captions live in `opt/sa02m-carel/sa02m_carel/carel_ahu_map.py` in Russian only —
  alarms carry `text_en`, the other rows do not — so the controller window's two tables stay Russian in
  the English UI. Fix: `text_en` in the map (its one home) and the renderer reads it, as for alarms.
  Found building the 1.0.6.31 window.
- [OPEN] 2026-09-03 **[LOW] The «Умный дом» setpoint range is one for both Carel families.**
  `SH_KINDS.setpoint` in `smarthome.js` sets `parameters.range` 0..99 for every device, while uAria
  tops out at 50 °C (`SETPOINT_RANGE`, `opt/sa02m-carel/sa02m_carel/controls.py`). The bridge clamps
  the write, so the unit is safe, but a slider built from the document offers an unreachable 50..99 and
  snaps back after the first report. Cause: the window does not know the family (the setpoint topic
  carries port and address; the topic inventory is a flat list). Fix: carry the family to the window —
  the bridge publishes it as control meta, or `sa02m_alice_topics.cgi` returns a device→family map.
  Found by the 1.0.6.31 review; recorded in `docs/contracts/carel-ahu.md` §6.
- [RESOLVED 2026-09-29 — not a Carel pause: the COM3 entry's DATA 2026-09-29 is the cause; the
  read-to-length fix was built, reviewed (APPROVED) and NOT shipped on the Operator's decision after a
  10-min bench A/B: Carel Short 99 → 98, control MR-02m 91 → 96, zero recovered pauses, poll cycle
  289 → 295 ms; the unshipped branch is `1.0.6.64` (local, `18f9fba0`…`1419353d`), the number is not
  reused] 2026-09-03 **[MED] Carel polling loses ~1.5 % of frames on an in-reply pause.** Bench 1.135,
  COM3 19200, both PLCs polled: ~9 `Short response` per minute for two devices, values still correct
  and fresh (the retry recovers the frame; no offline). The «phantom `mr02m-COM3-10`» hypothesis was
  tested and REJECTED (same rate with it removed). Cause: `bridge_serial.py` ends the read at the first
  silent gap without waiting for the computed frame length, and Carel PLCs pause mid-reply (both
  11/13- and 72/77-byte frames are cut). Fix locally, not globally — the early exit exists for MR-02m
  and changing it moves every poller's bus timing: a per-transaction «read to computed length» for
  Carel, measured on the bench before and after. Found in the 1.0.6.31 hardware acceptance. Related:
  the COM3 error-rate entry (2026-09-24 data).
- [OPEN] 2026-09-24 **[MED] OTA never deletes retired files.** The runner path applies `delete: []` on every manifest (`etc/sa02m-update-runner.sh` github manifest builder; `scripts/pack-offline-update.py` likewise), while the legacy rsync path did `--delete`. A file removed from the repo stays on every OTA-updated board forever (stale CGI, stale unit, stale helper twin). Fix shape: the packer / manifest builder computes `delete[]` from the previous release's deploy list (git), the runner already journals and rolls back `delete` records. Found while building 1.0.6.54 (fast OTA, CHANGELOG entry).
- [OPEN] 2026-09-24 **[LOW] Deploy loop, next rung (a2): one python process for the whole deploy.** 1.0.6.54 (a1) removed the per-item interpreter starts in bash (~2,100 → ~20 per apply); an in-process Python deploy (compare/backup/journal/install, per-file fdatasync kept) would take the 505-item deploy from ≈1.5–2 min to ≈10–20 s. Not taken: it moves the root apply core out of bash and breaks the single-function extraction of three harnesses. Trigger: the bench measurement of a 1.0.6.54 apply's DEPLOY PHASE (505 items) lands above 3 min. Recorded while building 1.0.6.54 (CHANGELOG entry). MEASURED 2026-09-24 on 1.135 (514 items, 411 changed, load ~8): deploy 2 min 30 s, «Применить» → done 4 min 15 s (repeat 2:31 / 4:18) — the deploy trigger is not met; the remaining ~1 min 45 s is clone/staging/rollback archive (~30 s) and the health gate's restarts (~70 s).
