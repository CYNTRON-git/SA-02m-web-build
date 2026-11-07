#!/usr/bin/env python3
"""test-factory-reset-runner.py — the config-only factory reset
(etc/sa02m-factory-reset-runner.sh) takes the backup it promises, keeps
secrets out of its log, never follows a name somebody else planted, and erases
the HomeKit bridge's pairings and the Home Connect sign-in (Operator decision
Q-F), the previous owner's scenario run history and template state, and the
audio upload token and destination (Operator decision 2026-11-04). Quality row
`factory-reset-runner`.

Why it exists — the runner is root and works in directories others can write:
  * /etc/sa02m-alice is root:www-data 0771 (any panel session plants any name
    there through cmd_exec.cgi). The runner staged its templates at the
    predictable `<dest>.tmp.$$`, then `chown`-ed that NAME (chown follows a
    symlink: swap the temp for a link to /etc/shadow and it became
    root:www-data), and forced `client_enabled` with `sed -i`, which reads
    through a planted symlink;
  * the "mandatory backup" called sa02m-web-backup.sh with `--output`, an
    argument that script never parsed: no backup file was ever written, and
    the tar.gz stream — .htpasswd, sa02m_web.env, the cloud agent.conf, the
    Alice confs — was appended to factory-reset.log (0644). A failed helper
    fell back to an archive the restore cannot read, or to an EMPTY one;
  * $STATEDIR is 0775 root:www-data (etc/tmpfiles.d/sa02m-update.conf), so
    the log, the lock (`exec 9>` truncates through a symlink), the
    transaction and the id in it (a path component of the root self-copy that
    is exec'd next) were all www-data-plantable; the transaction was written
    0600, so the status CGI (www-data) could not read the progress at all;
  * factory reset kept the HomeKit pairing store — Q-F says ERASE
    (docs/contracts/homekit-bridge.md §14, image-identity-reset.md §1/§7).

Method — the SHIPPED runner runs end to end, never a grep: every absolute
/etc/, /var/lib/, /var/www/, /run/ and /opt/sa02m-homekit path is retargeted
into a sandbox, systemctl/busctl/nginx are PATH shims, the backup helper is
the SHIPPED etc/sa02m-web-backup.sh retargeted the same way (or a failing
stub), and every attack is a planted name with a victim whose bytes, mode and
owner are compared before and after. The embedded fr_safe helper is also
exec'd directly for the one window a whole run cannot hit on demand (U1: the
temp name swapped between mkstemp and rename).

  B1 clean reset: the backup file exists, 0600 in a 0700 dir, is the whole
     restorable archive (manifest first, every entry present); the log holds
     no gzip bytes and is 0600; transaction.json is 0644 (the CGI reads it);
     the state dir, locked to 0755 for the run, is given its 0775 back
  B2/B3 a failing / garbage-emitting helper aborts the reset (E_BACKUP),
     nothing reset, no partial archive left
  B4 an old log holding a backup stream is truncated, made 0600, and the log
     says so
  D0 the defaults layout a full install leaves (1.0.5.66/, current/, lists/,
     templates/ and the artifact on the top level, no folder named after the
     running version — bench 1.135): the reset completes from 1.0.5.66/, never
     from a non-version name `sort -V` puts last (templates/); the running
     version is read from the seeded VERSION, so no folder may match it
  D1 no defaults folder holds templates/: E_INTERNAL «factory defaults not
     installed» is recorded (load_lists used to die under `set -e` with the
     transaction left at «validating»)
  S1/S2 a symlinked log / lock: the victim is untouched
  S3 a transaction id with `../` is refused; nothing is created outside
  S4 a symlinked transaction.json: the victim's JSON never lands in the
     transaction the CGI serves
  S5 a foreign entry in the state dir (a file planted as `rollback`): refused
     before any work, and the dir's 0775 is given back all the same
  A1 a symlinked Alice conf: the victim is untouched and the reset refuses
     (and rolls back) instead of writing through it
  A2 with no client template, a symlinked client conf is refused, never
     read through (the old `sed -i` copied the victim into the group-writable dir)
  A3 the temp name swapped for a symlink right after it is staged (an
     `install` shim that wins the race every time): the victim's owner stays
     put. Observing an owner change needs root; non-root uses real sudo when
     available, otherwise records the omitted assertion as a partial run
  U1 fr_safe replace_with with the temp swapped after mkstemp: refused, the
     victim untouched, the planted link not left at the destination
  H1 HomeKit: conf → the installer's seed bytes 0640 FIRST, the unit stopped
     while the store still existed, then the store's every entry erased
     (dir kept), setup.json removed
  H2 a unit that will not stop: E_APPLY, the store intact, configs and the
     HomeKit conf rolled back
  H3 a symlink and a directory holding a symlink inside the store: removed,
     their targets untouched
  H4 a symlinked HomeKit conf: refused before anything is erased
  H5 bridge not installed: the reset succeeds and never touches systemctl
     for it
  H6 a reset that fails its own verify keeps the pairings (the erase runs
     only after the reversible part verified)
  H7 no trusted package template: `enabled = false` forced, the rest kept
  H7b the same with the key spelled `Enabled:` (configparser reads it): forced
  H7c/H7d the same with a continuation line under `enabled` (dropped with the
     forced key) and with `enabled<NO-BREAK SPACE>=` (configparser takes Unicode
     whitespace around a key): the daemon's own config.load reads disabled
  C1 Home Connect: conf → the package's render() of the defaults 0640 FIRST
     (the unit was stopped with the conf already disabled, so the daemon
     removes its retained topics), stopped while the tokens still existed,
     then the state dir's every entry erased (dir kept), link.json removed
  C2 a client that will not stop: E_APPLY, the tokens intact, the Home
     Connect conf rolled back
  C3 a symlink inside the state dir: removed, its target untouched
  C4 a symlinked Home Connect conf: refused, the victim and the tokens intact
  C5 client not installed: the reset succeeds and never touches its unit
  C6 a reset failing its own verify keeps the sign-in (checked in H6's run)
  C7 no trusted package template: `enabled = false` forced, the Client ID
     kept, the state dir still erased
  C7c/C7d the H7c/H7d spellings for the Home Connect conf
  O1 a Home Connect client that will not stop: E_APPLY and NOTHING erased —
     the HomeKit pairing store and setup code intact, both confs rolled back
     (every unit whose state goes is verified down before any erase; the
     runner used to erase the pairings first and only then find the client
     still running)
  O2 a HomeKit bridge that will not stop never stops the Home Connect client
     and keeps its sign-in (HomeKit is quiesced first)
  O3 the rollback of O1 starts the HomeKit bridge again — it was active
     before the reset — and only after its conf is back
  O4 the same rollback with the bridge INACTIVE before the reset leaves it
     stopped (only units the reset found active are started again)
  R  the scenario engine (Operator decision 2026-11-04: run history and
     template state go, scenarios stay — docs/contracts/cloud-scenarios.md
     «Сброс к заводским настройкам»):
  R1 a clean reset: runs.json == store.empty_journal() (0644), logic_state.json
     and http-allow.json removed, scenarios.json byte-identical; the engine
     stopped while the previous owner's state still existed and started again
     after the erase; no run/notify/template byte in the log, the rollback
     journal or the pre-reset backup
  R2 an engine inactive before the reset: erased, never started
  R3 an engine that will not stop: E_APPLY, NOTHING erased (rules, HomeKit,
     Home Connect, audio), configs rolled back, HomeKit and Home Connect
     started again
  R4 engine not installed: done, its unit untouched (shares M5's run)
  R5 a pre-1.0.6.41 document still carrying runs/notify, no runs.json: the
     REAL store.load() shows none of them after the reset
  R6 the runner's paths and empty journal equal sa02m_rules.store /
     http_guard / sa02m_audio.constants (the runner cannot import them)
  R7 the engine will not start after the erase: done, a WARN line
  R8 the engine started by hand inside the window and wrote template state
     back: stopped again, erased again, done
  R9 a symlinked runs.json: the link replaced by the empty journal, the
     target untouched
  M  the audio recorder's upload credential (docs/contracts/audio-recorder.md
     §19): M1 token removed, the REAL config.load() gives upload_url "" and
     enabled still True, every other conf line, mode and owner kept, stopped
     while the token existed and started after, recordings / events.jsonl /
     voice-allow.json kept, no token or URL byte in the log, the rollback
     journal or the backup (shares R1's run); M2 a symlinked token: the link
     goes, the target stays; M3 a directory planted as the token: E_APPLY in
     phase 1, nothing erased; M4 a symlinked conf: E_APPLY, victim untouched,
     nothing erased; M5 not installed: done, its unit untouched; M6 a recorder
     that will not stop: E_APPLY, nothing erased, the stopped units started
     again; M7 `Upload_URL:` spelling forced to empty; M8/M8b a continuation
     line under upload_url (directly, or after a blank line and a comment):
     the REAL config.load() reads upload_url "" after the reset (it read the
     continuation as the URL — review round 1, F1); M9 `upload_url<NO-BREAK
     SPACE>=` forced too; M8c/M8d the forced line keeps the key line's
     indentation, so a less-indented next line stays its own option and an
     indented-throughout conf keeps every other option's value (review round
     2, F2); M10 the runner's belt audio_url_cleared agrees with config.load
     on every spelling, never follows a symlink, counts an unreadable conf as
     set; M11 a URL saved right after the rewrite (a shim on the audio
     upload_url force-key call only), before the re-read: E_APPLY, the token
     kept (the belt's call — commented out, the run completes with the
     racer's URL and no token)
  T  the runner's trusted-path resolver is byte-identical to the block in
     etc/sa02m-web-backup.sh (a third twin; no root script can import another)
  P  the embedded helper compiles

RED observed 2026-09-27 on the runner at HEAD 485f385 (FACTORY_SRC=<copy>):
recorded in the commit that adds this harness. C1-C7 RED observed 2026-09-28
on the runner at HEAD d7d9c4a (no Home Connect step): see the commit body.
O1 RED observed 2026-09-28 on the runner at HEAD f81855e (pairings erased
before the Home Connect stop was verified); O3 RED 2026-09-28 on the two-phase
runner without the rollback restart: see the commit body. D0 against the
1.0.7.29 runner (10b7597a): the harness alone is committed first, its CI RED
(the bundle resolved to templates/, E_INTERNAL) recorded in the body of the
resolver fix that follows it. R1-R8 / M1-M7 likewise: the cases are committed
alone against the runner without the erase, their CI RED recorded in the body
of the runner commit. H7c/H7d, C7c/C7d, M8/M8b/M9 and M10 likewise, ahead
of the force-key fix (review round 1, F1).

Run: python3 scripts/dev/test-factory-reset-runner.py   (python3 + bash;
root or not — CI runs non-root; FACTORY_SRC=<file> runs another runner copy)
"""
from __future__ import annotations

import gzip
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from posixpath import basename as posixbase, dirname as posixdir, join as posixjoin

ROOT = Path(__file__).resolve().parents[2]
FACTORY = Path(os.environ.get("FACTORY_SRC") or ROOT / "etc/sa02m-factory-reset-runner.sh")
BACKUP = ROOT / "etc/sa02m-web-backup.sh"
DEFAULTS = ROOT / "etc/sa02m-factory-defaults"
HK_PKG = ROOT / "opt/sa02m-homekit/sa02m_homekit"
HK_SEED = ROOT / "etc/sa02m-homekit/sa02m-homekit.conf"
HC_PKG = ROOT / "opt/sa02m-homeconnect/sa02m_homeconnect"
RULES_PKG = ROOT / "opt/sa02m-rules/sa02m_rules"
AUDIO_PKG = ROOT / "opt/sa02m-audio/sa02m_audio"
RULES_UNIT_FILE = ROOT / "etc/systemd/system/sa02m-rules.service"
AUDIO_UNIT_FILE = ROOT / "etc/systemd/system/sa02m-audio.service"
if os.name == "nt":
    print("SKIP  factory-reset-runner needs a POSIX host (grp, euid, the retargeted runner)")
    raise SystemExit(77)

IS_ROOT = getattr(os, "geteuid", lambda: 1)() == 0

fails = 0
skips = 0


def ok(msg: str) -> None:
    print("ok    " + msg)


def bad(msg: str) -> None:
    global fails
    fails += 1
    print("FAIL  " + msg)


def skip(msg: str) -> None:
    global skips
    skips += 1
    print("SKIP  " + msg)
    ledger = os.environ.get("QUALITY_SKIP_LOG")
    if ledger:
        try:
            with open(ledger, "a", encoding="utf-8") as output:
                output.write(msg + "\n")
        except OSError as error:
            bad(f"cannot record omitted assertion in QUALITY_SKIP_LOG: {error}")


if not IS_ROOT:
    if os.environ.get("SA02M_FACTORY_ROOT_CHILD") == "1":
        bad("sudo re-execution did not obtain root; ownership assertions cannot run")
        sys.exit(1)
    sudo = shutil.which("sudo")
    try:
        probe = subprocess.run([sudo, "-n", "id", "-u"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=10) if sudo else None
    except (OSError, subprocess.SubprocessError):
        probe = None
    if probe is not None and probe.returncode == 0 and probe.stdout.strip() == "0":
        timeout = shutil.which("timeout")
        if not timeout:
            bad("privileged fixture requires timeout for bounded sudo re-execution")
            sys.exit(1)
        inherited = [f"{key}={os.environ[key]}" for key in
                     ("PATH", "TMPDIR", "FACTORY_SRC", "QUALITY_SKIP_LOG", "PYTHONUTF8")
                     if key in os.environ]
        command = [sudo, "-n", "--", "/usr/bin/env", *inherited,
                   "SA02M_FACTORY_ROOT_CHILD=1", timeout, "--kill-after=5s", "600s",
                   sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
        print("factory-reset-runner: re-executing the complete fixture as root via sudo", flush=True)
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, timeout=620)
        except (OSError, subprocess.SubprocessError) as error:
            bad(f"privileged fixture execution failed: {error}")
            sys.exit(1)
        sys.exit(result.returncode)  # exit-status: bounded sudo/timeout child process status, not an assertion count


def check(cond: bool, good: str, fail: str) -> None:
    (ok if cond else bad)(good if cond else fail)


for p in (FACTORY, BACKUP, DEFAULTS, HK_PKG, HK_SEED, HC_PKG, RULES_PKG, AUDIO_PKG, RULES_UNIT_FILE,
          AUDIO_UNIT_FILE):
    if not p.exists():
        bad(f"missing input {p} (non-vacuity)")
if fails:
    sys.exit(1)


def module_py(pkg: Path, env_prefix: str, code: str, *args: str) -> subprocess.CompletedProcess:
    """`code` run against the repo's own package (its env overrides stripped,
    so the production defaults are what it sees)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(env_prefix)}
    env["PYTHONPATH"] = str(pkg.parent)
    return subprocess.run([sys.executable, "-c", code, *args], env=env, capture_output=True, text=True, timeout=30)


# The store's own names and empty journal, and the recorder's own paths: the
# one homes the runner's constants must equal (R6), read from the packages.
_r_rules = module_py(RULES_PKG, "SA02M_RULES_", (
    "import json, os, sys; from sa02m_rules import store, http_guard;"
    "sys.stdout.write(json.dumps({'dir': os.path.dirname(store.DEFAULT_PATH), 'doc': os.path.basename(store.DEFAULT_PATH),"
    " 'journal': store.JOURNAL_NAME, 'logic': store.LOGIC_STATE_NAME, 'empty': store.empty_journal(),"
    " 'allow': http_guard.DEFAULT_ALLOW_PATH}))"))
_r_audio = module_py(AUDIO_PKG, "SA02M_AUDIO_", (
    "import json, sys; from sa02m_audio import constants as C, config;"
    "sys.stdout.write(json.dumps({'etc': C.ETC_DIR, 'conf': C.CONF_FILE, 'token': C.TOKEN_FILE,"
    " 'seed': config.render(config.RecorderConfig(enabled=True, upload_url='https://donor.example/upload'))}))"))
try:
    RULES = json.loads(_r_rules.stdout)
    AUDIO = json.loads(_r_audio.stdout)
except ValueError:
    bad(f"the rules / audio packages cannot be read (rc={_r_rules.returncode}/{_r_audio.returncode}): "
        f"{_r_rules.stderr[-200:]!r} {_r_audio.stderr[-200:]!r}")
    sys.exit(1)
RULES_EMPTY = RULES["empty"]
# The previous owner's state the reset must erase — each carries a marker the
# log / rollback journal / backup scans look for.
DONOR_JOURNAL = {"runs": [{"ts": 1.0, "id": "s1", "name": "s1", "error": "DONOR-RUN", "ok": False,
                           "source": "scenario"}],
                 "notify_queue": [{"ts": 1.0, "text": "DONOR-NOTE"}],
                 "last": {"s1": {"last_run": 1.0, "last_error": "DONOR-RUN"}}}
DONOR_LOGIC = {"version": 1, "machine": "", "scenarios": {"s1": {"template": "thermostat",
                                                                "state": {"note": "DONOR-LOGIC"}}}}
RULES_SCENARIO = {"id": "s1", "name": "s1", "enabled": True, "type": "block",
                  "trigger": [{"kind": "state", "device": "lamp", "cap": "on_off", "op": "changed"}],
                  "condition": {}, "action": [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}]}
DONOR_TOKEN = "DONOR-TOKEN-" + "x7" * 16
RULES_NEEDLES = (b"DONOR-RUN", b"DONOR-NOTE", b"DONOR-LOGIC")
AUDIO_NEEDLES = (DONOR_TOKEN.encode(), b"donor.example")

# The Home Connect template: the package's own render() of the defaults (the
# 06d seed and the factory reset both come from this one home).
_r_hc = subprocess.run([sys.executable, "-c",
                        "import sys; from sa02m_homeconnect import config;"
                        "sys.stdout.write(config.render(config.ClientConfig()))"],
                       env={**{k: v for k, v in os.environ.items() if not k.startswith("SA02M_HOMECONNECT_")},
                            "PYTHONPATH": str(HC_PKG.parent)}, capture_output=True, text=True, timeout=30)
HC_TEMPLATE = _r_hc.stdout.encode("utf-8")
if _r_hc.returncode != 0 or b"enabled = false" not in HC_TEMPLATE:
    bad(f"the Home Connect package cannot render its template (rc={_r_hc.returncode}): {_r_hc.stderr[-200:]!r}")
    sys.exit(1)

RUNNER_TXT = FACTORY.read_text(encoding="utf-8")
BACKUP_TXT = BACKUP.read_text(encoding="utf-8")


def retarget(text: str, sb: Path) -> str:
    for a in ("/etc/", "/var/lib/", "/var/www/", "/run/"):
        text = text.replace(a, f"{sb}{a}")
    text = text.replace("/opt/sa02m-homekit", f"{sb}/opt/sa02m-homekit")
    text = text.replace("/opt/sa02m-homeconnect", f"{sb}/opt/sa02m-homeconnect")
    text = text.replace("/usr/share/sa02m-factory-defaults", f"{sb}/usr/share/sa02m-factory-defaults")
    return text


def snap(p: Path):
    st = os.lstat(p)
    return (p.read_bytes() if stat.S_ISREG(st.st_mode) else None, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)


def victim(sb: Path, name: str = "victim", body: bytes = b"VICTIM-SECRET\n") -> Path:
    v = sb / "outside" / name
    v.parent.mkdir(parents=True, exist_ok=True)
    v.write_bytes(body)
    os.chmod(v, 0o600)
    return v


class Sandbox:
    def __init__(self, tag: str):
        self.sb = Path(tempfile.mkdtemp(prefix=f"frr-{tag}-"))
        sb = self.sb
        self.etc = sb / "etc"
        self.state = sb / "var/lib/sa02m-update"
        self.alice = self.etc / "sa02m-alice"
        self.hk_var = sb / "var/lib/sa02m-homekit"
        self.hk_run = sb / "run/sa02m-homekit"
        self.hk_conf = self.etc / "sa02m-homekit/sa02m-homekit.conf"
        self.hc_var = sb / "var/lib/sa02m-homeconnect"
        self.hc_run = sb / "run/sa02m-homeconnect"
        self.hc_conf = self.etc / "sa02m-homeconnect/sa02m-homeconnect.conf"
        for d in ("etc/nginx", "etc/network/interfaces.d", "etc/sa02m-cloud", "var/lib/sa02m-alice",
                  "var/www/network_config", "run/sa02m-homekit", "run/sa02m-homeconnect", "bin", "outside"):
            (sb / d).mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True)
        os.chmod(self.state, 0o775)  # etc/tmpfiles.d/sa02m-update.conf
        for d, m in ((self.alice, 0o770), (self.etc / "sa02m-homekit", 0o770), (self.hk_var, 0o700),
                     (self.etc / "sa02m-homeconnect", 0o770), (self.hc_var, 0o700)):
            d.mkdir(parents=True)
            os.chmod(d, m)
        w = self.write
        w("etc/machine-id", "0123456789abcdef0123456789abcdef\n", 0o444)
        w("etc/nginx/.htpasswd", "olduser:SECRET-HT\n", 0o600)
        w("etc/sa02m_web.env", "SA02M_WEB_USER='olduser'\nSA02M_WEB_PASS='SECRET-ENV'\n", 0o640)
        w("etc/sa02m-cloud/agent.conf", "token=SECRET-CLOUD\n", 0o600)
        w("etc/sa02m-alice/sa02m-alice-client.conf", "[client]\nclient_enabled = true\n", 0o660)
        w("etc/sa02m-alice/sa02m-alice-devices.conf", '{"rooms": [], "devices": [1]}\n', 0o660)
        w("var/www/network_config/VERSION", "1.0.6.57\n", 0o644)
        for n in ("state.json", "aids.json", "identity.json", ".hk-abc.tmp"):
            w(f"var/lib/sa02m-homekit/{n}", '{"k": "HK-SECRET"}\n', 0o600)
        w("etc/sa02m-homekit/sa02m-homekit.conf", "[bridge]\nenabled = true\ninterface = eth1\nport = 21065\n", 0o660)
        w("run/sa02m-homekit/setup.json", '{"code": "111-22-333"}\n', 0o640)
        for n in ("tokens.json", "budget.json", "appliances.json", ".hc-abc.tmp"):
            w(f"var/lib/sa02m-homeconnect/{n}", '{"refresh_token": "HC-SECRET"}\n', 0o600)
        w("etc/sa02m-homeconnect/sa02m-homeconnect.conf",
          "[account]\nenabled = true\nclient_id = INTEGRATOR_APP_1\nvendor_client_id = \nhost = simulator\n"
          "link_requested_at = 0\n\n[control]\nmode = off\n", 0o660)
        w("run/sa02m-homeconnect/link.json", '{"user_code": "DONR-1234"}\n', 0o640)
        # The scenario engine's store (0755 root:root on the board,
        # scripts/06b-rules.sh) and the recorder's conf dir (2750
        # www-data:sa02m-audio, scripts/15-audio.sh — www-data plants any name
        # there), as a board that has run scenarios and uploaded clips.
        self.rules = self.etc / "sa02m-rules"
        self.audio = self.etc / "sa02m-audio"
        self.au_conf = self.audio / "sa02m-audio.conf"
        self.au_token = self.audio / "upload.token"
        for d, m in ((self.rules, 0o755), (self.audio, 0o770)):
            d.mkdir(parents=True)
            os.chmod(d, m)
        w("etc/sa02m-rules/scenarios.json",
          json.dumps({"scenarios": [RULES_SCENARIO], "library": "", "vars": {}}, separators=(",", ":")), 0o644)
        w("etc/sa02m-rules/runs.json", json.dumps(DONOR_JOURNAL), 0o644)
        w("etc/sa02m-rules/logic_state.json", json.dumps(DONOR_LOGIC), 0o644)
        w("etc/sa02m-rules/http-allow.json", '{"hosts": ["donor-nas.local"]}\n', 0o644)
        w("etc/sa02m-audio/sa02m-audio.conf", AUDIO["seed"], 0o640)
        w("etc/sa02m-audio/upload.token", DONOR_TOKEN + "\n", 0o640)
        w("etc/sa02m-audio/voice-allow.json", '{"commands": []}\n', 0o640)
        w("var/lib/sa02m-audio/rec/a.wav", "RIFF-clip\n", 0o640)
        w("var/lib/sa02m-audio/events.jsonl", '{"event": "record"}\n', 0o640)
        # Factory defaults bundle, lists retargeted like the runner.
        cur = sb / "usr/share/sa02m-factory-defaults/current"
        shutil.copytree(DEFAULTS / "templates", cur / "templates")
        (cur / "lists").mkdir(parents=True)
        for lst in ("wipe.list", "preserve.list"):
            (cur / "lists" / lst).write_text(retarget((DEFAULTS / "lists" / lst).read_text(encoding="utf-8"), sb),
                                             encoding="utf-8")
        self.cur = cur
        # The HomeKit package, as installed (root-only dirs in the sandbox).
        shutil.copytree(HK_PKG, sb / "opt/sa02m-homekit/sa02m_homekit",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        # copytree keeps the SOURCE modes: a checkout extracted as root with a
        # group-writable umask (comment-mutation-proof's `git archive | tar -x`)
        # would hand the runner a package it rightly refuses to import, so the
        # installed modes are set here, not inherited.
        for dirpath, _dirs, _files in os.walk(sb / "opt/sa02m-homekit"):
            os.chmod(dirpath, 0o755)
        shutil.copytree(HC_PKG, sb / "opt/sa02m-homeconnect/sa02m_homeconnect",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        for dirpath, _dirs, _files in os.walk(sb / "opt/sa02m-homeconnect"):
            os.chmod(dirpath, 0o755)
        # Backup helper: the SHIPPED script, retargeted.
        self.backup = sb / "bin-backup.sh"
        self.backup.write_text(retarget(BACKUP_TXT, sb), encoding="utf-8")
        os.chmod(self.backup, 0o755)
        # Shims: systemctl (records calls; is-active answers from a state file),
        # busctl (no manager: the watchdog hold reports unreadable), nginx (absent).
        self.shim("systemctl", f"""#!/bin/bash
echo "$*" >> "{sb}/systemctl.log"
case "$1" in
  stop)
    if [ "${{2:-}}" = sa02m-homekit.service ]; then
      [ -e "{self.hk_var}/state.json" ] && echo "stop:store-present" >> "{sb}/systemctl.log"
      [ -e "{sb}/hk-stuck" ] || echo inactive > "{sb}/hk-state"
    fi
    if [ "${{2:-}}" = sa02m-homeconnect.service ]; then
      [ -e "{self.hc_var}/tokens.json" ] && echo "hc-stop:tokens-present" >> "{sb}/systemctl.log"
      grep -Eiq '^[[:space:]]*enabled[[:space:]]*[=:][[:space:]]*(1|true|yes|on)[[:space:]]*$' \
          "{self.hc_conf}" 2>/dev/null && echo "hc-stop:conf-enabled" >> "{sb}/systemctl.log"
      [ -e "{sb}/hc-stuck" ] || echo inactive > "{sb}/hc-state"
    fi
    # The engine and the recorder: records whether the previous owner's state
    # was still there when the unit was stopped (stop BEFORE erase).
    if [ "${{2:-}}" = sa02m-rules.service ]; then
      grep -q DONOR-RUN "{self.rules}/runs.json" 2>/dev/null && echo "rules-stop:runs-donor" >> "{sb}/systemctl.log"
      [ -e "{self.rules}/logic_state.json" ] && echo "rules-stop:logic-present" >> "{sb}/systemctl.log"
      [ -e "{sb}/rules-stuck" ] || echo inactive > "{sb}/rules-state"
    fi
    if [ "${{2:-}}" = sa02m-audio.service ]; then
      [ -e "{self.au_token}" ] && echo "audio-stop:token-present" >> "{sb}/systemctl.log"
      [ -e "{sb}/audio-stuck" ] || echo inactive > "{sb}/audio-state"
    fi
    exit 0 ;;
  start)
    # Records whether the unit's own conf (enabled = true, the sandbox seed) is
    # back at start time — a rollback must restore configs BEFORE it restarts.
    if [ "${{2:-}}" = sa02m-homekit.service ]; then
      grep -q '^enabled = true$' "{self.hk_conf}" 2>/dev/null && echo "start:hk-conf-restored" >> "{sb}/systemctl.log"
      echo active > "{sb}/hk-state"
    fi
    if [ "${{2:-}}" = sa02m-homeconnect.service ]; then
      echo active > "{sb}/hc-state"
    fi
    # ...and whether the erase was already done when it came back.
    if [ "${{2:-}}" = sa02m-rules.service ]; then
      [ -e "{sb}/rules-nostart" ] && exit 1
      if ! grep -q DONOR "{self.rules}/runs.json" 2>/dev/null && [ ! -e "{self.rules}/logic_state.json" ]; then
        echo "start:rules-runs-empty" >> "{sb}/systemctl.log"
      fi
      echo active > "{sb}/rules-state"
    fi
    if [ "${{2:-}}" = sa02m-audio.service ]; then
      if [ ! -e "{self.au_token}" ] && ! grep -q donor.example "{self.au_conf}" 2>/dev/null; then
        echo "start:audio-token-gone-url-empty" >> "{sb}/systemctl.log"
      fi
      echo active > "{sb}/audio-state"
    fi
    exit 0 ;;
  is-active)
    case "${{2:-}}" in
      sa02m-homekit.service) st=$(cat "{sb}/hk-state" 2>/dev/null || echo active) ;;
      sa02m-homeconnect.service) st=$(cat "{sb}/hc-state" 2>/dev/null || echo active) ;;
      sa02m-rules.service)
        # rules-answers: one answer per call, consumed in order (R8); an
        # `active-replant` answer is the engine started by hand mid-reset,
        # which writes the previous owner's template state back.
        q="{sb}/rules-answers"
        if [ -s "$q" ]; then
          st=$(head -n 1 "$q")
          tail -n +2 "$q" > "$q.next"
          mv "$q.next" "$q"
          if [ "$st" = active-replant ]; then
            st=active
            echo '{{"version": 1, "scenarios": {{"s1": {{"template": "thermostat", "state": {{"note": "DONOR-LOGIC"}}}}}}}}' \
                > "{self.rules}/logic_state.json"
            echo "rules-replanted" >> "{sb}/systemctl.log"
          fi
        else
          st=$(cat "{sb}/rules-state" 2>/dev/null || echo active)
        fi ;;
      sa02m-audio.service) st=$(cat "{sb}/audio-state" 2>/dev/null || echo active) ;;
      *) st=inactive ;;
    esac
    echo "$st"
    [ "$st" = active ] && exit 0 || exit 3 ;;
  *) exit 0 ;;
esac
""")
        self.shim("busctl", "#!/bin/sh\nexit 1\n")
        self.shim("nginx", "#!/bin/sh\nexit 1\n")
        self.txn_id = uuid.uuid4().hex
        self.write_txn(self.txn_id)
        self.runner = sb / "runner.sh"
        self.runner.write_text(retarget(RUNNER_TXT, sb), encoding="utf-8")
        os.chmod(self.runner, 0o755)

    def write(self, rel: str, body: str, mode: int) -> Path:
        p = self.sb / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        os.chmod(p, mode)
        return p

    def shim(self, name: str, body: str) -> None:
        p = self.sb / "bin" / name
        p.write_text(body, encoding="utf-8")
        os.chmod(p, 0o755)

    def write_txn(self, txn_id: str) -> None:
        (self.state / "transaction.json").write_text(json.dumps({
            "schema_version": 1, "id": txn_id, "operation": "factory_reset", "stage": "confirmed",
            "confirm_phrase_ok": True, "backup_ok": True, "result": "pending"}), encoding="utf-8")

    def run(self, backup: Path | None = None, wrapper: str | None = None):
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.sb}/bin:{env.get('PATH', '/usr/bin:/bin')}",
            "SA02M_UPDATE_STATEDIR": str(self.state),
            "SA02M_FACTORY_DEFAULTS_ROOT": str(self.sb / "usr/share/sa02m-factory-defaults"),
            "SA02M_WEB_BACKUP": str(backup or self.backup),
            "SA02M_WEB_VERSION_FILE": str(self.sb / "var/www/network_config/VERSION"),
            "SA02M_DEVICE_ID_FILE": str(self.etc / "machine-id"),
        })
        env.pop("SA02M_FACTORY_LOG", None)
        env.pop("SA02M_UPDATE_LOCK", None)
        argv = ["bash", "-c", wrapper, "wrap", str(self.runner)] if wrapper else ["bash", str(self.runner), "run"]
        r = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        self.out = r.stdout.decode("utf-8", "replace")
        return r.returncode

    @property
    def log(self) -> Path:
        return self.state / "factory-reset.log"

    def txn(self) -> dict:
        try:
            return json.loads((self.state / "transaction.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def systemctl_log(self) -> str:
        p = self.sb / "systemctl.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def htpasswd_reset(self) -> bool:
        return (self.etc / "nginx/.htpasswd").read_text(encoding="utf-8").startswith("admin:")

    def why(self) -> str:
        t = self.txn()
        tail = self.log.read_bytes()[-600:].decode("utf-8", "replace") if self.log.is_file() else ""
        return f"stage={t.get('stage')} code={t.get('error_code')} msg={t.get('error_message')!r} out={self.out[-300:]!r} log…{tail!r}"

    def done(self) -> bool:
        return self.txn().get("stage") == "done"

    def cleanup(self) -> None:
        shutil.rmtree(self.sb, ignore_errors=True)


def log_is_clean(sbx: Sandbox) -> tuple[bool, str]:
    if not sbx.log.is_file() or sbx.log.is_symlink():
        return False, "log missing or not a regular file"
    data = sbx.log.read_bytes()
    probs = []
    if b"\x1f\x8b\x08" in data:
        probs.append("gzip stream bytes")
    if b"\x00" in data:
        probs.append("NUL bytes")
    mode = stat.S_IMODE(os.lstat(sbx.log).st_mode)
    if mode & 0o077:
        probs.append(f"mode {mode:o}")
    return not probs, ", ".join(probs) or "clean"


def reach_late_steps(sbx: Sandbox) -> None:
    """Drop the interfaces.d templates (the runner skips a missing template) so
    a runner still carrying the pre-fix basename-glob bug (G) reaches the Alice
    step and the end of the run instead of stopping at eth0.conf — without it
    the A cases would pass on 485f385 for the wrong reason, and H1 would fail
    there for the wrong one. B2–B4 and H2–H7b use it for the same reason;
    B1 keeps the templates so one clean run still reinstalls them end to end
    (on 485f385 B1a/B1c–B1e still fail for their own reasons)."""
    for n in ("eth0.conf", "eth1.conf"):
        (sbx.cur / "templates/etc/network/interfaces.d" / n).unlink(missing_ok=True)


def timeout_cases() -> None:
    """Real reset composition, one TERM-resistant control command at a time."""
    real_timeout = shutil.which("timeout")
    if not real_timeout:
        bad("W GNU timeout is required")
        return
    cases = (
        ("systemctl", "stop net-watchdog sa02m-watchdog-feed", 20),
        ("systemctl", "stop sa02m-link-monitor", 10),
        ("systemctl", "start net-watchdog", 20),
        ("systemctl", "is-enabled --quiet sa02m-link-monitor", 10),
        ("systemctl", "start sa02m-link-monitor", 20),
        ("systemctl", "start sa02m-watchdog-feed", 20),
        ("systemctl", "reload sa02m-serial-gateway", 20),
        ("systemctl", "restart sa02m-modbus-mqtt", 20),
        ("nginx", "-t", 10),
        ("systemctl", "reload nginx", 20),
    )
    for index, (tool, arguments, budget) in enumerate(cases, 1):
        s = Sandbox("W%d" % index)
        process = None
        try:
            reach_late_steps(s)
            s.shim("nginx", "#!/bin/sh\nexit 0\n")
            shim = s.sb / "bin" / tool
            original = shim.read_text(encoding="utf-8")
            hanging = f'''if [ "$*" = '{arguments}' ]; then
  echo reached > '{s.sb}/reached'
  echo $$ >> '{s.sb}/hanging-pids'
  trap '' TERM
  sleep 300 &
  echo $! >> '{s.sb}/hanging-pids'
  wait
fi
'''
            shim.write_text(original.replace("\n", "\n" + hanging, 1), encoding="utf-8")
            s.shim("timeout", f'''#!/bin/bash
printf '%s\\n' "$*" >> '{s.sb}/timeout.log'
original=("$@")
grace=()
if [ "${{1:-}}" = -k ]; then
  [ "${{2:-}}" = 2s ] || exit 97
  grace=(-k 0.15s)
  shift 2
fi
budget=${{1:-}}; shift
case "${{1:-}}" in
  systemctl|busctl|nginx) case "$budget" in 10|10s|20|20s) ;; *) exit 98 ;; esac ;;
  *) exec '{real_timeout}' "${{original[@]}}" ;;
esac
exec '{real_timeout}' "${{grace[@]}}" 0.15s "$@"
''')
            env = dict(os.environ, PATH=f"{s.sb}/bin:{os.environ.get('PATH', '/usr/bin:/bin')}",
                       SA02M_UPDATE_STATEDIR=str(s.state), SA02M_FACTORY_DEFAULTS_ROOT=str(s.cur.parent),
                       SA02M_WEB_BACKUP=str(s.backup), SA02M_WEB_VERSION_FILE=str(s.sb / "var/www/network_config/VERSION"),
                       SA02M_DEVICE_ID_FILE=str(s.etc / "machine-id"))
            for name in ("SA02M_FACTORY_LOG", "SA02M_UPDATE_LOCK"):
                env.pop(name, None)
            process = subprocess.Popen(["bash", str(s.runner), "run"], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            def kill_owned_groups():
                groups = {process.pid}
                pid_file = s.sb / "hanging-pids"
                if pid_file.exists():
                    for pid in map(int, pid_file.read_text().split()):
                        try:
                            if os.getsid(pid) == process.pid:
                                groups.add(os.getpgid(pid))
                        except ProcessLookupError:
                            pass
                for group in groups:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            supervised = False
            try:
                output, _ = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                supervised = True
                kill_owned_groups()
                output, _ = process.communicate(timeout=3)
            s.out = output.decode("utf-8", "replace")
            calls = (s.sb / "timeout.log").read_text() if (s.sb / "timeout.log").exists() else ""
            bound = any(line in calls.splitlines() for line in (
                f"-k 2s {budget} {tool} {arguments}", f"-k 2s {budget}s {tool} {arguments}"))
            log_text = s.log.read_text() if s.log.exists() else ""
            running = []
            pid_file = s.sb / "hanging-pids"
            if pid_file.exists():
                for pid in pid_file.read_text().split():
                    stat_file = Path("/proc") / pid / "stat"
                    if stat_file.exists() and stat_file.read_text().split(") ", 1)[1].split()[0] != "Z":
                        running.append(pid)
            complete = process.returncode == 0 and s.done() and not (s.sb / "run/sa02m-imaging.lock").exists()
            warned = "WARNING:" in log_text and arguments in log_text
            check((s.sb / "reached").exists() and bound and not supervised and complete and warned and not running,
                  f"W{index} {tool} {arguments}: {budget}s + kill grace; warning, journal and cleanup reached; no child survives",
                  f"W{index} {tool} {arguments}: reached={(s.sb / 'reached').exists()} bounded={bound} "
                  f"supervisor={supervised} complete={complete} warning={warned} survivors={running} ({s.why()})")
        finally:
            if process is not None:
                kill_owned_groups()
            s.cleanup()


# ── G. the lists' basename glob (the SHIPPED path_matches_glob, run by bash) ─
print("── G. wipe/preserve list globs ──")
mg = re.search(r"^path_matches_glob\(\) \{\n.*?^\}\n", RUNNER_TXT, re.S | re.M)
if not mg:
    bad("G path_matches_glob() not found in the runner (non-vacuity)")
else:
    probe = mg.group(0) + r'''
for c in "/etc/network/interfaces.d/eth0.conf|/etc/network/interfaces.d/*.conf|1" \
         "/etc/network/interfaces.d/sub/x.conf|/etc/network/interfaces.d/*.conf|0" \
         "/etc/network/interfaces.d/eth0.cfg|/etc/network/interfaces.d/*.conf|0" \
         "/etc/ssh/ssh_host_rsa_key|/etc/ssh/ssh_host_*|1" \
         "/etc/sa02m-alice/x.conf|/etc/sa02m-alice/|1" \
         "/etc/nginx/.htpasswd|/etc/nginx/.htpasswd|1"; do
  IFS='|' read -r p g want <<<"$c"
  if path_matches_glob "$p" "$g"; then got=1; else got=0; fi
  [ "$got" = "$want" ] || echo "MISMATCH $p vs $g: got $got want $want"
done
'''
    r = subprocess.run(["bash", "-c", probe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    out = r.stdout.decode("utf-8", "replace").strip()
    check(r.returncode == 0 and not out,
          "G a basename glob in wipe.list matches its directory's files, never a subdirectory",
          f"G glob matching wrong: {out or r.returncode}")

# ── B1 clean reset: backup file + clean log + readable transaction ──────────
print("── B. the mandatory backup ──")
s = Sandbox("b1")
rc = s.run()
check(rc == 0 and s.done(), "B1 a clean reset completes", f"B1 clean reset did not complete (rc={rc}; {s.why()})")
bp = s.txn().get("backup_path")
bpath = Path(bp) if bp else None
if bpath and bpath.is_file() and not bpath.is_symlink():
    st = os.lstat(bpath)
    dst = os.lstat(bpath.parent)
    check(stat.S_IMODE(st.st_mode) == 0o600 and not stat.S_IMODE(dst.st_mode) & 0o077,
          f"B1a the backup is 0600 in a {stat.S_IMODE(dst.st_mode):o} dir",
          f"B1a backup mode {stat.S_IMODE(st.st_mode):o}, dir {stat.S_IMODE(dst.st_mode):o} — not root-only")
    try:
        with tarfile.open(bpath, "r:gz") as tar:
            mem = tar.getmembers()
            man = json.load(tar.extractfile(mem[0])) if mem and mem[0].name == "backup-manifest.json" else {}
            names = {m.name for m in mem}
            paths = {e.get("path") for e in man.get("paths", [])}
            complete = all(e.get("archive_path") in names for e in man.get("paths", []))
        want = {str(s.etc / "nginx/.htpasswd"), str(s.etc / "sa02m-cloud/agent.conf"),
                str(s.alice / "sa02m-alice-client.conf")}
        check(complete and want <= paths,
              "B1b it is the restorable archive (manifest first, htpasswd + agent.conf + Alice conf inside)",
              f"B1b archive incomplete: manifest paths={sorted(paths)[:6]} complete={complete}")
    except (tarfile.TarError, OSError, ValueError, IndexError) as e:
        bad(f"B1b backup is not a readable tar.gz: {e}")
else:
    bad(f"B1a no backup file at the path the transaction reports ({bp!r}) — the CGI shows a path that does not exist")
clean, why = log_is_clean(s)
check(clean, "B1c the log holds no backup stream and is root-only", f"B1c log: {why}")
tm = stat.S_IMODE(os.lstat(s.state / "transaction.json").st_mode)
check(tm == 0o644, "B1d transaction.json is 0644 (the status CGI reads it as www-data)",
      f"B1d transaction.json is {tm:o} — the status CGI (www-data) cannot read the progress")
sm = stat.S_IMODE(os.lstat(s.state).st_mode)
check(sm == 0o775, "B1e the state dir is given its 0775 back (the CGI queues the next job there)",
      f"B1e the state dir is left {sm:o} — the factory CGI cannot write the next transaction until reboot")
s.cleanup()

for tag, body, what in (("b2", "#!/bin/sh\nprintf '\\037\\213partial'\nexit 1\n", "a failing helper"),
                        ("b3", "#!/bin/sh\necho 'not an archive'\nexit 0\n", "a helper emitting garbage")):
    s = Sandbox(tag)
    reach_late_steps(s)
    stub = s.sb / "bin-stub.sh"
    stub.write_text(body, encoding="utf-8")
    os.chmod(stub, 0o755)
    rc = s.run(backup=stub)
    t = s.txn()
    left = sorted(p.name for p in (s.state / "backup-export").glob("*")) if (s.state / "backup-export").is_dir() else []
    ok_ = (rc != 0 and t.get("stage") == "error" and t.get("error_code") == "E_BACKUP" and not s.htpasswd_reset()
           and (s.hk_var / "state.json").exists() and not left)
    check(ok_, f"{tag.upper()} {what} aborts the reset (E_BACKUP), nothing reset, no partial archive",
          f"{tag.upper()} {what}: rc={rc} htpasswd_reset={s.htpasswd_reset()} left={left} ({s.why()})")
    s.cleanup()

# ── D0 the defaults layout a full install leaves ────────────────────────────
# scripts/03-webserver.sh copies etc/sa02m-factory-defaults/ twice: into
# current/ and onto the top level. The top level then holds 1.0.5.66/, current/,
# lists/, templates/ and the .sa02m-defaults artifact, with no folder named
# after the running web version (bench 1.135, 2026-11-04). `sort -V` puts
# `templates` after every version number, so the runner took templates/ as the
# bundle and every web reset on such a board stopped with E_INTERNAL.
print("── D0. the installer's defaults layout ──")
s = Sandbox("d0")
droot = s.sb / "usr/share/sa02m-factory-defaults"
shutil.rmtree(droot)
shutil.copytree(DEFAULTS, droot / "current")
shutil.copytree(DEFAULTS, droot, dirs_exist_ok=True)
for lst in droot.rglob("*.list"):
    lst.write_text(retarget(lst.read_text(encoding="utf-8"), s.sb), encoding="utf-8")
top = sorted(p.name for p in droot.iterdir() if p.is_dir())
running = (s.sb / "var/www/network_config/VERSION").read_text(encoding="utf-8").strip()
if top != ["1.0.5.66", "current", "lists", "templates"] or not running or running in top:
    bad(f"D0 the sandbox is not the installer's layout, or a folder matches the running version {running!r} "
        f"(non-vacuity — the case must reach the version-name filter): {top}")
else:
    rc = s.run()
    bundle = str(s.txn().get("defaults_bundle") or "")
    check(rc == 0 and s.done() and bundle.endswith("/1.0.5.66"),
          "D0 installer layout (1.0.5.66/, current/, lists/, templates/, no folder for the running version): "
          "the reset completes from the versioned bundle",
          f"D0 rc={rc} defaults_bundle={bundle!r} ({s.why()})")
s.cleanup()

# D1 — no candidate holds templates/ (only lists/ on the top level, no
# /etc/sa02m-factory-defaults): refused with the error recorded. Without the
# refusal in load_lists, `set -e` ended the run there and the transaction
# stayed at «validating» for the panel.
s = Sandbox("d1")
droot = s.sb / "usr/share/sa02m-factory-defaults"
shutil.rmtree(droot)
(droot / "lists").mkdir(parents=True)
for lst in ("wipe.list", "preserve.list"):
    (droot / "lists" / lst).write_text(retarget((DEFAULTS / "lists" / lst).read_text(encoding="utf-8"), s.sb),
                                      encoding="utf-8")
rc = s.run()
t = s.txn()
check(rc != 0 and t.get("stage") == "error" and t.get("error_code") == "E_INTERNAL"
      and t.get("error_message") == "factory defaults not installed" and not s.htpasswd_reset()
      and not (s.etc / "sa02m-factory-defaults").exists(),
      "D1 no defaults folder holds templates/: E_INTERNAL «factory defaults not installed» recorded, nothing reset",
      f"D1 rc={rc} ({s.why()})")
s.cleanup()

s = Sandbox("b4")
reach_late_steps(s)
s.log.write_bytes(b"2026-01-01 old line\n" + gzip.compress(b"SECRET-HT stream") + b"\n")
os.chmod(s.log, 0o644)
rc = s.run()
clean, why = log_is_clean(s)
said = s.log.is_file() and b"log truncated by the factory-reset runner" in s.log.read_bytes()
check(clean and said and rc == 0, "B4 an old log holding a backup stream is truncated, made 0600, and says so",
      f"B4 old contaminated log: {why}, truncation noted in the log={said} (rc={rc})")
s.cleanup()

# ── S. names in the state dir ───────────────────────────────────────────────
print("── S. the state dir (0775 root:www-data until the runner takes it) ──")
s = Sandbox("s1")
v = victim(s.sb)
before = snap(v)
os.symlink(v, s.log)
s.run()
check(snap(v) == before, "S1 a symlinked log: the victim is untouched",
      f"S1 the runner wrote through a symlinked log: victim now {snap(v)[0]!r}")
s.cleanup()

s = Sandbox("s2")
v = victim(s.sb)
before = snap(v)
os.symlink(v, s.state / "update.lock")
s.run()
check(snap(v) == before, "S2 a symlinked lock: the victim is untouched",
      f"S2 the runner opened the lock through a symlink: victim now {snap(v)[0]!r}")
s.cleanup()

s = Sandbox("s3")
s.write_txn("../../../escape")
rc = s.run()
esc = s.sb / "var/escape"
check(rc != 0 and not esc.exists(), "S3 a transaction id with ../ is refused, nothing created outside",
      f"S3 txn id traversal: rc={rc}, {esc} exists={esc.exists()}")
s.cleanup()

s = Sandbox("s4")
v = victim(s.sb, "claim.json", b'{"claim_token": "SECRET-CLAIM"}\n')
before = snap(v)
(s.state / "transaction.json").unlink()
os.symlink(v, s.state / "transaction.json")
s.run()
tj = s.state / "transaction.json"
leaked = tj.is_file() and not tj.is_symlink() and b"SECRET-CLAIM" in tj.read_bytes()
check(snap(v) == before and not leaked, "S4 a symlinked transaction: its target never lands in the served transaction",
      f"S4 victim changed={snap(v) != before}, target content copied into transaction.json={leaked}")
s.cleanup()

s = Sandbox("s5")
(s.state / "rollback").write_text("planted\n", encoding="utf-8")
rc = s.run()
t = s.txn()
sm = stat.S_IMODE(os.lstat(s.state).st_mode)
check(rc != 0 and t.get("error_code") == "E_INTERNAL" and not s.htpasswd_reset() and sm == 0o775,
      "S5 a foreign entry in the state dir: refused before any work, the dir's 0775 given back",
      f"S5 rc={rc} code={t.get('error_code')} htpasswd_reset={s.htpasswd_reset()} state dir {sm:o} "
      f"({s.why()})")
s.cleanup()

# ── A. the www-data-writable Alice conf dir ─────────────────────────────────
print("── A. /etc/sa02m-alice (root:www-data 0771) ──")
s = Sandbox("a1")
reach_late_steps(s)
v = victim(s.sb)
before = snap(v)
dst = s.alice / "sa02m-alice-client.conf"
dst.unlink()
os.symlink(v, dst)
rc = s.run()
check(snap(v) == before, "A1a a symlinked Alice conf: the victim is untouched", f"A1a victim changed: {snap(v)}")
t = s.txn()
check(rc != 0 and t.get("error_code") == "E_APPLY" and not s.htpasswd_reset(),
      "A1b the reset refuses to write through it and rolls the configs back",
      f"A1b rc={rc} htpasswd_reset={s.htpasswd_reset()} ({s.why()})")
s.cleanup()

s = Sandbox("a2")
reach_late_steps(s)
(s.cur / "templates/etc/sa02m-alice/sa02m-alice-client.conf").unlink()
v = victim(s.sb, body=b"client_enabled = true\nVICTIM-SECRET\n")
before = snap(v)
dst = s.alice / "sa02m-alice-client.conf"
dst.unlink()
os.symlink(v, dst)
rc = s.run()
copied = [p.name for p in s.alice.iterdir() if p.is_file() and not p.is_symlink() and b"VICTIM-SECRET" in p.read_bytes()]
check(snap(v) == before and not copied and rc != 0,
      "A2 no client template: the symlinked client conf is refused, never read through",
      f"A2 rc={rc}, victim changed={snap(v) != before}, victim bytes copied into the group-writable dir as {copied}")
s.cleanup()

def has_group(name: str) -> bool:
    try:
        import grp
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


if IS_ROOT and has_group("www-data"):
    s = Sandbox("a3")
    reach_late_steps(s)
    v = victim(s.sb)
    before = snap(v)
    real_install = shutil.which("install") or "/usr/bin/install"
    s.shim("install", f"""#!/bin/bash
"{real_install}" "$@" || exit $?
last="${{@: -1}}"
case "$last" in
  {s.alice}/*.tmp.*) rm -f -- "$last"; ln -s "{v}" "$last"; echo swapped >> "{s.sb}/a3-swapped" ;;
esac
""")
    s.run()
    fired = (s.sb / "a3-swapped").exists()
    check(snap(v) == before,
          "A3 temp name swapped for a symlink after staging: the victim's owner/mode stay put"
          + ("" if fired else " (the runner stages no name-addressed temp at all; the window is U1's)"),
          f"A3 the runner chowned through the swapped temp name: victim {before[1:]} -> {snap(v)[1:]}")
    s.cleanup()
else:
    skip("A3 temp-name swap race: an owner change is only observable as root with a www-data group"
         f" (this run: {'root' if IS_ROOT else 'not root'}, www-data group {'present' if has_group('www-data') else 'absent'})")

# ── U1 the swap window inside fr_safe ───────────────────────────────────────
m = re.search(r"^IFS= read -r -d '' FR_SAFE_PY <<'PY' \|\| true\n(.*?)^PY$", RUNNER_TXT, re.S | re.M)
if not m:
    bad("P/U1/T no embedded fr_safe helper (FR_SAFE_PY heredoc) in the runner")
else:
    src = m.group(1)
    try:
        compile(src, "fr_safe", "exec")
        ok("P the embedded fr_safe helper compiles")
    except SyntaxError as e:
        bad(f"P fr_safe does not compile: {e}")
    ns: dict = {"__name__": "fr_safe_test"}
    body = src.rsplit("sys.exit(main(sys.argv[1:]))", 1)[0]  # exit-status: a string searched in fr_safe's source, not this script's exit
    with tempfile.TemporaryDirectory(prefix="frr-u1-") as td:
        tdp = Path(td)
        d = tdp / "d"
        d.mkdir()
        os.chmod(d, 0o770)
        v = tdp / "victim"
        v.write_bytes(b"VICTIM\n")
        os.chmod(v, 0o600)
        before = snap(v)
        target = d / "conf"
        target.write_bytes(b"old\n")
        try:
            exec(compile(body, "fr_safe", "exec"), ns)
            real_mkstemp = ns["tempfile"].mkstemp

            def swapping_mkstemp(*a, **kw):
                fd, tmp = real_mkstemp(*a, **kw)
                os.unlink(tmp)
                os.symlink(v, tmp)
                return fd, tmp

            ns["tempfile"].mkstemp = swapping_mkstemp
            refused = False
            try:
                ns["replace_with"](str(target), lambda out: out.write(b"new\n"), 0o660, None)
            except ns["Refused"]:
                refused = True
            finally:
                ns["tempfile"].mkstemp = real_mkstemp
            left_link = target.is_symlink()
            check(refused and snap(v) == before and not left_link,
                  "U1 temp swapped after mkstemp: refused, victim untouched, the planted link not left at the dest",
                  f"U1 refused={refused} victim changed={snap(v) != before} link left at dest={left_link}")
        except Exception as e:  # noqa: BLE001
            bad(f"U1 could not exercise fr_safe: {e!r}")

def daemon_enabled(pkg: Path, prefix: str, mod: str, conf: Path):
    """`enabled` as the module's own config.load() reads the sandbox conf."""
    r = module_py(pkg, prefix, (
        f"import sys; from {mod} import config;"
        "sys.stdout.write('1' if config.load(sys.argv[1]).enabled else '0')"), str(conf))
    return {"0": False, "1": True}.get(r.stdout.strip(), f"error: {r.stderr[-200:]}")


# ── H. HomeKit (Q-F: factory reset ERASES the pairings) ─────────────────────
print("── H. HomeKit bridge clear-list ──")
s = Sandbox("h1")
reach_late_steps(s)
rc = s.run()
store_left = sorted(p.name for p in s.hk_var.iterdir()) if s.hk_var.is_dir() else ["<dir gone>"]
check(rc == 0 and s.hk_var.is_dir() and not s.hk_var.is_symlink() and not store_left,
      "H1a every entry of the pairing store erased (state, aids, identity, .hk-*), the dir kept",
      f"H1a store after reset: {store_left} (rc={rc}; {s.why()})")
sl = s.systemctl_log()
check("stop sa02m-homekit.service" in sl and "stop:store-present" in sl,
      "H1b the unit was stopped while the store still existed (stop before erase)",
      f"H1b systemctl calls: {sl.splitlines()}")
conf_ok = s.hk_conf.is_file() and s.hk_conf.read_bytes() == HK_SEED.read_bytes() \
    and stat.S_IMODE(os.lstat(s.hk_conf).st_mode) == 0o640
check(conf_ok, "H1c the conf is the installer's seed bytes (enabled = false), 0640 (homekit-bridge.md §13)",
      f"H1c conf: {s.hk_conf.read_bytes()[:120]!r}")
# The owner is asked for by name; the sandbox host has no sa02m-homekit group,
# so owner_ids() keeps the old owner here — the call's argument is what the
# board resolves (www-data owns the conf, the bridge reads it through its group).
check('fr_safe hk-reset-conf "$HK_CONF" "$HK_PKG_DIR" www-data:sa02m-homekit' in FACTORY.read_text(encoding="utf-8"),
      "H1c2 the reset asks for owner www-data:sa02m-homekit (the CGI owns, the bridge reads by group)",
      "H1c2 the HomeKit conf reset does not name owner www-data:sa02m-homekit")
check(not (s.hk_run / "setup.json").exists(), "H1d the stale setup code is removed",
      "H1d /run/sa02m-homekit/setup.json survived the reset")
s.cleanup()

s = Sandbox("h2")
reach_late_steps(s)
(s.sb / "hk-stuck").write_text("1", encoding="utf-8")
rc = s.run()
conf_back = s.hk_conf.is_file() and b"enabled = true" in s.hk_conf.read_bytes()
check(rc != 0 and s.txn().get("error_code") == "E_APPLY" and (s.hk_var / "state.json").exists()
      and not s.htpasswd_reset() and conf_back,
      "H2 a unit that will not stop: E_APPLY, the store intact, configs and the HomeKit conf rolled back",
      f"H2 rc={rc} store={(s.hk_var / 'state.json').exists()} htpasswd_reset={s.htpasswd_reset()} "
      f"conf_back={conf_back} ({s.why()})")
hc_intact = (s.hc_var / "tokens.json").exists() and (s.hc_run / "link.json").exists() \
    and s.hc_conf.is_file() and b"enabled = true" in s.hc_conf.read_bytes()
check(rc != 0 and hc_intact and "sa02m-homeconnect" not in s.systemctl_log(),
      "O2 a HomeKit bridge that will not stop never stops the Home Connect client and keeps its sign-in",
      f"O2 rc={rc} hc_intact={hc_intact} systemctl={s.systemctl_log().splitlines()} ({s.why()})")
s.cleanup()

s = Sandbox("h3")
reach_late_steps(s)
v = victim(s.sb)
vdir = s.sb / "outside/vdir"
vdir.mkdir()
(vdir / "keep").write_text("KEEP\n", encoding="utf-8")
before, before_d = snap(v), snap(vdir / "keep")
(s.hk_var / "state.json").unlink()
os.symlink(v, s.hk_var / "state.json")
(s.hk_var / "sub").mkdir()
os.symlink(vdir, s.hk_var / "sub/link")
rc = s.run()
left = sorted(p.name for p in s.hk_var.iterdir()) if s.hk_var.is_dir() else ["<dir gone>"]
check(rc == 0 and not left and snap(v) == before and snap(vdir / "keep") == before_d and vdir.is_dir(),
      "H3 a symlink and a dir holding a symlink inside the store: removed, their targets untouched",
      f"H3 rc={rc} left={left} victim={snap(v) == before} vdir kept={vdir.is_dir()} ({s.why()})")
s.cleanup()

s = Sandbox("h4")
reach_late_steps(s)
v = victim(s.sb)
before = snap(v)
s.hk_conf.unlink()
os.symlink(v, s.hk_conf)
rc = s.run()
check(rc != 0 and snap(v) == before and (s.hk_var / "state.json").exists(),
      "H4 a symlinked HomeKit conf: refused before anything is erased, the victim untouched",
      f"H4 rc={rc} victim changed={snap(v) != before} store={(s.hk_var / 'state.json').exists()}")
s.cleanup()

s = Sandbox("h5")
reach_late_steps(s)
shutil.rmtree(s.hk_var)
shutil.rmtree(s.etc / "sa02m-homekit")
rc = s.run()
check(rc == 0 and s.done() and "sa02m-homekit" not in s.systemctl_log(),
      "H5 bridge not installed: the reset succeeds and leaves the HomeKit unit alone",
      f"H5 rc={rc} systemctl={s.systemctl_log().splitlines()} ({s.why()})")
s.cleanup()

s = Sandbox("h6")
reach_late_steps(s)
(s.cur / "templates/etc/nginx/.htpasswd").write_text("nobody:x\n", encoding="utf-8")
rc = s.run()
check(rc != 0 and s.txn().get("error_code") == "E_HEALTH" and (s.hk_var / "state.json").exists(),
      "H6 a reset failing its own verify keeps the pairings (erase runs only after verify)",
      f"H6 rc={rc} store={(s.hk_var / 'state.json').exists()} ({s.why()})")
check(rc != 0 and (s.hc_var / "tokens.json").exists() and "sa02m-homeconnect" not in s.systemctl_log(),
      "C6 the same failed verify keeps the Home Connect sign-in and never touches its unit",
      f"C6 rc={rc} tokens={(s.hc_var / 'tokens.json').exists()} systemctl={s.systemctl_log().splitlines()}")
s.cleanup()

s = Sandbox("h7")
reach_late_steps(s)
shutil.rmtree(s.sb / "opt/sa02m-homekit")
rc = s.run()
body = s.hk_conf.read_bytes() if s.hk_conf.is_file() else b""
check(rc == 0 and b"enabled = false" in body and b"interface = eth1" in body and not any(s.hk_var.iterdir()),
      "H7 no package template: enabled = false forced, the rest kept, the store still erased",
      f"H7 rc={rc} conf={body!r} ({s.why()})")
s.cleanup()

s = Sandbox("h7b")
reach_late_steps(s)
shutil.rmtree(s.sb / "opt/sa02m-homekit")
# configparser (the daemon's reader) takes the key in any case and `:` as well
# as `=`: the forced value must reach every spelling the daemon would honour.
s.hk_conf.write_text("[bridge]\nEnabled: true\ninterface = eth1\n", encoding="utf-8")
rc = s.run()
body = s.hk_conf.read_bytes() if s.hk_conf.is_file() else b""
check(rc == 0 and b"true" not in body and b"enabled = false" in body.lower() and b"interface = eth1" in body,
      "H7b no package template, `Enabled: true` spelling: forced to false too",
      f"H7b rc={rc} conf={body!r} ({s.why()})")
s.cleanup()

# H7c/H7d — `enabled = false` is forced with the configparser reader in mind:
# the daemon goes on reading a value over every following line indented
# deeper than its key (blank and comment lines between included), and takes
# Unicode whitespace around the key. A forced value that starts with `false`
# can never read true, so H7c pins the mechanism (no continuation left under
# the forced key) rather than an exploit; H7d — a NO-BREAK SPACE before `=`,
# which the old ASCII-only key pattern did not match — is one.
s = Sandbox("h7c")
reach_late_steps(s)
shutil.rmtree(s.sb / "opt/sa02m-homekit")
s.hk_conf.write_text("[bridge]\nenabled = true\n  on\ninterface = eth1\n", encoding="utf-8")
rc = s.run()
body = s.hk_conf.read_bytes() if s.hk_conf.is_file() else b""
en = daemon_enabled(HK_PKG, "SA02M_HOMEKIT_", "sa02m_homekit", s.hk_conf)
check(rc == 0 and en is False and b"  on" not in body and b"enabled = false\ninterface = eth1" in body,
      "H7c no package template, a continuation line under `enabled`: dropped with the forced key, the daemon reads disabled",
      f"H7c rc={rc} daemon enabled={en} conf={body!r} ({s.why()})")
s.cleanup()

s = Sandbox("h7d")
reach_late_steps(s)
shutil.rmtree(s.sb / "opt/sa02m-homekit")
s.hk_conf.write_text("[bridge]\nenabled\u00a0= true\ninterface = eth1\n", encoding="utf-8")
rc = s.run()
body = s.hk_conf.read_bytes() if s.hk_conf.is_file() else b""
en = daemon_enabled(HK_PKG, "SA02M_HOMEKIT_", "sa02m_homekit", s.hk_conf)
check(rc == 0 and en is False and b"interface = eth1" in body,
      "H7d no package template, `enabled<NO-BREAK SPACE>= true` (configparser reads it): forced, the daemon reads disabled",
      f"H7d rc={rc} daemon enabled={en} conf={body!r} ({s.why()})")
s.cleanup()

# ── C. Home Connect (Q-F: factory reset ERASES the sign-in) ─────────────────
print("── C. Home Connect sign-in clear-list ──")
s = Sandbox("c1")
reach_late_steps(s)
rc = s.run()
left = sorted(p.name for p in s.hc_var.iterdir()) if s.hc_var.is_dir() else ["<dir gone>"]
check(rc == 0 and s.done() and s.hc_var.is_dir() and not s.hc_var.is_symlink() and not left,
      "C1a every entry of the Home Connect state dir erased (tokens, budget, appliances, .hc-*), the dir kept",
      f"C1a state dir after reset: {left} (rc={rc}; {s.why()})")
sl = s.systemctl_log()
check("stop sa02m-homeconnect.service" in sl and "hc-stop:tokens-present" in sl and "hc-stop:conf-enabled" not in sl,
      "C1b the client was stopped with the conf already disabled and while the tokens still existed (conf → stop → erase)",
      f"C1b systemctl calls: {sl.splitlines()}")
hc_ok = s.hc_conf.is_file() and s.hc_conf.read_bytes() == HC_TEMPLATE \
    and stat.S_IMODE(os.lstat(s.hc_conf).st_mode) == 0o640
check(hc_ok, "C1c the conf is the package's own render() of the defaults (enabled = false, no Client ID), 0640",
      f"C1c conf: {s.hc_conf.read_bytes()[:160]!r}")
check('fr_safe hc-reset-conf "$HC_CONF" "$HC_PKG_DIR" www-data:sa02m-homeconnect' in FACTORY.read_text(encoding="utf-8"),
      "C1c2 the reset asks for owner www-data:sa02m-homeconnect (the CGI owns, the client reads by group)",
      "C1c2 the Home Connect conf reset does not name owner www-data:sa02m-homeconnect")
check(not (s.hc_run / "link.json").exists(), "C1d the pending sign-in code (link.json) is removed",
      "C1d /run/sa02m-homeconnect/link.json survived the reset")
log_txt = s.log.read_text(encoding="utf-8", errors="replace") if s.log.is_file() else ""
check("HC-SECRET" not in log_txt, "C1e no token byte reached the factory-reset log",
      "C1e the token file's content is in the factory-reset log")
s.cleanup()

s = Sandbox("c2")
reach_late_steps(s)
(s.sb / "hc-stuck").write_text("1", encoding="utf-8")
rc = s.run()
hc_back = s.hc_conf.is_file() and b"enabled = true" in s.hc_conf.read_bytes() and b"INTEGRATOR_APP_1" in s.hc_conf.read_bytes()
check(rc != 0 and s.txn().get("error_code") == "E_APPLY" and (s.hc_var / "tokens.json").exists() and hc_back,
      "C2 a client that will not stop: E_APPLY, the tokens intact, the Home Connect conf rolled back",
      f"C2 rc={rc} tokens={(s.hc_var / 'tokens.json').exists()} conf_back={hc_back} ({s.why()})")
s.cleanup()

# O1 — the two erase steps are one transaction: a Home Connect client that
# refuses to stop must not cost the HomeKit pairings (the old order erased the
# store, THEN stopped Home Connect, and the E_APPLY rollback cannot bring keys
# back).
s = Sandbox("o1")
reach_late_steps(s)
(s.sb / "hc-stuck").write_text("1", encoding="utf-8")
rc = s.run()
hk_left = sorted(p.name for p in s.hk_var.iterdir()) if s.hk_var.is_dir() else []
hk_back = s.hk_conf.is_file() and b"enabled = true" in s.hk_conf.read_bytes()
hc_back = s.hc_conf.is_file() and b"enabled = true" in s.hc_conf.read_bytes()
check(rc != 0 and s.txn().get("error_code") == "E_APPLY"
      and hk_left == [".hk-abc.tmp", "aids.json", "identity.json", "state.json"]
      and (s.hk_run / "setup.json").exists() and hk_back
      and (s.hc_var / "tokens.json").exists() and (s.hc_run / "link.json").exists() and hc_back
      and not s.htpasswd_reset(),
      "O1 Home Connect will not stop: E_APPLY, nothing erased — HomeKit pairings + setup code kept, both confs rolled back",
      f"O1 rc={rc} hk_store={hk_left} setup={(s.hk_run / 'setup.json').exists()} hk_conf_back={hk_back} "
      f"tokens={(s.hc_var / 'tokens.json').exists()} hc_conf_back={hc_back} ({s.why()})")
sl = s.systemctl_log()
hk_state = (s.sb / "hk-state").read_text(encoding="utf-8").strip() if (s.sb / "hk-state").exists() else "?"
check(rc != 0 and "start sa02m-homekit.service" in sl and "start:hk-conf-restored" in sl and hk_state == "active",
      "O3 the rollback starts the HomeKit bridge again (active before the reset), after its conf is back",
      f"O3 rc={rc} hk_state={hk_state} systemctl={sl.splitlines()} ({s.why()})")
s.cleanup()

# O4 — only a unit the reset found ACTIVE is started again.
s = Sandbox("o4")
reach_late_steps(s)
(s.sb / "hc-stuck").write_text("1", encoding="utf-8")
(s.sb / "hk-state").write_text("inactive\n", encoding="utf-8")
rc = s.run()
sl = s.systemctl_log()
check(rc != 0 and s.txn().get("error_code") == "E_APPLY" and "stop sa02m-homekit.service" in sl
      and "start sa02m-homekit.service" not in sl and (s.hk_var / "state.json").exists(),
      "O4 a bridge inactive before the reset stays stopped after the rollback (pairings kept)",
      f"O4 rc={rc} systemctl={sl.splitlines()} ({s.why()})")
s.cleanup()

s = Sandbox("c3")
reach_late_steps(s)
v = victim(s.sb)
before = snap(v)
(s.hc_var / "tokens.json").unlink()
os.symlink(v, s.hc_var / "tokens.json")
rc = s.run()
left = sorted(p.name for p in s.hc_var.iterdir()) if s.hc_var.is_dir() else ["<dir gone>"]
check(rc == 0 and not left and snap(v) == before,
      "C3 a symlink inside the state dir: removed, its target untouched",
      f"C3 rc={rc} left={left} victim={snap(v) == before} ({s.why()})")
s.cleanup()

s = Sandbox("c4")
reach_late_steps(s)
v = victim(s.sb)
before = snap(v)
s.hc_conf.unlink()
os.symlink(v, s.hc_conf)
rc = s.run()
check(rc != 0 and snap(v) == before and (s.hc_var / "tokens.json").exists(),
      "C4 a symlinked Home Connect conf: refused, the victim and the tokens untouched",
      f"C4 rc={rc} victim changed={snap(v) != before} tokens={(s.hc_var / 'tokens.json').exists()}")
s.cleanup()

s = Sandbox("c5")
reach_late_steps(s)
shutil.rmtree(s.hc_var)
shutil.rmtree(s.etc / "sa02m-homeconnect")
rc = s.run()
check(rc == 0 and s.done() and "sa02m-homeconnect" not in s.systemctl_log(),
      "C5 client not installed: the reset succeeds and leaves the Home Connect unit alone",
      f"C5 rc={rc} systemctl={s.systemctl_log().splitlines()} ({s.why()})")
s.cleanup()

s = Sandbox("c7")
reach_late_steps(s)
shutil.rmtree(s.sb / "opt/sa02m-homeconnect")
rc = s.run()
body = s.hc_conf.read_bytes() if s.hc_conf.is_file() else b""
check(rc == 0 and b"enabled = false" in body and b"client_id = INTEGRATOR_APP_1" in body
      and not any(s.hc_var.iterdir()),
      "C7 no package template: enabled = false forced, the Client ID kept, the state dir still erased",
      f"C7 rc={rc} conf={body!r} ({s.why()})")
s.cleanup()

# C7c/C7d — the same two spellings for the Home Connect conf (see H7c/H7d).
for tag, conf_text, what in (
        ("c7c", "[account]\nenabled = true\n  on\nclient_id = INTEGRATOR_APP_1\n",
         "a continuation line under `enabled`: dropped with the forced key"),
        ("c7d", "[account]\nenabled\u00a0= true\nclient_id = INTEGRATOR_APP_1\n",
         "`enabled<NO-BREAK SPACE>= true`: forced")):
    s = Sandbox(tag)
    reach_late_steps(s)
    shutil.rmtree(s.sb / "opt/sa02m-homeconnect")
    s.hc_conf.write_text(conf_text, encoding="utf-8")
    rc = s.run()
    body = s.hc_conf.read_bytes() if s.hc_conf.is_file() else b""
    en = daemon_enabled(HC_PKG, "SA02M_HOMECONNECT_", "sa02m_homeconnect", s.hc_conf)
    check(rc == 0 and en is False and b"  on" not in body and b"client_id = INTEGRATOR_APP_1" in body,
          f"{tag.upper()} no package template, {what}; the daemon reads disabled, the Client ID kept",
          f"{tag.upper()} rc={rc} daemon enabled={en} conf={body!r} ({s.why()})")
    s.cleanup()

# ── R / M. the scenario engine's run history and the recorder's upload ──────
# Operator decision 2026-11-04: runs.json, logic_state.json, http-allow.json and
# the audio upload token + destination go; scenarios.json stays
# (docs/contracts/cloud-scenarios.md «Сброс к заводским настройкам»,
# audio-recorder.md §19).
def donor_leaks(sbx: Sandbox, needles) -> list[str]:
    """Where the previous owner's bytes survive a reset that reported done:
    the log, the rollback journal (kept on the board), the pre-reset backup."""
    hits = []
    if sbx.log.is_file() and any(n in sbx.log.read_bytes() for n in needles):
        hits.append("log")
    rb = sbx.state / "rollback"
    for p in (rb.rglob("*") if rb.is_dir() else []):
        if p.is_file() and not p.is_symlink() and any(n in p.read_bytes() for n in needles):
            hits.append(str(p.relative_to(sbx.sb)))
    bp = sbx.txn().get("backup_path")
    if not bp or not Path(bp).is_file():
        hits.append(f"no backup to scan ({bp!r}) — non-vacuity")
    else:
        try:
            with tarfile.open(bp, "r:gz") as tar:
                for m in tar.getmembers():
                    f = tar.extractfile(m) if m.isfile() else None
                    if f is not None and any(n in f.read() for n in needles):
                        hits.append(f"backup:{m.name}")
        except (tarfile.TarError, OSError) as e:
            hits.append(f"backup unreadable: {e}")
    return hits


def rules_view(sbx: Sandbox) -> dict:
    """The REAL sa02m_rules.store.load() of the sandbox store — what the
    engine, the cloud channel and the agent API read after the reset."""
    r = module_py(RULES_PKG, "SA02M_RULES_", (
        "import json, sys; from sa02m_rules import store;"
        "d = store.load(sys.argv[1]);"
        "sys.stdout.write(json.dumps({'runs': d['runs'], 'notify': d['notify_queue']}))"),
        str(sbx.rules / "scenarios.json"))
    return {"error": r.stderr[-300:]} if r.returncode else json.loads(r.stdout)


def audio_view(sbx: Sandbox) -> dict:
    """The REAL sa02m_audio.config.load() of the sandbox conf."""
    r = module_py(AUDIO_PKG, "SA02M_AUDIO_", (
        "import json, sys; from sa02m_audio import config;"
        "c = config.load(sys.argv[1]);"
        "sys.stdout.write(json.dumps({'upload_url': c.upload_url, 'enabled': c.enabled, 'warnings': c.warnings}))"),
        str(sbx.au_conf))
    return {"error": r.stderr[-300:]} if r.returncode else json.loads(r.stdout)


def unit_state(sbx: Sandbox, name: str) -> str:
    p = sbx.sb / f"{name}-state"
    return p.read_text(encoding="utf-8").strip() if p.exists() else "?"


def rules_erased(sbx: Sandbox) -> tuple[bool, str]:
    runs = sbx.rules / RULES["journal"]
    try:
        jr = json.loads(runs.read_text(encoding="utf-8")) if runs.is_file() and not runs.is_symlink() else None
    except ValueError:
        jr = "<not JSON>"
    logic = (sbx.rules / RULES["logic"]).exists() or (sbx.rules / RULES["logic"]).is_symlink()
    allow = (sbx.rules / "http-allow.json").exists() or (sbx.rules / "http-allow.json").is_symlink()
    return jr == RULES_EMPTY and not logic and not allow, f"runs.json={jr!r} logic_state={logic} http-allow={allow}"


def nothing_erased(sbx: Sandbox) -> tuple[bool, str]:
    """Every owner store the reset erases is as seeded (a phase-1 refusal)."""
    state = {
        "rules runs": b"DONOR-RUN" in (sbx.rules / "runs.json").read_bytes() if (sbx.rules / "runs.json").is_file() else False,
        "rules logic": (sbx.rules / "logic_state.json").is_file(),
        "rules allow": (sbx.rules / "http-allow.json").is_file(),
        "homekit store": (sbx.hk_var / "state.json").exists(),
        "homeconnect tokens": (sbx.hc_var / "tokens.json").exists(),
        "audio token": sbx.au_token.exists() or sbx.au_token.is_symlink(),
        "audio url": sbx.au_conf.is_file() and not sbx.au_conf.is_symlink()
        and b"donor.example" in sbx.au_conf.read_bytes(),
    }
    lost = [k for k, v in state.items() if not v]
    return not lost, f"erased: {lost}"


print("── R/M. scenario run history and the recorder's upload ──")
# R6 — the runner cannot import the packages (it runs standalone as root), so
# its paths and the empty journal are pinned to their one homes here.
r6 = []
for pat_, want, what in (
        (r"^RULES_DIR=(\S+)$", RULES["dir"], "RULES_DIR vs dirname(store.DEFAULT_PATH)"),
        (r"^AU_DIR=(\S+)$", AUDIO["etc"], "AU_DIR vs sa02m_audio.constants.ETC_DIR")):
    m_ = re.search(pat_, RUNNER_TXT, re.M)
    if not m_ or m_.group(1) != want:
        r6.append(f"{what}: runner {m_.group(1) if m_ else 'NOT FOUND'!r} != {want!r}")
m_ = re.search(r"^RULES_EMPTY_JOURNAL='(.*)'$", RUNNER_TXT, re.M)
try:
    if not m_ or json.loads(m_.group(1)) != RULES_EMPTY:
        r6.append(f"RULES_EMPTY_JOURNAL {m_.group(1) if m_ else 'NOT FOUND'!r} != store.empty_journal() {RULES_EMPTY!r}")
except ValueError:
    r6.append(f"RULES_EMPTY_JOURNAL is not JSON: {m_.group(1)!r}")
m_ = re.search(r"^AU_CONF=\$AU_DIR/(\S+)$", RUNNER_TXT, re.M)
if not m_ or posixjoin(AUDIO["etc"], m_.group(1)) != AUDIO["conf"]:
    r6.append(f"AU_CONF {m_.group(1) if m_ else 'NOT FOUND'!r} != constants.CONF_FILE {AUDIO['conf']!r}")
m_ = re.search(r"^AU_TOKEN_NAME=(\S+)$", RUNNER_TXT, re.M)
if not m_ or posixjoin(AUDIO["etc"], m_.group(1)) != AUDIO["token"]:
    r6.append(f"AU_TOKEN_NAME {m_.group(1) if m_ else 'NOT FOUND'!r} != constants.TOKEN_FILE {AUDIO['token']!r}")
for needle in (f'"$RULES_DIR/{RULES["journal"]}"', f'remove-name "$RULES_DIR" {RULES["logic"]}',
               f'remove-name "$RULES_DIR" {posixbase(RULES["allow"])}', 'RULES_UNIT=sa02m-rules.service',
               'AU_UNIT=sa02m-audio.service'):
    if needle not in RUNNER_TXT:
        r6.append(f"runner line {needle!r} not found")
if posixdir(RULES["allow"]) != RULES["dir"]:
    r6.append(f"http_guard.DEFAULT_ALLOW_PATH {RULES['allow']!r} is not in the store dir {RULES['dir']!r}")
check(not r6, "R6 the runner's store/recorder paths and empty journal equal sa02m_rules.store / http_guard / "
      "sa02m_audio.constants (and both unit files exist)", "R6 parity: " + "; ".join(r6))

# R1 + M1 — one clean reset.
s = Sandbox("rm1")
reach_late_steps(s)
doc_before = (s.rules / "scenarios.json").read_bytes()
conf_before = s.au_conf.read_bytes()
conf_st = os.lstat(s.au_conf)
rc = s.run()
erased, why_r = rules_erased(s)
runs_mode = stat.S_IMODE(os.lstat(s.rules / "runs.json").st_mode) if (s.rules / "runs.json").is_file() else None
check(rc == 0 and s.done() and erased and runs_mode == 0o644,
      "R1a runs.json == store.empty_journal() (0644), logic_state.json and http-allow.json removed",
      f"R1a rc={rc} {why_r} mode={runs_mode} ({s.why()})")
check((s.rules / "scenarios.json").read_bytes() == doc_before, "R1b scenarios.json byte-identical (scenarios stay)",
      "R1b scenarios.json changed by the reset")
sl = s.systemctl_log()
check("stop sa02m-rules.service" in sl and "rules-stop:runs-donor" in sl and "rules-stop:logic-present" in sl,
      "R1c the engine was stopped while the previous owner's journal and template state still existed",
      f"R1c systemctl calls: {sl.splitlines()}")
check("start sa02m-rules.service" in sl and "start:rules-runs-empty" in sl and unit_state(s, "rules") == "active",
      "R1d the engine (active before) is started again, after the erase",
      f"R1d state={unit_state(s, 'rules')} systemctl calls: {sl.splitlines()}")
leaks = donor_leaks(s, RULES_NEEDLES)
check(not leaks, "R1e no run/notify/template bytes in the log, the rollback journal or the pre-reset backup",
      f"R1e the previous owner's scenario state survives in: {leaks}")
token_gone = not s.au_token.exists() and not s.au_token.is_symlink()
av = audio_view(s)
check(rc == 0 and token_gone and av.get("upload_url") == "" and av.get("enabled") is True,
      "M1a upload.token removed; the real config.load() gives upload_url == \"\" and enabled still True",
      f"M1a rc={rc} token_gone={token_gone} config={av}")
after = s.au_conf.read_bytes().splitlines() if s.au_conf.is_file() else []
before_l = conf_before.splitlines()
others_kept = len(after) == len(before_l) and all(
    a == b or (b.startswith(b"upload_url") and a == b"upload_url = ") for a, b in zip(after, before_l))
st2 = os.lstat(s.au_conf) if s.au_conf.exists() else None
check(others_kept and st2 is not None and stat.S_IMODE(st2.st_mode) == stat.S_IMODE(conf_st.st_mode)
      and (st2.st_uid, st2.st_gid) == (conf_st.st_uid, conf_st.st_gid),
      "M1b every other conf line byte-identical, mode and owner kept",
      f"M1b conf before={before_l} after={after} mode/owner kept={st2 is not None and stat.S_IMODE(st2.st_mode) == stat.S_IMODE(conf_st.st_mode)}")
check("stop sa02m-audio.service" in sl and "audio-stop:token-present" in sl
      and "start sa02m-audio.service" in sl and "start:audio-token-gone-url-empty" in sl,
      "M1c the recorder stopped while the token existed, started again after the erase",
      f"M1c systemctl calls: {sl.splitlines()}")
kept = [p for p in ("var/lib/sa02m-audio/rec/a.wav", "var/lib/sa02m-audio/events.jsonl",
                    "etc/sa02m-audio/voice-allow.json") if (s.sb / p).is_file()]
check(len(kept) == 3, "M1d recordings, events.jsonl and voice-allow.json stay (decision F3: recordings out of scope)",
      f"M1d kept only {kept}")
leaks = donor_leaks(s, AUDIO_NEEDLES)
check(not leaks, "M1e no token or upload URL bytes in the log, the rollback journal or the pre-reset backup",
      f"M1e the upload credential survives in: {leaks}")
s.cleanup()

s = Sandbox("r2")
reach_late_steps(s)
(s.sb / "rules-state").write_text("inactive\n", encoding="utf-8")
rc = s.run()
erased, why_r = rules_erased(s)
sl = s.systemctl_log()
check(rc == 0 and s.done() and erased and "start sa02m-rules.service" not in sl,
      "R2 an engine inactive before the reset: erased, never started",
      f"R2 rc={rc} {why_r} systemctl={sl.splitlines()} ({s.why()})")
s.cleanup()

s = Sandbox("r3")
reach_late_steps(s)
(s.sb / "rules-stuck").write_text("1", encoding="utf-8")
rc = s.run()
intact, why_n = nothing_erased(s)
sl = s.systemctl_log()
check(rc != 0 and s.txn().get("error_code") == "E_APPLY" and intact and not s.htpasswd_reset(),
      "R3a an engine that will not stop: E_APPLY, NOTHING erased (rules, HomeKit, Home Connect, audio), configs rolled back",
      f"R3a rc={rc} {why_n} htpasswd_reset={s.htpasswd_reset()} ({s.why()})")
check("start sa02m-homekit.service" in sl and "start sa02m-homeconnect.service" in sl
      and "sa02m-audio" not in sl,
      "R3b the rollback starts HomeKit AND Home Connect again (both active before); the recorder was never stopped",
      f"R3b systemctl calls: {sl.splitlines()}")
s.cleanup()

# R4 + M5 — neither installed.
s = Sandbox("rm-none")
reach_late_steps(s)
shutil.rmtree(s.rules)
shutil.rmtree(s.audio)
rc = s.run()
sl = s.systemctl_log()
check(rc == 0 and s.done() and "sa02m-rules" not in sl,
      "R4 scenario engine not installed: done, its unit untouched",
      f"R4 rc={rc} systemctl={sl.splitlines()} ({s.why()})")
check(rc == 0 and s.done() and "sa02m-audio" not in sl,
      "M5 recorder not installed: done, its unit untouched",
      f"M5 rc={rc} systemctl={sl.splitlines()} ({s.why()})")
s.cleanup()

# R5 — a document from before 1.0.6.41 still carrying runs/notify/last_* and no
# runs.json: without a journal present the engine would migrate them back.
s = Sandbox("r5")
reach_late_steps(s)
legacy = {"scenarios": [dict(RULES_SCENARIO, last_run=1.0, last_error="DONOR-RUN")], "library": "", "vars": {},
          "runs": DONOR_JOURNAL["runs"], "notify_queue": DONOR_JOURNAL["notify_queue"]}
(s.rules / "scenarios.json").write_text(json.dumps(legacy), encoding="utf-8")
(s.rules / "runs.json").unlink()
doc_before = (s.rules / "scenarios.json").read_bytes()
rc = s.run()
view = rules_view(s)
check(rc == 0 and s.done() and "error" not in view and not view.get("runs") and not view.get("notify")
      and (s.rules / "scenarios.json").read_bytes() == doc_before,
      "R5 legacy document, no runs.json: the real store.load() shows no previous run or notify; the document unchanged",
      f"R5 rc={rc} view={view} ({s.why()})")
s.cleanup()

s = Sandbox("r7")
reach_late_steps(s)
(s.sb / "rules-nostart").write_text("1", encoding="utf-8")
rc = s.run()
erased, why_r = rules_erased(s)
log_txt = s.log.read_text(encoding="utf-8", errors="replace") if s.log.is_file() else ""
check(rc == 0 and s.done() and erased
      and "WARN sa02m-rules.service was active before the reset and did not start again" in log_txt,
      "R7 the engine will not start after the erase: done, erase kept, a WARN line says to start it by hand",
      f"R7 rc={rc} {why_r} ({s.why()})")
s.cleanup()

s = Sandbox("r8")
reach_late_steps(s)
(s.sb / "rules-answers").write_text("active\ninactive\nactive-replant\ninactive\n", encoding="utf-8")
rc = s.run()
erased, why_r = rules_erased(s)
sl = s.systemctl_log()
log_txt = s.log.read_text(encoding="utf-8", errors="replace") if s.log.is_file() else ""
check(rc == 0 and s.done() and erased and "rules-replanted" in sl
      and "sa02m-rules.service was started during the reset" in log_txt and "start sa02m-rules.service" in sl,
      "R8 an engine started by hand inside the window (it wrote template state back): stopped, erased again, done",
      f"R8 rc={rc} {why_r} systemctl={sl.splitlines()} ({s.why()})")
s.cleanup()

# R9 — runs.json is a symlink (only root can plant one in the 0755 store dir;
# the reset still never writes through it): the link goes, the empty journal
# is written in its place, the target is untouched.
s = Sandbox("r9")
reach_late_steps(s)
v = victim(s.sb, "runs-victim.json", json.dumps(DONOR_JOURNAL).encode())
before = snap(v)
(s.rules / "runs.json").unlink()
os.symlink(v, s.rules / "runs.json")
rc = s.run()
erased, why_r = rules_erased(s)
check(rc == 0 and s.done() and erased and not (s.rules / "runs.json").is_symlink() and snap(v) == before,
      "R9 a symlinked runs.json: the link replaced by the empty journal, its target untouched",
      f"R9 rc={rc} {why_r} link left={(s.rules / 'runs.json').is_symlink()} victim changed={snap(v) != before} "
      f"({s.why()})")
s.cleanup()

s = Sandbox("m2")
reach_late_steps(s)
v = victim(s.sb, "token-victim", b"VICTIM-TOKEN\n")
before = snap(v)
s.au_token.unlink()
os.symlink(v, s.au_token)
rc = s.run()
check(rc == 0 and s.done() and not s.au_token.is_symlink() and not s.au_token.exists() and snap(v) == before,
      "M2 a symlinked upload.token: the link removed, its target untouched",
      f"M2 rc={rc} link left={s.au_token.is_symlink()} victim changed={snap(v) != before} ({s.why()})")
s.cleanup()

s = Sandbox("m3")
reach_late_steps(s)
s.au_token.unlink()
s.au_token.mkdir()
(s.au_token / "inside").write_text("x\n", encoding="utf-8")
rc = s.run()
tok_dir = s.au_token.is_dir() and (s.au_token / "inside").is_file()
state_ok = (s.rules / "runs.json").is_file() and b"DONOR-RUN" in (s.rules / "runs.json").read_bytes() \
    and (s.hk_var / "state.json").exists() and b"donor.example" in s.au_conf.read_bytes()
check(rc != 0 and s.txn().get("error_code") == "E_APPLY" and tok_dir and state_ok and not s.htpasswd_reset(),
      "M3 upload.token is a directory: E_APPLY in phase 1, nothing erased anywhere",
      f"M3 rc={rc} token dir kept={tok_dir} nothing erased={state_ok} ({s.why()})")
s.cleanup()

s = Sandbox("m4")
reach_late_steps(s)
v = victim(s.sb, "conf-victim", b"[recorder]\nupload_url = https://victim.example/x\n")
before = snap(v)
s.au_conf.unlink()
os.symlink(v, s.au_conf)
rc = s.run()
check(rc != 0 and s.txn().get("error_code") == "E_APPLY" and snap(v) == before and s.au_token.is_file()
      and (s.hk_var / "state.json").exists() and b"DONOR-RUN" in (s.rules / "runs.json").read_bytes(),
      "M4 a symlinked sa02m-audio.conf: E_APPLY, the victim untouched, the token and every store intact",
      f"M4 rc={rc} victim changed={snap(v) != before} token={s.au_token.is_file()} ({s.why()})")
s.cleanup()

s = Sandbox("m6")
reach_late_steps(s)
(s.sb / "audio-stuck").write_text("1", encoding="utf-8")
rc = s.run()
intact, why_n = nothing_erased(s)
sl = s.systemctl_log()
check(rc != 0 and s.txn().get("error_code") == "E_APPLY" and intact
      and "start sa02m-rules.service" in sl and "start sa02m-homekit.service" in sl,
      "M6 a recorder that will not stop: E_APPLY, nothing erased, the units it stopped started again",
      f"M6 rc={rc} {why_n} systemctl={sl.splitlines()} ({s.why()})")
s.cleanup()

s = Sandbox("m7")
reach_late_steps(s)
s.au_conf.write_text("[recorder]\nenabled = true\nUpload_URL: https://donor.example/upload\n", encoding="utf-8")
rc = s.run()
av = audio_view(s)
body = s.au_conf.read_bytes() if s.au_conf.is_file() else b""
check(rc == 0 and av.get("upload_url") == "" and b"donor.example" not in body,
      "M7 the key spelled `Upload_URL:` (configparser reads it): forced to empty too",
      f"M7 rc={rc} config={av} conf={body!r} ({s.why()})")
s.cleanup()

# M8/M8b/M9 — the destination must be gone as the DAEMON reads it
# (sa02m_audio.config.load: configparser, the value stripped). Forcing only the
# `upload_url` line left a planted continuation line as the whole value: the
# daemon read it as the URL and, with the token removed, would send the next
# clip there unauthenticated (review round 1, F1). /etc/sa02m-audio is
# www-data's own, so any panel session can plant such a line.
URL_LINE = "upload_url = https://donor.example/upload\n"
for tag, conf_text, what in (
        ("m8", AUDIO["seed"].replace(URL_LINE, URL_LINE + "  https://donor2.example/x\n"),
         "a continuation line under upload_url"),
        ("m8b", AUDIO["seed"].replace(URL_LINE, URL_LINE + "\n# kept\n\thttps://donor2.example/x\n"),
         "a blank line, a comment, then a tab-indented continuation"),
        ("m9", AUDIO["seed"].replace(URL_LINE, "upload_url\u00a0= https://donor2.example/upload\n"),
         "`upload_url<NO-BREAK SPACE>=` (configparser reads it)")):
    s = Sandbox(tag)
    reach_late_steps(s)
    if conf_text == AUDIO["seed"]:
        bad(f"{tag.upper()} the seed has no `{URL_LINE.strip()}` line to plant against (non-vacuity)")
        s.cleanup()
        continue
    s.au_conf.write_text(conf_text, encoding="utf-8")
    rc = s.run()
    av = audio_view(s)
    body = s.au_conf.read_bytes() if s.au_conf.is_file() else b""
    check(rc == 0 and s.done() and av.get("upload_url") == "" and b"donor2" not in body
          and not s.au_token.exists() and av.get("enabled") is True,
          f"{tag.upper()} {what}: the real config.load() gives upload_url \"\" after the reset, the token removed",
          f"{tag.upper()} rc={rc} config={av} conf={body!r} ({s.why()})")
    s.cleanup()

# M8c/M8d — the forced line keeps the key line's own indentation. Written at
# column 0 (review round 2, F2), a following line indented less than the
# original key line but more than 0 became the forced value (M8c), and in a
# conf indented throughout every option after the key did (M8d: delete_after_upload
# lost its meaning and the value read non-empty).
s = Sandbox("m8c")
reach_late_steps(s)
s.au_conf.write_text("[recorder]\n  upload_url = https://donor.example/upload\n https://donor2.example/x\n",
                     encoding="utf-8")
rc = s.run()
av = audio_view(s)
check(rc == 0 and s.done() and av.get("upload_url") == "" and not s.au_token.exists(),
      "M8c a line indented less than the key line but more than 0: still its own option after the rewrite, "
      "the real config.load() gives upload_url \"\", the token removed",
      f"M8c rc={rc} config={av} conf={s.au_conf.read_bytes() if s.au_conf.is_file() else b''!r} ({s.why()})")
s.cleanup()

s = Sandbox("m8d")
reach_late_steps(s)
s.au_conf.write_text("[recorder]\n  enabled = true\n  upload_url = https://donor.example/upload\n"
                     "  storage = emmc\n  delete_after_upload = false\n", encoding="utf-8")
rc = s.run()
r = module_py(AUDIO_PKG, "SA02M_AUDIO_", (
    "import json, sys; from sa02m_audio import config; c = config.load(sys.argv[1]);"
    "sys.stdout.write(json.dumps([c.upload_url, c.enabled, c.storage, c.delete_after_upload, c.warnings]))"),
    str(s.au_conf))
got = json.loads(r.stdout) if r.returncode == 0 else r.stderr[-200:]
check(rc == 0 and s.done() and got == ["", True, "emmc", False, []],
      "M8d a conf indented throughout: after the rewrite upload_url is \"\" and every other option keeps its value "
      "(enabled, storage, delete_after_upload = false), no warning",
      f"M8d rc={rc} config.load=[upload_url, enabled, storage, delete_after_upload, warnings]={got!r} ({s.why()})")
s.cleanup()

# M11 — the belt is reached by what the rewrite cannot see: a URL written into
# the conf right AFTER force-key ran (a panel session saving the recorder card
# while the reset works — /etc/sa02m-audio is www-data's own and the CGI writes
# it with the recorder stopped). Modelled by a python3 shim on the ONE call
# `fr_safe force-key <the audio conf> upload_url ""`: the real call runs, then
# the URL is saved into the forced line, before anything else of the runner
# runs. The reset must refuse and keep the token: a URL with no token would go
# out unauthenticated (audio-recorder.md §19). Without the belt's call the run
# completes with the racer's URL and no token.
s = Sandbox("m11")
reach_late_steps(s)
real_py = shutil.which("python3") or sys.executable
s.shim("python3", f"""#!/bin/bash
# python3 -I -B -c "$FR_SAFE_PY" force-key <dest> <key> <value>
if [ "${{2:-}}" = -B ] && [ "${{5:-}}" = force-key ] && [ "${{6:-}}" = "{s.au_conf}" ] && [ "${{7:-}}" = upload_url ]; then
  "{real_py}" "$@"
  rc=$?
  sed -i 's|^upload_url = $|upload_url = https://racer.example/u|' "{s.au_conf}"
  echo raced >> "{s.sb}/m11-raced"
  exit "$rc"
fi
exec "{real_py}" "$@"
""")
rc = s.run()
t = s.txn()
raced = (s.sb / "m11-raced").read_text(encoding="utf-8").count("raced") if (s.sb / "m11-raced").exists() else 0
conf_url = b"racer.example" in s.au_conf.read_bytes() if s.au_conf.is_file() else False
check(raced == 1 and rc != 0 and t.get("error_code") == "E_APPLY" and "still set" in str(t.get("error_message"))
      and s.au_token.is_file(),
      "M11 a URL saved right after the rewrite, before the re-read: E_APPLY, the token kept (the belt, audio_url_cleared)",
      f"M11 shim fired {raced}x (want 1: the audio upload_url force-key only), rc={rc}, racer URL in the conf={conf_url}, "
      f"token kept={s.au_token.is_file()} ({s.why()})")
s.cleanup()

# M10 — the belt behind force-key: the runner's audio_url_cleared (the
# daemon's own reading of the conf) decides "no destination" exactly where
# config.load would find none, and never follows a symlink. Extracted from the
# SHIPPED runner and run by bash, like G.
mb = re.search(r"^audio_url_cleared\(\) \{\n.*?^\}\n", RUNNER_TXT, re.S | re.M)
if not mb:
    bad("M10 audio_url_cleared() not found in the runner (non-vacuity)")
else:
    with tempfile.TemporaryDirectory(prefix="frr-m10-") as td:
        tdp = Path(td)
        url_conf = tdp / "url.conf"
        url_conf.write_text("[recorder]\nupload_url = https://donor.example/u\n", encoding="utf-8")
        link = tdp / "link.conf"
        os.symlink(url_conf, link)
        probes = (
            ("url", url_conf, 1),
            ("nbsp", "[recorder]\nUpload_URL\u00a0: https://donor.example/u\n", 1),
            ("continuation", "[recorder]\nupload_url = \n  https://donor.example/u\n", 1),
            ("empty", "[recorder]\nenabled = true\nupload_url = \n", 0),
            ("no-section", "[other]\nupload_url = https://donor.example/u\n", 0),
            ("unparsable", "[recorder]\nupload_url = a\nupload_url = b\n", 0),
            ("absent", tdp / "missing.conf", 0),
            ("symlink-to-a-URL-conf", link, 1),
            ("a-directory", tdp, 1),
        )
        wrong = []
        for name, conf, want in probes:
            if isinstance(conf, str):
                path = tdp / f"{name}.conf"
                path.write_text(conf, encoding="utf-8")
                conf = path
            r = subprocess.run(["bash", "-c", "log() { :; }\n" + mb.group(0) + 'AU_CONF="$1"; audio_url_cleared', "m10",
                                str(conf)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
            got = 0 if r.returncode == 0 else 1
            if got != want:
                wrong.append(f"{name}: rc={r.returncode} want {'cleared' if want == 0 else 'still set'}")
        check(not wrong, "M10 audio_url_cleared agrees with config.load: a URL in any spelling or a continuation "
              "is still set; empty, no section, unparsable (the daemon uploads nothing) or absent is cleared; "
              "a symlink is never followed and an unreadable conf (a directory) counts as set",
              "M10 " + "; ".join(wrong))

# ── T. the resolver twin ────────────────────────────────────────────────────
print("── T. trusted-path resolver ──")
pat = re.compile(r"^# >>> trusted-path resolver.*?^# <<< trusted-path resolver$", re.S | re.M)
a, b = pat.search(RUNNER_TXT), pat.search(BACKUP_TXT)
if a and b:
    check(a.group(0) == b.group(0), "T the runner's resolver is byte-identical to etc/sa02m-web-backup.sh's",
          "T the runner's resolver differs from etc/sa02m-web-backup.sh's — the copies drifted")
else:
    bad(f"T resolver block missing (runner={bool(a)}, backup={bool(b)})")

print("── W. bounded reset control commands ──")
timeout_cases()

print()
if fails:
    print(f"factory-reset-runner: {fails} FAIL")
    sys.exit(1)
if skips:
    print(f"factory-reset-runner: PARTIAL — {skips} assertion(s) not executed")
else:
    print("factory-reset-runner: ALL OK")
