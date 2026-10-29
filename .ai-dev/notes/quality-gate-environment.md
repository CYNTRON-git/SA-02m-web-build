# Quality-gate environment — where a local green is not a real green

Durable project knowledge (committed, team-shared). Records which quality rows
tell the truth on which machine, so a session does not mistake a skip for a
pass. The registry itself is the one home for *what* each row checks
(`.ai-dev/quality/tools.json`); this file only records *where it actually runs*.

**The current Windows box** (re-provisioned 2026-10-28; every «measured» for the current box below is
2026-10-29, `main` at `9bf0ab60`): git-bash 5.2 (MSYS), Node 24, a native
Python 3.12.10 whose `python3.exe` is a hand-made copy of `python.exe`, `gh`.
It has **no WSL distro** (`wsl.exe` answers «not installed»), **no Cygwin, no
`flock`, no `visudo`, no `shellcheck`**. The full build beat there is 149 rows:
117 PASS, 6 PARTIAL, 12 SKIP, 14 FAIL — every FAIL is in the FAIL table below
with its cause (one is marked «not traced»); CI on Linux is green on the same code. Rows below that say
«Cygwin's `flock` on `PATH` (the former box)» were measured on the box before
2026-10-28; on the current box those rows take their no-`flock` branch.

---

## Rows that SKIP on a Windows dev box

A row that cannot run here exits 77 and `node .ai-dev/quality/run.mjs` prints
`SKIP` for it, counts it apart and names it in the summary (the convention's home
is `run.mjs`'s header); the beat stays green — a missing optional tool must not
block a dev machine. Before 1.0.6.58 a skip exited 0 and printed `PASS`. Either
way **a green local run is not evidence for these rows**. A partial skip (one
assert inside a row that otherwise ran) prints `PARTIAL` only where the harness
reports it to the runner (`.ai-dev/quality/checks/lib_skip.sh`) or unittest
prints its native `OK (skipped=N)` summary. The protocol lives in `run.mjs`'s
header; another harness that reports neither still prints `PASS` — the
«Partial skips» section below says which is which:

| Row | Skips when | Real authority |
|---|---|---|
| `shellcheck` | `shellcheck` not on `PATH` (whole row SKIP — review beat; measured 2026-10-29: `checks/shellcheck.sh` prints «skipped (not installed here; CI/Linux runs it)» and exits 77). The current box has no WSL either, so not even the «evidence only» WSL run below exists; a skipped `shellcheck` is the whole local answer | Linux CI |
| `py-unit-*` (pytest-suite.sh), `sh-model-schema` | pytest / a listed dep / `jsonschema` not importable by the first working `python3`/`python`/`py` (whole row SKIP) | Linux CI (`web-quality.yml` installs them) |
| `py-unit-agent-api` | `test_devices_ops.DevicesSocket` (the fake devices daemon on AF_UNIX that checks the agent's panel-session cookie): Windows CPython has no `socket.AF_UNIX` — native unittest skips make the row PARTIAL | WSL / Linux CI |
| `update-deploy-skip` | the mode probe: git-bash cannot represent POSIX modes (whole row SKIP — the live one on a Windows box); also no `python3` | WSL / Linux CI |
| `update-conditional-restart`, `update-recover-boot`, `web-update-launcher-guard` | no `python3` (whole row SKIP) | Linux CI |
| `cache-bust-r` | no reference state reachable (no `origin/<ver>`, `origin/<ver-1>`, `origin/main`; whole row SKIP) | the local build beat (CI's clean checkout is toothless by design — the row's own header) |
| `ui-layout` | playwright not installed (whole row SKIP; the skip is a dev-box courtesy; install locally with `npm run ui-layout:install`) | Linux CI — installs the scripts/dev harness + chromium since 1.0.6.49 (`web-quality.yml`) |
| `cloud-card-smoke` | **never — it does NOT skip**: exit 2 RED without playwright/chromium (deliberate, never a vacuous green); local install = `npm run ui-layout:install` | Linux CI (same install step) |
| `sh-modal-layout-smoke` | **never — it does NOT skip**: exit 2 RED without playwright/chromium (deliberate); local install = `npm run ui-layout:install` | Linux CI (same install step) |
| `i18n-dict-contract` | never (node only) | Linux CI |
| `html-id-contract` | never (node only) | Linux CI |
| `web-auth-behaviour` | two POSIX-mode asserts skip off Linux | Linux CI |
| `install-atomic` | case 8b only: MSYS derives a mode from the file body (a `#!` first line reads 755, anything else 644) and ignores `chmod`, so `-m 644` and `-m 755` are indistinguishable | Linux CI |
| `update-cgroup-escape` | E5's «lock held again» assert only, and only when NO `flock` is on `PATH`: the harness shims it as a no-op and prints SKIP for that one line. With Cygwin's `flock` on `PATH` (the former box) the row RUNS and FAILs — see the FAIL table; on the current box (no `flock`) it PASSes with E5 skipped (measured) | Linux CI |
| `sudoers-visudo` | `visudo` not on `PATH` (git-bash): whole row SKIP | WSL / Linux CI (`sudo` package) |
| `path-mask-lib` | case P5 only: git-bash cannot create the dangling symlink the case needs; the summary line counts it as a skip, never as a pass (1.0.7.2) | Linux CI |
| `web-update-launcher-guard` | whole row SKIP (exit 77) when NO `flock` is on `PATH` (since 1.0.7.5): the launcher's first act is `flock(1)` on launch.lock, so without a flock every L case would exit 1 and read as a guard failure. With Cygwin's `flock` on `PATH` (the former box) the row RUNS and FAILs — see the FAIL table; on the current box it SKIPs (measured) | WSL / Linux CI |
| `status-vplc-probe` | two cases need `AF_UNIX` (Windows CPython has none): 5 PASS + 2 SKIP here, 7/7 under WSL (1.0.7.2) | WSL / Linux CI |
| `py-unit-audio` | POSIX-only cases (fcntl leases, `dir_fd`, process groups, the real `timeout`): partial skip — roughly half the suite skips here and the row prints `PARTIAL` (audit 2026-10-24, build table); the whole suite runs under WSL (1.0.7.4) | WSL / Linux CI |
| `audio-trigger` | no `flock` on `PATH`: whole row SKIP (exit 77), never a pass (1.0.7.4). With Cygwin's `flock` on `PATH` (the former box, 2026-10-23) the row RUNS and FAILs — every helper call answers `busy` (the same MSYS-fd-under-Cygwin-`flock` suspect as `install-profile` P6l below), so `comment-mutation-proof` reports its case «not green on an unmutated tree»; on the current box it SKIPs (measured) | WSL / Linux CI |
| `sudoers-pin-contract` | two cases print SKIP and the row still exits 0: sandbox (c), the iface-conf-write symlink refusal (git-bash cannot create a real symlink, `ln -s` copies), and 8c/8d, the 0440 root:root normalisation and the runner's read-back (MSYS maps a mode onto the DOS read-only attribute: `chmod 0440` reads back **444**, `0644` reads back 644 — measured 2026-10-29; the gate's probe wants exactly 440 and SKIPs). Because 8c/8d cannot run, `comment-mutation-proof`'s three cases on the `if chmod 0440 …` / `_now=$(stat -c` lines (`scripts/lib.sh`, `etc/sa02m-update-runner.sh`) cannot go RED here and FAIL — host-dependent, not a hollow gate: «Reading the proof's FAILs» below. On Linux 8c/8d run as any user: as non-root (CI) the chown to root is modelled, never skipped; GREEN under WSL as root and as `nobody` (2026-10-23) | WSL / Linux CI |
| `install-maintenance`, `update-control-boundaries` | whole row SKIP (exit 77): Windows CPython has no `fcntl` (the wrapper's and the controller's flock idiom). Neither needs root since 1.0.7.10 — each suite rebinds its owner constant to the running uid (`install-maintenance`: `JOURNAL_OWNER_UID`, plus an `install -o/-g` stub in the 03a fixture; `update-control-boundaries`: the state owner). `install-maintenance`: 18 OK under WSL as root and as `nobody`; `update-control-boundaries`: 15 OK as root and as `nobody` (both 2026-10-23) | WSL / Linux CI |
| `py-unit-update` | PARTIAL: `tests/test_control_cgi.py` (HTTP → real Bash CGI → controller) skips as a module without `fcntl` and writes one QUALITY_SKIP_LOG line — 25 passed + 1 skipped here, 36 passed under WSL (2026-10-21) | WSL / Linux CI |
| `board-verifier-safety` | whole row SKIP (exit 77) off Linux — the script guards itself (`sys.platform`, `/proc`; it needs `/proc`, `os.pidfd_open`, `select` on pipes) and writes the skip-log line; the row's `run` is the plain python call | WSL / Linux CI |
| `status-reliability-browser` | whole row SKIP (exit 77) when `playwright` is not resolvable from `scripts/dev` — the guard is in the row's `run` (the script imports playwright unconditionally); with `npm run ui-layout:install` done it RUNS here and under WSL (PASS on both, 2026-10-20/21) | Linux CI (installs the harness) |
| `daemon-access-effect` | whole row SKIP (exit 77) when not root and no passwordless `sudo -n`: every case chowns to foreign uids / uses `setpriv` (`scripts/dev/test-daemon-access.sh` header). PASS under WSL as root (audit 2026-10-24) | WSL (root) / Linux CI |
| `alice-conf-homes` | whole row SKIP (exit 77) on `os.name == "nt"`: it retargets Linux paths and needs a symlink-capable host. PASS under WSL as root (audit 2026-10-24) | WSL / Linux CI |
| `factory-reset-runner` | whole row SKIP (exit 77) on `os.name == "nt"`: it needs a POSIX host (`grp`, euid, the retargeted runner). PASS under WSL as root (audit 2026-10-24) | WSL / Linux CI |
| `status-coordinator-projection` | whole row SKIP (exit 77) off Linux: «Linux /proc + AF_UNIX required» (the script's own guard). PASS under WSL (audit 2026-10-24) | WSL / Linux CI |
| `dtb-patch-unit` | PARTIAL: U8 (the real `tools/imaging/boot/sun8i-a40i-sk.dtb` through `dtc` + `fdtget`) is a native unittest skip without `device-tree-compiler` — `OK (skipped=1)`, 7 passed, here (2026-10-29); U1–U7 run everywhere | Linux CI (installs `device-tree-compiler` since 1.0.7.14, `web-quality.yml`) |
| `dtb-patch-tool` | whole row SKIP (exit 77) when no `python3` / `python` / `py -3` executes (the real patcher runs through a shim; the Store `python3` stub is not a working interpreter). With the real Python dir first on `PATH` it RUNS and PASSes here (T1–T12, git-bash, 2026-10-29) — `dtc`/`fdtget` are shims, none needed | any host with a python; Linux CI |

When reporting results, say "skipped", never "passed". A reviewer that reports a
skipped row as a pass is making a false claim about verification.

## Rows that FAIL (not skip) on a Windows dev box — environment, CI is the authority

Observed on git-bash (MINGW64). The 14 rows named below are the FAILs of the full
build beat on the current box (2026-10-29, 149 rows); each was re-run alone. The
other lines of the table keep older measurements (2026-10-07, audit 2026-10-24)
from the former box, which had Cygwin's util-linux `flock` on `PATH`.
A row whose harness needs a POSIX host and finds a working `python3` does not exit
77: it runs and FAILs, so `run.mjs` and `comment-mutation-proof` count it as RED,
and every `comment-mutation-proof` case of theirs reports «not green on an
unmutated tree». The provisioning session reported that a `python3` newly on
`PATH` (the hand-made `python3.exe`) turned the Linux-only python rows from SKIP
into FAIL — not re-measured with it removed; the 14 rows are `py-unit-rules`,
`py-unit-alice`, `py-unit-gateway`, `py-unit-homekit`, `py-unit-homeconnect`,
`homekit-trigger`, `homekit-cgi`, `homeconnect-trigger`, `homeconnect-cgi`,
`port-coordinator`, `update-conditional-restart`, `alice-image-identity`,
`service-ctl-policy-write`, `web-install-secret`. Hiding the interpreter again
would hide them, not fix them. Not evidence of a defect, not evidence of health
either:

| Row | Exact failure here | Cause |
|---|---|---|
| `homekit-trigger`, `homeconnect-trigger` | with Cygwin's `flock` on `PATH`: 57 failed asserts, every helper call answering `{"ok":false,"error":"busy"}`; with no `flock` on `PATH` — the current box, measured 2026-10-29 — `FAIL — flock not found (util-linux)` (first line, exit 1; these two rows FAIL where `audio-trigger` SKIPs) | an MSYS shell's fd under a Cygwin `flock` (the `install-profile` P6l suspect below); git-bash itself ships no `flock`. PASS under WSL (audit 2026-10-24) |
| `update-cgroup-escape` | 23 failed checks (audit 2026-10-24) | Cygwin's `flock` on `PATH` (the former box): PASS with `/c/cygwin64/bin` removed from `PATH` (E5 then skips, see the SKIP table), and PASS under WSL. The current box has no Cygwin: PASS with E5 skipped (2026-10-29) |
| `web-update-launcher-guard` | 18 failed checks (audit 2026-10-24) | Cygwin's `flock` on `PATH` makes the row RUN instead of SKIP: same proof — PASS with `/c/cygwin64/bin` removed from `PATH` (the row then SKIPs), PASS under WSL. The current box has no Cygwin: the row SKIPs (2026-10-29) |
| `homekit-cgi`, `homeconnect-cgi` | `FAIL 5a GET → the dispatch answer, verbatim …` — the CGI answers `{"ok":false,"error":"homekit_api_failed","message":"python dispatch failed or timed out"}` (and the `homeconnect_api_failed` twin) | NOT `flock`: the CGI's python dispatch imports `sa02m_homekit.api` (`import grp`) / `sa02m_homeconnect.api` (`import pwd`), which Windows CPython does not have — `python3 -c "import sa02m_homekit.api"` from `opt/sa02m-homekit` prints `ModuleNotFoundError: No module named 'grp'` (traced 2026-10-29; the earlier «not traced» is closed). PASS under WSL |
| `py-unit-homekit`, `py-unit-homeconnect` | `ModuleNotFoundError: grp` / `pwd` / `fcntl`, `OSError: [WinError 1314]` (symlink privilege) in 13-21 tests; the rows print FAIL, not SKIP, because pytest itself is importable | Windows CPython has no `grp`/`fcntl` and the account lacks the symlink privilege; rc=0 under WSL Ubuntu-24.04 on the same tree (2026-10-14) |
| `alice-image-identity` | 70 failures, `ln: failed to create symbolic link … No such file or directory` (CB2 belt, Home Connect dirty-link dimensions) | git-bash cannot make the symlinks the cases plant; rc=0 under WSL |
| `service-ctl-policy-write` | 2 failures, both the `control:` cases «the planted package is not importable at all» (2026-10-29) | the plant is `open('/tmp/tmp.X/planted-…','w')` inside a `python3 -c` string: native python reads `/tmp` as `C:\tmp`, which does not exist (measured: `FileNotFoundError`; the same path handed as an argv word or in `cygpath -m` form works — MSYS converts argv, not code). The same MSYS-`/tmp` class as `update-conditional-restart`; rc=0 under WSL |
| `update-conditional-restart` | run8: `python3.exe: can't open file 'C:\tmp\tmp.*\gen-manifest.py'` (SKIPped while no python existed; 2026-10-29) | MSYS `/tmp` is not Windows python's `C:\tmp`; rc=0 under WSL |
| `port-coordinator` (1.0.7.5) | `FAILED (errors=7)` in 4 tests (2026-10-29): 3 × `ModuleNotFoundError: No module named 'fcntl'` on the import of `sa02m_ports`, and 4 subtest errors in `test_gateway_status`, each `FileNotFoundError` on `\\proc\\self\\stat` (no `/proc`); NOT a console-encoding error — no `UnicodeEncodeError` in the log under any of the three encoding settings below) | Windows CPython has no `fcntl` (nor AF_UNIX, `/proc`, SO_PEERCRED); PASS under WSL. (Its sibling `status-coordinator-projection` now SKIPs off Linux — the SKIP table.) On Linux `port-coordinator` (and `py-unit-gateway`'s real-broker test, which imports its fixture) runs as any user: unprivileged, the occupant scan is scoped to the suite's own process tree since 1.0.7.10 — a root process elsewhere in the session (the CI runner's launcher, `runuser`/`sudo` without `setsid`) is not read; the session scope it replaced refused every lease on CI with `PermissionError` on `/proc/<pid>/fd` |
| `py-unit-gateway` | 35 failed / 105 passed (audit 2026-10-24): `AttributeError: module 'os' has no attribute 'openpty'` (`tests/test_lease_retry.py` + `tests/test_transport_lifecycle.py`), `ModuleNotFoundError: No module named 'termios'`, and `socket.AF_UNIX` cases; the row prints FAIL, not SKIP, because pytest, serial and yaml are importable | Windows CPython has no pty (`os.openpty`, `termios`) and no AF_UNIX; 140 passed under WSL on the same tree (2026-10-21, 1.0.7.7) |
| `js-unit-mqtt-scan-type`, `js-unit-mqtt-tcp-dialog` | NOT a Windows row: they FAILed under the former WSL distro's Node 18 (`SyntaxError: Unexpected token 'export'` from `scripts/dev/mqtt-js-sandbox.mjs` importing a module-only `.js`, 2026-10-21) and PASS on Windows Node 24 and on CI's Node 20 (`web-quality.yml` `node-version: '20'`) | Node before 20.19 has no ESM syntax detection for a `.js` without `"type":"module"`; CI is the authority — any Node >= 20.19 runs them (the former WSL distro's Node 22.22.1 qualified; not re-run there on 2026-10-24) |
| `install-profile` | P6l only (the real-flock wait): with Cygwin's util-linux `flock` on `PATH` (`/c/cygwin64/bin/flock` on the former box) the case RUNS instead of skipping and FAILs — «lock content '4242', grant removed=no, rc=1»: the removal never got the lock the holder released; cause not traced (an MSYS shell's fd under a Cygwin `flock` is the suspect). Without any `flock` on `PATH` it prints SKIP. 62/62 OK under WSL (2026-10-23) | WSL / Linux CI |
| `py-unit-alice` | 910 tests, 1 deterministic failure: `test_mtd_distance.test_atomic_commit_concurrent_writers_and_save_failure` — `Lists differ: [1, 1] != [0, 1]` (2026-10-29, default and `PYTHONUTF8=1`; under `PYTHONIOENCODING=utf-8` the same test ends in an ERROR, `PermissionError [WinError 5]` from `os.replace` in the racing writers; the cp1251 console adds nine `--- Logging error ---` blocks on the `→` in a log line, which print but fail no test) | `devices_lock` (`sa02m_alice/common/config_store.py`) is a no-op without `fcntl` by design («Windows dev box»), so two concurrent `commit_provision` calls both append. A Linux-only test with no skip guard, so it FAILs instead of SKIPping; PASS under WSL |
| `py-unit-rules` | 331 tests, 1 error: `RecursionError: maximum recursion depth exceeded while encoding a JSON object` at `json.dumps(bucket, allow_nan=False)` in `test_non_finite_consumers.VarsSweepBoundTests.test_a_deep_chain_slipped_in_through_the_sandbox_ends_within_budget` (2026-10-29, same under all three encoding settings) | **cause not traced.** Linux CI is green on the same tree; that a Windows CPython 3.12.10 recursion/stack difference is the reason is a guess, not an established fact — re-run under Linux before calling it a host artifact |
| `web-install-secret` (1.0.7.11, `9bf0ab60`) | 16 failures (2026-10-29): 12 in group E (the log scrub), where the scrub's `python3` heredoc dies with `AttributeError: module 'os' has no attribute 'O_NOFOLLOW'` and every later E assert follows from the unscrubbed log; 4 in group C («… staging mode is not 0600»). Group E also prints two SKIPs (foreign owner needs root; `ln -s` makes no symlink) | Windows CPython has no `os.O_NOFOLLOW`; MSYS cannot represent a 0600 mode (the same mode model as the `sudoers-pin-contract` row). rc=0 under Linux CI |
| transient (host load) | `py-unit-alice` broker-restart timing (`test_mqtt_link_broker` — red in 1 of 3 sequential runs on 2026-10-29),  `telemetry-device-id-contract`, `no-absolute-api-paths`, `iface-dns-ensure` wait budgets, `install-atomic` 9c - seen RED once each with 4-6 parallel agent runs on the box, PASS alone | not environment, not a defect: contention; re-run alone before filing |

### The console code page — what `PYTHONUTF8=1` fixes and what it does not

Measured 2026-10-29, each of the 14 FAIL rows run alone through its registry
`run` command under the default environment, `PYTHONUTF8=1` and
`PYTHONIOENCODING=utf-8`. On this box a piped `python3` writes with **cp1251**
(the Russian ANSI code page; `sys.stdout.encoding` = `cp1251`,
`sys.flags.utf8_mode` = 0) — not cp1252.

- Only `py-unit-alice` prints a `UnicodeEncodeError` ('charmap' codec can't
  encode `→`): nine `--- Logging error ---` blocks from one `log.info`
  with a `→` in it. `logging` swallows them; **no test fails because of it**.
  `port-coordinator` and `py-unit-rules` print none (the first dies on a missing
  `fcntl`, the second is its own row above).
- Either variable removes those blocks (0 left). **Neither turns any of the 14
  rows green**: all 14 exit 1 under all three settings. `py-unit-alice` shows
  1 failure (`test_mtd_distance`, the `fcntl` row above) under `PYTHONUTF8=1`
  and 1 error in the same test under `PYTHONIOENCODING=utf-8`; the default run's
  second failure was the broker-restart transient.
- So set `PYTHONUTF8=1` to keep a log readable, never to «fix» a row: a row that
  fails with the encoding noise gone fails for the reason in its table line.

### Reading `comment-mutation-proof` on Windows

Its FAILs and SKIPs here (2026-10-29) are three kinds; only the last concerns a
mutation.

- **`<row> is not green on an unmutated tree`** — `web-install-secret`,
  `homekit-trigger`, `homekit-cgi`, `homeconnect-trigger`, `homeconnect-cgi`:
  the row is already FAIL above, so its mutation cases prove nothing. Nothing to
  read into the cases.
- **`<row> skips in this environment (exit 77) — … NOT measured here`** —
  `alice-conf-homes`, `factory-reset-runner`, `daemon-access-effect`,
  `audio-trigger`, `board-verifier-safety`. A SKIP, reported as one.
- **`sudoers-pin-contract stays GREEN with … commented out — the gate is
  hollow`**, three cases: `if chmod 0440 "$_p"` in `scripts/lib.sh`,
  `if chmod 0440 "$_path"` and `_now=$(stat -c` in `etc/sa02m-update-runner.sh`.
  **Host-dependent, not a hollow gate on Linux.** Read in the gate: it holds no
  static pin on any of those three lines (the `chmod 0440` / `stat -c` calls in
  `checks/sudoers-pin-contract.sh` are its own sandbox seed and host probe); they
  are covered only by the behavioural 8c/8d run, whose probe SKIPs on MSYS
  (`chmod 0440` reads back 444, not 440 — the SKIP table) while the row still
  exits 0, so no mutation can turn it red here. On Linux the same three cases
  print `ok … goes RED` — CI run 40694932196 (`web-quality.yml`, strict mode,
  2026-10-28, head `d526d172`). Two honest limits: the generic wording «the gate
  is hollow» is the proof's text for any case that stays green, and the proof
  turns a gate's inner SKIP into a FAIL instead of a SKIP — conservative (never
  a false green), at the price of a red review beat on Windows. Neither is a
  protection gap, so neither is backlogged. The run ended `8 FAILURE(S)` — these
  three plus the five «not green» rows above — and nothing else; had a Linux run left a case green,
  that would be the defect to route.

## `nginx -t` runs only on the board

No dev box here is known to have nginx (git-bash none; the former WSL
distro was never checked for it), and CI does not run it. The site file
`etc/nginx/network_config.conf` is syntax-checked for the first time on the
board, at install: `scripts/03-webserver.sh:134`
(`nginx -t >> "$LOG_FILE" 2>&1 && log OK "nginx config OK"`), and again in
`scripts/11-devices.sh:72` and the OTA apply/rollback in
`etc/sa02m-update-runner.sh`. The static gates that read the site file
(`devices-api-upstream`, `flasher-auth-header-strip`, `vplc-route-contract`)
pin intent, not syntax — a green beat says nothing about whether nginx accepts
the file.
## Partial skips, and CI strict mode

**Reported as `PARTIAL`** (the harness sources `lib_skip.sh`): `web-auth-behaviour`
(cases 5–6 off Linux, 34–37 without a hasher), `install-atomic` (6c without
coreutils `timeout`; its 8b/8h skips left with the launcher's extracted helper in
1.0.7.5) and `py-unit-update` (the Linux-only CGI module writes its own
QUALITY_SKIP_LOG line, since pytest's `N skipped` summary is not parsed by the
runner — only unittest's `OK (skipped=N)` is). Every stdlib-unittest row is
`PARTIAL` by that summary with no opt-in; that is how strict mode caught, on
PR #217, `py-unit`'s root-only cases, `py-unit-alice`'s paho/socketio cases and
`py-unit-homekit`'s segno-absent case — closed in 1.0.7.10 (the root cases
re-run through passwordless `sudo -n`, CI installs paho-mqtt and
python-socketio, segno is hidden in-process).

**Still printed `PASS` although part of the row did not run** — not opted in yet:
`update-cgroup-escape` (E5 without `flock`), `update-deploy-skip` (15e when not
root — this one also skips on Linux CI), `sudoers-pin-contract` (the symlink
case, «note» line — also on Linux CI; 8c/8d off Linux), `install-profile` (P6l
without a `flock` on `PATH`, named in its summary line only), and every
`pytest-suite.sh` row whose tests skip per test — the runner does not parse
pytest's `N skipped`: `py-unit-devices` prints PASS here with 6 `test_api_socket.py`
cases skipped (no `AF_UNIX` on Windows; 0 skipped under WSL, 2026-10-23). Its
`openpyxl` case is no longer one of them: `openpyxl` is a listed dep of the row,
so without it the whole row SKIPs. Opting in is one `quality_skip` call per skip
site; the two marked «also on Linux CI» would then FAIL there under strict mode,
so each needs its CI skip removed (or the row's scope decided) in the same
change.

**Strict mode**: `web-quality.yml` sets `QUALITY_STRICT=1` for the whole job, so
in CI a `SKIP` or a `PARTIAL` is a `FAIL` — every tool is installed there on
purpose, and a skip means a broken install step. Locally the variable is absent.
The CI runs the switch was measured against had zero whole-row skips and no
`PARTIAL`-reporting row skipping: 1.0.6.67 (run 36709584151) 102/102 build,
9/9 review; the non-version branch `ci-linux-green` (run 36695168996) 96/96,
9/9.

## shellcheck — version skew is real and it has already cost a CI failure

**Do not substitute `npx shellcheck` for the row and call it green.**

`npx shellcheck` resolves to the **latest** binary (0.11.0 as of 2026-08).
CI's `ubuntu-latest` uses the distro package (0.9.0 family), and on real files
they disagree.

On 2026-08-06 a change passed `npx shellcheck --severity=error` (exit 0) three
rounds running and then failed CI on three genuine `SC2218` errors ("this
function is only defined later") in a new test harness.

**What is established:** 0.9.0 flagged that file, 0.11.0 did not, on the same
bytes with the same flags. **What is NOT established:** *why*. 0.11.0 does emit
`SC2218` in general — a three-line file (`cp a b` above `cp() { :; }`) triggers
it on both versions. So the honest statement is "0.11.0 missed this file's
pattern", not "0.11.0 dropped the check". The trigger resisted isolation:
`unset -f`, repeated definitions, an unresolvable `source`, ANSI-C quoting,
heredocs and multi-line `awk` were all ruled out, and a bisect that appeared to
localise it turned out to be a truncation artifact (the slice cut mid-`awk`, and
the resulting parse error suppressed all analysis).

**Do not treat this as a known-checks difference you can reason around.** The
operational conclusion stands on the observation alone: a newer local shellcheck
can silently miss what CI catches, so a clean local run is not evidence that CI
will be clean.

Worth recording about that specific defect, because it is the reason the miss
mattered: the harness defined injection stubs (`cp`, `mv`, `mktemp`, `wc`) below
some of their call sites, so behaviour depended on textual position. A trace of
every stub invocation showed nothing was actually resolving to the wrong
binary — each stub was bracketed `define; run; unset -f`, so the live window
only ever contained the code under test. It was a **latent hazard**, not a live
wrong result: the first future case added below a definition would have inherited
a stub it never asked for.

### WSL — gone on the current box; a Linux host, not a CI replica, where it exists

Since 2026-10-28 the box has **no WSL distro**: `wsl.exe -l -v` answers that the
Windows Subsystem for Linux is not installed (measured 2026-10-29). Every «under
WSL» result in this note is from the former box and is history until a distro is
reinstalled; the rest of this section describes that distro.

The distro was `Ubuntu` (Ubuntu 26.04 LTS) — `wsl -d Ubuntu-24.04` fails with
`WSL_E_DISTRO_NOT_FOUND`; use `wsl -d Ubuntu`. Measured by audit 2026-10-24:
native Node v22.22.1, python 3.14.4, util-linux `flock`, shellcheck 0.11.0,
runs as root. It differs from CI's `ubuntu-latest` (Node 20, non-root runner,
the 0.9.0-family shellcheck), so a WSL green is Linux evidence, not CI proof.

Two traps from the earlier distro still apply to how the beat is run: put
`/usr/bin` first on PATH so a Windows `node.exe` symlink (the old
`/usr/local/bin/node` → `/mnt/c/Program Files/nodejs/node.exe`) cannot win and
silently spawn Windows children —
`PATH=/usr/bin:/bin:/usr/sbin:/sbin node .ai-dev/quality/run.mjs build` — and
redirect the log INSIDE WSL (the wsl.exe stdout relay dropped a multi-MB run
mid-way). `node_modules` (a Windows junction) is invisible under WSL, so
Playwright rows run from Windows.

### Getting the real row to run on Windows — there is no exact local replica

No local shellcheck here matches CI's version: the former WSL distro's package
was 0.11.0, the same as `npx shellcheck`. A WSL 0.11.0 run (former box) of
`.ai-dev/quality/checks/shellcheck.sh` is **evidence only** (audit 2026-10-24:
rc=0) and is reported as such — the row stays SKIP locally and CI is the
authority. The only way to reproduce CI's version is a distro whose package is
the 0.9.0 family (e.g. a separate `Ubuntu-24.04` WSL install with
`apt-get install -y shellcheck`), reached through a `shellcheck` shim on `PATH`
that forwards to it — WSL inherits the Windows working directory, so relative
paths pass straight through. Keep the shim in the session scratchpad; it is a
dev-box convenience, not a repo artifact. The current box has no WSL, so this
route first needs a distro installed.

**Never shim a newer binary (`npx`, or the 0.11.0 WSL package) onto `PATH` to
make the row report PASS.** That converts an honest skip into a false green on
the one check CI is the authority for.

## A row that RUNS everywhere but gives the wrong answer per shell — `sed | grep -q` under pipefail

The skip class above is not the only way a local green lies. A row can run on
BOTH machines and still disagree, when its implementation is environment-fragile.

On 2026-08-12 `no-retired-session-token` was RED on CI/WSL (GNU sed) and GREEN on
Windows Git Bash (MSYS sed) — same bytes, same flags, opposite verdict. Root
cause: the gate matched a comment-stripped file with

```sh
strip_comments < "$f" | grep -qF "$needle"      # DO NOT
```

`grep -q` exits on the first match; the still-writing `sed` then takes a SIGPIPE
and exits 141; and under `set -o pipefail` that 141 becomes the pipeline status —
so a token that WAS found reads back as "not found" (`PIPESTATUS=141 0`). GNU sed
dies on the SIGPIPE; MSYS sed did not (buffering/exit-order), so Windows was
falsely green. The race only fires when sed is mid-write as grep quits — a match
past the pipe buffer — which is why the small-`$body` sibling check passed while
the ~200-line file failed, and why it produced BOTH a false positive (pin C) and
a latent false negative (pin B would MISS a real violation).

**Rule:** never pipe a producer straight into `grep -q` (or any early-exit
consumer: `-q`, `-l`, `-m N`, `head`) under `set -o pipefail`. Capture first,
then match in-shell (a `case "$text" in *"$needle"*)` substring test needs no
subprocess at all). The gate's `stripped_has` helper is the fixed form.

## A row that runs everywhere but reads CRLF into a unit name — Windows CPython pipes

Same class as the `sed | grep -q` row above: runs on both machines, opposite
verdict. `update-conditional-restart` was **RED on the former Windows box and GREEN on
WSL** (audit 2026-09-08, D4) on the same bytes. The shipped runner feeds its
service loops from `python3 -c 'print(u)'` through a process substitution;
Windows CPython (`3.14 [MSC]`) writes text-mode stdout as **CRLF**, so
`while IFS= read -r u` keeps a trailing `\r` in the unit name and
`IFS=$'\t' read -r u prefix` puts it on the prefix. The harness's systemctl shim
then answered `is-active` with `grep -qxF "$unit\r"` against a file of LF names
(never a match → restart skipped) and the journal gate's `startswith("/opt/…/\r")`
was always False. Not a runner defect — on the board (Linux CPython) the output
is LF. A false comfort hid the cause: MSYS grep strips CR from *input lines* but
not from the *pattern*, so `called "restart …"` still matched and the failure
read as "loop missing".

**Rule:** a harness that consumes the output of a shipped `python3 -c …` (or any
text-mode producer) normalises CRLF at ITS read sites, never in the shipped
code — a `python3` PATH shim (`| tr -d '\r'`, exit status via `PIPESTATUS`) for
producer-side normalisation, and `${a%$'\r'}` on every argv word in a recording
shim. The fixed form is in `scripts/dev/test-update-conditional-restart.sh`
(header + the two shims). Where a Windows RED cannot be explained, run the row
on a Linux host first (WSL where installed, else CI): a Linux GREEN on the same bytes points at this class.

**Sibling trap in the same harness — MSYS argv path conversion.** The CR fix
alone left the `restart_if_changed` case RED: git-bash rewrites any argument
that looks like a POSIX path when it spawns a NATIVE executable, so the change
gate's `/opt/sa02m-modbus-mqtt/` prefix reached `python.exe` as
`C:/Program Files/Git/opt/sa02m-modbus-mqtt/` and `startswith()` was always
False (proven: `MSYS_NO_PATHCONV=1 python3 -c 'print(sys.argv[1])' /opt/x/`
prints `/opt/x/`; without the variable, `C:/Program Files/Git/opt/x/`). Fix
shape: `export MSYS_NO_PATHCONV=1` in the harness, and — because that also
stops the conversion python needs for real files — every sandbox path python
opens is handed over in mixed form (`cygpath -m`), while `PATH` keeps the MSYS
form (a `C:/` PATH entry is not searched). Any harness that passes a literal
absolute POSIX prefix to a shipped python snippet is exposed to this class.

## A masked host command is still found through its `.exe` twin — MSYS

On MSYS/git-bash `command -v CMD` (and `stat`) fall back to
`CMD.exe`. A harness that plays «CMD is not installed» by mirroring the `PATH`
directories without a file named `CMD` therefore leaves `CMD.exe` in the
mirror, and the harness runs the host binary. It surfaced the day a
`python3.exe` first existed on this box: `fix-eth-link-cycle` masked `python3`
and the masked python still resolved. `path_without`
(`scripts/dev/lib_path_mask.sh`) now drops `CMD.exe` with `CMD` (1.0.7.12,
`d526d172`; `path-mask-lib` cases P9 and P9b pin it, RED on the previous lib).
Until that branch merges, `main` still carries the old lib.

**The shape to watch** in any other harness that hides, stubs or shadows a host
binary by NAME — a mirrored `PATH`, a `command -v` guard, an `rm` of a shim — is a mask that handles `NAME` and not `NAME.exe`. It reads GREEN on
Linux and on a box with no such binary, and runs the real tool on a box that
has one. Check a mask by asking the shell afterwards (`command -v NAME` must
fail), not by listing the directory. Only the harnesses sourcing
`lib_path_mask.sh` are covered; other masking idioms in `scripts/dev/` have not
been swept.

## Installing the harness where IPv6 has no route

Reported by the provisioning session on 2026-10-28 (not re-measured): this
network hands out AAAA answers it cannot route, so downloads that try IPv6 first
hang until they time out. What worked:

```bash
export NODE_OPTIONS=--dns-result-order=ipv4first    # npm and node fetches
pip install --timeout 180 --retries 8 <package>     # a slow link, not a dead one
curl.exe -4 -C - -o <zip> <playwright-chromium-url> # IPv4 only, resumable
```

`npx playwright install chromium` did not complete here, so the chromium zip is
fetched with `curl.exe -4 -C -` (run the same line again to resume) and extracted
into `%LOCALAPPDATA%\ms-playwright\chromium-<rev>\`. Playwright counts a browser
as installed only when an `INSTALLATION_COMPLETE` marker file sits in that
directory (`DEPENDENCIES_VALIDATED` beside it for the dependency check), so a
half-extracted directory is re-downloaded: write the marker only after the
extraction finished. The browsers are shared by every worktree, the `playwright`
package is not — it resolves from `scripts/dev/node_modules`, which a fresh
`git worktree` does not have. Measured 2026-10-29: `status-reliability-browser`
SKIPs («playwright not installed») in such a worktree; run
`npm --prefix scripts/dev install` there before the Playwright-driven rows.

## Red everywhere, not an environment cause — do not file these as «CI is the authority»

A row that is red on every host is a defect to fix, never an entry here. The
1.0.7.1 review beat met four such rows at once; all four are fixed:

- `cloud-card-smoke`, `homekit-card-smoke`, `homeconnect-card-smoke` — since
  1.0.7.0 (`a9c5dd4c`) «Управление» renders only the selected sub-pane, and the
  harnesses opened the tab without selecting «Облако» / «Умный дом», so the
  cards were measured hidden (`visible=false`, then a screenshot timeout).
  Fixed in the harnesses (`systemSelectSub`).
- `sh-modal-layout-smoke` — `the fit has a real margin, not a pixel: 7.5 px
  headroom (floor 24)` at 1280×720: the «Умный дом» form's preset row had
  grown the actions pane to 510.5 of 518 px. Fixed in the form (field gap
  12 → 8 px, main.css), not in the floor.

## The general rule

A local substitute for a skipping row is **evidence, not proof**. Say which
tool and which version produced a result, and name CI as the authority where it
is. The remote gate exists precisely because a local judgement can be wrong in
ways the local machine cannot see.

## New rows, 1.0.6.24 — what they need and what they cost

- **`comment-mutation-proof`** (review beat) needs `git`, `tar` and `awk`. It extracts
  a pristine copy of HEAD, overlays the working-tree gates, comments out each pinned
  line and requires the gate to fail. It is the largest single row we
  have added; budget it when judging CI time (`.ai-dev/notes/ci-budget.md`). It
  took ~40 s on Windows when added (1.0.6.24); **measured 2026-10-29 on the
  current box, alone, it ran ~44 min** (log file created 10:18, last written 11:02;
  about 220 cases, each a fresh gate run under slow git-bash process spawning) — start it in the
  background and read its log, never wait on it in a foreground call.
- **`check-lib`** (build beat) is a pure shell self-test of `checks/lib_check.sh`, no
  external tooling beyond bash. Fast.
- Both are **standing measurements, not one-off sweeps**: they exist so the
  hollow-gate class cannot return through a newly added gate. A future gate that
  greps for a pinned line WILL be caught by `comment-mutation-proof` only if its case
  is registered there — adding a gate means adding its case.

## Python bytecode cache can hide a same-second mutation

A RED-first proof that mutates a `.py` file and re-runs within the same second,
keeping the file length, can read the STALE `__pycache__` bytecode (mtime
granularity), so the mutation appears not to bite. The `web-bus-mode-bacnet` gate
sets a private `PYTHONPYCACHEPREFIX`; a hand-run proof should do the same or
`find -name __pycache__ -exec rm -r` first (found 2026-09-08 while proving R6).
