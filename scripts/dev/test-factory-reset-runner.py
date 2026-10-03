#!/usr/bin/env python3
"""test-factory-reset-runner.py — the config-only factory reset
(etc/sa02m-factory-reset-runner.sh) takes the backup it promises, keeps
secrets out of its log, never follows a name somebody else planted, and erases
the HomeKit bridge's pairings and the Home Connect sign-in (Operator decision
Q-F). Quality row `factory-reset-runner`.

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
     put. Observing an owner change needs root — non-root: SKIP, not a pass
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
  T  the runner's trusted-path resolver is byte-identical to the block in
     etc/sa02m-web-backup.sh (a third twin; no root script can import another)
  P  the embedded helper compiles

RED observed 2026-09-27 on the runner at HEAD 485f385 (FACTORY_SRC=<copy>):
recorded in the commit that adds this harness. C1-C7 RED observed 2026-09-28
on the runner at HEAD d7d9c4a (no Home Connect step): see the commit body.
O1 RED observed 2026-09-28 on the runner at HEAD f81855e (pairings erased
before the Home Connect stop was verified); O3 RED 2026-09-28 on the two-phase
runner without the rollback restart: see the commit body.

Run: python3 scripts/dev/test-factory-reset-runner.py   (python3 + bash;
root or not — CI runs non-root; FACTORY_SRC=<file> runs another runner copy)
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FACTORY = Path(os.environ.get("FACTORY_SRC") or ROOT / "etc/sa02m-factory-reset-runner.sh")
BACKUP = ROOT / "etc/sa02m-web-backup.sh"
DEFAULTS = ROOT / "etc/sa02m-factory-defaults"
HK_PKG = ROOT / "opt/sa02m-homekit/sa02m_homekit"
HK_SEED = ROOT / "etc/sa02m-homekit/sa02m-homekit.conf"
HC_PKG = ROOT / "opt/sa02m-homeconnect/sa02m_homeconnect"
if os.name == "nt":
    print("SKIP  factory-reset-runner needs a POSIX host (grp, euid, the retargeted runner)")
    raise SystemExit(77)

IS_ROOT = getattr(os, "geteuid", lambda: 1)() == 0

fails = 0


def ok(msg: str) -> None:
    print("ok    " + msg)


def bad(msg: str) -> None:
    global fails
    fails += 1
    print("FAIL  " + msg)


def skip(msg: str) -> None:
    print("SKIP  " + msg)


def check(cond: bool, good: str, fail: str) -> None:
    (ok if cond else bad)(good if cond else fail)


for p in (FACTORY, BACKUP, DEFAULTS, HK_PKG, HK_SEED, HC_PKG):
    if not p.exists():
        bad(f"missing input {p} (non-vacuity)")
if fails:
    sys.exit(1)

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
    exit 0 ;;
  is-active)
    case "${{2:-}}" in
      sa02m-homeconnect.service) st=$(cat "{sb}/hc-state" 2>/dev/null || echo active) ;;
      *) st=$(cat "{sb}/hk-state" 2>/dev/null || echo active) ;;
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

# ── T. the resolver twin ────────────────────────────────────────────────────
print("── T. trusted-path resolver ──")
pat = re.compile(r"^# >>> trusted-path resolver.*?^# <<< trusted-path resolver$", re.S | re.M)
a, b = pat.search(RUNNER_TXT), pat.search(BACKUP_TXT)
if a and b:
    check(a.group(0) == b.group(0), "T the runner's resolver is byte-identical to etc/sa02m-web-backup.sh's",
          "T the runner's resolver differs from etc/sa02m-web-backup.sh's — the copies drifted")
else:
    bad(f"T resolver block missing (runner={bool(a)}, backup={bool(b)})")

print()
if fails:
    print(f"factory-reset-runner: {fails} FAIL")
    sys.exit(1)
print("factory-reset-runner: ALL OK")
