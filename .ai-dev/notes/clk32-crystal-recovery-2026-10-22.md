# Dead 32.768 kHz crystal on bench 136 — recovered picture (2026-10-22 work)

Durable record reconstructed on 2026-10-23 after the workstation that ran the
2026-10-21/22 sessions lost its SSD. Sources: what survived on the bench
boards, GitHub, and lost-session transcript excerpts the Operator pasted back
into the 2026-10-23 recovery session; claims from those excerpts say so. Where
the source is gone, the note says so instead of guessing.

The evidence files are preserved in `tools/dev/clk32-recovery/` (layout
below). They are bench-only evidence: four scripts plus the helper (its
bytecode and the source rebuilt from it) write `/dev/mem` when run, and must
never be shipped to or run on a fleet device from there. Observations read live on
2026-10-23 and not otherwise backed by a board file are transcribed in
`live-reads-20261023.txt` there, with the commands to repeat them.

"Board A" below is the faulty board (MAC `02:53:ce:20:92:4c`, machine-id
`6f7b9576…`), on 192.168.1.136 until 2026-10-23 ~17:20; another board has
held that address since.

## Established

- **Root cause of the reboot hang: board A's external 32.768 kHz crystal
  (LOSC) is dead.** On sunxi a software reboot is a watchdog reset and the
  watchdog counts from the 32 kHz domain; with the crystal dead it never fires
  and the board hangs after «Rebooting.» until power is removed. Sources: the
  header of `hardpy_tests/lib/sa02m_hw_diag.py` (2026-10-22) and the
  dead-crystal session excerpt (Timer2 on LOSC gives 0 ticks/s while OSC24M
  counts; the WDT restart bit stays 1; re-selecting the crystal, maximum gain
  and a cold re-flash all leave it dead).
- This **supersedes** the cause question left open in
  `.ai-dev/notes/reboot-hang-2026-10-21.md` as pushed on `1.0.7.5` (`e1e852fe`):
  that revision predates the crystal finding and names the i2c-2 SDA storm as
  the lead. Its facts on `device_shutdown`, the trace recipe and
  `watchdog_stop_on_reboot` still stand. The later revision of that note the
  hardpy header cites (`42f3518a`) is lost.
- **Registers.** `LOSC_AUTO_SWT_STA` (0x01C20404) reads `0x4` on board A —
  bit2, the hardware fell back to its internal RC — and `0x1` on the healthy
  135 (`sa02m_hw_diag.py` records the same pair; `board135/page-c20.txt`,
  dumped on 2026-10-22, shows 135's `0x400`/`0x404`/`0x310`/`0x314`). Board A's
  `LOSC_CTRL = 0` and its CCU `0x310 = 0x100`, `0x314 = 0` were read live only.
- U-Boot in the eMMC boot area is byte-identical on board A and 135 (read live
  only), so the difference is hardware, not the loader.
- Board A's system clock comes from `sun6i-rtc`, which runs on the internal
  RC: its file and journal timestamps are approximate.

## The internal-RC workaround that was tested on hardware

Scripts in board A's `/root`, mtimes 2026-10-22 14:26–14:30 on the board's own
clock (read live; the copies here carry new mtimes):

- `board136/rc-cali.py`, `rc-cali2.py` — measure the SYS_32K rate through the
  spare Timer2 under each divider (`0x01C20310`) and calibration value
  (`0x01C20314`), with the watchdog held off.
- `board136/switch-cali.py` — the switch itself: `0x01C20314 = 3`, then
  `0x01C20310 = 0x16AA0000 | 0x1F` (SYS_32K from the calibrated CCU RC), then
  one watchdog ping to confirm the restart bit self-clears (the clock ticks
  again).
- `board136/wdt-time.log` — `slot=11 SYS_32K=0000001f rc_rate=15996`, then a
  0.25 s counter that stops at 16.21 s — consistent with a watchdog reset at
  its 16 s slot, though the log alone cannot rule out a power loss. The script
  that produced it, `wdt-time.py`, survived only as a file of blanks (read
  live) and is not preserved.
- `board136/journal-boots-20261022.txt` — 11 boots between 14:28 and 16:24 on
  the board clock. 6 end with journald's `Journal stopped` (the boot after
  five of them finds no clean-shutdown marker; the sixth's successor ends
  before logging one),
  one with «watchdog did not stop!», 4 with no stop marker. 9 boots log
  «REBOOT REASON: unexpected — no clean-shutdown marker»; two (`1c1cde20`,
  `ee966d7a`) end about 2 s into boot. With the RC-driven clock the
  shutdown-to-next-boot gaps are not trustworthy (some are negative), so the
  journal shows that the board came back after each stop without saying what
  reset it.

The switch is held in the CCU and is lost on every reset: on 2026-10-23 board
A's registers read back at the defaults above, so **a software reboot of that
board will hang again**. Nothing on its rootfs or FAT partition applied the
switch persistently, and the 2026-10-21 `zz-bench-rebootwdt.conf` trial
drop-in was gone (all read live; details in `live-reads-20261023.txt`).

The preserved scripts disagree on the watchdog register layout: `regs.py` and
`rc-cali*.py` treat 0x01C20C94 as MODE (sun4i style), `sa02m_hw_diag.py` puts
MODE at 0x01C20C98. The helper below uses 0x01C20C94 for its `WDT_MODE`.

## The helper `sa02m-losc-fallback` — source rebuilt from its bytecode

The session that owned this fix (pasted excerpt) researched the reset paths,
chose the CCU RC switch and built a once-per-boot helper. Its source is lost,
but CPython 3.12 bytecode of it survived on 135 in `/tmp/__pycache__`
(compiled 2026-10-22 16:01 from `/tmp/sa02m-losc-fallback.py`; per the
excerpt it was tried on 135 and reported `healthy` 3/3, writing nothing). It is
preserved in `losc-fallback/` with its full disassembly.

The source is rebuilt as `losc-fallback/sa02m-losc-fallback.py` (2026-10-24).
Compiled by CPython 3.12.2, 3.12.3 or 3.12.4, its marshal payload is
byte-identical to the recovered file's: all 52 code objects match in
bytecode, constants, names, variables, flags, exception tables, line numbers
and every instruction position. Under 3.12.10 the bytecode and line numbers
still match, but 13 code objects differ in the end-line and column positions
of for-loop, comprehension and with-statement instructions — a compiler
change, not a source difference. 3.12.5–3.12.9 are unchecked. The board's
patch release is unknown: a pyc records only the 3.12 magic number, and the
only hint is the `Python 3.12.3` line heading `losc-strings.txt`.
`losc-fallback/compare_dis.py` repeats the check (how to run it: its header).
What is not recoverable: the comments. The original was 24961 bytes (pyc
header), the rebuild about 1.7 kB shorter; which of the original's
non-code lines were blank and which were comments is unknown, and every
comment in the rebuild is new (its line 33 says so). The rebuilt file itself
has never run on hardware. The original bytecode's three runs on 135 (above)
took the healthy path only, so the helper's switch and revert paths have
never run on a board, and the helper, as installed, still owes its first
bench proof.

Found while rebuilding — the planned change has to settle it: `SAFE_MAX_HZ`
is 32768 × 16 / 12, so the docstring's "effective watchdog timeout ≥ 12 s"
holds for the 16 s watchdog slot only. The helper records the programmed
slot (`wdt_interval_s`) and the resulting `wdt_timeout_eff_s` but does not
gate on them; with a shorter slot an accepted rate can give less than 12 s.

Its module docstring says:

- feed SYS_32K from the CCU internal RC with auto-calibration on, so the
  watchdog — and with it `reboot`, `reboot -f` and panic reset — has a clock;
- the hand-made switch was bench-proven; **the helper as installed was not
  yet bench-proven**;
- promises: no CCU or watchdog write on a board whose crystal counts (G2);
  never leave the switch at a rate not measured safe — effective watchdog
  timeout ≥ 12 s — else both registers go back (G3); exactly one journal line
  per run and, once the lock is held, one `/run/sa02m-losc-fallback.json` (G4);
- runs once per boot as root from `sa02m-losc-fallback.service`.

Outcomes: `healthy | switched | already-switched | reverted | anomalous |
unsupported | busy | internal-error`; dead-crystal threshold `DEAD_HZ = 100`;
RC accepted between `RC_MIN_HZ = 16000` and `SAFE_MAX_HZ = 43690` (÷1.02
margin); switch divider 31, calibration bits `0x3`.

Lost with it, named by the docstring: `docs/decisions/dead-losc-reset-path.md`
(the research), `docs/contracts/losc-fallback-state.md`, the `docs/deployment.md`
section, the unit, `scripts/dev/test_losc_fallback.py` and registry row
`losc-fallback-logic`.

## Lost sibling work on the same defect

Known from the excerpts only:

- **Detector** `usr/local/sbin/sa02m-clk32-check.sh` (verdict
  `ok|failed|unknown|skip`, boot cache `/run/sa02m-clk32.state`) on branch
  `claude/mystifying-meninsky-270f10`, commit `085279ba` — lost; Operator
  decision: no UI hint in the detector change.
- **Reboot guard** for the panel — plan only; its Operator decisions are in
  `workstation-loss-2026-10-22.md`.
- **PMIC path.** The research named the AXP221 (I2C bus 0, `0x34`) soft-restart
  bit as the fallback candidate — runs on the PMIC's own oscillator, not
  proven to reset the SoC on this board, unreliable on a kernel panic — and
  found no second watchdog, a stub PSCI reset in U-Boot 2022.04, and a
  poweroff-only AXP driver. No PMIC code is known to have been written.
- The commits `42f3518a`, `048a6a4e`, `a5112d54` cited for the reboot-hang
  analysis are not on GitHub (the note itself landed as `e1e852fe` on
  `1.0.7.5`).

The rest of the lost version chain is mapped in `workstation-loss-2026-10-22.md`.

## Preserved files (`tools/dev/clk32-recovery/`)

| Path | Origin | Writes `/dev/mem` |
|---|---|---|
| `live-reads-20261023.txt` | 2026-10-23 recovery session, read-only commands and their output | — |
| `common/regs.py` | identical on 135 and 136 | no |
| `common/clk32-probe.py` | identical on 135 and 136; drives Timer2 and pings the watchdog | **yes** |
| `board135/collect.sh`, `dumppage.py`, `page-c20.txt`, `page-f00.txt` | 135 `/root`, 2026-10-22 ~09:59 | no |
| `board136/rc-cali.py`, `rc-cali2.py`, `switch-cali.py` | 136 `/root` | **yes** |
| `board136/wdt-time.log`, `journal-boots-20261022.txt` | 136 | — |
| `hardpy_tests/lib/sa02m_hw_diag.py`, `acceptance/test_58_sa02m_hw_diag.py` | 135 `/opt/hardpy_tests`, 2026-10-22 12:41–12:47 | no (read-only by design) |
| `losc-fallback/sa02m-losc-fallback.cpython-312.pyc.bin` | 135 `/tmp/__pycache__/sa02m-losc-fallback.cpython-312.pyc`, 2026-10-22 16:01 (md5 `d89ca8d7…`); `.bin` added only because `*.pyc` is gitignored | **yes**, when run |
| `losc-fallback/losc-strings.txt`, `losc-dis.txt` | per-function constants/names and full `dis` output of that file, made with `inspect_pyc.py` under Python 3.12 (needs 3.12: the bytecode format is version-bound) | — |
| `losc-fallback/sa02m-losc-fallback.py` | source rebuilt from that bytecode, 2026-10-24; comments new | **yes**, when run |
| `losc-fallback/compare_dis.py` | 2026-10-24; compiles the rebuild and compares it with the bytecode, marshal-loading the pyc only | no |

The hardpy files are a snapshot of work never pushed to `CYNTRON-git/hardpy_tests`
(its `main` is at 2026-08-07); their home is that repository. Not preserved
here: its CLI wrapper `scripts/sa02m_hw_diag.py` (carries a default device
credential) and the edited `sa02m_platform.py`, `suite_presets.py`,
`test_67_sa02m_kernel_cpu.py` (still on 135 under `/opt/hardpy_tests`).

## Next steps

- The helper's source is rebuilt (above); still to write: its unit, state
  contract, decision record and tests (the docstring lists them), and land it
  through the normal loop. It has to settle two open points first: the
  watchdog-slot gap found in the rebuild, and which address is the watchdog
  MODE register, 0x01C20C94 or 0x01C20C98 (the disagreement above). The
  helper's `wdt_enabled`, `ping` and `wdt_fields` all depend on that answer.
  The first bench proof of the helper as installed is still owed.
  The detector and the panel reboot guard follow it.
- Until then, do not software-reboot the board with MAC `02:53:ce:20:92:4c`.
