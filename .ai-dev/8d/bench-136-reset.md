# 8D — bench 1.136 hard reset mid `install.sh --refresh` (2026-09-08)

Transient run-note (`.ai-dev/procedures/8d.md`; deleted at D8). Branch `1.0.6.41`.
Backlog: `.ai-dev/backlog.md` [HIGH] 2026-09-08 «Bench 1.136 reset in the middle of
`install.sh --refresh`» → RESOLVED by this work. Facts below are the Orchestrator's SSH
evidence; code facts are `file:line` at HEAD (1.0.6.40 content).

## D1 — Team

The loop's roles: Orchestrator (evidence, board reads, git), Researcher-Planner (this
note), Builder (D5 steps), fresh Reviewer (D6). No new seat.

## D2 — Define

Offline full update 1.0.6.37 → 1.0.6.40 on 192.168.1.136 (`sa02m-1eth`, mplc4 on, tree
extracted under `/tmp` = tmpfs = RAM), launched 22:07:08 via paramiko `exec_command`
`nohup … --unattended &`; the relay session closed right after.

| Time | Fact |
|---|---|
| 22:07–22:14:43 | `install.sh` log complete through `[OK] /opt/sa02m-mplc установлен` (`scripts/03-webserver.sh:633`); the next durable line is a torn `[2026-` — a `lib.sh:19` log line whose tail never reached disk |
| 22:15:08 | wrapper heartbeat «install.sh работает (8 мин)» (`offline-full-update.sh:186`) — the wrapper survived the session close; nothing later from it is on disk |
| 22:15 | `/etc/systemd/system/sa02m-flasher.service` mtime — 0-byte regular file (systemd reads an empty unit as **masked**, systemd.unit(5)); written by `04-flasher.sh:90` `install -m 0644` |
| ≈22:20:05 | boot (watchdog heartbeat «uptime=25s» at 22:20:30); `who -b` = 1970 (RTC) — the wall-clock at boot came from fake-hwclock/chrony |
| after | `/opt/sa02m-modbus-mqtt/bridge_led.py` = 1.0.6.40; `/opt/sa02m-led/sa02m_led/led_mb2ws.py` lacks `MB2WS_TEXT_BASE` ⇒ bridge crash-loop (119 restarts); `VERSION` + runner stamp already 1.0.6.40 |
| logs | userspace-watchdog: no FORCED REBOOT; failure-monitor: **no lines 22:05→22:20:30** (its 300 s heartbeats at ~22:10/22:15 are missing too); journal lists one boot; wtmp has no shutdown record |
| control | 1.135 (same HW class) ran the identical update twice, 13/13 PASS, ≈11 min; the 1.136 re-run at 22:48 (`setsid nohup … </dev/null &`, tree under `/root`, `--force --no-backup`) PASSED 13/13 in 10 min, no reset |

## D3 — Contain (done)

Re-run to completion (installer is idempotent); `sa02m-flasher` unit restored and started by
hand (the re-run captured the 0-byte unit as `masked` and preserved it — `lib.sh:993-1003`,
the "never-widen" branch). No further containment needed; the bench is at 1.0.6.40.

## D4 — Root cause

**What the on-disk state proves.** Every clean-reboot path on the board syncs before
rebooting (`etc/sa02m-userspace-watchdog.sh:212-218` `sync` then `systemctl reboot --force`;
`systemctl reboot`; `sa02m-web-reboot.sh`). A synced tree cannot show a torn log line, a
created-but-empty unit, and a shared-package file missing a constant. The tree is a **torn
page cache — an unclean (hard) reset**, and the writeback lag was minutes, not seconds:
the last durable installer line is 22:14:43, the wrapper's 22:16–22:20 heartbeats and the
failure-monitor's 22:10/22:15 heartbeats never landed. Sharper: 1.0.6.37's `bridge_led.py`
reads `lm.MB2WS_TEXT_BASE` at **module level** (`_LEGAL_BLOCK_WRITES`, origin/1.0.6.37 l.88),
so if the bridge was active before 22:07 (the `svc-before` row says — Orchestrator: read it),
the pre-update `led_mb2ws.py` HAD the constant, and the post-reset file is not "the old one"
but a **torn 1.0.6.40 copy** (`04-flasher.sh:52` / `05-mqtt.sh:166` `install` + `sed -i`).

| Candidate | Evidence | Verdict |
|---|---|---|
| `sa02m-userspace-watchdog` reboot | threshold 60×10 s; it logs + `sync`s before acting (`:203-218`); log has nothing; grace/threshold not reachable in the window | **excluded** |
| `sa02m-failure-monitor` reboot | has **no reboot path at all** — it only logs/snapshots (`etc/sa02m-failure-monitor.sh`, whole file) | **excluded** |
| `net-watchdog` | loop = `fix-eth.sh` (ifdown/ifup only, `:361-364`) + `inet-failover.sh`; no reboot | **excluded** |
| a module issuing `reboot` | `grep reboot install.sh scripts/*.sh` → only comments and the install of `sa02m-web-reboot.sh` | **excluded** |
| SSH teardown killed the process group, later independent reboot | wrapper wrote heartbeats 8 min after the session closed; no independent reboot mechanism exists (rows above) | **excluded** |
| `daemon-reexec` (`01-system.sh:762`) stalling PID 1 | runs at the END of module 01 (≈22:08); reset ≈22:20 | excluded by timing |
| **HW watchdog (`sunxi-wdt`, 15 s, PID-1 fed)** after a PID-1 stall | fits a hard reset + minutes of writeback lag (I/O starvation: `rsync`/`install` bursts on eMMC, the tree in tmpfs eating RAM on a 1 GB board running mplc4 — 1.135 has a different memory profile; `daemon-reload` at `04-flasher.sh:91` and `sa02m_systemctl daemon-reload` in 05 make PID 1 read unit files from disk); nothing on record contradicts it | **leading** |
| external power/reset | indistinguishable from the row above by the filesystem alone; bench 1.136 is shared (`docs/bench-board-target-state.md`) | **open** |

### D4 addendum — two more freezes on 2026-09-09, and what measurement excluded

Verifying 1.0.6.41 reran the installer on 1.136 twice. **Both runs ended the same way:**
every module completed, `=== [12] Docker: модуль завершён ===` was the last install-log
line, and the board came back on a fresh boot minutes later. Run 1: last log 10:22:28,
boot ~10:26. Run 2: last log 13:02:37, boot 13:04:36 (`uptime -s`).

**The reset was HARD, and every reset of this board has been.**
`sa02m-shutdown-marker.service` is enabled and active and writes
`/var/lib/sa02m-clean-shutdown` on any clean stop. That file **does not exist at all**, so
no shutdown of this board has ever run its stop job.

#### Excluded by measurement on the board (2026-09-09 evening)

| Candidate | How it was excluded |
|---|---|
| **HW watchdog (`sunxi-wdt`, PID-1 fed)** — D4's former **leading** row | `/proc/1/fd` shows PID 1 holding `/dev/watchdog0` in normal operation; after `sa02m_runtime_watchdog_set 0` it holds **no** watchdog fd — systemd magic-closes the device, so the timer is DISARMED, not merely unfed. The board then survived **40 s**, well past its 16 s hardware timeout. Repeated across a `systemctl daemon-reexec`: property still 0, still no fd, still alive. The hold was taken at 12:53:15 and install.sh never reached its EXIT trap, so the watchdog was disarmed for the entire run. **This row is now excluded, not leading.** |
| `daemon-reexec` (`01-system.sh:762`) stalling PID 1 | Already "excluded by timing"; now excluded a second way — the runtime override survives the re-exec (measured above), so the re-exec changes nothing about the watchdog. |
| Kernel panic → reboot | `kernel.panic=0` and `panic_on_oom=0`: a panic on this board HANGS, it cannot reboot. |
| Thermal critical trip | `cpu0-thermal` critical = 115 °C, GPU = 125 °C; idle 62 °C and 61–64 °C under a sustained write load. Zero thermal events in the journal. |
| A long post-module `sync` stalling the board | Measured: a 120 MB burst leaves ~28 MB dirty and `sync` clears it in **2.06 s** (idle sync: 0.02 s). `vm.dirty_expire_centisecs=3000` bounds writeback to ~30 s — `commit=600` is the ext4 *journal* interval, not the page-writeback window. A post-module sync costs seconds, not minutes. |

#### The correction that matters most — a claim in the first draft of this addendum was WRONG

The first draft said: «journald has NO entries between 13:01:46 and the boot — the system
was WEDGED, not busy». **That does not follow, and it is withdrawn.** Measured cause of the
gap:

- journald's `SyncIntervalSec` is the compiled default **5 minutes**; nothing in
  `/etc/systemd/journald.conf*` overrides it.
- `/` is mounted `commit=600`, and the journal files' mtimes are **13:01:37 / 13:01:39** —
  exactly where the readable entries stop.
- The install log is a plain `>>` append, so **its tail lives in the page cache too**. Its
  last visible line at 13:02:37 is therefore a lower bound on how far the run got, not the
  moment it stopped.

So the entries after ~13:01:37 were written into the page cache and died with the reset.
**There is no evidence of a wedge.** The system was probably running normally until the
instant of the reset, and the run may well have progressed past 13:02:37 invisibly.

#### What this leaves, and the honest verdict

Every software reset path is now excluded by measurement. What remains is **power delivery
or an external reset** on this shared bench board — physical, and outside what the software
can settle. 1.135 runs the identical archive and has never done this; 1.136 additionally
carries Docker and MPLC4 on **491 MB with no swap and no zram**, and the PMIC gives no
usable rail telemetry (`in_voltage0_raw` is pegged at full scale 4095, `in_current*_raw`
reads 0), so a voltage sampler built in software would report a constant and prove nothing.
Settling it needs a meter on the rail or a serial console.

#### The finding that actually unblocks the next investigation

**Three incidents have produced no root cause for the same reason each time: the last one to
three minutes before a hard reset are not recoverable on this board.** The journal is
fsynced every 5 minutes and the installer's own log is buffered, so the window that matters
is exactly the window that is always lost. Until that changes, a fourth reset will teach us
no more than the first three.

That is cheap to fix — `sync` costs ~2 s after a 120 MB burst, and the installer already
calls it at every module boundary. Forcing a **journal** sync there too (and fsyncing the
install log) would cost roughly 25 s across a 13-minute install and would make the next
post-mortem possible. Tracked as a fix, not a note.

**Reproducibility is the other new fact.** Three resets, all on 1.136, all in the same
install phase, none on 1.135 running the identical archive. Whatever it is, it is a property
of THIS board plus a full install, not of a particular release: run 2 carried the 1.0.6.41
fences and they held — no 0-byte unit fragment, no failed unit, the board came back on
1.0.6.41 and passed 12 of 13 direct probes, the one FAIL being the installer report that was
never written.

**Attempt ceiling.** Two runs, same symptom; a third install was NOT started, and must not be
until the logging window is closed and a serial console is attached — otherwise it produces
the same absence of evidence.

**Verdict.** The reset was a hard reset. **Superseded 2026-09-09 by the addendum above:**
the two remaining causes were «HW watchdog vs. power»; the HW watchdog is now excluded on
the board by measurement, so power delivery / external reset is what is left, and no
software path remains. The
installer's own defects made the reset an OUTAGE: non-atomic live-path writes (0-byte unit
⇒ flasher masked), dependency written after its consumer (`05-mqtt.sh:160` `bridge_led.py`
before `:166` LED pkg), no `sync` at module boundaries (a 5-minute tear), and a capture
that reads a corrupt unit as an operator decision (`lib.sh:993`). Those are fixable
regardless of which hard-reset cause wins; D5 fixes them all.

**Cheapest discriminating reads on 1.136 (non-destructive, Orchestrator):**

1. `journalctl -b 0 -k | grep -iE 'EXT4-fs.*(recover|orphan)|mmc[0-9].*(error|timeout)|sunxi-wdt'` — unclean-shutdown proof + eMMC error traces; `journalctl -b -1 -k | tail -50` if a previous boot exists.
2. `cat /sys/class/watchdog/watchdog0/bootstatus` — 32 (WDIOF_CARDRESET) = watchdog reset proven; 0 is inconclusive (the driver may not report it).
3. `findmnt -no OPTIONS /; sysctl vm.dirty_expire_centisecs vm.dirty_ratio` — the commit/writeback windows (Armbian often ships `commit=600`) that decide how wide a tear a reset leaves.
4. `journalctl -b 0 | grep -iE 'chrony.*(wrong|step)|fake-hwclock|time has been changed'` — the true boot time (the 22:20 figure rests on the post-boot clock).
5. `free -m; df -h /tmp; du -sh /tmp/sa02m-upd 2>/dev/null` — memory-pressure hypothesis (tree in tmpfs).
6. `systemctl set-property --runtime Manager RuntimeWatchdogSec=0; echo rc=$?; busctl get-property org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager RuntimeWatchdogUSec; systemctl set-property --runtime Manager RuntimeWatchdogSec=15s` — whether the runner's precedent guard (`etc/sa02m-update-runner.sh:216`, `|| true`) is real on this systemd (bullseye = v247; writable Manager watchdog properties arrived in v250 — I expect a silent no-op, i.e. a hollow guard, `quality-gate-rigor.md` shape).
7. `ls -la /var/log/journal/; journalctl --list-boots` — why only one boot is listed.

## D5 — Fix (atomic build steps; each RED on today's tree first)

Surface scope: **installer + device scripts** (`install.sh`, `scripts/lib.sh`, `scripts/0*.sh`,
`scripts/offline-full-update.sh`, `scripts/dev/*`, `.ai-dev/quality/tools.json`); frontend and
CGI untouched. Guarantee first: **a hard reset at any instant of an installer run leaves every
live-path file either old or new (never empty), every consumer never newer than its dependency,
and a re-run repairs — never preserves — a corrupt unit.**

**A. Atomic live-path writes** — `lib.sh` `sa02m_install_atomic [-m MODE] [-o U] [-g G] SRC DST`:
`install` to `DST.sa02m-tmp.$$` in the same directory, `sync -f`/`fsync` where available,
`mv -f` over DST (rename-over ⇒ ext4 `auto_da_alloc` forces data allocation; old-or-new, never
empty). Codemod (`scripts/dev/codemod-install-atomic.py`, re-runnable — Rule of 500) rewrites
every `install -m … /etc/systemd/system/…` (25 sites) and the `/usr/local/{sbin,lib,libexec}/…`
helpers to the helper; `sed -i 's/\r$//'` after-passes stay (they are rename-over already).
RED test: `scripts/dev/test-install-atomic.sh` — (1) helper: DST exists with old content, SRC
is a FIFO whose writer stops mid-body → DST still old, no `.sa02m-tmp` left after the trap;
(2) static pin via `lib_check.sh`: no comment-stripped `install -m` line in `scripts/*.sh`
targets `/etc/systemd/system/` — RED today (25 hits). Registry row `install-atomic`
(build, `covers: scripts/**/*.sh, install.sh`), case in `comment-mutation-proof`.
Commit: `fix(1.0.6.41): unit files land atomically, never as an empty file`.

**B. Dependency before consumer + module-boundary sync.** `05-mqtt.sh`: move
`sa02m_install_carel_pkg`/`sa02m_install_led_pkg` ABOVE the bridge-module loop (`:158`), so a
tear leaves {new pkg, old bridge} (additive pkg — old bridge still imports). Correct the
`:152-156` comment (the entry is NOT self-contained since 1.0.6.33 — it imports `bridge_led`).
`install.sh`: `sync` after each module call (bounds a tear to one module; <1 s on eMMC).
RED test: `scripts/dev/test-installer-order.sh` — comment-stripped line order in `05-mqtt.sh`
and `04-flasher.sh`: first `sa02m_install_*_pkg` < first `install … bridge_*.py` /
`rsync … $INSTALL_DIR` — RED today for 05 (166 > 160); plus `install.sh` carries a `sync`
after every `bash "$SCRIPT_DIR/scripts/…"` line. Registry row `installer-order`; exempt
marker per `quality-gate-rigor.md` (an order has no comment-out form) or a case on the `sync`.
Commit: `fix(1.0.6.41): shared LED/Carel pkg lands before the bridge that imports it`.

**C. Corrupt unit ≠ operator decision (capture blind spot).** `lib.sh sa02m_svc_capture`: when
`is-enabled` says `masked` but the fragment in an `/etc` unit dir is a **regular file of size
0** (not a symlink) ⇒ state `broken` (systemd never masks a unit whose fragment lives in
`/etc` — the comment at `:993-995` already relies on that). `_sa02m_svc_apply_app`: `broken` ⇒
`log WARN "$u: файл юнита пуст (обрыв прошлой установки) — переустановлен"` and the
first-install branch (`first` default). `_sa02m_svc_apply_infra`: same detection ⇒ unmask
path. Test seam: `SA02M_UNIT_FILE_DIRS` (word-split, mirrors `SA02M_SYSV_RC_DIRS` `:680`).
RED test: new cases in `scripts/dev/test-installer-svc-helpers.sh` — seed `masked inactive` +
0-byte regular file in the seam dir → expect `en=broken`, apply `app on` ⇒ verbs
`enable start`, `LAST_RESULT=started`; a /dev/null symlink still ⇒ `left-masked` (1d stays).
Contract: `docs/contracts/installer-refresh-policy.md` state list (+`broken`).
Commit: `fix(1.0.6.41): a 0-byte unit is broken, not operator-masked`.

**D. Post-check honesty** — `offline-full-update.sh post_checks`: a core service whose
`is-enabled` is `masked` (or whose `/etc` fragment is empty) is a FAIL row «юнит повреждён»
even when `svc-before` says inactive (the re-run's PASS «состояние сохранено» hid the flasher
outage). RED test: `scripts/dev/test-offline-update-postcheck.sh` with a `systemctl` shim
(harness idiom of `test-installer-svc-helpers.sh`; `post_checks` extracted by sourcing with
`MODE_STATUS` stubbed or via a `SA02M_OFU_SOURCE_ONLY=1` seam).
Commit: `fix(1.0.6.41): offline post-check fails on a masked core unit`.

**E. Detached launch + non-tty runbook.** `offline-full-update.sh:371`:
`nohup setsid env "${LAUNCH_ENV[@]}" bash install.sh </dev/null >"$LOG" 2>&1 &` (own session;
stdin never a dead channel); dry-run line `:361` mirrors it. `docs/deployment.md` «Офлайн-
вариант»: a paragraph for a non-tty transport (`sa02m_remote.py exec`, paramiko): launch the
WRAPPER as `setsid nohup bash … --unattended --log /root/install-offline-<ver>.wrapper.log
</dev/null >/dev/null 2>&1 &`, then `--status` — the form that worked at 22:48. RED test:
static pin (comment-stripped) that the live launch line carries `setsid` and `</dev/null`;
`--dry-run` output asserted equal to the pin (existing dry-run path, no root needed for the
string check — the EUID guard is bypassed by running the parse under `EUID` shim or by pinning
the source). Commit: `fix(1.0.6.41): offline update launches install.sh in its own session`.

**F. Install lock (class-level; not this incident's trigger).** `install.sh`: after `lib.sh`,
`sa02m_install_lock_hold` writes `/run/sa02m-imaging.lock` (the lock the userspace watchdog
already honours, `etc/sa02m-userspace-watchdog.sh:32,92`; one home shared with
`sa02m-update-runner.sh:212`) and an `EXIT` trap releases it; `01-system.sh` re-asserts nothing
(the lock is a file — `daemon-reexec` does not clear it). The HW watchdog is the fork below.
RED test: `scripts/dev/test-install-lock.sh` — run `install.sh` with `SA02M_ROOTFS_BUILD=1`
against a scratch `IMAGING_LOCK` path (seam `SA02M_IMAGING_LOCK`) and a stub module that
records whether the lock existed while it ran and after exit (RED: no lock today).
Commit: `feat(1.0.6.41): installer holds the imaging lock for its run`.

Out of scope (explicit): the HW-watchdog hold (fork), `update-www-only.sh`'s 38 `install -m`
sites beyond units/helpers (follow-up backlog line), `net-watchdog` behaviour, and any change
to the runner's own lock code (its hollow-guard question goes to the backlog if read 6 confirms).

## D6 — Validate

- Each step: RED observed on the pristine tree → GREEN after; the mutation recorded in the
  commit body (`git-commits.md` Verification method). `node .ai-dev/quality/run.mjs build
  --touched` green per commit; full suite + `review` at ship.
- Real-layer verification scenario (primary integration layer: **bash over SSH on the board**):
  on a bench board, `scripts/offline-full-update.sh --dry-run` prints the detached launch line;
  then a full run on 1.135 (13/13 PASS, flasher active, bridge imports) — offered, not
  automatic. A hard-reset drill (power-cut mid-run) is destructive and stays **descoped** —
  the tear-shape guarantee is validated by the unit tests (A: FIFO writer, B: order pins).
- Regression net: `installer-svc-helpers` (all existing cases stay green — 1d `masked` via
  symlink unchanged), `installer-svc-policy-gate`, `comment-mutation-proof` coverage.

## D7 — Prevent (durable homes)

- `docs/agent-rules/web-code-rigor.md` §System scripts / installer floors — three lines: live-path
  files are written via `sa02m_install_atomic` (old-or-new, never empty); a shared package
  lands before its first consumer file; the installer holds the imaging lock for its run.
- `docs/deployment.md` «Офлайн-вариант» — the non-tty launch paragraph (E).
- `docs/contracts/installer-refresh-policy.md` — the `broken` capture state (C).
- `docs/bugs/BUGLOG.md` — one entry in the file's format (date, branch, files, type, cause, fix).
- `.ai-dev/backlog.md` — the HIGH entry → RESOLVED (1.0.6.41, steps A–F); two new lines:
  (1) [MED] runner `RuntimeWatchdogSec=0` guard possibly hollow on bullseye (pending read 6);
  (2) [LOW] `update-www-only.sh` non-unit `install -m` sites → atomic helper.
- `CHANGELOG.md` 1.0.6.41 section (Russian, «Установщик»).

## D8 — Close

Land A–F + D7 on `1.0.6.41`; delete this note after the PR is open.

## Structural forks for the Operator (Orchestrator relays)

1. **HW watchdog during install** — Option 1: also hold `RuntimeWatchdogSec=0` for the run
   (needs a mechanism that WORKS: read 6; fallback = runtime drop-in
   `/run/systemd/system.conf.d/` + `daemon-reexec`, itself a PID-1 event); Option 2: leave it
   armed, ship A–F (the tree survives a reset), decide after reads 1–7. **Recommend 2 now**;
   revisit only if read 2 proves a watchdog reset or read 6 proves a real handle.
2. **Codemod breadth (A)** — units + `/usr/local/*` helpers (recommended) vs. units only.

## Plan checklist (floor items not covered above)

- Contracts touched: `installer-refresh-policy.md` (modified: `broken`), `led-mb2ws.md`,
  `carel-ahu.md` (honoured: install order), `web-update.md` (read; runner untouched).
- Behaviour: no web-user-visible change; device: flasher survives a torn run; bridge cannot
  import ahead of its package; offline post-check no longer green over a masked core unit.
- Security surface: none new (root-only installer; the lock file lives in `/run`, root-owned;
  no untrusted input). Threat-model actor untouched.
- Concurrency: the lock is a file; two concurrent installs are already refused by the wrapper
  PID file (`:308`); the EXIT trap removes the lock only if this run created it.
- Estimate: non-trivial logic (A, C), tests that can break (svc-helpers, mutation-proof
  registration), one open design fork (HW watchdog) — **medium**: ~6 atomic commits, one
  Builder day; codemod reviewable as a script.
- Elicitation (pre-mortem): "shipped, 1.136 reset again next month — what still broke?" →
  the tree survives (A/B), the flasher is repaired by the re-run (C/D); what would NOT: the
  reset itself — hence reads 1–7 and fork 1 stay open rather than closed by prose.
- Adversary probe: (i) `mv -f` over a unit while systemd holds it — safe (systemd re-reads at
  `daemon-reload`); (ii) `sync` per module on a wedged eMMC blocks the installer — acceptable,
  a hung install is visible; (iii) `broken` misfires on a legitimately empty drop-in — only
  full fragments in `/etc/systemd/system/<unit>` are judged, never `.d/` drop-ins; (iv) reads
  1–7 may all come back inconclusive — then the note records "hard reset, cause unresolved",
  and A–F still stand on their own guarantee.

## D6 progress — evidence per step (Builder, 2026-09-09)

Host: Windows git-bash (the two environment normalisations of
`.ai-dev/notes/quality-gate-environment.md` apply). `shellcheck` is **not
installed here — skipped**, not passed; `bash -n` run on every touched script.

**A — atomic live-path writes** (`scripts/lib.sh sa02m_atomic_install`, codemod, 131 sites).
GREEN: `bash scripts/dev/test-install-atomic.sh` → `install-atomic: ALL OK`, incl.
`7b non-vacuity: the sweep sees 131 converted sites across 13 files (floor 100 / 10)`.
RED by mutation (scratch copy of the tree, at the real 1.136 site): reverting
`scripts/04-flasher.sh:87` to `install -m 0644 … /etc/systemd/system/sa02m-flasher.service` →
`FAIL 7a codemod --check rc=1: scripts\04-flasher.sh:87: install -m 0644 …` plus
`codemod-install-atomic: 1 live-path install -m site(s) still raw`.
RED on the helper itself: `SVC_HELPERS_LIB=<HEAD lib.sh>` →
`FAIL … does not define sa02m_atomic_install() — the atomic helper is missing`.
**Defect found and fixed while re-deriving this evidence:** case 7a read `rc=$?`
off a `python3 … | tr` PIPELINE, so it took `tr`'s status and could NEVER go RED —
the mutation above stayed GREEN until the harness was fixed to capture-then-transform
(`quality-gate-rigor.md` shape (f)).

**B — dependency before consumer + module-boundary `sync`.**
GREEN: `bash scripts/dev/test-installer-order.sh` → `installer-order: ALL OK`
(`1a LED pkg → bridge copy: dependency at line 161 precedes consumer at line 172`;
`2a/2b 04-flasher pkg at 46/48 before rsync at 52`;
`3a one bash line (l.146) inside sa02m_run_module (l.144), sync at l.147`;
`3b 15 sa02m_run_module call lines (floor 12)`).
RED by mutation on a scratch copy: putting the two `sa02m_install_*_pkg` calls back
below the bridge loop (the 1.0.6.40 order) →
`FAIL 1a … dependency at line 174 comes AFTER consumer at line 170` (and 1b).
Replacing one `sa02m_run_module 04-flasher.sh` with the bare `bash "$SCRIPT_DIR/…"` →
`FAIL 3a install.sh runs a module outside sa02m_run_module (2 raw bash lines…)`.

**C — a 0-byte fragment is `broken`, not operator-masked.**
GREEN: `bash scripts/dev/test-installer-svc-helpers.sh` → `installer-svc-helpers: ALL OK`,
including the four new cases (`14a` capture + apply verbs `enable start`, `14b` a
/dev/null symlink still `left-masked`, `14c` a non-empty masked unit stays masked,
`14d` infra enable without unmask) with every pre-existing case unchanged.
`bash .ai-dev/quality/checks/installer-svc-policy-gate.sh` → `all checks passed`
(incl. `(f) >=25 sa02m_svc_apply sites (41)`).
RED by mutation on a scratch tree, both branches separately: removing the capture
branch → `FAIL 14a capture: en='masked' (expected broken)` and
`FAIL 14a broken app unit: verbs='' LAST_RESULT=left-masked`; reverting
`_sa02m_svc_apply_infra` to the plain `unmask` →
`FAIL 14d infra broken fragment: verbs='unmask enable'`.

**D — post-check honesty** (`scripts/offline-full-update.sh`; the harness is renamed
to `scripts/dev/test-offline-update-wrapper.sh` because it now covers D and E).
RED FIRST on the shipped wrapper, reproducing the 1.136 lie verbatim:
`FAIL 1 svc-masked expected a FAIL row naming the mask, got: PASS служба svc-masked … состояние сохранено`,
same for the 0-byte fragment. GREEN after:
`FAIL служба svc-masked  юнит замаскирован (inactive) — сам не поднимется` and
`FAIL служба svc-empty  файл юнита пуст (обрыв установки) — переустановите модулем`,
while `3 svc-stopped: PASS «состояние сохранено»` (the refresh guarantee) and
`4 svc-alive: PASS active` stay green.

**E — detached launch + runbook.**
GREEN: `6a live launch (l.411) … nohup setsid env "${LAUNCH_ENV[@]}" bash install.sh </dev/null > "$LOG" 2>&1 &`
and `6b DRY-RUN line (l.401) mirrors the live launch`.
RED on a scratch copy reverted to the 1.0.6.40 form: `FAIL 6a live launch lacks
setsid and/or </dev/null` plus `FAIL 6b … advertises a form that is not run`.
Comment-out mutation of the launch line → `FAIL 6a launch line: expected exactly one
live … found 0`.

**G — runtime-watchdog hold. The mechanism, verified as far as this host allows.**
The real home of the value is **`etc/systemd/sa02m-watchdog.conf:48`**
(`[Manager] RuntimeWatchdogSec=15s`, installed by `scripts/01-system.sh:564` into
`/etc/systemd/system.conf.d/sa02m-watchdog.conf`) — the `:11` occurrence is that same
file's header prose, so the value was never "only a comment".
Mechanism: **`busctl set-property … org.freedesktop.systemd1.Manager
RuntimeWatchdogUSec t <µs>`**, with `systemctl set-property --runtime Manager
RuntimeWatchdogSec=<µs>us` as a second attempt, and **the property read back after
every write** (`busctl get-property … RuntimeWatchdogUSec`), so the caller is told
the value ACTUALLY in force. The bus write is an override and survives the modules'
`daemon-reload`; `daemon-reexec` is deliberately NOT used (D4 names re-execing PID 1
as a stall candidate).
Honesty: **not executed on the board by this Builder** — no device access from here.
What IS proven here is that a manager which accepts the write and changes nothing is
REPORTED, never believed; the Orchestrator's read 6 on 1.136 remains the confirmation
that the first ladder step really works there, and is the only open item of G.
GREEN: `bash scripts/dev/test-watchdog-hold.sh` → `watchdog-hold: ALL OK`
(`4a rc=1 and the value ACTUALLY in force (15000000 µs) is what the caller is told`,
`4b the systemctl fallback was tried before giving up`,
`7a identical block in both homes (56 lines)`, `9a`/`9b` runner wiring).
RED by mutation: dropping the two read-backs from `sa02m_runtime_watchdog_set` (the
1.0.6.40 shape) → `FAIL 4a hollow manager: rc=0 printed='0' — a no-op write was
reported as success`; commenting out the install.sh EXIT trap → `FAIL 8b`;
re-introducing a hollow `set-property … RuntimeWatchdogSec=15s` in the runner →
`FAIL 9a` (and `9b restore=0`); one byte of drift between the two copies of the
shared block → `FAIL 7a` with the diff printed.
**Second defect found by mutation:** the first cut of pin 9a banned only `=0` and
`=${UPPER}`, so a re-introduced hardcoded `=15s` slipped through; the pin now allows
exactly ONE live `set-property --runtime Manager` line in the runner — the helper's
own read-back-checked one.

**Regression net after all of it:** `installer-svc-helpers` ALL OK ·
`installer-svc-policy-gate` all checks passed · `update-conditional-restart` PASS ·
`iface-dns-ensure` PASS · `update-deploy-skip` **SKIP**, reported as a skip (its own
message: «sandbox filesystem cannot represent POSIX modes … run under WSL/Linux») —
CI is authoritative for that row.

**Not built (outside this Builder's task):** step **F** (install lock). Nothing in
A–E/G depends on it; it stays open in D5.

## Handoff — the files this Builder must not touch (Orchestrator's to land)

**1. `.ai-dev/quality/tools.json` — four new rows**, beat `build`:

| id | run | covers |
|---|---|---|
| `install-atomic` | `bash scripts/dev/test-install-atomic.sh` | `scripts/`, `install.sh`, `scripts/dev/test-install-atomic.sh`, `scripts/dev/codemod-install-atomic.py` |
| `installer-order` | `bash scripts/dev/test-installer-order.sh` | `scripts/05-mqtt.sh`, `scripts/04-flasher.sh`, `install.sh`, `scripts/dev/test-installer-order.sh` |
| `offline-update-wrapper` | `bash scripts/dev/test-offline-update-wrapper.sh` | `scripts/offline-full-update.sh`, `scripts/dev/test-offline-update-wrapper.sh` |
| `watchdog-hold` | `bash scripts/dev/test-watchdog-hold.sh` | `scripts/lib.sh`, `install.sh`, `etc/sa02m-update-runner.sh`, `etc/systemd/sa02m-watchdog.conf`, `scripts/dev/test-watchdog-hold.sh` |

Each `checks` text must state what the harness really verifies (its header is the
source): sandbox behaviour for `install-atomic`, `offline-update-wrapper` §1–5 and
`watchdog-hold` §1–6; comment-stripped line pins for `installer-order`,
`offline-update-wrapper` §6 and `watchdog-hold` §7–9. `installer-svc-helpers`'
`covers` already includes `scripts/lib.sh`; only its `checks` text needs the new
`broken` state named.

**2. `comment-mutation-proof` `CASES` — three rows carry pins and need a case:**
- `installer-order`: comment out `sa02m_install_led_pkg "$BASE_DIR"` in
  `scripts/05-mqtt.sh` → RED (`1a` dependency line not found).
- `offline-update-wrapper`: comment out the `nohup setsid env …` launch line in
  `scripts/offline-full-update.sh` → RED (`6a … found 0`) — **measured**.
- `watchdog-hold`: comment out `trap sa02m_restore_runtime_watchdog EXIT` in
  `install.sh` → RED (`8b`) — **measured**.
`install-atomic` keeps the `comment-mutation-proof-exempt:` marker already in its
header (its site rule is the codemod sweep plus a drive-to-failure, not a needle).

**3. `CHANGELOG.md`, section 1.0.6.41 «Установщик» (Russian):**
- Файлы юнитов и helper-скриптов ставятся атомарно: аппаратный сброс посреди
  установки больше не оставляет пустой файл (systemd читал такой юнит как
  «замаскирован» — на стенде 1.136 так пропал прошивальщик).
- Общие пакеты Carel/LED ставятся перед мостами, которые их импортируют; после
  каждого модуля установщик делает `sync` — обрыв теряет один модуль, а не пять
  минут записей.
- Пустой файл юнита теперь считается обрывом установки, а не решением оператора:
  служба переустанавливается и включается по дефолту первой установки.
- Пост-проверки офлайн-обновления: замаскированная или пустая core-служба — FAIL,
  даже если до обновления она не работала.
- `install.sh` запускается в отдельной сессии (`setsid`, stdin из `/dev/null`) —
  закрытая SSH-сессия больше не может убить установку.
- На время установки снимается PID1-watchdog systemd (значение читается обратно и
  возвращается на выходе); тот же общий код заменил недействующую гарантию в
  OTA-runner'е.

**4. `.ai-dev/backlog.md`:**
- 2026-09-08 [HIGH] «Bench 1.136 reset in the middle of `install.sh --refresh`» →
  **RESOLVED** (1.0.6.41, steps A–E + G; step F still open).
- NEW [MED] «`etc/sa02m-factory-reset-runner.sh:41,45` still carries the hollow
  `systemctl set-property --runtime Manager RuntimeWatchdogSec=0/15s || true` guard
  the update runner just lost — port it to the shared block; `test-watchdog-hold.sh`
  case 9 is the pattern.» *(found while building G; that file is outside the D5/D7
  named set, so it was deliberately not edited here.)*
- NEW [LOW] «`scripts/update-www-only.sh`: the non-unit, non-`/usr/local` `install -m`
  sites are still non-atomic — widen the codemod's `LIVE_PREFIXES` or record why not.»
- NEW [LOW] «D5 step F (install lock) not built in this branch.»
- NEW [MED] «the runner's `SA02M_RUNTIME_WATCHDOG_SEC` env seam is gone (the restore
  value is read back from the manager instead) — confirm no deployment recipe sets it;
  an in-tree grep found no other user.»

## Progress note

- Goal: 8D for the 1.136 mid-install reset; A–E + G built on `fix/8d-installer`, D7 landed.
- Done: D1–D4; D5 steps **A, B, C, D, E, G** built and each proven RED-by-mutation /
  GREEN (evidence per step: «D6 progress» above); D7 homes written
  (`web-code-rigor.md` §System scripts / installer floors, `docs/deployment.md`
  «Офлайн-вариант», `docs/contracts/installer-refresh-policy.md`, `docs/bugs/BUGLOG.md`).
  Registry rows, mutation cases, CHANGELOG and backlog lines are in «Handoff» —
  the Builder does not own those files.
- Next: Orchestrator commits by the boundaries the Builder named, lands the Handoff
  items, then a fresh Reviewer over the cumulative diff.
- Open: (1) read 6 on 1.136 — does the `busctl` write really take on that manager
  (G is safe either way: it reports what is in force, it does not assume);
  (2) D5 step **F** (install lock) not built; (3) `svc-before` row for
  `sa02m-modbus-mqtt` still sharpens the D4 "torn 1.0.6.40 copy" reading.
