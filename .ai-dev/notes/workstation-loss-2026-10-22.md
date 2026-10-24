# Work lost with the workstation SSD (2026-10-22) — what survived, what to rebuild

The workstation that ran the 2026-10-21/22 sessions (user `admin`, all
worktrees under `C:/Users/admin/Downloads/`) lost its SSD. This note is the one
map of that loss, reconstructed on 2026-10-23 from GitHub, three bench boards
and session transcript excerpts the Operator pasted back. Hashes below that are
marked *lost* do not exist on GitHub (`git cat-file` fails). Sibling-session
notes (`…/side-session-notes/…`) and every `.ai-dev/plans/*` file of that
machine are gone too.

The crystal work has its own note: `clk32-crystal-recovery-2026-10-22.md`.

## The version chain at the time of loss

Stacked: `1.0.7.5 → 1.0.7.7 → 1.0.7.9 → 1.0.7.10`. `1.0.7.7` forks from
`1.0.7.5` at `e00e43ec`, before its last 7 commits, so it lacks the
reboot-hang note `e1e852fe` and the watchdog fix `18fa035d`; the later links
are known from the excerpts only. The top session's last plan was to
push the combined head as `1.0.7.10` and open one PR replacing #216 and #215;
the machine went down before that push. *Inferred*: the excerpts show the plan,
not the push target's final content. In the table, «Content» and «State at
loss» come from the excerpts except where a GitHub ref is named.

| Version / branch | Content | State at loss | Survived |
|---|---|---|---|
| `1.0.7.5` (PR #216, `ac80081d`) | audit remediation: UART port coordinator, root OTA controller, status polling, reboot-hang note | on GitHub, last commit 2026-10-21 23:18; per the excerpts a later integration review round passed | yes, on GitHub; the uncommitted Codex worktree `.ai-dev/worktrees/audit-remediation` is lost |
| `1.0.7.6` | updater build | named in excerpts only | no trace |
| `1.0.7.7` (PR #215, `3927cfa7`) | voice → LED: `R;G;B` colour, `voice-allow.default.json`, `docs/contracts/led-effects.json` | pushed; review approved per the excerpts | yes, on GitHub |
| `1.0.7.8` | reserved for the mic-gain task, then not used for it | — | — |
| `1.0.7.9` (head `675ec8e9`; phase 1 on `claude/light-music-plan-fe79e6`: `52d8af9e`, `b40704b4`, `64e2bae8`) | light music: `opt/sa02m-audio/sa02m_audio/lightmusic.py` (22 tests), contract `audio-recorder.md` §22, LED contract «light-music stream», `components.json` entry for the LED firmware repo `RGBW_WS2812` pinned at `32ed509` | reviewed, full gates green, kept local on the Operator's word | git history **lost**; code survives inside the deployed 1.0.7.10 below |
| `1.0.7.10` | mic gain: `[recorder] capture_gain` applied by the daemon on mic → present, panel field, contract §8/§15 | reviewed, local | git history **lost**; **deployed copy survives** (below) |

## The deployed 1.0.7.10 on the second bench board

A second board (MAC `02:53:4e:1c:79:58`, machine-id `597af708…`), on
192.168.1.136 since 2026-10-23, runs **web 1.0.7.10**, installed by the
offline wrapper on 2026-10-22 13:10–13:20 from a git-archive tree in
`/tmp/sa02m-upd` (gone — `/tmp` is tmpfs), after 1.0.7.5 at 12:16. Its
`/root` keeps both install logs and `/etc` + web-root backups taken before
each install. The kernel command line carries `watchdog.stop_on_reboot=0`
(`/root/boot.scr.min215`, the 1.0.7.5 watchdog fix shape). All of this was
read live on 2026-10-23 and is transcribed in
`tools/dev/clk32-recovery/live-reads-20261023.txt`.

The deployed trees are there: `/var/www/network_config`, `/opt/sa02m-*` —
19 of them **with their `tests/`** (snapshot listing) — e.g.
`sa02m_audio/lightmusic.py`, `lightshow.py`, `mixer.py` with `capture_gain`,
plus `/usr/local/{sbin,lib,libexec}` and the systemd units. A read-only
snapshot of those trees (949 files, no
`__pycache__`, no secret-bearing `/etc` file) was taken on 2026-10-23 to
`C:\Users\perev\Downloads\SA-02m-recovery-20261023\board136-1.0.7.10-deployed.tgz`
(md5 `85ab1d14…`) — outside the repo, on one disk only. Not deployed and so
not recoverable from the board: `docs/` (contracts, decisions,
`deployment.md` sections), `CHANGELOG.md`, `.ai-dev/` (registry rows, notes,
backlog) and `scripts/dev/` tests.

## Operator decisions recorded only in the lost sessions

Re-ask nothing below; these were the Operator's words (2026-10-21/22).

- **Light music** (1.0.7.9): one shared 16 kHz microphone stream, voice and
  recording keep working during light music (no pause); default style is the
  level bar (LED effect 75), colour-wash and equaliser stay selectable; the
  LED strip has no sound input, so all audio analysis is ours; re-send the
  strip state at most every 2 s so its flash is not worn (the strip saves 3–4 s
  after the last write).
- **Mic gain** (1.0.7.10): no MQTT control for the gain; the reboot and
  re-plug check runs on the bench after merge, per `docs/deployment.md`.
- **Deploy prep for 1.0.7.7 on bench 135** (lost branch
  `claude/deploy-prep-1077-5ef484`, `47eb1022`, `5a665775`, a reviewed
  `docs/deployment.md` «Стенд 192.168.1.135» section):
  - carry `7399d9dd` (the devices-socket group hook) into the release — it
    exists only on `origin/1.0.7.1`, not on `main` or `1.0.7.7`;
  - COM5 scope (`"/dev/COM5": ["sa02m-test-trigger.service"]` in
    `/etc/sa02m/port-coordinator.json`) is a board step after install, not a
    repo template;
  - the deploy window is any time, with the «no tests running» check kept;
  - the colour check without voice uses one fixed RK3588 reply at the current
    `upload_url`.
  - Risk found there: the bench gunicorn `sa02m-stand-api` loads its config
    from `/opt/sa02m-devices`, which the installer syncs with `--delete`.
- **Reboot guard on a dead-crystal board** (plan only, never built): warn,
  never refuse; show the warning only on the «Перезагрузка» click; leave the
  failure watchdog as it is; no warning on a board whose watchdog was measured
  counting on the RC.

## Investigations with no code to lose

Per the excerpts:

- MR-02m 12AI (`mr02m-COM4-12`) on 135 goes offline ~13×/h: UART RX FIFO
  overrun on COM4 (ttyS5) for its 89-byte AI block reply; fixed by #210 (web
  ≥ 1.0.7.2), not yet installed on 135 (bridge from 2026-10-08). After the
  install the bridge log should read `AI block read @400+21`, with no
  `Short response` for 12AI and no `CRC mismatch on FC04` for the DTV.
- USB microphone on 135: `snd-usb-audio` is in both kernels since `1.0.7.4`
  (`aa83eec1`), on GitHub.

## Rebuild order

1. Merge #216 and #215 (they are intact) or re-stack them, carrying `7399d9dd`.
2. Rebuild the dead-crystal reset path from the recovered bytecode: the
   helper's source is rebuilt (2026-10-24); its unit, contract, tests and
   landing remain (crystal note).
3. Restore 1.0.7.9 light music and 1.0.7.10 mic gain from the deployed
   snapshot: map each deployed path back to its repo path (`install.sh` and
   `scripts/*.sh` are the map), diff against `origin/1.0.7.7`, and rewrite the
   missing contracts, CHANGELOG sections and registry rows from the decisions
   above.
