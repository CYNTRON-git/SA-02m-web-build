# Backlog — SA-02m web interface

Recorded findings and deferred work (`.ai-dev/procedures/backlog.md` owns the
format). One status per finding: `- [OPEN|RESOLVED] <date> <item>`. Resolved
entries are pruned (history lives in git); last prune 2026-10-24 (audit 2026-10-24 F6: H2 billing lock, the 1.0.7.5 dashboard freshness flicker, the shipped CE CT write and one RESOLVED entry removed; the three module-size worklists folded into one; the removed text is that commit's diff of this file).

## Open

- [RESOLVED] 2026-10-29 **[MED] Same class as the 1.0.7.12 Carel fix, left outside its fence (review r1/r2 A2/A3): `DTVPoller.setup()` autodetect loop (5 × 3 s) held the port thread while a DTV answered «no sensors» (a silent one broke out after one timeout but was assumed to carry ALL sensors), and `DeviceLiveCache.flush_file` defaulted an unknown device to `ok:true` in the panel live cache.** Fixed in 1.0.7.17 (branch `1.0.7.17`): one non-blocking detect attempt at setup + retry from `poll_io` every 3 s under the back-off, the «5 empty answers ⇒ all present» bound kept, an explicit empty `sensors_present: []` still = all; unknown ⇒ `ok:false` in the live cache until the first tracked read; RED-first tests `TestDtvSetupNeverBlocksThePort` / `TestLiveCacheUnknownIsNotOk` in `test_device_availability.py`.
- [OPEN] 2026-10-29 **[LOW] `opt/sa02m-modbus-mqtt/bridge_mr02m.py` is 1038 lines (review r2 A6)** — over the ~1000 default; `decompose` candidate (behaviour net = the 941-test bridge suite). Not started.
- [OPEN] 2026-10-29 **[MED] Panel network save refuses on a board whose interfaces.d conf carries a hand-added
  static route (1.0.7.18 follow-up, bench 1.135).** `etc/sa02m-iface-conf-write.sh` allows only the KLogic hook and
  the installer default-route `post-up`; a conf with `post-up ip route add 192.168.10.0/24 via 192.168.1.100 …`
  (the 1.135 Keenetic↔MikroTik route) makes every panel network save fail closed. Options (Operator decision):
  (a) allow exactly the `sa02m-routes-ensure.sh` grammar (`docs/contracts/boot-network-routes.md` §3) in the
  write helper — security review required (a stolen panel session could plant routes = traffic redirection);
  (b) a panel-managed static-routes feature with its own validated store. Not started.
- [OPEN] 2026-10-29 **[LOW] Route-belt leftovers (1.0.7.18 plan §4 / review round 3).** (1) A failed `ifup` also
  skips the KLogic `post-up /home/klogic/adjust-<if>` hook — not covered by the belt (it runs a vendor binary,
  must not be re-run blindly). (2) `etc/fix-eth1-internet.sh` (legacy one-shot fixer) embeds an old `fix-eth` copy
  with neither the DNS nor the route belt. (3) Still silent in `sa02m-routes-ensure.sh`: a quoted object
  (`ip 'route' add …`), `ip -batch <file>`, and line :124 matching the raw token instead of `base()`; name them in
  contract §3 «Чего хелпер не видит» or route them through `base()`. (4) Helper 299 lines / harness 631 lines —
  `decompose` if either keeps growing.

- [OPEN] 2026-10-21 **[LOW] A long clip is always uploaded twice: the recogniser answers after the socket
  timeout (bench 1.135, RK3588 agent, series 8×30 s).** RK3588 needs 11–13 s to recognise a 30 s clip, more
  than `UPLOAD_TIMEOUT_S` = 10 s (audio-recorder.md §12). The first attempt times out, the second (10 s later)
  gets the receiver's cached answer by `recording_id` — all 8 uploads in `events.jsonl` were `ok` exactly 20 s
  after `end`. The seam holds (dedup by id), but every long clip costs a redundant upload and +10 s before its
  voice commands apply. Direction (an Operator decision, §12 change): a reply timeout that grows with the clip
  length (e.g. 15 s for clips ≥ 20 s), or a separate response-read timeout. Not started. Same fix corrects the
  §12 wording: attempts are not at 0/10/60 s «после сохранения» — `upload.py:233` waits `at - previous` after the
  previous attempt ENDS, so a timed-out first attempt puts the second at ~20 s (1.0.7.7 review D4).
- [OPEN] 2026-10-21 **[MED] sa02m-audio grammars still accept a trailing line break (1.0.7.7 build finding).**
  1.0.7.7 moved the voice, command-topic and ALSA-device grammars to `\Z`; three remain on `$` + `match`:
  `TOKEN_RE` (`config.valid_token("tok\n")` is True, the panel's `set_token` stores it and `read_token` returns a
  token ending in `\n`), `ID_RE` (`valid_id(id + "\n")`; `store.py` slices `C.ID_RE[:-1]` for its name patterns,
  so the fix touches that too), `CARD_ID_RE` (input from `/proc/asound`, lowest); for a complete sweep also
  `mqttio._INT` and `config._INT_RE` (harmless today: input is stripped first). Same branch, two 1.0.7.7 review
  wording nits: `led-effects.json` `notes_ru.sound_reactive` repeats «в карте регистров нет»; `threat-model.md`
  «для голосового управления нужен https://» is advice — the code accepts `http://` («рекомендуется https://,
  код http:// не запрещает»). Queued to one sa02m-audio
  hardening branch, cut AFTER 1.0.7.9 merges (it refactors capture.py/service.py): this entry, then the next,
  then the five recorder observability findings light-music recorded on 1.0.7.9 (stuck mic, early-cut reason,
  one-shot retained warning, silent `stop` without a recording, repr in the journal) — at branch time merge
  those entries with these, never duplicate.
- [OPEN] 2026-10-21 **[LOW] No committed harness for the 15-audio.sh conf / allow-list seeder (1.0.7.7 review
  A1).** The O_EXCL/O_NOFOLLOW seeder (only-if-absent, symlink left alone, invalid default not seeded) is
  verified only by manual WSL runs. Direction: extract and run the heredoc in a sandbox like
  `test-mqtt-acl-audio.sh` part B, as a registry row with its mutation case.
- [OPEN] 2026-10-15 **[MED] Factory reset and the image pipeline do not wipe audio recordings (1.0.7.4, plan audio-record-on-command §13 step 12).**
  The recordings are speech of people on site (threat model S5) and the upload token is a secret. Factory reset
  should wipe `/var/lib/sa02m-audio/{rec,tmp}/*`, `events.jsonl` and `/etc/sa02m-audio/upload.token` (and the
  `sa02m-audio/` dir on a present removable medium); the five image sites of `docs/contracts/image-identity-reset.md`
  §8 should do the eMMC part. Today only the manual golden-image step `docs/deployment.md` 2c does it.
- [OPEN] 2026-09-30 **[MED] Sweep the other harnesses for host-binary / host-root side effects (ci-linux-green
  finding).** Two harnesses could, on a ROOT host, reach real system actions: firstboot-sb-csum a real
  `fsfreeze -f /`, web-update-apply-guard reboot.cgi's real `sudo … reboot -f` chain. Both fixed on
  ci-linux-green (PATH masking via scripts/dev/lib_path_mask.sh; sandboxed CGI copy). Other `scripts/dev/`
  harnesses that remove a stand-in and rely on `command -v` failing, or run a shipped CGI whose side
  effects are real on a writable host, were not swept. The CI runner has passwordless sudo.
- [OPEN] 2026-09-30 **[LOW] `opt/sa02m-alice/sa02m_alice/client/main.py` is 912 lines (1.0.6.66 review A4).**
  A `decompose` candidate (reconnect/push loop, session lifecycle, status file).
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
- [OPEN] 2026-09-29 **[MED] RTU Modbus exception replies are misreported as «Short response» — a live
  device that answers «illegal address» is treated as silent and driven offline.**
  `opt/sa02m-modbus-mqtt/bridge_serial.py:462-477` (`_transact`): `_modbus_read_frame_len` (`:144`)
  sizes an exception frame at 5 bytes, `_read_rtu_response` returns those 5 bytes, and the length
  check at `:462` raises `Short response: 5/N` before the exception branch at `:475` — which is
  unreachable for reads. The TCP path decodes exceptions correctly. Found by the Pult audit plan
  (2026-09-29); still present on `main` at 1.0.7.1. Pending: fix on its own branch from `main`.
  Fix direction: CRC-check the short frame, then raise a typed exception error before the length
  check; RED-first test with a 5-byte exception frame on a fake serial.
- [OPEN] 2026-09-29 **[LOW] `docs/threat-model.md` row `A2/A3→S1` «Публикация из LAN в `1883` (без
  auth) …» says the 1883 broker accepts LAN publishes without auth; the repo binds it to 127.0.0.1
  only** (`etc/mosquitto/10listeners.conf:8`, LAN goes to 1884 with auth). Other rows cite the same
  premise (grep `1883` in that file). Still worded so on `main` at 1.0.7.1. Pending: a docs fix
  on its own branch.

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

- [OPEN] 2026-09-16 **[MED] Retro-review owed for 1.0.6.46 (`1dfa503`), the one direct push to `main` (audit 2026-09-16 H1).** `1dfa503` (2026-09-10, 13 files incl. `bridge_fmb.py`/`bridge_mr02m.py`) landed with NO PR and no review stamp. The protection half of H1 is resolved: live `gh api …/branches/main/protection` on 2026-10-24 (audit F6) reads `contexts:["quality"]`, `strict:true`, `enforce_admins:true`, force pushes/deletions off (`.ai-dev/notes/ci-budget.md`). Still owed: a retroactive Reviewer pass over `1dfa503`'s diff.
- [OPEN] 2026-09-16 **[MED] Audit M7: verification coverage** — no dependency-CVE row (pip deps installed unpinned `scripts/lib.sh:739-763`; Node-RED payload; frpc) — re-rank the 08-28 LOW item; 16 mutating/root-capable CGIs (cmd_exec, hw_set, kernel_ctrl, reboot, restart, web_creds, web_factory_reset, storage_format_set, mqtt_config, mqtt_ctrl, cpu_profile, gateway_ctrl, web_update_upload/cancel, ssh_debug, web_backup) have zero behavioural test references. **Operator decision owed:** add `pip-audit` / `npm audit --package-lock-only` rows, or record the accepted risk (1.0.6.49 plan: no row until decided). The CGI half now has a shape to reuse — `cgi-csrf-behaviour` covers two of the 16; the rest stay uncovered.
- [OPEN] 2026-09-16 **[LOW] `tools/imaging/_run_reset_cloud.py` — keep or retire (Operator).** Tracked via the `.gitignore` exception (`:89-93`) since 1.0.5.64; used only by hand against the bench (audit L2).
- [OPEN] 2026-09-16 **[LOW] The firstboot overlay carries the DNS-belt callers but not the belt.** `tools/imaging/firstboot-overlay/` has `sa02m-eth-coldboot.sh` and `fix-eth.sh` with `dns_ensure` (synced 1.0.6.49) but no `usr/local/sbin/sa02m-dns-ensure.sh`; `dns_ensure` guards on `-x`, so a clone flashed from a pre-1.0.6.6 image gets the guard, not the belt. Fix: add the helper to the overlay + its PAIRS row in `firstboot-overlay-parity` (audit M1 follow-up). Since 1.0.7.18 the same applies to the route belt: the overlay callers carry `routes_ensure` (`-x`-guarded) but not `usr/local/sbin/sa02m-routes-ensure.sh` — add both helpers together.
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

- [OPEN] 2026-08-27 **[LOW] A contract sentence is hostage to an unpinned JS line.**
  `docs/contracts/alice-mqtt-mapping.md` states that a `complete_link` over an already
  connected client is unreachable from the panel; that is true only because
  `www/network_config/static/js/app/alice.js` computes `linked` as
  `st === 'connected' || certOk` and renders «Завершить привязку» in a later branch.
  Nothing asserts it — edit that line and the contract silently becomes false. Same
  seam class the 1.0.6.19 gate pins on the helper↔client side, left unpinned on the
  JS↔contract side. Fix direction: a small assertion in the headless driver (the
  linked-state card offers «Отвязать», never «Завершить привязку»).

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
- [OPEN] 2026-08-18 **[LOW→follow-up] Configurable serial parity for
  `type:template`.** Stop bits 1 or 2 are in (`stopbits` on the device,
  `get_port` key `port:baud:stopbits`, contract §8). Parity is still none.
  A port that mixes the two stop-bit settings is logged and the second handle
  does not open.
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

- [OPEN] 2026-09-27 **[LOW] Modbus TCP follow-ups from the 1.0.6.56 review.** A8: `mqtt.js` 2937 lines,
  `stand_devices.py` 750, `bridge_serial.py` 699 — decompose worklist. (A3 dialog unit test, A4 «проверить
  связь», A7 timeout-disagreement WARN: RESOLVED 1.0.6.67.)
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
  the UI, so a dedicated pass is warranted given the non-HttpOnly cookie. (3) answered by audit
  2026-10-24: a pickaxe over all refs (`git log --all -S` for key/token markers, `-G` for quoted
  password/token/secret literals) found nothing; the only `.pem` ever added is a public key.
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
- [OPEN] 2026-09-24 **[MED] OTA never deletes retired files.** The runner path applies `delete: []` on every manifest (`etc/sa02m-update-runner.sh` github manifest builder; `scripts/pack-offline-update.py` likewise), while the legacy rsync path did `--delete`. A file removed from the repo stays on every OTA-updated board forever (stale CGI, stale unit, stale helper twin). Fix shape: the packer / manifest builder computes `delete[]` from the previous release's deploy list (git), the runner already journals and rolls back `delete` records. Found while building 1.0.6.54 (fast OTA, CHANGELOG entry).
- [OPEN] 2026-09-24 **[LOW] Deploy loop, next rung (a2): one python process for the whole deploy.** 1.0.6.54 (a1) removed the per-item interpreter starts in bash (~2,100 → ~20 per apply); an in-process Python deploy (compare/backup/journal/install, per-file fdatasync kept) would take the 505-item deploy from ≈1.5–2 min to ≈10–20 s. Not taken: it moves the root apply core out of bash and breaks the single-function extraction of three harnesses. Trigger: the bench measurement of a 1.0.6.54 apply's DEPLOY PHASE (505 items) lands above 3 min. Recorded while building 1.0.6.54 (CHANGELOG entry). MEASURED 2026-09-24 on 1.135 (514 items, 411 changed, load ~8): deploy 2 min 30 s, «Применить» → done 4 min 15 s (repeat 2:31 / 4:18) — the deploy trigger is not met; the remaining ~1 min 45 s is clone/staging/rollback archive (~30 s) and the health gate's restarts (~70 s).

- [OPEN] 2026-10-07 **[MED] CE-02m-3: boards keep an orphan retained `…/controls/total/meta/*`.** The old `CE02M3Poller` setup (`opt/sa02m-modbus-mqtt/bridge_dtv_ce.py`) published `meta/readonly` + `meta/units=W` for a control named `total` while the value is `power_total`, so every CE device on every board that ran it carries the orphan retained topics. Found by the vPLC session's read-only survey (2026-10-07). Code half shipped (on `main`): the meta row is `power_total`, pinned by `opt/sa02m-modbus-mqtt/tests/test_ce02m3_meta.py`. Still OPEN — broker half: the fix stops publishing `…/controls/total/meta/*` but does not clear it, so every board that ran the old bridge keeps the retained orphan (`total/meta/readonly`, `/units`, `/precision`, the `/meta` blob) until an empty retained publish clears it (Operator word; the general fix is the «retained value of a disabled channel» entry).
- [OPEN] 2026-10-07 **[LOW] vPLC-requested panel conveniences.** (a) Pre-fill «Добавить устройство» from a link (`/#mqtt?type=carel&port=COM3&addr=N`, `transport=tcp&host=…`) so the vPLC settings page can hand the operator a ready form; (b) a server-side rouble tariff (`kwh_rub`) home instead of the browser's localStorage, read by `/history/summary` by default; (c) a formal contract for `/api/devices/history` groups `climate|energy|ahu|mtd` and `/history/summary` (today only `kind=mr` is contracted in `devices-mr-history.md`; the grammar's home is `handle_history` in `api.py`). Source: the vPLC session's SA02M_INTEGRATION_POINTS survey (2026-10-07).
- [OPEN] 2026-10-07 **[MED] СЭ-02м-3 residuals of the integration-docs/UI audit** (audit `ce02m3-audit.md`). Shipped (on `main`): the docs half (`docs/MQTT_TOPICS.md`, `docs/MPLC4_MQTT.md`, `README.md`, CE meta rows), the UI half (CT hint, `line_voltages` key, poll default) and the bridge half (CE meta units/readonly and flag-gated meta — `tests/test_ce02m3_meta.py`; dead-meter-chip `meta/error` — `tests/test_ce_meter_link.py`, `docs/contracts/ce-energy-mqtt.md`; stale retained `meta/error` cleared after each poller's setup — `tests/test_pub_error_stale.py`). Still OPEN: (#37) the `phases` filter applies to U/P only (bridge + MQTT tab); (#44) the bus-scan fingerprint `Holding 1 == 0xCE02` is dead code (Input 0 == 100) — still at `opt/sa02m-modbus-mqtt/mqtt_bus_scan.py:531`; (#45) the «CE has no bus-mode register» rationale is false (Holding 122 is 3-state since fw 1.0.7.0); and `CHANGELOG.md` dating the FMB wire to 1.0.6.12 (it is 1.0.6.13), which no shipped half named — not re-verified.
- [OPEN] 2026-10-07 **[LOW] CE-02m-3 firmware docs** (not ours; relay to the CE-02m-3 session): README reg 122 says classic/Fast only vs 3-state in MODBUS_VARIABLES; README 560–571 «raw» vs 563–565 primary; hours snapshot «every 6 h» (README) vs hourly (MODBUS_VARIABLES); MCU temp 124 doc lacks «0.1 °C». Plus the inactive-energy truncation (648–679) found by the energy planner (`.ai-dev/plans/ce-energy-unit.md` §1 «Попутный дефект»).

- [OPEN] 2026-10-07 **[LOW] vPLC asks for a bridge device type for Wilo SK712 drainage cabinets (Modbus RTU).** Their adapter is written against a proposed control set (vPLC `vplc/docs/UNITS_API.md` §5.4: `tank_level`, `pump1_run/pump2_run/pump1_alarm/pump2_alarm`, `overflow`, `manual`, frequency/hours, `ack`). SA-02m has no SK712 type; the cheapest path is a `type: template` descriptor in the shared devices catalogue (`CYNTRON-git/devices`, `docs/contracts/template-device.md`) — control names are then the descriptor's channel names, so agree the names in the descriptor, not in the bridge. Needs the SK712 Modbus map (not in any declared sibling). Nothing built; recorded from the vPLC session's message.

- [OPEN] 2026-10-14 **[LOW] Stale remote branches — delete on the Operator's word (audit 2026-10-14 C-7/C-8, re-listed by audit 2026-10-24).** The local half is done: 0 plans + 1 stamp in the main checkout (`clk32-recovery`, its branch still open), `git worktree list` = the main checkout only; the one stale local branch is `1.0.7.10` (squash-merged, deletable locally). Left on `origin`, all non-version: `chore/backlog-2026-10-08`, `ci-linux-green`, `claude/audit-project-no-vplc-bf164f`, `claude/home-connect-homekit-integration-4llog3`, `claude/karel-controller-integration-847728`, `claude/side-findings-fixes-4llog3`, `replay/{backlog-2026-10-08,com-overrun,devices-value-clip,harness-sweep,mr-ai-type,mtd-scan-fix,rules-mqtt-overflow,template-poller-pult}` (replayed and merged), `wip/beeper-override-no-exec`, `wip/integrate-1.0.6.34-leftovers`. Keep every `origin/<version>` branch (the version record) and `origin/1.0.7.10` in particular (see the 2026-10-24 squash-body entry).
- [OPEN] 2026-10-08 **[LOW, flasher UX]** A CE-02m-3 wired to the panel through its EXTERNAL (top) RS-485 connector
  cannot be flashed from the panel: that connector is the meter's AUX port (USART1) and the shared bootloader
  listens on USART2 (end-face bus) only; CE app ≤ 1.0.7.10 even ACKs reg 129 on AUX without resetting (fixed on
  the app side in CE 1.0.7.11). Today the panel's flasher fails honestly (by-serial info block → Modbus exc 2)
  but with no hint. Surface «СЭ подключён внешним разъёмом — прошивка только по торцевой шине» when the scan sees
  a CE whose BL never answers at 247 after reg 129. Bench record: CE repo BUGLOG 2026-10-08, runbook
  `scripts/hw/RUNBOOK_gateway_flash_1.0.7.11.md` (in the CE-02m-3 repo, not here).

- [OPEN] 2026-10-08 **[MED, from vPLC, Operator decision, contract change]** `devices.summary` across the Wh unit change:
  a window that contains the 2026-10-08 transition returns a meaningless `delta` (rows before it are in the
  old 1/320 unit, so the §6 non-negative-step sum adds old-unit steps — bench .135: 238 kWh for 24 h at 32 W).
  Accepted on 2026-10-07 («archives stay, counter break accepted»); vPLC asks for a break signal so clients stop
  summing across it: either `break: true` / `unit_changed` on the summary object when the window straddles a
  unit change, or a new series started at the change. Needs the Operator's word (contract `devices-history-api.md`
  §6 + test `test_ce_energy_summary_break.py`); until then clients treat straddling windows as unreliable.
- [OPEN] 2026-10-14 **[LOW] GitHub-OTA maps `etc/sa02m-stacks-policy.sh` to a stray executable `/usr/local/sbin/sa02m-stacks-policy`** (runner `map_dst` `.sh` rule) and never refreshes `/usr/local/lib/sa02m-stacks-policy.sh`; the offline pack ships neither. Low (the lib changes rarely). Fix: runner `map_dst` + packer rename + allowlist + an allow-listed cleanup of the stray. Found by the 1.0.7.2 planner (Q4); own branch after 1.0.7.2.
- [OPEN] 2026-10-14 **[MED] A flasher-window address/baud change of an MR-02m / ДТВ / СЭ module leaves the bridge polling the old address** (`flasher.js` network write updates only the scan row; nothing touches `/etc/sa02m-modbus-mqtt.yaml`). The MTD window (1.0.7.3) solves it for the sensor with `flasher/bridge-relink.js` (read `mqtt_config.cgi`, edit the one matching entry, post with `restart:false`); reuse it for the other families. Own branch after 1.0.7.3.
- [OPEN] 2026-10-14 **[LOW] MTD presence card (1.0.7.0) is not in the `ui-layout` «Устройства» fixture** (`ui-layout.mjs` names the omission): at the 1101 px three-card width its settings tiles clip (input row hides «м»/«с» by 4–6 px; labels «Освещённость», «Зона экранирования» clipped), «да» has no EN DICT entry, and the 70 px header reserve should include `.dev-card--mtd` (`main.css` ~:6212, no remove button). Fix, then add the MTD card to `DEVICES_LIVE` / `DEV_KINDS`. From the devices-value-clip replay review.
- [OPEN] 2026-10-14 **[LOW] EN gaps on the «Устройства» tab:** «+ Добавить виджет» stays Russian at 1101/1280 px and «Архив: —» is untranslated (devices-value-clip review A4). DICT + `uiT()`.
- [OPEN] 2026-10-14 **[LOW, Operator judgement] Devices card value fitting is per tile, so values in one card end up different sizes** (at 1101 px «12.34 mg/m³» / «780.5 mmHg» ~10 px next to full-size «5000 ppm»). Alternative: one scale per card. Asked 2026-10-14, no answer yet; keep per-tile until the Operator says otherwise.
- [OPEN] 2026-10-14 **[LOW] Fast Modbus `poll_events` (up to 128 data bytes, `bridge_fmb.py`) and FC17 replies are not capped on fast built-in ttyS lines** — the 1.0.7.2 FIFO cap covers FC03/FC04 register reads only (`docs/MQTT_TOPICS.md` says so). Measure on the bench whether these replies overrun before designing a cap. com-overrun review A1.
- [OPEN] 2026-10-14 **[LOW] A retained value of a disabled channel is never cleared** — `di_N_count` after `counter: false` (mr-ai-type), `meta/error = r` of a channel removed from a template or set `enabled:false` (template-poller review Q4); the same class as other orphaned retained topics (CE `controls/total/meta/*`). One general «clear retained topics of a channel the bridge no longer publishes» fix, own branch.
- [OPEN] 2026-10-14 **[LOW] MTD window (1.0.7.3) residuals:** a devices-card write (`mqtt_set.cgi`, QoS 0, bridge stopped under the flasher lease) during an open window is dropped, never replayed; a deferred register (applies after a power cycle) is not followed by the bridge entry — both stated in `docs/contracts/mtd-presence.md`. Bench walk on 1.135 (COM3 @19200, address 20) offered to the Operator, not run.
- [OPEN] 2026-10-14 **[LOW] The `services` status part has no contract.** Its fields (`svc_*`, `optional_services`, `dash_services`, `svc_vplc_ui`) are read by `app/status.js`/`app/services.js` with no `docs/contracts/` entry; only `svc_vplc_ui` is contracted (`vplc-page-seam.md` §4). Pre-existing; the audit A-4 entry's second half. Fix: a `status-services-part.md` contract + a validating row.
- [OPEN] 2026-10-14 **[LOW] ~35 `'<prefix>: ' + err.message` sites in `flasher.js` leave the daemon message Russian in EN mode** (the prefixes are translated by REGEX rows; the suffix is not). Audit A-3 reduced to its true residual. Fix: translate daemon refusal texts through standalone REGEX rows as the MTD window does (1.0.7.3 `i18n.js`), one family at a time.
- [OPEN] 2026-10-14 **[LOW] `etc/sa02m-factory-reset-runner.sh:1334` opens the update lock with `>` (truncating) instead of `>>`** — the same pattern the 1.0.7.2 review caught in the profile lib (F1): it wipes the OTA runner's pid line the panel reads for the `stale` status. Own branch; the runner's own `8>>` idiom (`sa02m-update-runner.sh:165-168`) is the model.
- [OPEN] 2026-10-21 **[LOW] `install.sh --refresh` under the maintenance wrapper logs two misleading WARNs** — «fcgiwrap сокет не найден после 5 с» and «nginx reload не удался» — because the wrapper has stopped those units for the run by design; modules `03-webserver.sh`/`11-devices.sh` should skip the reload/probe when `SA02M_INSTALL_GUARDED=1`. Seen on 136 (2026-10-21, ИТОГ 13/13 otherwise).
- [OPEN] 2026-10-21 **[LOW] After the root-only state migration `web_update_check.cgi` reports `deployed_commit: null`** — the quarantine moves `state/deployed_commit`/`deployed_at` with the untrusted dir and nothing re-stamps them until the next OTA; `deployed_version` is read from VERSION and stays right. Decide: re-stamp from the installed tree at refresh, or drop the field from the UI.
- [OPEN] 2026-10-21 **[LOW] `verify-release-on-board.sh` check 2 treats `sa02m-rules` as a core unit** — on 136 the Operator keeps rules disabled, so the verifier reports 1 FAIL on a healthy board (14 PASS / 1 FAIL for 1.0.7.5). Make the core set follow the pre-install snapshot or the install profile.
- [OPEN] 2026-10-21 **[LOW] `gateway_status.cgi` answers `stale: true` while the gateway unit is masked** (136); harmless for the masked case but the flag should mean «status file older than its unit», not «no daemon».
- [OPEN] 2026-10-21 **[INFO] Coordinator banner and S1 alert lag the recovery by one `services` TTL (≤45 s + poll)** — measured on 136: S1 alert cleared 45 s after fcgiwrap restart, «Координатор портов недоступен» cleared 40 s after the broker restart. Document in `status-polling.md` or bypass the cache when `available:false`.
- [OPEN] 2026-10-21 **[HIGH, bench 136, Operator: «regression in the last week»] `systemctl reboot` hangs inside the kernel after systemd-shutdown completes; only a power cycle recovers.** Both fleet kernels (Jul 30 and Oct 15 builds); not on the 1.0.7.1-derived board earlier the same day with the same software; appears on the fresh golden+OTA-1.0.7.3 base after our full install. Traces, ruled-out causes (kernel build, USB gadget), the ttyGS0 console recipe and the I2C-storm lead: `.ai-dev/notes/reboot-hang-2026-10-21.md`. Next: ttyGS0 trace to the hanging device, then unbind-bisection under the watchdog backstop.
- [OPEN] 2026-10-21 **[MED] A hung reboot is never reset by the hardware watchdog**: policy `RebootWatchdogSec=0` disarms it (BUGLOG 2026-08-05 left the «slow shutdown cut by the watchdog» chain a hypothesis; trace 2 shows systemd arms it only in the final phase, which took 0.35 s), and even armed, the watchdog core stops it before `device_shutdown` (`watchdog_stop_on_reboot` from `sunxi_wdt_probe`). Fix: `watchdog.stop_on_reboot=0` on the kernel command line (`etc/boot.cmd.sa02m.min` through the boot-script promotion procedure) + `RebootWatchdogSec=16s` in `etc/systemd/sa02m-watchdog.conf` and its pinned copies (`watchdog-cap` gate). Trial state is on bench 136 (note above); repo change after it is observed on hardware.
- [OPEN] 2026-10-21 **[INFO] Bench 135 restarted at ~13:26 MSK without a shutdown sequence** — boot -1 ends mid-run (last lines: `net-watchdog` reporting NO carrier on eth0 AND eth1 for >60 s, i.e. cable/switch work on the bench while 136 was being re-flashed), boot 0 logs «REBOOT REASON: unexpected — no clean-shutdown marker», journal «uncleanly shut down», FAT «not properly unmounted». `net-watchdog` only logs (no reboot action); the userspace watchdog's last heartbeat 13:25:58 shows no failure; so it was a power event or the 15 s PID1 hardware watchdog. No software reboot was attempted on 135 recently (journal history is capped at 20 MB), so 135 is neither proven to hang nor proven safe; it does not carry the 1.0.7.5 install. Keep 135 un-rebooted unattended until the 136 root cause is known. **Correction 2026-10-23:** the «no clean-shutdown marker» line proves nothing — no shutdown on any board ever wrote the marker, clean `systemctl reboot` included (`sa02m-shutdown-marker.service` was never stopped at shutdown, so its ExecStop never ran; BUGLOG 2026-10-23). The unclean-restart reading rests on the other evidence only: boot -1 ending mid-run, journal «uncleanly shut down», FAT «not properly unmounted».
- [OPEN] 2026-10-23 **[MED] `etc/systemd/sa02m-pre-start.service` ExecStop (system time -> DS3231 via `sa02m-rtc-sync.sh`) never runs at shutdown** — same cause as the shutdown-marker defect (BUGLOG 2026-10-23): `DefaultDependencies=no` with no `Conflicts=shutdown.target`/`Before=shutdown.target`, so nothing stops the unit at shutdown; the shutdown half of the RTC flow described at `scripts/01-system.sh` ("Shutdown: sa02m-pre-start.service ExecStop → DS3231") is dead. Not fixed with the marker because arming it adds an i2c-2 write to every shutdown on a bus known to wedge (BUGLOG 2026-09-30) and the reboot-hang investigation is open: the fix needs a bounded ExecStop (`timeout`/`TimeoutStopSec=`) and a hardware check. Carried as the one `KNOWN_OPEN` entry of the `unit-shutdown-semantics` gate — the entry fails the gate once the unit is fixed, so it is removed in the same change.
- [OPEN] 2026-10-24 **[MED] OTA does not deliver `sa02m-shutdown-marker.service`** — the 1.0.7.10 fix (Conflicts=shutdown.target, `%%`) reaches a board only through `install.sh` / `--refresh` (`scripts/01-system.sh`); a board updated by «Обновление веб» alone keeps the broken unit and keeps logging every reboot as «unexpected». Operator decision 2026-10-23: add it to the OTA allowlist, deploy-map and the `ota-dst-allowlist-parity` gate as a separate change after 1.0.7.10 merges.
- [OPEN] 2026-10-24 **[MED, Operator decision] A crashed MQTT bridge leaves the light-music strip looking online.** `opt/sa02m-audio/sa02m_audio/service.py` `_led_online` checks only the strip's own `meta/error`; the bridge's last will flags only `/devices/sa02m-bridge/meta/error`. A session then reports `running` and writes to nobody; a restore sent then is lost and its session file deleted, leaving the strip on effect 75. Options: state the limit in `docs/contracts/audio-recorder.md` §22, or also watch the bridge flag. (Ship review round 4, A1.)
- [OPEN] 2026-10-24 **[LOW] A strip report is never taken back** — an empty (delete) payload after a report leaves the strip in `led_reported` (`service.py`), so a strip whose topics the bridge removes stays «online» until the daemon restarts. (Review round 4, A2.)
- [OPEN] 2026-10-24 **[LOW] www-only pscp fallback check is weaker than the risk it names** — `docs/deployment.md` www-only step 2 warns about unpushed content but checks only `git status`; also compare `HEAD` with `origin/<ref>`. (Review round 4, A4.)
- [OPEN] 2026-10-24 **[LOW] `scripts/lib.sh` `sa02m_normalise_sudoers_modes` logs its read-back at OK level** even when the chown did not land (the runner now WARNs; gate 8d covers the runner only). Make the installer WARN the same way and extend 8d to the lib path.
- [OPEN] 2026-10-24 **[MED] `opt/sa02m-modbus-mqtt/tests/test_poll_intent_refusal.py` is flaky under load** — failed twice as nobody while another suite ran and left a multiprocessing child blocked on a socket (killed after ~14 min); a CI hang risk. Its cleanup should kill the broker/bridge children.
- [OPEN] 2026-10-24 **[INFO, other repo] Three stale hardpy acceptance tests** (`CYNTRON-git/hardpy_tests`, run against bench 1.136 on 2026-10-24): `test_61 test_sa02m_request_key_matches_mac` knows only the MAC-fallback key form `000C0000+MAC` (the healthy form is `00930000+SystemKey`); `test_67 test_sa02m_kernel_switch_reboot` reads the kernel state before the board actually went down (`reboot_pending 1`); `test_71` reuses a panel session obtained before an earlier test's reboot (`unauthorized`; passes run alone). Also: the image lacks `memtester` and `sa02m-hw-set`, so two tests skip — Operator to decide whether the golden image carries them.
- [OPEN] 2026-10-24 **[LOW, Operator decision] `components.json` entry for the LED firmware repo `RGBW_WS2812@32ed509`** (approved in a lost 1.0.7.9 session) was not rebuilt; `opt/sa02m-led/sa02m_led/led_mb2ws.py` cites that repo.
- [OPEN] 2026-10-24 **[MED, advisory] Decomposition worklist — THE one home (audit 2026-10-24 F2; supersedes the 2026-08-28 worklist, the 2026-09-16 M8 and the 2026-10-14 C-5 entries).** Threshold 800 lines for code (no size row): 80 tracked code files over it, 55 shipped non-test (2026-10-24). Growth since 2026-09-24: `devices.js` 2618→4190, `mqtt.js` 2775→3995, `etc/sa02m-update-runner.sh` 2308→3213, `main.css` 6069→6990. By cohesion, behaviour net first, every split through the normal loop (`.ai-dev/procedures/decompose.md`):
  1. `opt/sa02m-audio/sa02m_audio/service.py` 1331 L (one `Daemon`, ~70 methods) — light-music controller (`_light_*`, `light_*`, `_led_*`, `_lm_set`, `observe_led`, `_snapshot_ready`, `_restore_strip`, `_*_session`, `_end_stream`; `:670-987`) → a `LightMusicSession` in `lightmusic.py`; mic gain (`_gain_*`, `_sync_gain`, `_conf_warnings`; `:1025-1149`) → `mixer.py`'s side. Pinned by `py-unit-audio` + `audio-cgi`; fold in the `_load_session` swallow (2026-10-24 entry below).
  2. `devices.js` 4190 L, 178 functions → an ES-module cluster like `flasher/`: the history chart engine (`devices/charts.js`, pinned by `js-unit-devices-charts`), the Carel card/window, the CE card, the MTD presence card.
  3. `mqtt.js` 3995 L, 208 functions → continue the `mqtt/` cluster (`tcp-dialog.js` exists): the scan flow, the device-add modal + per-family builders, the live monitor, the `type:template` picker, broker+credential settings.
  4. `etc/sa02m-update-runner.sh` 3213 L → keep the bash single-file (the delivering update runs the INSTALLED runner — `self_reexec_before_deploy` copies one file — so a sourced-lib split gives old-runner + new-lib skew; three harnesses extract single functions by awk marker, `test-update-recover-rollback.sh:46`); move the embedded Python out — `prepare_github_overlay` (316 L) and `run_validate_and_extract` (301 L), i.e. the manifest builder (`map_dst` + `DST_RE`) and the validator's second `DST_RE`/`DEL_RE`/`PRESERVE` — into one module in `opt/sa02m-update/lib/` next to `validate_package.py`; fix there audit 2026-09-28 L3 (`map_dst` sends the non-unit drop-ins `etc/systemd/sa02m-journald.conf` / `sa02m-watchdog.conf` to `/etc/systemd/system/` on every OTA).
  5. `flasher.js` 6025 L → the next windows after `flasher/led.js` / `flasher/mtd.js`: Carel, MR module-config, scan/flash job UI vs port lease.
  6. `app/status.js` 2825 L → `app/webupdate.js`, `app/mplcdeploy.js` (the XHR CSRF entry lands there naturally).
  7. `status.cgi` 2748 L → `gather_services_metrics` (248 L) into a sourced `lib_status_services.sh`, keeping the O(1)-process rule.
  8. `scripts/lib.sh` 1568 L → along its own section banners (`:67` subnet, `:132` resolvconf, `:262` apt, `:387` sudoers, `:609-674` watchdog block, `:794-806` policy/profile/port, `:1020` service state); mind the rows whose `covers` name `scripts/lib.sh` and the runner twins (`cleanup_b1_deploy_artifacts` vs `sa02m_cleanup_b1_deploy_artifacts`).
  9. Watch list (cohesive or single-purpose for now): `flash_protocol.py` 2517 (three drivers), `device_config.py` 2302, `app/smarthome.js` 2301, `led_mb2ws.py` 1957, `sa02m_telemetry.py` 1835, `etc/sa02m-web-service-ctl.sh` 1755 (the Node-RED block), `scanner.py` 1735, `alice/config/api.py` 1705, `flasher/runner.py` 1686, `rules/engine.py` 1323, `rules/store.py` 1185, `device_history_db.py` (ranges/schema/write/query), `bridge_mqtt.py` (the `/meta` blob cluster → `bridge_meta.py`, covered by `test_meta_blob.py`); gate files `ui-layout.mjs` 1577, `sudoers-pin-contract.sh` 1513.
  10. **Operator decision owed:** `main.css` 6990 L has reached the «re-judge at 7k» trigger — split vs accept (sectioned, one token root; a split costs one link tag plus a `?v=`/`&r=` bust per file on the no-build stack).
  Already done, do not re-raise: `modbus_mqtt_bridge.py` (3422 L → 298, split into `bridge_*.py`), `app.js` (F10, a ~389 L core + the `app/` cluster).
- [OPEN] 2026-10-24 **[LOW] Keep `origin/1.0.7.10` — it holds the verification records the squash lacks (audit 2026-10-24 F5).** The squash `d9ad2c8a` (295 files) carries a two-line body without the three `git-commits.md` sections; the RED/GREEN records `quality-gate-rigor.md` «Record the mutation» requires live only on `origin/1.0.7.10` (69 commits) and PR #217. Every earlier squash on `main` (`64dcf148`, `4e4779e8`, `dff2c0d2`) concatenates the per-commit bodies — restore that step at the next squash.
- [OPEN] 2026-10-24 **[LOW] Three contracts are in no quality row's `covers` and named by no row (audit 2026-10-24 F7).** `docs/contracts/gateway-transport.md` (run by `py-unit-gateway` → `test_transport_lifecycle.py`, named at `:79`), `port-ownership.md` (`port-coordinator`; `py-unit` → `test_poll_intent_refusal.py`), `status-polling.md` (`status-reliability`, `status-reliability-browser`, `status-cache-metadata`, `status-coordinator-projection`) — an edit to one selects no row under `--touched`. Shape (c) inside the last: `scripts/dev/test-status-reliability.mjs:163` `BENCH_TTL_S` copies the per-part TTLs of `status.cgi:2702-2737`, but `status-reliability` covers only `static/js/` and its own script, so a TTL change in `status.cgi` neither re-runs nor fails it. Fix: name each contract (and `status.cgi` for `status-reliability`) in the rows' `covers`, or derive the TTLs from `status.cgi`.
- [OPEN] 2026-10-24 **[LOW, security] Dead `/etc/nginx/.htpasswd` rewritten on every install (audit 2026-10-24 F9).** `scripts/03-webserver.sh:24-29` writes `admin:<apr1 hash of ADMIN_PASS>` on every run and nothing reads it (no `auth_basic` under `etc/nginx/`) — a dead file holding a password hash, reset to the default on `--refresh`. Fix: stop writing it and remove it on install (the installer is the upgrade path).
- [OPEN] 2026-10-24 **[LOW] `sa02m-domain.md` tab table misses three cards (audit 2026-10-24 F10).** «Subsystems behind the tabs» (`docs/agent-rules/sa02m-domain.md:80-89`) claims to be the one home for tab ↔ frontend ↔ backend but has no row for HomeKit (`app/homekit.js`), Home Connect (`app/homeconnect.js`) or the agent API (`app/agentapi.js`) and their CGIs.
- [OPEN] 2026-10-24 **[LOW, product — Operator] `docs/product.md` §6 predates six shipped capabilities (audit 2026-10-24 F11).** `:61-73` (last touched 2026-09-29) lists seven value items and nothing about the audio recorder and light music (1.0.7.4 / 1.0.7.10), HomeKit, Home Connect, the LED strip or the agent API. Offer the Operator a product-discovery update; the brief is not written without them.
- [OPEN] 2026-10-24 **[LOW] Two silent swallows on persistence paths (audit 2026-10-24 F12).** `opt/sa02m-cloud-agent/sa02m-cloud-agent.py:103-107` `_write_status` — `except Exception: pass`, no log (the panel can still age the status by `ts`, `docs/contracts/cloud-agent-status.md:20`, but the cause is invisible); `opt/sa02m-audio/sa02m_audio/service.py:952-955` `_load_session` — `except OSError: return` treats EACCES/EIO like ENOENT, so a crash-left light-music session that cannot be read is dropped with no log line and the strip is never restored. Fix: log the failure; for the session, return silently on ENOENT only (folds into worklist item 1 or the queued sa02m-audio hardening branch).
