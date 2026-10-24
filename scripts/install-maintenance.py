#!/usr/bin/env python3
"""Quiesce an old installation before refresh; restore operator service state."""
import contextlib
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
WEB = ("nginx.service", "fcgiwrap.service", "fcgiwrap.socket", "fcgiwrap-unix.socket")
UPDATE = tuple("sa02m-" + name + ".service" for name in (
    "update", "update-download", "update-verify", "update-recover", "factory-reset"))
FLASHER_SOCKET = "/run/sa02m-flasher/flasher.sock"
# The recovery journal directory must belong to root. A module constant only so
# the test suite can rebind it to its own uid and run unprivileged.
JOURNAL_OWNER_UID = 0
UART = (
    "sa02m-flasher.service", "sa02m-serial-gateway.service", "sa02m-modbus-mqtt.service",
    "mplc.service", "mplc4.service", "codesys.service", "codesyscontrol.service",
    "CODESYSControl.service", "CODESYSControlRuntime.service", "vplc.service",
    "sa02m-vplc.service", "sa02m-test-trigger.service", "sa02m-test-stand.service",
)


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ctl(*args):
    result = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError("systemctl %s: %s" % (" ".join(args), result.stderr.strip()))
    return result.stdout.strip()


def snapshot(unit):
    text = ctl("show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState")
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if values.get("LoadState") == "not-found":
        return None
    if values.get("ActiveState") not in {"active", "inactive", "failed"}:
        raise RuntimeError("unstable service state: " + unit)
    if values.get("UnitFileState") not in {
        "enabled", "enabled-runtime", "disabled", "masked", "masked-runtime",
        "static", "indirect", "generated", "alias",
    }:
        raise RuntimeError("unknown autostart state: " + unit)
    return values


class Guard:
    """An unmet systemd condition survives installer unmask/start/restart calls."""
    def __init__(self, units, run=Path("/run/systemd/system"), journal=None):
        self.units, self.run = units, run
        self.journal = journal
        self.saved, self.paths = {}, []
        self.installed_infra = False
        self.token = "sa02m-install-" + uuid.uuid4().hex

    def save(self):
        if self.journal:
            temporary = self.journal.with_suffix(".tmp")
            with temporary.open("w") as stream:
                json.dump({"saved": self.saved, "token": self.token,
                           "installed_infra": self.installed_infra}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.journal)
            sync_directory(self.journal.parent)

    def use_installed_infra(self):
        # The web module intentionally retires the distro's old socket units.
        # Preserve its infra policy, rather than resurrecting an obsolete listener.
        self.installed_infra = True
        self.save()

    def enter(self):
        # Capture everything before the first stop, including active+disabled.
        self.saved = {unit: snapshot(unit) for unit in self.units}
        self.save()
        for unit in self.units:
            if self.saved[unit] is None and unit in UART:
                continue  # new consumers start only after the new broker is ready
            directory = self.run / (unit + ".d")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (self.token + ".conf")
            with path.open("x") as stream:
                stream.write("[Unit]\nConditionPathExists=/run/%s-permit\n" % self.token)
            self.paths.append(path)
        ctl("daemon-reload")

    def stop(self, units):
        for unit in units:
            if self.saved.get(unit) is not None:
                ctl("stop", unit)
                if ctl("show", unit, "-p", "ActiveState", "--value") not in {"inactive", "failed"}:
                    raise RuntimeError("cannot quiesce " + unit)

    def close(self):
        errors = []
        for path in self.paths:
            try:
                path.unlink()
                with contextlib.suppress(OSError):
                    path.parent.rmdir()
            except OSError as exc:
                errors.append(str(exc))
        try:
            ctl("daemon-reload")
        except Exception as exc:
            errors.append(str(exc))
        # Web comes last: old CGI is never reachable during consumer restoration.
        for unit in sorted(self.saved, key=lambda name: name in WEB):
            saved = self.saved[unit]
            if self.installed_infra and unit in WEB:
                try:
                    current = snapshot(unit)
                    if current is not None:
                        en = current["UnitFileState"]
                        active = not en.startswith("masked") and (
                            (unit in {"nginx.service", "fcgiwrap.service"} and en.startswith("enabled"))
                            or (unit.endswith(".socket") and en.startswith("enabled")
                                and saved is not None and saved["ActiveState"] == "active")
                        )
                        ctl("start" if active else "stop", unit)
                        actual = snapshot(unit)
                        if (actual["ActiveState"] == "active") != active or actual["UnitFileState"] != en:
                            raise RuntimeError("infra restoration failed: " + unit)
                except Exception as exc:
                    errors.append(str(exc))
                continue
            if saved is None:
                if unit in {"nginx.service", "fcgiwrap.service"} and snapshot(unit):
                    ctl("start", unit)
                continue
            try:
                current = snapshot(unit)
                en = saved["UnitFileState"]
                if current is None:
                    raise RuntimeError("installed unit disappeared: " + unit)
                if current["UnitFileState"] != en:
                    if en in {"enabled", "disabled", "enabled-runtime", "masked", "masked-runtime"}:
                        ctl("unmask", unit)
                        verb = "mask" if en.startswith("masked") else ("enable" if en.startswith("enabled") else "disable")
                        ctl(verb, *(["--runtime"] if en.endswith("-runtime") else []), unit)
                    else:
                        # static/generated/indirect/alias carry no operator decision
                        # (nothing to enable or disable); an installer that lands a
                        # real unit file over one decides its autostart. Raising here
                        # kept the journal and made --recover raise forever.
                        print("install maintenance: %s was %s, now %s (installer decision kept)"
                              % (unit, en, current["UnitFileState"]), file=sys.stderr)
                        en = current["UnitFileState"]
                if saved["ActiveState"] == "active":
                    # A masked active unit must be temporarily unmasked to restart.
                    if en.startswith("masked"):
                        ctl("unmask", *(["--runtime"] if en.endswith("-runtime") else []), unit)
                    try:
                        ctl("start", unit)
                    finally:
                        if en.startswith("masked"):
                            ctl("mask", *(["--runtime"] if en.endswith("-runtime") else []), unit)
                else:
                    ctl("stop", unit)
                actual = snapshot(unit)
                if actual["UnitFileState"] != en or (
                    actual["ActiveState"] == "active") != (saved["ActiveState"] == "active"):
                    raise RuntimeError("service state restoration failed: " + unit)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))
        if self.journal:
            self.journal.unlink(missing_ok=True)
            sync_directory(self.journal.parent)

    def recover(self):
        data = json.loads(self.journal.read_text())
        if not re.fullmatch(r"sa02m-install-[0-9a-f]{32}", data["token"]):
            raise RuntimeError("invalid guard recovery token")
        if set(data["saved"]) != set(self.units):
            raise RuntimeError("invalid guard recovery units")
        self.saved, self.token = data["saved"], data["token"]
        self.installed_infra = data.get("installed_infra", False)
        self.paths = [self.run / (unit + ".d") / (self.token + ".conf")
                      for unit in self.units]
        self.paths = [path for path in self.paths if path.exists()]
        self.close()


def flasher_idle():
    # /health is the daemon's only unauthenticated route (docs/contracts/
    # flasher-health.md): every other one, /jobs included, answers 401 to a
    # caller without a panel session, which root has none of. poll_locked is
    # the daemon's own union of the on-disk flock and its active jobs, so it is
    # the whole "line busy" fact. Fail closed: an unreadable answer or a missing
    # field (an older daemon) is busy; a daemon that is not running holds no
    # lease and is not probed.
    state = snapshot("sa02m-flasher.service")
    if not state or state["ActiveState"] != "active":
        return
    connection = http.client.HTTPConnection("localhost", timeout=5)
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.settimeout(5)
    try:
        connection.sock.connect(FLASHER_SOCKET)
        connection.request("GET", "/health")
        reply = connection.getresponse()
        body = reply.read(65537)
        health = None
        if reply.status == 200 and len(body) <= 65536:
            try:
                health = json.loads(body)
            except ValueError:
                health = None
        if not isinstance(health, dict) or not isinstance(health.get("poll_locked"), bool):
            raise RuntimeError("cannot read flasher health (HTTP %s); the RS-485 line counts as busy" % reply.status)
        if health["poll_locked"]:
            raise RuntimeError("flasher operation pending/running; finish it before refresh")
    finally:
        connection.close()


def uart_owners(allow_managed):
    sys.path.insert(0, str(ROOT / "opt/sa02m-port-coordinator"))
    from sa02m_ports.broker import occupants
    for port in Path("/dev").glob("COM[1-5]"):
        for pid in occupants(port):
            try:
                cgroup = Path("/proc/%d/cgroup" % pid).read_text()
            except FileNotFoundError:
                continue
            groups = {part for line in cgroup.splitlines() for part in line.split("/")}
            if not allow_managed or not set(UART).intersection(groups):
                raise RuntimeError("UART owner cannot be interrupted: %s PID %d" % (port, pid))


def legacy_idle():
    # The legacy launcher can run outside systemd; the marker is not proof of life.
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        if any(Path(os.fsdecode(arg)).name in {
            "sa02m-web-update-apply", "sa02m-update-runner", "sa02m-update-runner.sh",
            "sa02m-factory-reset-runner", "sa02m-factory-reset-runner.sh", "mqtt_bus_scan.py"
        } for arg in argv if arg):
            raise RuntimeError("existing update process: " + proc.name)


def locked_file(path, stack):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    stream = stack.enter_context(os.fdopen(fd, "a+b"))
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return stream


def web_health():
    for unit in ("nginx.service", "fcgiwrap.service"):
        state = snapshot(unit)
        if not state or state["ActiveState"] != "active":
            raise RuntimeError("web health: inactive " + unit)
    for unit in ("fcgiwrap.socket", "fcgiwrap-unix.socket"):
        state = snapshot(unit)
        if state and state["ActiveState"] == "active":
            raise RuntimeError("web health: obsolete socket still active: " + unit)
    subprocess.run(["nginx", "-t"], check=True, timeout=20)
    port = subprocess.check_output(
        ["bash", "-c", '. "$1"; sa02m_web_port', "port",
         str(ROOT / "etc/sa02m-web-port-lib.sh")], text=True, timeout=5).strip()
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise RuntimeError("web health: invalid panel port")
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=10)
    try:
        connection.request("GET", "/login.html")
        reply = connection.getresponse()
        body = reply.read(1048577)
        if reply.status != 200 or not body or len(body) > 1048576:
            raise RuntimeError("web health: login page unavailable")
        connection.request("GET", "/cgi-bin/status.cgi?part=priority")
        reply = connection.getresponse()
        body = reply.read(65537)
        if reply.status != 200 or len(body) > 65536 or json.loads(body).get("error") != "unauthorized":
            raise RuntimeError("web health: unauthenticated CGI guard unavailable")
    finally:
        connection.close()


def run_installer(argv):
    if os.geteuid() != 0:
        raise RuntimeError("root required")
    if argv and argv[0] == "--require-coordinator":
        argv = argv[1:]
        if not Path("/opt/sa02m-port-coordinator/sa02m_ports").is_dir():
            raise RuntimeError("E_BOOTSTRAP_REQUIRED: run install.sh --refresh first")
    if os.environ.get("SA02M_ROOTFS_BUILD"):
        return subprocess.call(["bash", *argv], env={**os.environ, "SA02M_INSTALL_GUARDED": "1"})
    journal = Path("/var/lib/sa02m-install-maintenance")
    journal.mkdir(mode=0o700, exist_ok=True)
    st = journal.lstat()
    if journal.is_symlink() or st.st_uid != JOURNAL_OWNER_UID or st.st_mode & 0o077:
        raise RuntimeError("unsafe install-maintenance journal directory")
    web = Guard(WEB, journal=journal / "web.json")
    consumers = Guard(UART + UPDATE, journal=journal / "uart.json")
    with contextlib.ExitStack() as locks, contextlib.ExitStack() as stack:
        install_lock = locked_file(Path("/run/sa02m-install.lock"), locks)
        if argv == ["--recover"]:
            locked_file(Path("/run/sa02m-port-coordinator/maintenance.lock"), locks)
            legacy_idle()
            for guard in (consumers, web):
                if guard.journal.exists():
                    guard.recover()
            if web.installed_infra:
                web_health()
            return 0
        if web.journal.exists() or consumers.journal.exists():
            raise RuntimeError("unfinished bootstrap; run scripts/install-maintenance.py --recover")
        successful = [False]
        stack.callback(lambda: web_health() if successful[0] else None)
        # Register cleanup before any guard write, including partial failures.
        stack.callback(web.close)
        web.enter()
        web.stop(WEB)
        legacy_idle()
        # Old downloaders used a separate lock before entering the shared runner.
        locked_file(Path("/var/lib/sa02m-web-build/launch.lock"), locks)
        flasher_idle()
        uart_owners(True)
        # The migration owns the insecure-state quarantine policy.
        subprocess.run(["bash", str(ROOT / "etc/sa02m-update-state-migrate.sh")], check=True)
        locked_file(Path("/var/lib/sa02m-update/control.lock"), locks)
        gate = locked_file(Path("/run/sa02m-port-coordinator/maintenance.lock"), locks)
        if Path("/opt/sa02m-port-coordinator/sa02m_ports").is_dir():
            subprocess.run([sys.executable, "-I", "-c",
                            "import sys; sys.path.insert(0,'/opt/sa02m-port-coordinator'); "
                            "from sa02m_ports import assert_idle_for_maintenance; assert_idle_for_maintenance()"],
                           check=True)
        elif Path("/var/lib/sa02m-port-coordinator/state.json").exists() or Path(
            "/var/lib/sa02m-port-coordinator/state.json"
        ).is_symlink():
            raise RuntimeError("coordinator journal exists without installed broker; explicit recovery required")
        stack.callback(consumers.close)
        consumers.enter()
        consumers.stop(UART)
        uart_owners(False)
        web.use_installed_infra()
        env = {**os.environ, "SA02M_INSTALL_GUARDED": "1", "SA02M_PORT_MAINTENANCE_FD": str(gate.fileno())}
        child = subprocess.Popen(["bash", *argv], env=env,
                                 pass_fds=(gate.fileno(), install_lock.fileno()), start_new_session=True)
        try:
            rc = child.wait()
            successful[0] = rc == 0
            return rc
        except BaseException:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            raise


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt("installer interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        raise SystemExit(run_installer(sys.argv[1:]))
    except (Exception, KeyboardInterrupt) as exc:
        print("install maintenance refused/failed: %s" % exc, file=sys.stderr)
        raise SystemExit(1)
