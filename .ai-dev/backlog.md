# Backlog — SA-02m web interface

Recorded findings and deferred work (`.ai-dev/procedures/backlog.md` owns the
format). One status per finding: `- [OPEN|RESOLVED] <date> <item>`. Resolved
entries are pruned (history lives in git); last prune 2026-09-23 (1.0.6.51 — 34 entries
verified fixed by the whole-backlog triage removed; evidence per entry in that commit's body).

## Open

- [OPEN] 2026-09-16 **[HIGH] Audit 2026-09-16 (whole tree at 1.0.6.48) — H1: branch-protection floor half-wired and already bypassed.** Live `gh api …/branches/main/protection`: `quality` is required but `enforce_admins:false`, `strict:false`, no review rule; `1dfa503` (2026-09-10, «1.0.6.46», 13 files incl. `bridge_fmb.py`/`bridge_mr02m.py`) sits on `main` with NO PR (direct push, no review stamp evidence). `.ai-dev/notes/ci-budget.md:32` claims `enforce_admins: true` — false. Fix: set `enforce_admins=true` + `strict=true` on the forge (Operator's word), correct the note, retroactive Reviewer pass over 1.0.6.46's diff.
- [OPEN] 2026-09-16 **[HIGH] Audit H2: GitHub Actions billing-locked since 1.0.6.24 (2026-08-28 20:47, run 33209713129), not 1.0.6.40.** Last green = 1.0.6.23. 25 releases + 3 side branches merged with the required check never executed; the substitute (local suite) runs on Windows where `shellcheck`, `ui-layout`, two `web-auth-behaviour` asserts and `install-atomic` 8b skip. Not recorded durably until now. Operator action: unlock billing; until then run the substitute under WSL/Linux where possible (node is absent in WSL today).
- [RESOLVED 2026-09-16, 1.0.6.49 — the workflow installs the harness + chromium; the first real run is the proof (CI billing-locked, H2)] 2026-09-16 **[HIGH] Audit H3: CI cannot go green even after unlock** — `.github/workflows/web-quality.yml` installs no playwright/chromium while review rows `cloud-card-smoke` (`scripts/dev/cloud-card-smoke.mjs:47,143`) and `sh-modal-layout-smoke` (`:33,292`) exit 2 without it. Latent since 1.0.6.28. Fix: `npm run ui-layout:install` (+ chromium deps) in the workflow.
- [RESOLVED 2026-09-16, 1.0.6.49 — overlay synced, row `firstboot-overlay-parity`] 2026-09-16 **[MED] Audit M1: firstboot-overlay drift** — `tools/imaging/firstboot-overlay/usr/local/sbin/sa02m-eth-coldboot.sh` (76-line diff vs `usr/local/sbin/`: no resolver belt/bootstrap shim) and `…/usr/local/bin/fix-eth.sh` vs `etc/fix-eth.sh` (no `dns_ensure`) are stale; `tools/imaging/autorun.sh:55-60` copies them over a fresh rootfs, so clones flashed via that path lose the `docs/contracts/boot-network-dns.md` guarantee. No registry row covers the overlay copies (the expand script is the only pinned pair). Unchecked twins: `sa02m-failure-monitor`, `sa02m-userspace-watchdog`. Fix: overlay copies byte-identical to their homes + an overlay-parity row.
- [RESOLVED 2026-09-16, 1.0.6.49 — both CGIs POST-only + token; rows `cgi-csrf-policy` + `cgi-csrf-behaviour`; BUGLOG 2026-09-17] 2026-09-16 **[MED] Audit M2 (security): CSRF gaps** — `www/network_config/cgi-bin/mqtt_scan.cgi:19-30,66` runs `sudo python3 mqtt_bus_scan.py` on POST AND GET with no `web_csrf_*` call (the GET-mutation class `docs/decisions/selective-csrf-policy.md:24` declared closed); `web_update_check.cgi:25` POST→sudo without a token. Only `web-update-csrf-contract` gates one endpoint; `docs/threat-model.md` §4 «держится (1.0.5.72)» over-claims. Fix: set-wide row (every sudo/mutating CGI ⇒ POST-only + `web_csrf_*`, recorded exemptions) + the two endpoints.
- [RESOLVED 2026-09-16, 1.0.6.49] 2026-09-16 **[MED] Audit M3: threat model stale** — §5 (`docs/threat-model.md:341-345`) says cloud is latent/backend not working, contradicted by §3's 2026-09 entries and the bench; `:391-392` says the cloud deploy is absent from deployment.md while `docs/deployment.md:682-700` has the unbind runbook; `:199` cites a deleted transient; no §4 row for LAN→1883→`/devices/<id>/controls/{do,beeper,alarm_led}/on` (the 1.0.6.42/43 path).
- [RESOLVED 2026-09-16, 1.0.6.49 — pruned, shipped items closed] 2026-09-16 **[MED] Audit M4: backlog hygiene** — header says resolved entries are pruned, 20 `RESOLVED` remain; OPEN-but-shipped items: headless-smoke (script gone), rs485 consumer row (exists), «docs say 3 rows» (fixed), architecture.md missing (stub exists), bundle list (fixed), i18n/html-id rows (exist). Prune on the next branch.
- [RESOLVED 2026-09-16, 1.0.6.49 — docs half: the bench facts landed in `docs/deployment.md`; the pointer rewrite is the orchestrator's] 2026-09-16 **[MED] Audit M5: state pointer journals** — DONE blocks and bench runbook facts (stick G:, donor ssh keys, `D:\` paths) belong in `docs/deployment.md`; cadence marker omits the 2026-09-08 line audit (CHANGELOG:357-361). Rewrite the pointer as a pointer.
- [RESOLVED 2026-09-16, 1.0.6.49 — §0/§3/§6 current; values 5–7 await the Operator's `[?]`] 2026-09-16 **[MED] Audit M6: product brief stale** — `docs/product.md` §0/§3/§6 omit cloud, Alice/«Умный дом», scenarios, «Устройства»; `lite` reviews judge product fit against it.
- [OPEN] 2026-09-16 **[MED] Audit M7: verification coverage** — no dependency-CVE row (pip deps installed unpinned `scripts/lib.sh:739-763`; Node-RED payload; frpc) — re-rank the 08-28 LOW item; 16 mutating/root-capable CGIs (cmd_exec, hw_set, kernel_ctrl, reboot, restart, web_creds, web_factory_reset, storage_format_set, mqtt_config, mqtt_ctrl, cpu_profile, gateway_ctrl, web_update_upload/cancel, ssh_debug, web_backup) have zero behavioural test references. **Operator decision owed:** add `pip-audit` / `npm audit --package-lock-only` rows, or record the accepted risk (1.0.6.49 plan: no row until decided). The CGI half now has a shape to reuse — `cgi-csrf-behaviour` covers two of the 16; the rest stay uncovered.
- [OPEN] 2026-09-16 **[LOW] Audit M8 (advisory): module size** — 47 files > 800 lines (40 on 08-28): main.css 6055, flasher.js 5863, status.js 2584, smarthome.js 1873, config/api.py 1411, engine.py 1117; gate files too (test_binding_reset.py 1735, sh-modal-layout-smoke.mjs 1209, sudoers-pin-contract.sh 1148, ui-layout.mjs 1062). Decompose worklist (`.ai-dev/procedures/decompose.md`), by cohesion.
- [RESOLVED 2026-09-16, 1.0.6.49 — L1–L5 landed; the workspace dimension is its own OPEN entry below] 2026-09-16 **[LOW] Audit L1–L5** — L1 `docs/SA02M_IMAGING_GUIDE.md:1179` «Этап 1 — MVP (текущий)» stale; L2 `tools/imaging/_run_reset_cloud.py` tracked on a gitignore exception, no backlog line; L3 seven contracts not in their gate's `covers` (a contract edit never re-runs its row under `--touched`); L4 no `transient-hygiene` row (inert now); L5 `.ai-dev/notes/quality-gate-environment.md` lacks the two smoke rows. Orchestrator's own dimension: 9 git worktrees (3 nested under `carel-smart-home-support-3e146c/.ai-dev/worktrees/`, 4 `claude/*` on stale commits), local branches without upstream (`1.0.6.33–36`, `fix/alice-room-membership`), local `main` behind origin — prune once the Operator confirms which sessions are dead.
- [RESOLVED 2026-09-16, 1.0.6.49 — (1) §12 NOCSUM/ERR, (2) `rm -f "$RESULT.tmp"` at start + harness 6e, (3) CHANGELOG, (4) headers collapsed, (5) two smoke rows in the environment note] 2026-09-16 **[LOW] Reviewer advisories of the 1.0.6.48 review — next fixup.** (1) the verdict file may carry `NOCSUM`/`ERR` besides `OK|BAD` (`etc/sa02m-rootfs-expand.sh` `write_result`, `sb_csum_of_block`), but `docs/deployment.md` §12 / CHANGELOG / BUGLOG describe only `OK | BAD` — one clause in §12. (2) a `.result.tmp` can survive a power cut between the temp write and the rename commit and nothing removes it later — `rm -f "$RESULT.tmp"` at `start`. (3) CHANGELOG/§12 do not say the flash→cut→boot re-run of the verdict file is deferred to the next golden capture. (4) the bench cause is restated in six places pointing to BUGLOG 16:40 — collapse the script/harness headers to one clause + pointer. (5) `.ai-dev/notes/quality-gate-environment.md` lacks rows for `cloud-card-smoke` / `sh-modal-layout-smoke`, which FAIL (not skip) without playwright. Source: `.ai-dev/reviews/1.0.6.48_review.md` (transient).
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
- [RESOLVED 1.0.6.50] 2026-09-20 **[MED] Every Carel device declares a fan binding its controller cannot have.**
  `opt/sa02m-alice/sa02m_alice/config/ahu_status.py` `_FLOAT_ROWS` emits BOTH
  `("fan_speed","fan_supply","unit.percent")` and `("fan_step","fan_step","unit.step")` for every
  `kind=carel` device, while `docs/contracts/carel-ahu.md` §5 says `fan_supply` exists only on crst
  (c.pCOmini) and `fan_step` only on uAria. Measured on bench 1.135: COM3-1 (crst) publishes
  fan_supply 80.0 / no fan_step, COM3-2 (uaria) publishes fan_step 7 / no fan_supply — the DATA is
  family-correct, only the declaration is not. Inert today (both rows `cloud_only`, the absent one
  reports `present:false`), and it becomes a live defect when the Alice fan capability ships: a uAria
  would declare a percent control nothing publishes and `_wb_fan` cannot write (no `HR_FAN_SUPPLY`).
  FIX: make the two fan rows family-aware — `_OPTIONAL_FLOAT_ROWS` (:62-65) is the existing
  mechanism — and correct the contract in the same change.
  **Second finding, same root:** `carel-ahu.md` §7 claims the Devices tab picks the family by the
  PRESENCE of `fan_step`. It does not: `www/network_config/static/js/devices.js:698` branches on
  `d.family === "uaria"`, and `bridge_carel.py:69` resolves that field from the YAML entry or the
  FC17 signature. The tab is correct today; the CONTRACT is wrong and is what sent the cloud session
  hunting a symptom that does not exist. No `devices.js` change is needed.
  **Hard constraint from the cloud session — do NOT drop the float rows:** `fan_speed`/`fan_step` are
  not valid Yandex float instances, so both rows must keep `cloud_only: true`; an out-of-schema
  Discovery response makes every device on that account vanish from the app.
  **[SUPERSEDED — see «CLAIM RETRACTED» below: that last clause is not true as written.]**
  The float-row instruction itself stands; only the stated consequence was withdrawn. Shipment 2 adds ONE
  `devices.capabilities.mode` (instance `fan_speed`) BESIDE them, percent-backed on c.pCOmini and
  step-backed on uAria. Open on the cloud side: the value vocabulary (per-family vs shared) — their
  Operator's product call.
  Honest note for the CHANGELOG when this ships: the narrowing alone changes nothing a user sees
  (`present:false` and «absent» render identically); its value is a truthful declaration and a safe
  shipment 2. Scope of any config write: bench 1.135 ONLY (six Skolkovo boards are on 1.0.6.37 and
  10 of 13 enrolled devices have been offline since 09-07/08). Deploy order: board → cloud.
  **UPDATE 2026-09-20 — the cloud Operator merged the two shipments into one and settled the
  vocabulary.** One release: family-aware float rows AND the Alice capability, verified on the bench
  in one pass. One shared vocabulary for both families, `devices.capabilities.mode` instance
  `fan_speed`, five values — quiet/low/medium/high/turbo = 20/40/60/80/100 % on crst and
  steps 2/4/6/8/10 on uAria. `auto` deliberately absent (no auto register on c.pCOmini).
  Read-back clamps to NEAREST, ties round up. Constants VERIFIED here, and note the path their brief
  gives is wrong — they live in `opt/sa02m-carel/sa02m_carel/carel_ahu.py`, not under
  `opt/sa02m-modbus-mqtt/`: `FAN_PCT_MIN=20.0` (:103), `FAN_PCT_MAX=100.0` (:104),
  `UARIA_FAN_STEP_MIN=1` (:105), `UARIA_FAN_STEP_MAX=10` (:106), `HR_FAN_SUPPLY=53` (:44),
  `HR_UARIA_FAN_SP=197` (:69, USINT steps 1..10). Their caveat is correct: step 1 = 10 % is below
  anything a c.pCOmini accepts, so the unified scale starts at 20 %.
  **Wart to record in the plan, inherent to 5 names over a continuous range:** read-back and set are
  asymmetric at off-vocabulary values — a uAria at step 1 reports `quiet`, but tapping `quiet`
  writes step 2 and MOVES the fan; a c.pCOmini at 73 % reports `high`, tapping `high` writes 80.
  Acceptable, but it must be stated, not discovered by a customer.
  Note for the Builder: `.ai-dev/quality/checks/carel-shared-home.sh` gates the one-home rule for the
  Carel register map — the new mapping module must not open a second home for these constants.
  **SETTLED 2026-09-20 (cloud Operator).** The read/write asymmetry is ACCEPTED AS DESIGNED —
  nearest-clamp on read, unconditional write on set — chosen over the per-family `quiet`=step 1
  alternative because that one fixes a single case and buys back the cross-family divergence the
  «приведи к единому виду» instruction existed to remove. **Binding: this is contract text, not a
  code comment** — it goes into `docs/contracts/carel-ahu.md` beside the mapping table, in
  customer-facing wording («выбор режима, в котором устройство уже показано, всё равно записывает
  значение этого режима; на промежуточных значениях это сдвинет вентилятор»). Without it the first
  support ticket becomes an investigation, which is the cost the decision was made to avoid.
  Final brief: one shipment; one `devices.capabilities.mode`/`fan_speed` BESIDE the float rows
  (which keep `cloud_only: true`); five values 20/40/60/80/100 % ≡ steps 2/4/6/8/10; no `auto`;
  constants CONSUMED from `opt/sa02m-carel/sa02m_carel/carel_ahu.py` (a restated 20 or 10 is a second
  home and turns `carel-shared-home` red); `_FLOAT_ROWS` narrowed per family first; §5/§7 corrections
  ride along; config write on bench 1.135 only; deploy board → cloud. Joint bench pass agreed:
  COM3-2 `fan_step` 7→6→7, then COM3-1 `fan_supply` 80→75→80 (both off-vocabulary on purpose —
  they exercise nearest-clamp while proving the raw value still reaches the cloud intact).
  **BENCH PASS DONE 2026-09-20 17:48-17:52 (joint, cloud session drove the writes, this session
  watched the wire).** uAria COM3-2 `fan_step` 7→6→7 and c.pCOmini COM3-1 `fan_supply` 80→75→80,
  both confirmed by two independent watches; unit stayed running, supply temp unchanged; the
  off-vocabulary 6 and 75.0 reached the cache EXACT, so nearest-clamp is a presentation rule only —
  measured, not inferred. Line behaviour: a write costs ~5 extra frame errors per 2-min window and
  zero offline events on COM3-1; COM3-2's two offline events sat inside its own rate (9 per 80 min).
  Recorded as «bus disturbance NOT SUPPORTED» rather than «ruled out» — one clean pair of windows is
  evidence, not proof. NOT exercised today (nothing to exercise yet): the Alice capability, the
  mapping, a tap from the app.
  **NEW REQUIREMENT that ships WITH the capability — write-backs are silent in the journal.** Four
  writes across both families left zero trace: no arriving topic/payload, no resolved register, no
  clamped value, no retry count, no outcome. `_wb_done`/`_wb_write_retry` in `bridge_carel.py` carry
  no logging. Tolerable while only a deliberate MQTT publish reaches the path; unacceptable once a
  customer's app button moves a real fan on a line that loses ~1.5 % of frames — both «it moved and
  we do not know why» and «it did not move and we do not know why» become unanswerable, and the log
  is what keeps the ACCEPTED read/write asymmetry cheap to support. Log spec (cloud session's
  wording, better than ours): arriving topic + payload, resolved register + clamped value, whether
  the retry wrapper fired and how many attempts, outcome. Write path only — do not add noise to the
  poll loop, which already logs its own failures.
  **Deploy risk lowered:** the cloud half is labels-only (`fan_step` gets «Ступень вентилятора» /
  «Ступ.», `unit.step` keeps its empty suffix) and is independent of ours in BOTH directions, so
  board → cloud is a preference for coherence, not a dependency — if our release lands first their
  page simply keeps its current labels. **Offered and ACCEPTED for ship time:** before/after the
  1.135 config write they re-run their bench chain against our build (board conf → hub catalogue →
  built function, byte-compared) — the check that catches a mapping module quietly changing what the
  catalogue publishes.
  Operator's go given 2026-09-20 (full cycle + bench pass). Branch `1.0.6.50` cut; planning under way.
  **CLAIM RETRACTED 2026-09-20 (cloud session), superseding the «Hard constraint» paragraph above.**
  «An out-of-schema Discovery response makes every device on that account vanish from the app» is
  NOT established: it lived in their repo as two docstrings with no primary source, no recorded
  observation, and nobody had measured it. **Their research has since CONCLUDED (2026-09-20)**;
  the conclusion, attributed to them and with its missing citations stated, is recorded once in
  `docs/contracts/carel-ahu.md` §6. The BEHAVIOUR the retracted claim was used to justify is
  unchanged — `cloud_only` on both float rows, the mode item validated at write time — and now
  rests on the cost that conclusion names (one rejected device until the next Discovery), not on
  the retracted one. The verified fact that should carry the weight instead: Discovery
  is ON DEMAND, so nothing reaches an app until the user taps «Обновить список устройств» (their
  `docs/yandex-smart-home-rules.md` §5; our own record of the same behaviour is in
  `docs/contracts/alice-mqtt-mapping.md`). Every site where THIS repo stated the claim as fact was
  introduced on branch 1.0.6.50 and is corrected in it (`config/models.py`, `config/ahu_status.py`,
  `tests/test_models.py`, `docs/contracts/carel-ahu.md`); `git show origin/main` carries none of it.
  **RESOLVED in 1.0.6.50.** Family-true fan rows + one `devices.capabilities.mode`/`fan_speed`
  (shared four-name ladder, positions written once, the percent side DERIVED from the map limits,
  in the new one-home module
  `opt/sa02m-carel/sa02m_carel/carel_fan.py`), the write-back audit trail across every Carel
  handler, the mode schema closed in `models.py`, and the contract corrected — §6 (not §5: §5 was
  already right) plus the §7 layer clarification. Two further findings fixed in the same change,
  both recorded here because the next sweep would otherwise re-derive them:
  (1) the brief's claim that «a restated 20 or 10 turns `carel-shared-home` red» was FALSE at HEAD —
  case 5 swept two coil needles, neither a fan constant; widened, and the registry text corrected in
  the same change;
  (2) that same sweep was COMMENT-BLIND — `grep -rlF` counted `#FAN_PCT_MIN = 20.0` as a definition,
  so its non-vacuity half could not be turned RED by the one mutation it exists to survive, and a
  commented-out copy elsewhere read as a live second home. Every hit is now confirmed through
  `lib_check.sh`, and the case is registered in `comment-mutation-proof`.
  STILL OPEN, carried out of this entry into its own line below: the `comment-mutation-proof`
  enumeration gap for `04-flasher.sh` (plan F4), and the exact official Yandex value set for
  `devices.capabilities.mode` instance `fan_speed` (plan F5 — we admit auto/quiet/low/medium/high/
  turbo and declare only the five; the set was NOT verified against a canonical source).
- [OPEN] 2026-09-20 **[LOW] `comment-mutation-proof` does not case `carel-shared-home` for
  `04-flasher.sh`.** Cases exist for `05-mqtt.sh`, `06-alice.sh` and `update-www-only.sh`, though
  case 1 of the gate pins `04-flasher.sh` identically — so a commented-out installer call there is
  the one of the four the mutation proof does not measure. Same shape as the widening done in
  1.0.6.50; deliberately left out of that change, which already carried two structural forks.
  FIX: one CASES row plus the run that proves it RED. Own branch.
- [RESOLVED 1.0.6.50] 2026-09-20 **[LOW] The Yandex value set for `devices.capabilities.mode` /
  `fan_speed` is unverified.** Verified at source during review round 2: `auto` IS in the
  recommended set, and the per-instance lists are ADVISORY («Допускается использовать любые
  комбинации режимов работы и функций без ограничений»), so a wider allowlist is not a hole.
  `models.MODE_INSTANCES` keeps auto/quiet/low/medium/high/turbo and the comment now cites
  https://yandex.ru/dev/dialogs/smart-home/doc/ru/concepts/mode-instance and .../mode-instance-modes
  instead of «unverified». Excluding `auto` from what we DECLARE is a product choice (no auto fan
  register on a c.pCOmini), not a schema limit. Same round: the vocabulary itself became the
  recommended four (low/medium/high/turbo = 20/40/70/100 % = steps 2/4/7/10); `quiet` was the one
  value outside the recommended set and was dropped.
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
- [OPEN] 2026-09-23 **[LOW] `scripts/sync-app-version.py` writes CRLF on Windows.** It calls
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
- [OPEN] 2026-09-23 **[MED] Bench 1.135 COM3 answers ~500 short/CRC polls per hour, bus-wide, and
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
- [OPEN] 2026-09-09 **[MED] Two live-path `install -m` sites under `etc/` remain**, and
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
- [OPEN] 2026-09-09 **[LOW] 8D step F (install lock) not built.** The installer does not
  hold `/run/sa02m-imaging.lock` for its run, so the userspace watchdog is not told to
  stand down. Class-level measure, not this incident's trigger (nothing in A–E/G
  depends on it).
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
  (b) Оставить как есть и держать одним домом в `docs/deployment.md`: site-файл nginx приезжает полной
  установкой (`03-webserver.sh`), www-only из чекаута с `etc/` + `opt/sa02m-devices/`
  (`update-www-only.sh` → `11-devices.sh`), офлайн-пакетом и образом — никогда GitHub-OTA; правка
  nginx, которая должна дойти до флота, едет одним из этих путей. Until decided, the texts say (b).
- [OPEN] 2026-09-24 **[LOW] `docs/contracts/cloud-panel-proxy.md` carries a placeholder for the
  cloud's commit.** The cloud fix (forward the `X-SA02M-` header family) is their PR #120 (cloud
  0.18.4, branch adc9b2c — a branch hash, they squash). Replace «cloud commit: <to be filled after
  the cloud merge>» with their `main` commit once they send it; the marker in the file is the only
  reminder (transient-hygiene does not scan prose).
- [OPEN] 2026-09-24 **[LOW] Two load-induced harness flakes.** (1) `test-web-update-apply-guard.sh`
  R1: the live-runner fixture was `sleep 30`; under quality-runner load section R reached it 44–50 s
  later → false RED. FIXED in 1.0.6.52 (`sleep 900`). (2) `test-web-auth.sh` case 59 «NOT locked
  after MAXFAIL failures — brute force is unthrottled» FAILED once inside a full `build` beat on
  2026-09-24 (87/88) and passed on the immediate re-run and every later run — a timing window of
  the throttle test under load, class (1); not reproduced, not investigated. When it recurs: read the
  case's time budget against the throttle's window and pin the fixture like R1.
- [OPEN] 2026-09-23 **[MED] `sa02m-devices-api` listens on `127.0.0.1:8765` with no auth of its
  own.** `opt/sa02m-devices/sa02m_devices/api.py` reads only Content-Length and relies entirely on
  nginx's `auth_request` in front of `/api/devices*`; any local process or user on the board can
  open the loopback port and bypass the panel's session. Found while sweeping the `X-SA02M-Auth`
  class (1.0.6.53): a different class (unauthenticated loopback TCP), the same shape the Alice config
  API had before it moved to a root-only unix socket (1.0.6.24, `88032f4`). Fix direction: the same
  move (AF_UNIX socket, 0660 root:www-data) or a shared local secret set by nginx only. Threat model
  row to add with the fix.
- [OPEN] 2026-09-23 **[LOW] XHR upload paths have no CSRF refresh-and-retry of their own.** The
  panel's two XMLHttpRequest uploads (`status.js` ~:1756 and ~:2318, offline package / MPLC project)
  read the refreshed token but bypass the fetch wrapper, so an E_CSRF there still ends the upload
  with a plain error instead of the 1.0.6.53 refresh-once-retry-once path. Route them through the
  same reaction or document the difference in `docs/contracts/cloud-panel-proxy.md`.
- [OPEN] 2026-09-23 **[LOW, honesty] `ui-layout` reports PASS when Playwright is absent.** In a
  checkout without `scripts/dev/node_modules` (a fresh git worktree, 2026-09-23) the review beat
  printed `ui-layout: skipped — playwright not installed` followed by `PASS  ui-layout`, while
  `cloud-card-smoke` and `sh-modal-layout-smoke` in the same state correctly FAILED with «chromium/
  playwright missing». quality-gate-rigor.md: a skipped row is reported as skipped, never as
  passed. Fix: exit non-zero (or the runner's SKIP status, if it has one) when the driver cannot
  run; add the case to `run.test.mjs`. Queued for R58 (gates and tools).
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

<!-- Whole-project audit 2026-08-28 at 1.0.6.23 (3 parallel auditors: contracts,
     security, docs). Suite was GREEN (build 47/47, review 6/6) and branch
     protection verified live — every finding below is what green does NOT cover. -->

- [RESOLVED 2026-09-16, `373a2f9` — all six grant homes read] 2026-08-28 **[HIGH] The B1 escalation gate reads 1 of the 6 homes that grant
  www-data root — hollow ratchet #9, on a security-load-bearing claim.**
  `.ai-dev/quality/checks/sudoers-pin-contract.sh:29` reads only `etc/sudoers.d/sa02m-www`.
  The other homes: `etc/sudoers.d/sa02m-cloud`, `etc/sudoers.d/sa02m-mqtt`,
  `scripts/06-alice.sh:105`, `scripts/06-gateway.sh:47-51`, and a RUNTIME APPEND at
  `etc/sa02m-web-update-apply.sh:308`. The `sa02m-www` header claims to be "the COMPLETE,
  single-home" grant list — false. This is why the two BLOCKERs above are green today, and why
  `docs/threat-model.md` still says the escalation class is closed (B1). Fix: extend the gate to
  all six homes and prove it RED against the two injections BEFORE fixing them.
- [RESOLVED 2026-09-16, `4686bf3` + row `comment-mutation-proof`] 2026-08-28 **[HIGH] Five safety gates are defeated by putting a comment mark in front
  of a line.** Mutation-proven GREEN on comment-out (16 of 22 mutations correctly went RED;
  these 5 did not): `mplc-ota-deploy-contract.sh:20`, `mplc-project-deploy-contract.sh:36`,
  `kernel-policy-contract.sh:177`, `health-gate-operator-disabled.sh:28`,
  `sudoers-pin-contract.sh:76`. All five DO go red on deletion — only the comment form slips.
  `tools.json:159` and `:303` explicitly promise these cases fail. 8 of 16 check scripts have no
  comment handling, though `no-retired-session-token.sh` already solved it in-repo.
  Fix: reuse that pattern across the 8; add a comment-out mutation to each row's own proof.
- [RESOLVED 2026-09-16, `3b44f24` — row `mqtt-set-contract`] 2026-08-28 **[HIGH] The one endpoint that switches real relay outputs is guarded only
  by a comment.** No registry row touches `www/network_config/cgi-bin/mqtt_set.cgi` beyond
  `bash -n`; `docs/contracts/mqtt-set-endpoint.md:74-96` is a MANUAL recipe, and `mqtt_set.cgi:88`
  claims "Hard floor of this endpoint; asserted by the contract check" — no such check exists.
  Re-adding the MQTT retain flag would re-fire every output on the next bridge restart with
  nothing in the pipeline noticing. Fix: a real row + delete the false comment.
- [RESOLVED 2026-09-16, row `web-auth-behaviour`] 2026-08-28 **[HIGH] The panel's login/CSRF core has zero functional tests.**
  `www/network_config/cgi-bin/lib_web_auth.sh` — 354 lines, 27 functions (session tokens, CSRF
  minting/validation, password hashing, credential-file repair), sourced by every mutating
  endpoint — is reached only by `bash -n` and CI shellcheck. A mistake in the code deciding
  "is this person logged in" ships green. Harness pattern already in-repo: `test-subnet-validate.sh`.
- [RESOLVED 2026-09-16, script + baseline gone from the tree; the headless rows are `ui-layout` / `cloud-card-smoke` / `sh-modal-layout-smoke`] 2026-08-28 **[MED-HIGH] `headless-smoke` is a dormant row over a stale baseline.**
  `headless-smoke.sh:12` skips unless playwright AND `SA02M_WEB_PASS` are present; CI provides
  neither, so it has no environment where it runs. Its committed baseline
  `scripts/dev/baseline/manifest.json` was last touched at 1.0.5.81 while the JS is at 1.0.6.23.
- [RESOLVED 2026-09-16, row `rs485-roster-consumer`] 2026-08-28 **[MED-HIGH] The `rs485-roster` contract is validated producer-side only.**
  `py-unit-roster` covers `opt/sa02m-rs485-roster/` (`tools.json:127`); the contracted `modules`
  field is emitted from `status.cgi:1116-1124`, which NO row covers. Edit `status.cgi`, lose the
  RS-485 module list from the dashboard, every gate stays green — the exact blind spot this audit
  dimension exists for.
- [RESOLVED 2026-09-16, the rule docs now point at `tools.json` as the one home] 2026-08-28 **[MED-HIGH] Four always-loaded rule docs say the quality registry has 3
  rows; it has 53.** `web-code-rigor.md:142`, `web-diagnostic-tools.md:96-97`,
  `web-workflow.md:78-79`, `sa02m-web-testing/SKILL.md:64-65`. The registry is the one home —
  the prose should point at it, not enumerate.
- [RESOLVED 2026-09-16, row `web-update-csrf-contract` (1.0.6.24); set-wide since 1.0.6.49 (`cgi-csrf-policy`)] 2026-08-28 **[MED] CSRF gap on the legacy OTA endpoint contradicts our own canon.**
  `web_update_apply.cgi:318-330` runs the update helper marked "no CSRF", while
  `docs/decisions/selective-csrf-policy.md` names only `logout` as an exception and the threat
  model requires CSRF on ALL mutating endpoints. SameSite=Lax still holds, so this is broken
  defence-in-depth plus false canon — fix the endpoint or amend the decision, not neither.
- [RESOLVED 2026-09-16, 1.0.6.49 — audit H3: `web-quality.yml` installs the harness + chromium] 2026-08-28 **[MED] `ui-layout` never runs in CI** — the workflow installs Python deps
  only, no playwright, so layout / mobile KPI centring / clipping / the WCAG contrast ledger are
  dev-box-only. Honestly labelled (`tools.json:482`, `.ai-dev/notes/quality-gate-environment.md`),
  so this is coverage, not dishonesty.
- [RESOLVED 2026-09-16, the pointer stub exists; filling it is its own OPEN entry] 2026-08-28 **[MED] `docs/architecture.md` does not exist yet is cited 12x in
  always-loaded files** (`PROTOCOL.md` 6x, `.claude/ai-dev.md` 3x, `.ai-dev/notes/README.md:4,9,20`).
  Every session is pointed at a missing home.
- [RESOLVED 2026-09-16, `sa02m-domain.md` now says «do not keep a bundle list here» — `index.html` + `ls` are the inventory] 2026-08-28 **[MED] `sa02m-domain.md:28` named 5 JS bundles while the tree had 8 + the `app/` cluster** and contradicted its own tab table. (Split from the polling-architecture entry above; the SKILL restatement half stays OPEN.)
- [RESOLVED 2026-09-16, rows `i18n-dict-contract` + `html-id-contract`] 2026-08-28 **[MED-LOW] Two declared reviewer floors have no mechanical row** — i18n
  completeness and the HTML-id contract are named as floors in `web-code-rigor.md` but nothing
  implements them. Concrete: a DICT-completeness script (RU strings in markup/JS vs `i18n.js`
  keys) and an id-contract grep (getElementById against `index.html`).
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
- [RESOLVED 2026-09-16, 1.0.6.49 — audit H3] 2026-09-02 **[MED] CI runs the `ui-layout` review gate VACUOUSLY — Playwright is never
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

### [LOW] Тексты входов/выходов Carel не переведены на английский

Строки таблиц «Входы/выходы» и расшифровки цифровых состояний живут в
`opt/sa02m-carel/sa02m_carel/carel_ahu_map.py` только по-русски — у тревог там
есть `text_en`, у остальных строк нет. В английском интерфейсе окна настройки
контроллера эти две таблицы остаются русскими. Заметно только при переключении
языка; чинится добавлением `text_en` в карту (её единственный дом) и
использованием его в рендерере, как уже сделано для тревог.
Найдено при сборке окна 1.0.6.31. OPEN.

### [LOW] Граница уставки Carel в документе «Умного дома» одна на оба семейства

`SH_KINDS.setpoint` в `smarthome.js` проставляет `parameters.range` = 0..99
всем, тогда как потолок uAria — 50 °C (`SETPOINT_RANGE` в
`opt/sa02m-carel/sa02m_carel/controls.py`). Мост зажимает значение при записи,
поэтому агрегат в безопасности, но ползунок, построенный по документу,
предложит недостижимые 50..99 и «отскочит» после первого отчёта.

Причина: окно не знает семейства — топик уставки несёт порт и адрес, а
инвентарь топиков отдаётся плоским списком строк. Чинится доведением семейства
до окна: либо мост публикует его метой контрола, либо
`sa02m_alice_topics.cgi` отдаёт карту «идентификатор устройства → семейство».
Найдено ревью 1.0.6.31, зафиксировано в `docs/contracts/carel-ahu.md` §6. OPEN.

### [MED] Опрос Carel теряет ~1,5 % кадров на «тихой паузе» внутри ответа

Измерено на стенде 1.135, COM3 19200, оба ПЛК под опросом моста: ~9 записей
`Short response` в минуту на два устройства (14 за 90 с при двух устройствах на
линии). Значения при этом верные и свежие, `meta/error` пуст — повтор чтения
восстанавливает кадр, устройство не уходит в offline. Доля — около 1,5 % от
~600 чтений в минуту.

Гипотеза «виноват фантом `mr02m-COM3-10`, которого нет на линии» ПРОВЕРЕНА И
ОТВЕРГНУТА: со снятым фантомом частота на Carel та же (7+7 за 90 с). Причина в
чтении: `bridge_serial.py` выходит из чтения по первой тихой паузе, не дожидаясь
полной длины кадра (там же и комментарий про `frame_timeout` wb-mqtt-serial), а
ПЛК Carel умеют паузу в середине ответа. Обрезаются оба размера: 11/13 байт
(IR1..4 у c.pCOmini) и 72/77 (IR0..35 у uAria) — длина кадра тут ни при чём,
таймаут 0,3 с обоим избыточен.

Чинить надо адресно, не глобально: ранний выход по паузе существует ради МР-02м,
и менять его для всех — это трогать тайминг шины всем поллерам сразу. Нужен
поcимвольный добор до вычисленной длины кадра для Carel-транзакций (флаг
транзакции либо свой допуск паузы), с повторным замером на стенде до и после.
Найдено при приёмке 1.0.6.31 на железе. OPEN.
