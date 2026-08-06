# Backlog — SA-02m web interface

Recorded findings and deferred work (`.ai-dev/procedures/backlog.md` owns the
format). One status per finding: `- [OPEN|RESOLVED] <date> <item>`. Resolved
entries are pruned (history lives in git); last prune 2026-08-05 (whole-project
audit).

## Open

- [OPEN] 2026-08-06 **[LOW] `run.test.mjs` does not pin the regex-escaping in
  `coversToRegex`.** Dropping the escape at `.ai-dev/quality/run.mjs:64` leaves
  all 15 table assertions passing while behaviour really changes — `install.sh`
  would then also match `installXsh`, because the `.` stops being literal.
  Fail-safe in both directions (an unescaped metachar only widens the match, and
  widening only makes MORE rows run), so it is low severity — but it is a hole
  in a suite whose whole job is to pin this function. One table row closes it.
  Found by the reviewer as a fourth mutation during the backlog sweep, after the
  builder's own three were confirmed.

- [RESOLVED] 2026-08-06 **[was MED, FILED IN ERROR] `lib_rtc.sh` write path
  still uses local time.** **Wrong — the fix was already on `main` as `fbeac21`
  (PR #96) when this was filed.** The Orchestrator grepped `lib_rtc.sh` while
  sitting on a feature branch cut from `main` BEFORE that merge, so the
  comparison was device-vs-stale-worktree, not device-vs-`main`, and produced a
  false "the repo is missing the fix" plus a false "do not deploy www" warning
  to the Operator. **Lesson, worth keeping:** when comparing a device against
  the repo, read the blob from `origin/main` (`git show origin/main:<path>`),
  never from the working tree — a feature branch is by definition behind. The
  real residue of this investigation is the item below.
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
- [RESOLVED] 2026-08-06 **[LOW] `nodered-pin-consistency` can print `ok` for a
  removed marker.** Fixed: check 7 now greps UNCOMMENTED lines only (and fails
  on a file with no executable lines). Driven to failure — with the ru spelling
  deleted from the ctl matcher the old gate exits 0 with `ok`, the new one
  exits 1. Was: `nodered-pin-consistency.sh:157` grepped the whole file while
  `etc/sa02m-web-service-ctl.sh:936` mentions «Запущены потоки» in a *comment*,
  so deleting the marker from the matcher still satisfied the gate, which then
  printed `…matches the started-flows marker in both en and ru` at exit 0. The
  same mutation on `scripts/07-nodered.sh` WAS caught, its ru literal being
  code-only — that asymmetry was the proof. Never a coverage hole
  (`nodered-ctl-install` pins the behaviour with two named assertions); the
  gate's own message was what lied. Reviewer M-A on the nodered-offline change.
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
- [RESOLVED] 2026-08-06 **[LOW] `extract_guard`'s comment over-claims what it
  pins.** Fixed by asserting the LOCATION, not by softening the claim: the
  comment now says it pins the suffix, and a named check below `extract_guard`
  pins the `mktemp "$conf.sa02m-gwtmp.XXXXXX"` template. Driven to failure —
  with the template moved to `$TMPDIR` the old suite passes, the new one exits 1
  with the cross-filesystem explanation; the braced `${conf}` spelling still
  passes (no cry-wolf). `scripts/dev/test-iface-gw-repair.sh`, reviewer N5.
- [RESOLVED] 2026-08-06 **[LOW] Broken inline code span in `docs/deployment.md:4-5`.**
  Fixed by rewrapping the paragraph so the path sits on one line. Scanned all
  146 tracked `*.md` for the same shape (fenced blocks stripped, then split on
  backticks so odd segments are spans): **23 spans wrap across a line**, and
  after this fix **none** breaks mid-token — every one joins where a space
  belongs. So this was the only genuine instance in the tree. Was: renders
  `.ai-dev/procedures/ deployment.md`, a broken pointer in the file that forbids
  improvising a deploy. Found by two independent reviewers.
- [OPEN] 2026-08-05 **[MED — NEEDS AN OPERATOR DECISION] Default device password
  hard-coded in the imaging fallback.** `tools/imaging/make-image.sh:20` defaults
  `SSH_PASS` to `cyntron` when `SA02M_PASS` is unset, so a build host without the
  key silently authenticates with a well-known credential. Untouched by the
  boot.scr format fix (surfaced by its review, out of that diff's scope).
  **Re-rated [LOW] → [MED] on 2026-08-06** (sweep reviewer, advisory A6): it is
  a literal default credential in a committed artifact — the one swept item
  carrying a hard security floor. The proposed remedy is one line (drop the
  default, require an explicit `SA02M_PASS`) but it breaks any build host
  relying on the fallback, so it is an **Operator call, not a sweep call**. Code
  deliberately unchanged.
- [RESOLVED] 2026-08-05 **[LOW] x-bit-dependent invocation documented for the rootfs
  builder.** Fixed in both homes: `tools/debian-rootfs/README.md:31` and the
  script's own `Usage:` header now say `sudo bash tools/…`, matching
  `docs/contracts/uboot-boot-script.md` and the sibling `pack-sa02m-image.sh`
  line. Was: `sudo ./tools/debian-rootfs/create-sa02m-rootfs.sh`, which fails on
  a fresh clone because tracked `*.sh` are `100644` (all but five).
  **Residual filed separately below** — nine other 644 scripts are still
  documented with `./`.

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
  `./` invocation.** Same class as the rootfs-builder entry above, quantified
  while fixing it: `etc/storage-mount.sh`, `scripts/03-webserver.sh`,
  `scripts/update-www-only.sh`, `tools/debian-rootfs/prepare-sa02m-flash-usb.sh`,
  and under `tools/imaging/`: `cleanup-donor.sh`, `flash-receiver.sh`,
  `make-image.sh`, `prepare-flash-media.sh`, `restore-donor-ssh.sh` — all
  `100644` in git yet documented as `./…` in markdown. Only `tools/kernel-wb/*` (100755) are legitimately invoked that way.
  Left out of the backlog sweep deliberately: it is a ten-file doc edit across
  unrelated runbooks, better done as one scoped change with a gate pinning it
  (mode-vs-invocation is mechanically checkable).
- [OPEN] 2026-08-05 **[LOW] `compress-bin.sh` patch step swallows failures.**
  `tools/imaging/compress-bin.sh:177-210` defines a watchdog-only `patch_image()`
  that returns 0 on failure. Verified during the boot.scr review NOT to carry the
  boot.scr fail-open class (it never generated `boot.scr`), but the same
  swallow-the-error style remains.
- [RESOLVED] 2026-08-06 **[LOW] Stale bootargs comments after the payload default
  change.** Both rewritten to the current truth: the default payload
  `etc/boot.cmd.sa02m.min` sets no `console=` at all (so default images boot with
  no kernel console anywhere), the opt-in `etc/boot.cmd.sa02m` sets
  `console=tty1`, and neither ever names a ttyS — which is the property the
  empty `chosen{}` node and the getty masking both exist to hold. Comment-only,
  no behaviour change; `bash -n` green on `scripts/01-system.sh`, no `dtc` on
  this box for the `.dts`. Was: both claimed `console=tty1` "generated from
  etc/boot.cmd.sa02m".
- [RESOLVED] 2026-08-06 **[was MED, ALREADY FIXED WHEN RE-READ] Direct sysfs
  writes of a watchdog timeout above the 16s cap.** **Not fixed by this sweep —
  fixed by `66212b3` (PR #87) and verified against `origin/main` today.** Both
  writers now ask for `min(30, max_timeout)` and skip the write when
  `max_timeout` is unreadable (`etc/sa02m-watchdog-feed.sh:49-57`,
  `tools/imaging/ssh-flash-safe.sh:153-158`), and `watchdog-cap` pin 8 gates
  exactly this: no numeric literal above the cap, and every file writing the
  path must also read `max_timeout`. The entry was filed mid-change against the
  pre-fix text and never re-read. The one live residue is a cleanup question,
  not a defect: the feeder is the unit `sa02m-watchdog.conf` masks and disables,
  so it may be dead code on a correctly-installed device — re-file if that
  matters, and note `watchdog-cap`'s `WD_SYSFS_MIN_SITES=2` would need lowering
  with it.
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
  `WD_SYSFS_MIN_SITES` (the feeder is a delete candidate — see the RESOLVED
  sysfs entry above) and the hole becomes real. Demonstrated: with `etc` dropped
  AND `WD_SYSFS_MIN_SITES=1` the old gate exits 0, the new one exits 1 naming
  `etc/systemd/sa02m-watchdog.conf`. Still OPEN: the widened
  value regex can absorb a following English word on a prose line (fail-closed
  cry-wolf, reachable via the swept `tools/system-hardening/README.md`), and the
  gate has no meta-test — matching its two sibling gates
  (`iface-naming-contract`, `kernel-policy-contract`), so this is a shared shape,
  not a regression. Reviewer advisories A5–A7 on the watchdog-cap change.
- [RESOLVED] 2026-08-06 **[MED] `covers` directory-prefix entries match nothing under
  `--touched`.** Fixed in `.ai-dev/quality/run.mjs` `coversToRegex()`: a pattern
  with no glob metacharacter now compiles as a PREFIX with a `/` boundary
  (`"etc/"` → `/^etc(\/.*)?$/`) instead of literally (`/^etc\/$/`), which is the
  semantics `tools.json:9` documents. The boundary keeps `"install.sh"` from
  matching `install.sh.bak` and an exact file path from matching its own
  `.orig`; the glob branch is untouched. Pinned by a 15-case table in
  `.ai-dev/quality/run.test.mjs` (row `quality-runner-self-test`) covering the
  prefix form, the `/` boundary, exact paths, `**` and single-`*`. Driven to
  failure three ways: the pre-fix `coversToRegex` → 5 assertions fail; the
  prefix without its `/` boundary → 3 fail; `*` allowed to cross `/` → 1 fails.
  Effect measured on the fixing branch itself: `iface-naming-contract` and
  `kernel-policy-contract` went from SKIP (a false green — a skipped row prints
  PASS) to RUN. **The "vendored, do NOT patch locally" premise was wrong** and
  is withdrawn: `git log -- .ai-dev/quality/run.mjs` shows `7882252` (#62), a
  merged local patch to this exact file that also added `run.test.mjs` and its
  registry row. The file is ours; the practice is patch-and-pin. Was: found by
  the builder + reviewer during the watchdog-cap work, filed twice (see the
  2026-07-22 entry) and mis-triaged as NOT A BUG in the 2026-08-06 sweep before
  the reviewer refuted the premise.
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
- [RESOLVED] 2026-07-22 **[LOW] `run.mjs --touched` vacuous on this repo.**
  **Both named halves are now fixed, by different changes.** (1) The
  `git status --short` fallback trimming the whole output before `slice(3)` and
  mangling the first filename — fixed by **`7882252` (#62)**, not by this sweep;
  the entry had carried it as live ever since. (2) `coversToRegex` anchoring
  `^…$` with no prefix semantics, so every scoped row silently skipped — fixed
  2026-08-06, see the RESOLVED `covers` entry above. The two entries were **not**
  duplicates: this one named two defects, one of which the other never mentioned.
  The "vendored framework file — do NOT patch locally" line was wrong (#62 is a
  merged local patch to that exact file, with a local self-test) and is withdrawn;
  the sibling D8/D9 entry is about vendored **doc pointers** and stands unchanged.
  `--touched` is now trustworthy; the full beat remains the ship gate by design.
  Builder 1.0.5.46.
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
- [OPEN] 2026-08-06 **[MED] `etc/sa02m-kernel-select.sh:23` still defaults RT to
  `5.10.35-rt36`.** That kernel/RT pair never existed upstream (rt36 belongs to
  5.10.27); the RT flavour now builds `5.10.35-rt39`. Not a live fault —
  `load_conf()` falls through to `detect_installed_module_ver rt`, which matches
  the real `5.10.35-rt39` module dir and writes it back to
  `/etc/sa02m_kernel.conf` — but the default names a kernel that cannot exist.
  Deliberately NOT fixed with the CI work: `etc/` is device code and was fenced
  out of that change's scope. One-line edit + a device check.
- [OPEN] 2026-08-06 **[LOW] `kernel-port/README.md:35` documents a patch file
  that never existed.** Names `patch-5.10.35-rt36.patch.gz` as the PREEMPT_RT
  patchset. Same root cause as above; left alone because `kernel-port/` was
  outside the CI fix's scope. Should read rt39 and point at
  `docs/decisions/rt-patch-pinning.md`.
- [OPEN] 2026-08-06 **[MED] The `.deb` job of `build-sa02m-kernel` has never
  completed once.** July runs died at `dpkg-checkbuilddeps: Unmet build
  dependencies: build-essential:native`; that one missing package is now in the
  workflow's apt list, but nothing past that point has ever executed, so the
  job's remaining steps (bindeb-pkg, artefact collection, upload) are unproven.
  The job is push-only, so it blocks no PR. Needs one deliberate
  `workflow_dispatch` run on a kernel-touching branch to find out.
- [OPEN] 2026-08-06 **[MED] The RT patch fallback leaves a half-patched kernel
  tree.** `tools/kernel-wb/build-sa02m-kernel.sh:147-155` (and the same shape in
  `kernel-port/apply.sh:47-59`): when the forward dry-run fails it retries with
  `patch --merge=diff3`. Measured on GNU patch 2.7.6: that exits 1 whenever it
  writes conflict markers, so the `|| exit 1` guard does fire and CI fails
  loudly — but the tree keeps the hunks that already applied, the diff3 markers,
  and `.orig` siblings. CI is safe (`actions/cache` is `post-if: success()`, so
  a failed job never saves the poisoned tree); a local build root is not — the
  script skips the re-clone whenever `wb-linux/.git` exists, so the next local
  run compounds the damage until someone passes `--rebuild`. Options: fail fast
  without the merge attempt, or `git checkout`/re-clone on merge failure.
- [OPEN] 2026-08-06 **[MED] The kernel build's WB base is a moving branch ref,
  so the build is not reproducible.** `tools/kernel-wb/build-sa02m-kernel.sh:84`
  clones `-b release/wb-2606/wb7-bullseye` — a branch, not a tag or a SHA — so
  what the build compiles depends on the day it runs, for BOTH flavours. The
  rt39 work pinned the RT patch (version + sha256) but deliberately left this
  half alone: changing the base changes the kernel that ships. Two consequences
  worth acting on: builds cannot be reproduced across dates, and the measured
  "rt39 applies cleanly" result (0 rejects, 27 offsets, 1 fuzz in
  `drivers/tty/serial/8250/8250_port.c`) is only valid against WB HEAD
  `048d31b` of 2026-03-30 — a WB commit touching that file can turn the fuzz
  into a conflict. Fix = pin `WB_BRANCH` to a tag/SHA (keeping the env override)
  and re-measure on each deliberate bump. Recorded in
  `docs/decisions/rt-patch-pinning.md`.
- [OPEN] 2026-08-06 **[LOW] `build-sa02m-kernel.sh` .deb collection is a no-op
  and the package version hides the RT level.** `mv "$BUILD_ROOT"/wb-linux/../*.deb
  "$BUILD_ROOT/"` resolves source and destination to the same directory (the
  error is swallowed by `2>/dev/null || true`); `bindeb-pkg` already writes
  there. Separately, `KDEB_PKGVERSION="5.10.35-${FLAVOUR}-<date>"` records the
  flavour but not the RT patch level, so an `-rt` package does not say whether
  it is rt39 or something else. Both are cosmetic until someone debugs a
  device's kernel provenance.
- [OPEN] 2026-08-06 **[LOW] `build-sa02m-kernel` only fires on kernel paths, so
  a red workflow goes unnoticed for months.** Its triggers are `kernel-port/**`
  and `tools/kernel-wb/**`; between kernel changes nothing runs and nobody sees
  the failure — which is how it stayed broken from its first run. A weekly
  `schedule:` smoke run (or the same on push to main regardless of path) would
  surface breakage while someone still remembers the context.
- [OPEN] 2026-07-12 **[task] On-device verification (pre-deploy).** All device-
  side changes tested only locally/logically. Verify on a real SA-02m before
  deploy: login (hashed + legacy plaintext), password change, network apply +
  re-run `install.sh` preserves the static IP, cloud activation, storage
  autoformat off by default. A www-only OTA needs `/etc/sa02m_web.env` present
  (login now fails closed).
