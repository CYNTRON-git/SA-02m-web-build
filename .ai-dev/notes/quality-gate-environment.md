# Quality-gate environment — where a local green is not a real green

Durable project knowledge (committed, team-shared). Records which quality rows
tell the truth on which machine, so a session does not mistake a skip for a
pass. The registry itself is the one home for *what* each row checks
(`.ai-dev/quality/tools.json`); this file only records *where it actually runs*.

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
| `shellcheck` | `shellcheck` not on `PATH` (whole row SKIP) | Linux CI |
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
| `update-cgroup-escape` | E5's «lock held again» assert only: git-bash has no `flock`, so the harness shims it as a no-op and prints SKIP for that one line | Linux CI |
| `sudoers-visudo` | `visudo` not on `PATH` (git-bash): whole row SKIP | WSL / Linux CI (`sudo` package) |
| `path-mask-lib` | case P5 only: git-bash cannot create the dangling symlink the case needs; the summary line counts it as a skip, never as a pass (1.0.7.2) | Linux CI |
| `web-update-launcher-guard` | whole row SKIP (exit 77) since 1.0.7.5: the launcher's first act is `flock(1)` on launch.lock, so without util-linux flock every L case would exit 1 and read as a guard failure; before 1.0.7.5 only L8-L11 skipped | WSL / Linux CI |
| `status-vplc-probe` | two cases need `AF_UNIX` (Windows CPython has none): 5 PASS + 2 SKIP here, 7/7 under WSL (1.0.7.2) | WSL / Linux CI |
| `py-unit-audio` | POSIX-only cases (fcntl leases, `dir_fd`, process groups, the real `timeout`): partial skip — roughly half the suite skips here and the row still prints PASS; the whole suite runs under WSL (1.0.7.4) | WSL / Linux CI |
| `audio-trigger` | no `flock` on `PATH`: whole row SKIP (exit 77), never a pass (1.0.7.4). With Cygwin's `flock` on `PATH` (this box, 2026-10-23) the row RUNS and FAILs — every helper call answers `busy` (the same MSYS-fd-under-Cygwin-`flock` suspect as `install-profile` P6l below), so `comment-mutation-proof` reports its case «not green on an unmutated tree» | WSL / Linux CI |
| `sudoers-pin-contract` | two cases print SKIP and the row still exits 0: sandbox (c), the iface-conf-write symlink refusal (git-bash cannot create a real symlink, `ln -s` copies), and 8c/8d, the 0440 root:root normalisation and the runner's read-back (MSYS ignores `chmod`: 0440 reads back 644). Because 8c/8d cannot run, `comment-mutation-proof`'s cases on the `if chmod 0440 …` lines (`scripts/lib.sh`, `etc/sa02m-update-runner.sh`) cannot go RED here and FAIL on this box. On Linux 8c/8d run as any user: as non-root (CI) the chown to root is modelled, never skipped; GREEN under WSL as root and as `nobody` (2026-10-23) | WSL / Linux CI |
| `install-maintenance`, `update-control-boundaries` | whole row SKIP (exit 77): Windows CPython has no `fcntl` (the wrapper's and the controller's flock idiom). Neither needs root since 1.0.7.10 — each suite rebinds its owner constant to the running uid (`install-maintenance`: `JOURNAL_OWNER_UID`, plus an `install -o/-g` stub in the 03a fixture; `update-control-boundaries`: the state owner). `install-maintenance`: 18 OK under WSL as root and as `nobody`; `update-control-boundaries`: 15 OK as root and as `nobody` (both 2026-10-23) | WSL / Linux CI |
| `py-unit-update` | PARTIAL: `tests/test_control_cgi.py` (HTTP → real Bash CGI → controller) skips as a module without `fcntl` and writes one QUALITY_SKIP_LOG line — 25 passed + 1 skipped here, 36 passed under WSL (2026-10-21); before 1.0.7.5 the whole row was RED on Windows (collection error, `final-build-1.log:3655`) | WSL / Linux CI |
| `board-verifier-safety` | whole row SKIP (exit 77) off Linux — the script guards itself (`sys.platform`, `/proc`; it needs `/proc`, `os.pidfd_open`, `select` on pipes) and writes the skip-log line; the row's `run` is the plain python call | WSL / Linux CI |
| `status-reliability-browser` | whole row SKIP (exit 77) when `playwright` is not resolvable from `scripts/dev` — the guard is in the row's `run` (the script imports playwright unconditionally); with `npm run ui-layout:install` done it RUNS here and under WSL (PASS on both, 2026-10-20/21) | Linux CI (installs the harness) |

When reporting results, say "skipped", never "passed". A reviewer that reports a
skipped row as a pass is making a false claim about verification.

## Rows that FAIL (not skip) on a Windows dev box — environment, CI is the authority

Observed 2026-10-07 on git-bash (MINGW64), each row run by hand. They do not
exit 77, so `run.mjs` and `comment-mutation-proof` count them as RED, and
every `comment-mutation-proof` case of theirs reports «not green on an
unmutated tree». Not evidence of a defect, not evidence of health either:

| Row | Exact failure here | Cause |
|---|---|---|
| `homekit-trigger`, `homeconnect-trigger` | `FAIL — flock not found (util-linux)` (first line, exit 1) | git-bash ships no `flock` |
| `homekit-cgi`, `homeconnect-cgi` | `FAIL 5a GET → the dispatch answer, verbatim …` — the CGI answers `{"ok":false,"error":"homekit_api_failed","message":"python dispatch failed or timed out"}` (and the `homeconnect_api_failed` twin) | NOT `flock`: the CGI's python dispatch fails on this box; cause not traced (Windows python under the stubbed CGI is the suspect) |
| `py-unit-homekit`, `py-unit-homeconnect` | `ModuleNotFoundError: grp` / `fcntl`, `OSError: [WinError 1314]` (symlink privilege) in 13-21 tests; the rows print FAIL, not SKIP, because pytest itself is importable | Windows CPython has no `grp`/`fcntl` and the account lacks the symlink privilege; rc=0 under WSL Ubuntu-24.04 on the same tree (2026-10-14) |
| `alice-image-identity`, `service-ctl-policy-write` | `ln: failed to create symbolic link` cases (CB2 belt, Home Connect dirty-link dimensions) | git-bash symlinks need the Windows privilege; rc=0 under WSL |
| `update-conditional-restart` | run8: `python.exe: can't open file 'C:\tmp\tmp.*\gen-manifest.py'` | MSYS `/tmp` is not Windows python's `C:\tmp`; rc=0 under WSL |
| `port-coordinator`, `status-coordinator-projection` (1.0.7.5) | `port-coordinator`: AF_UNIX / `/proc` / SO_PEERCRED cases error out; `status-coordinator-projection`: `UnicodeDecodeError: 'charmap'` — the harness reads `status.cgi` with the locale codec (cp1251) and the file is UTF-8 | Windows CPython has no AF_UNIX and defaults text I/O to cp1251; both PASS under WSL Ubuntu-24.04 (`final-build-2-wsl.log`). The scripts belong to the ports/status areas; a `read_text(encoding='utf-8')` would make the second one run here. On Linux `port-coordinator` (and `py-unit-gateway`'s real-broker test, which imports its fixture) runs as any user: unprivileged, the occupant scan is scoped to the suite's own process tree since 1.0.7.10 — a root process elsewhere in the session (the CI runner's launcher, `runuser`/`sudo` without `setsid`) is not read; the session scope it replaced refused every lease on CI with `PermissionError` on `/proc/<pid>/fd` |
| `py-unit-gateway` | `AttributeError: module 'os' has no attribute 'openpty'` (30 tests, `tests/test_lease_retry.py` + `tests/test_transport_lifecycle.py`) and one `ModuleNotFoundError: No module named 'termios'`; the row prints FAIL, not SKIP, because pytest, serial and yaml are importable | Windows CPython has no pty (`os.openpty`, `termios`); 140 passed under WSL Ubuntu-24.04 on the same tree (2026-10-21, 1.0.7.7) |
| `js-unit-mqtt-scan-type`, `js-unit-mqtt-tcp-dialog` | NOT Windows: they FAIL under WSL Ubuntu-24.04's native Node 18 (`SyntaxError: Unexpected token 'export'` from `scripts/dev/mqtt-js-sandbox.mjs` importing a module-only `.js`) and PASS on Windows Node 24 (`final-build-1.log`, the 1.0.7.5 touched run 2026-10-21) | Node < 22 has no ESM syntax detection for a `.js` without `"type":"module"`; CI's Node version is the authority — a WSL run of these two rows needs Node >= 22 on PATH |
| `install-profile` | P6l only (the real-flock wait): with Cygwin's util-linux `flock` on `PATH` (`/c/cygwin64/bin/flock` on this box) the case RUNS instead of skipping and FAILs — «lock content '4242', grant removed=no, rc=1»: the removal never got the lock the holder released; cause not traced (an MSYS shell's fd under a Cygwin `flock` is the suspect). Without any `flock` on `PATH` it prints SKIP. 62/62 OK under WSL (2026-10-23) | WSL / Linux CI |
| transient (host load) | `py-unit-alice` broker-restart timing, `telemetry-device-id-contract`, `no-absolute-api-paths`, `iface-dns-ensure` wait budgets, `install-atomic` 9c - seen RED once each with 4-6 parallel agent runs on the box, PASS alone | not environment, not a defect: contention; re-run alone before filing |

## `nginx -t` runs only on the board

No dev box here has nginx (git-bash none; WSL Ubuntu-24.04 none, its docker
daemon not running), and CI does not run it. The site file
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

### Node under WSL — the gate silently ran on Windows until 2026-10-21

WSL Ubuntu-24.04 on this box had NO native Node: `/usr/local/bin/node` is a
symlink to `/mnt/c/Program Files/nodejs/node.exe`, so `wsl -- node
.ai-dev/quality/run.mjs build` spawned Windows children (Windows Python,
`C:	mp` paths) and every Linux-only row FAILed exactly as on git-bash. Since
2026-10-21 the distro package is installed (`apt-get install nodejs`, v18.19);
run the beat with `/usr/bin` first on PATH so the symlink does not win:
`PATH=/usr/bin:/bin:/usr/sbin:/sbin node .ai-dev/quality/run.mjs build`, and
redirect the log INSIDE WSL (the wsl.exe stdout relay dropped a multi-MB run
mid-way). Node 18 cannot import a module-only `.js` through
`scripts/dev/mqtt-js-sandbox.mjs`, so `js-unit-mqtt-scan-type` and
`js-unit-mqtt-tcp-dialog` FAIL there and are authoritative on Windows Node 24;
`node_modules` (a Windows junction) is invisible under WSL, so Playwright rows
run from Windows. A Linux run is `final-build-3-wsl.log` /
`final-review-3-wsl.log` in `.ai-dev/audit/remediation-20261020/`.

### Getting the real row to run on Windows

WSL reproduces CI exactly:

```bash
wsl -d Ubuntu-24.04 -- sudo apt-get install -y shellcheck   # 0.9.0
```

Then put a shim named `shellcheck` on `PATH` that forwards to WSL — WSL
inherits the Windows working directory, so relative paths pass straight
through — and `bash .ai-dev/quality/checks/shellcheck.sh` runs for real instead
of skipping. Keep the shim in the session scratchpad; it is a dev-box
convenience, not a repo artifact.

**Never shim the newer `npx` binary onto `PATH` to make the row report PASS.**
That converts an honest skip into a false green on the one check CI is the
authority for.

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
verdict. `update-conditional-restart` was **RED on this Windows box and GREEN on
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
under WSL first: a Linux GREEN on the same bytes points at this class.

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
  line and requires the gate to fail. **~40 s on Windows** — the largest single row we
  have added; budget it when judging CI time (`.ai-dev/notes/ci-budget.md`).
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
