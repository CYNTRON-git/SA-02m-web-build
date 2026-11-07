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
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "opt/sa02m-port-coordinator"))
WEB = ("nginx.service", "fcgiwrap.service", "fcgiwrap.socket", "fcgiwrap-unix.socket")
UPDATE = tuple("sa02m-" + name + ".service" for name in (
    "update", "update-download", "update-verify", "update-recover", "factory-reset"))
FLASHER_SOCKET = "/run/sa02m-flasher/flasher.sock"
# The recovery journal directory must belong to root. A module constant only so
# the test suite can rebind it to its own uid and run unprivileged.
JOURNAL_OWNER_UID = 0
GATEWAY = "sa02m-serial-gateway.service"
BROKER_DIR = "/opt/sa02m-port-coordinator"
BROKER_RUN = "/run/sa02m-port-coordinator"
BROKER_STATE = "/var/lib/sa02m-port-coordinator"
HANDOFF_SECONDS = 5
UART = (
    "sa02m-flasher.service", GATEWAY, "sa02m-modbus-mqtt.service",
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
        self.gateway_ports = {}
        self.token = "sa02m-install-" + uuid.uuid4().hex

    def save(self):
        if self.journal:
            temporary = self.journal.with_suffix(".tmp")
            with temporary.open("w") as stream:
                json.dump({"saved": self.saved, "token": self.token, "gateway_ports": self.gateway_ports,
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
        if self.gateway_ports:
            try:
                wait_for_gateway(self.gateway_ports)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))
        if self.journal:
            self.journal.unlink(missing_ok=True)
            sync_directory(self.journal.parent)

    def load_recovery(self):
        data = json.loads(self.journal.read_text())
        if not re.fullmatch(r"sa02m-install-[0-9a-f]{32}", data["token"]):
            raise RuntimeError("invalid guard recovery token")
        if set(data["saved"]) != set(self.units):
            raise RuntimeError("invalid guard recovery units")
        self.saved, self.token = data["saved"], data["token"]
        self.installed_infra = data.get("installed_infra", False)
        self.gateway_ports = data.get("gateway_ports", {})
        if (not isinstance(self.gateway_ports, dict)
                or any(not re.fullmatch(r"/dev/COM[1-5]", port)
                       or not isinstance(key, str) or not re.fullmatch(r"[0-9]+-[0-9]+", key)
                       for port, key in self.gateway_ports.items())
                or (self.gateway_ports and (self.saved.get(GATEWAY) or {}).get("ActiveState") != "active")):
            raise RuntimeError("invalid gateway recovery ports")
        self.paths = [self.run / (unit + ".d") / (self.token + ".conf")
                      for unit in self.units]
        self.paths = [path for path in self.paths if path.exists()]

    def recover(self):
        self.load_recovery()
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
    from sa02m_ports.broker import occupants
    managed = {}
    for port in Path("/dev").glob("COM[1-5]"):
        for pid in occupants(port):
            try:
                groups = systemd_groups(pid)
            except FileNotFoundError:
                continue
            candidates = {part for group in groups for part in group.split("/")}.intersection(UART) if allow_managed else ()
            for unit in candidates:
                if unit not in managed:
                    managed[unit] = ctl("show", unit, "-p", "ControlGroup", "--value")
            if not any(managed[u] and (g == managed[u] or g.startswith(managed[u] + "/"))
                       for u in candidates for g in groups):
                raise RuntimeError("UART owner cannot be interrupted: %s PID %d" % (port, pid))


def systemd_groups(pid):
    lines = Path("/proc/%d/cgroup" % pid).read_text().splitlines()
    return {path for hierarchy, controllers, path in (line.split(":", 2) for line in lines)
            if controllers == "name=systemd" or (hierarchy, controllers) == ("0", "")}


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


def locked_file(path, stack, *, shared=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    stream = stack.enter_context(os.fdopen(fd, "a+b"))
    fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    return stream


def broker_idle(timeout=HANDOFF_SECONDS):
    # .31/.32 expose one authoritative, root-only RPC. Only its specific
    # live busy reply admits inspection for a reversible gateway stop.
    probe = """import sys
sys.path.insert(0, sys.argv[1])
from sa02m_ports import PortError, assert_idle_for_maintenance
try:
    result = assert_idle_for_maintenance(sys.argv[2])
except PortError as exc:
    if type(exc) is PortError and str(exc) == 'UART operation or restoration is active':
        sys.exit(75)
    raise
if result != {'ok': True} or result['ok'] is not True:
    raise ValueError('Unexpected maintenance reply')
"""
    reply = subprocess.run([sys.executable, "-I", "-c", probe, str(Path(BROKER_DIR)),
                            str(Path(BROKER_RUN) / "control.sock")], timeout=timeout, capture_output=True, text=True)
    if reply.returncode == 75:
        return False
    if reply.returncode:
        reason = reply.stderr.strip().splitlines()
        raise RuntimeError("coordinator maintenance check failed: " + (reason[-1][:256] if reason else str(reply.returncode)))
    return True


def checked_directory(path, private=False):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != JOURNAL_OWNER_UID
            or info.st_mode & (0o077 if private else 0o022)):
        raise RuntimeError("unsafe coordinator directory: " + str(path))


def gateway_ledger():
    for name in ("/", "/var", "/var/lib", BROKER_STATE):
        checked_directory(Path(name), private=name == BROKER_STATE)
    path = Path(BROKER_STATE) / "state.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != JOURNAL_OWNER_UID
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise RuntimeError("unsafe coordinator ownership journal")
        raw = stream.read(65537)
        if len(raw) > 65536 or not os.path.samestat(info, path.lstat()):
            raise RuntimeError("oversized or replaced coordinator ownership journal")
    state = json.loads(raw)
    if (not isinstance(state, dict) or set(state) != {"leases", "services"}
            or not isinstance(state["services"], dict) or not isinstance(state["leases"], dict)):
        raise RuntimeError("unknown coordinator ownership journal")
    return state


def gateway_identity():
    values = dict(line.split("=", 1) for line in ctl(
        "show", GATEWAY, "-p", "ActiveState", "-p", "MainPID", "-p", "ControlGroup").splitlines())
    pid = int(values["MainPID"])
    group = values["ControlGroup"]
    if values["ActiveState"] != "active" or pid <= 1 or group.split("/")[-1] != GATEWAY:
        raise RuntimeError("gateway systemd identity is unavailable")
    proc = "/proc/%d/" % pid
    if group not in systemd_groups(pid):
        raise RuntimeError("gateway PID is outside the systemd unit")
    uids = [line.split()[1:] for line in Path(proc + "status").read_text().splitlines() if line.startswith("Uid:")]
    if uids != [[str(JOURNAL_OWNER_UID)] * 4]:
        raise RuntimeError("unexpected gateway process owner")
    return pid, Path(proc + "stat").read_text().rsplit(")", 1)[1].split()[19]


def gateway_holds_lock(pid, key):
    # A recycled PID can own the tty without holding the broker's transferred
    # open-file description. Linux reports that description's flock in fdinfo.
    inode = (Path(BROKER_RUN) / "locks" / (key + ".lock")).lstat()
    if not stat.S_ISREG(inode.st_mode) or inode.st_uid != JOURNAL_OWNER_UID or inode.st_mode & 0o077:
        raise RuntimeError("unsafe coordinator UART lock")
    for fd in Path("/proc/%d/fd" % pid).iterdir():
        try:
            if os.path.samestat(inode, fd.stat()):
                info = Path("/proc/%d/fdinfo/%s" % (pid, fd.name)).read_text()
                if re.search(r"^lock:\s+\d+: FLOCK\s+ADVISORY\s+WRITE\s", info, re.MULTILINE):
                    return True
        except FileNotFoundError:
            continue
    return False


def gateway_lines(expected=None):
    """Prove a gateway-only before-image, or verify the saved lines after startup."""
    from sa02m_ports.broker import ALLOWED_SERVICES, device_key, occupants
    state = gateway_ledger()
    if expected is None and (state["services"] or not state["leases"]):
        raise RuntimeError("UART operation or restoration is active")
    identity = gateway_identity()
    lines = {}
    for token, lease in state["leases"].items():
        if not isinstance(lease, dict):
            raise RuntimeError("invalid coordinator lease")
        if expected is not None and lease.get("port") not in expected:
            continue
        if (not re.fullmatch(r"[0-9a-f]{32}", token)
                or set(lease) != {"id", "port", "key", "purpose", "pid", "services", "phase", "error"}
                or lease["id"] != token or lease["purpose"] != "gateway" or lease["phase"] != "owned"
                or lease["error"] != "" or type(lease["pid"]) is not int or lease["pid"] != identity[0]
                or not isinstance(lease["services"], list) or any(s not in ALLOWED_SERVICES for s in lease["services"])
                or not isinstance(lease["port"], str) or not re.fullmatch(r"/dev/COM[1-5]", lease["port"])):
            raise RuntimeError("UART lease is not an owned managed gateway line")
        port = lease["port"]
        device = Path(port)
        key = device_key(device)
        if (key != lease["key"] or port in lines or key in lines.values()
                or occupants(device) != [identity[0]] or not gateway_holds_lock(identity[0], key)):
            raise RuntimeError("gateway UART identity or exclusive owner changed: " + port)
        lines[port] = key
    if gateway_identity() != identity or (expected is not None and lines != expected):
        raise RuntimeError("gateway did not own the expected UART lines")
    return lines, identity


def check_service_locks():
    from sa02m_ports.locks import open_locked
    directory = Path(BROKER_RUN) / "locks"
    for path in (Path("/run"), Path(BROKER_RUN), directory):
        checked_directory(path)
    for path in directory.iterdir():
        if path.match("service-*.lock"):
            os.close(open_locked(path, shared=True))


def wait_for_gateway(expected):
    deadline = time.monotonic() + HANDOFF_SECONDS
    while True:
        try:
            gateway_lines(expected)
            return
        except (RuntimeError, OSError) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError("gateway did not restore working UART lines: " + ", ".join(expected)) from exc
            time.sleep(.1)


def wait_for_idle():
    deadline = time.monotonic() + HANDOFF_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            if broker_idle(remaining):
                return
        except subprocess.TimeoutExpired:
            break
        time.sleep(.1)
    raise RuntimeError("gateway did not release UART or restoration is pending")


def admitted_gateway():
    """Classify the installed broker under maintenance EX. A stale ledger cannot admit."""
    if (Path(BROKER_DIR) / "sa02m_ports").is_dir():
        if not broker_idle():
            check_service_locks()
            uart_owners(True)
            return gateway_lines()
    elif os.path.lexists(Path(BROKER_STATE) / "state.json"):
        raise RuntimeError("coordinator journal exists without installed broker; explicit recovery required")
    return {}, None


def recover_installation(consumers, web, gate_lock, locks):
    if consumers.journal.exists():
        locked_file(Path("/var/lib/sa02m-web-build/launch.lock"), locks)
        locked_file(Path("/var/lib/sa02m-update/control.lock"), locks)
    with contextlib.ExitStack() as admission:
        locked_file(gate_lock, admission)
        legacy_idle()
        if consumers.journal.exists():
            consumers.load_recovery()
            flasher_idle()
            uart_owners(True)
            live, _ = admitted_gateway()
            if consumers.gateway_ports and not (Path(BROKER_DIR) / "sa02m_ports").is_dir():
                raise RuntimeError("coordinator unavailable during gateway recovery")
            if consumers.gateway_ports and live != consumers.gateway_ports:
                state = snapshot(GATEWAY)
                if state and state["ActiveState"] == "active":
                    gateway_identity()
                    consumers.stop((GATEWAY,))
                    wait_for_idle()
    if consumers.journal.exists():
        consumers.close()
    if web.journal.exists():
        web.recover()
    if web.installed_infra:
        web_health()


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
        gate_lock = Path(BROKER_RUN) / "maintenance.lock"
        if argv == ["--recover"]:
            recover_installation(consumers, web, gate_lock, locks)
            return 0
        if web.journal.exists() or consumers.journal.exists():
            raise RuntimeError("unfinished bootstrap; run scripts/install-maintenance.py --recover")
        successful = [False]
        stack.callback(lambda: web_health() if successful[0] and not web.journal.exists() else None)
        # Register cleanup before any guard write, including partial failures.
        stack.callback(lambda: web.close() if not consumers.journal.exists() else None)
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
        gate = locked_file(gate_lock, stack)
        legacy_idle()
        flasher_idle()
        consumers.gateway_ports, live_gateway = admitted_gateway()
        # On every outcome, open admission before a saved consumer needs its lease.
        # The install and update locks and the web fence still cover this handoff.
        def restore_consumers():
            gate.close()
            # An orphan still sharing the child's EX descriptor must finish
            # first. Keep the before-image and the web fence for --recover.
            with contextlib.ExitStack() as probe:
                locked_file(gate_lock, probe, shared=True)
            consumers.close()
        stack.callback(restore_consumers)
        consumers.enter()
        if live_gateway:
            if gateway_lines() != (consumers.gateway_ports, live_gateway):
                raise RuntimeError("gateway ownership changed before stop")
            consumers.stop((GATEWAY,))
            wait_for_idle()
        consumers.stop(tuple(unit for unit in UART if not live_gateway or unit != GATEWAY))
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
