"""Root broker: durable pause obligations and one owner per physical UART."""

import array
import fcntl
import grp
import json
import logging
import os
from pathlib import Path
import pwd
import re
import socket
import struct
import subprocess
import threading
import time
import uuid

from .client import LOCK_DIR, SOCKET, PortError, device_key, lock_device
from . import locks
from .locks import open_locked, provision, secure_directory

log = logging.getLogger(__name__)
MQTT_UNIT = "sa02m-modbus-mqtt.service"
MQTT_PAUSE_SOCKET = "/run/sa02m-modbus-mqtt/scan-lease.sock"
ALLOWED_SERVICES = frozenset({
    "mplc.service", "mplc4.service", "codesys.service", "vplc.service",
    "codesyscontrol.service", "CODESYSControl.service", "CODESYSControlRuntime.service",
    "sa02m-vplc.service", "sa02m-test-trigger.service", "sa02m-test-stand.service",
})
PURPOSES = frozenset({"operation", "flasher", "scan", "gateway"})


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    os.fchmod(fd, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        raise


def sd_notify(state):
    """READY=1 to systemd over $NOTIFY_SOCKET with the stdlib only.

    Sent once the control socket listens, so `After=` on the consumers waits
    for a broker that can answer, not merely for its fork (Type=notify).
    Absent or unreachable socket: a hand run, nothing to tell.
    """
    path = os.environ.get("NOTIFY_SOCKET", "")
    if not path:
        return False
    if path.startswith("@"):
        path = "\0" + path[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(path)
        sock.sendall(state.encode())
        return True
    except OSError:
        log.warning("sd_notify(%s) failed", state, exc_info=True)
        return False
    finally:
        sock.close()


def snapshot_document(state, config, pid, start_ticks):
    """The public status.json layout — a fixed top-level key order.

    status.cgi projects this file in bash without a JSON parser and relies on
    exactly these keys in exactly this order (scripts/dev/
    test-status-coordinator-projection.py pins both ends).
    """
    return {
        "timestamp": time.time(), "leases": list(state["leases"].values()),
        "services": state["services"], "configured_scopes": config["ports"],
        "broker_pid": pid, "broker_start_ticks": start_ticks,
    }


# None in the product: every process is scanned, and one whose descriptors
# cannot be read makes the device unsafe (the PermissionError propagates).
# A test suite run by an unprivileged user cannot read the /proc/<pid>/fd of
# other users — nor of its own uid's processes in another sandbox or user
# namespace (a browser's renderers) — so it names its own pid here and only that
# process and its descendants are scanned (tests/test_process_ownership.py):
# every process that can hold its PTYs is one of them. Not its session: a CI
# runner or `runuser`/`sudo` keeps a root ancestor in the session of the test
# it launched, and that ancestor's descriptors are unreadable. Inside the scope
# an unreadable process still refuses.
OCCUPANT_SCAN_ROOT = None


def _parent(entry):
    try:
        return int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
    except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
        return None


def _process_tree(entries, root):
    children = {}
    for entry in entries:
        children.setdefault(_parent(entry), []).append(int(entry.name))
    tree, pending = set(), [root]
    while pending:
        pid = pending.pop()
        if pid not in tree:
            tree.add(pid)
            pending.extend(children.get(pid, ()))
    return tree


def occupants(device):
    """Read actual character-device descriptors; an unreadable process is unsafe."""
    target = os.stat(device).st_rdev
    found = []
    entries = [entry for entry in Path("/proc").iterdir() if entry.name.isdigit()]
    scope = None if OCCUPANT_SCAN_ROOT is None else _process_tree(entries, OCCUPANT_SCAN_ROOT)
    for entry in entries:
        try:
            if scope is not None and int(entry.name) not in scope:
                continue
            for fd in (entry / "fd").iterdir():
                try:
                    info = fd.stat()
                    if info.st_rdev == target and info.st_rdev:
                        found.append(int(entry.name))
                        break
                except FileNotFoundError:
                    pass
        except (FileNotFoundError, ProcessLookupError):
            pass
    return found


class System:
    # The bridge binds its pause socket only after its MQTT connect and port
    # threads; at boot a lease can arrive before that. Bounded wait, re-checking
    # the unit each round so a bridge that stopped meanwhile needs no pause.
    MQTT_PAUSE_RETRIES = 10
    MQTT_PAUSE_RETRY_DELAY = 0.5

    def _show(self, unit, prop):
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=" + prop, "--value"],
            capture_output=True, text=True, timeout=10, check=True)
        return result.stdout.strip()

    def canonical(self, unit):
        if unit not in ALLOWED_SERVICES | {MQTT_UNIT}:
            raise PortError("Service is not permitted")
        name = self._show(unit, "Id")
        if name not in ALLOWED_SERVICES | {MQTT_UNIT}:
            raise PortError("Unrecognized canonical service: " + unit)
        return name

    def installed(self, unit):
        """False when systemd has no unit file for the name (LoadState=not-found)."""
        return self._show(unit, "LoadState") != "not-found"

    def _state(self, unit):
        state = self._show(unit, "ActiveState")
        if state not in {"active", "inactive", "failed"}:
            return "changing"
        return state

    def active(self, unit):
        state = self._state(unit)
        if state == "changing":
            raise PortError("Service is changing state: " + unit)
        return state == "active"

    def stop(self, unit):
        subprocess.run(["systemctl", "stop", unit], check=True, timeout=25,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.active(unit):
            raise PortError("Service did not stop: " + unit)

    def start(self, unit):
        subprocess.run(["systemctl", "start", unit], check=True, timeout=25,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not self.active(unit):
            raise PortError("Service did not become active: " + unit)

    def mqtt_pause(self, port, path=MQTT_PAUSE_SOCKET):
        for attempt in range(self.MQTT_PAUSE_RETRIES + 1):
            state = self._state(MQTT_UNIT)
            if state != "active":
                if state != "changing":
                    return None
                conn = None
            else:
                conn = socket.socket(socket.AF_UNIX)
                conn.settimeout(20)
                try:
                    conn.connect(path)
                except (FileNotFoundError, ConnectionRefusedError):
                    conn.close()
                    conn = None
            if conn is not None:
                break
            if attempt >= self.MQTT_PAUSE_RETRIES:
                raise PortError("MQTT pause socket is not ready" if state == "active"
                                else "Service is changing state: " + MQTT_UNIT)
            time.sleep(self.MQTT_PAUSE_RETRY_DELAY)
        try:
            conn.sendall(json.dumps({"via": "com", "port": port}).encode() + b"\n")
            data = b""
            while b"\n" not in data and len(data) < 4096:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            if not json.loads(data).get("ok"):
                raise PortError("MQTT did not acknowledge port pause")
            return conn
        except BaseException:
            conn.close()
            raise


class Broker:
    def __init__(self, config, state_dir="/var/lib/sa02m-port-coordinator",
                 runtime="/run/sa02m-port-coordinator", system=None):
        self.config = config
        self.runtime = Path(runtime)
        self.state_dir = Path(state_dir)
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_dir = self.runtime / "locks"
        secure_directory(self.lock_dir)
        self.journal = self.state_dir / "state.json"
        self.system = system or System()
        self.mutex = threading.RLock()
        self.fds = {}
        self.holds = {}
        self.connected = set()
        self.last_retry = 0
        self.state = {"leases": {}, "services": {}}
        if self.journal.exists():
            self.state = json.loads(self.journal.read_text())
            if set(self.state) != {"leases", "services"}:
                raise PortError("Invalid ownership journal; refusing to start")
        self.validate_config()
        self.validate_installed()
        self.canonicalize()
        self.validate_state()
        for port in self.config["ports"]:
            if Path(port).exists():
                provision(self.lock_dir / (device_key(port) + ".lock"))
        self.save()

    def canonicalize(self):
        """Collapse aliases before memberships or restoration keys are persisted."""
        def names(values):
            return sorted({self.system.canonical(unit) for unit in values})
        self.config = {"ports": {port: names(units) for port, units in self.config["ports"].items()}}
        restored = {}
        for name, obligation in self.state["services"].items():
            canonical = self.system.canonical(name)
            if canonical in restored and restored[canonical] != obligation:
                raise PortError("Ambiguous aliased restoration journal")
            restored[canonical] = obligation
        self.state["services"] = restored
        for lease in self.state["leases"].values():
            lease["services"] = names(lease["services"])

    def validate_state(self):
        if not isinstance(self.state["leases"], dict) or not isinstance(self.state["services"], dict):
            raise PortError("Invalid ownership journal")
        if any(unit not in ALLOWED_SERVICES for unit in self.state["services"]):
            raise PortError("Invalid restoration service")
        for lease in self.state["leases"].values():
            if (not isinstance(lease, dict) or lease.get("port") not in self.config["ports"]
                    or not isinstance(lease.get("key"), str)
                    or not re.fullmatch(r"[0-9]+-[0-9]+", lease["key"])
                    or any(s not in ALLOWED_SERVICES for s in lease.get("services", []))):
                raise PortError("Invalid saved UART lease")

    def validate_config(self):
        if set(self.config) != {"ports"} or not isinstance(self.config["ports"], dict):
            raise PortError("Invalid port configuration")
        for port, services in self.config["ports"].items():
            if not re.fullmatch(r"/dev/COM[1-5]", port):
                raise PortError("Only COM1 through COM5 can be configured")
            if not isinstance(services, list) or any(s not in ALLOWED_SERVICES for s in services):
                raise PortError("Service is not in the fixed pause allowlist")

    def validate_installed(self):
        """Fail closed on a scope naming a unit this board does not have.

        Such a mapping would read `inactive` forever and protect nothing while
        looking configured; refusing to start surfaces it as
        `coordinator_unavailable` with the unit named in the journal.
        """
        for services in self.config["ports"].values():
            for unit in services:
                if not self.system.installed(unit):
                    raise PortError("Configured pause service is not installed: " + unit)

    def resolve(self, port):
        if not isinstance(port, str) or port not in self.config["ports"]:
            raise PortError("Port is not configured")
        key = device_key(port)
        provision(self.lock_dir / (key + ".lock"))
        return key

    def save(self):
        try:
            atomic_json(self.journal, self.state)
            atomic_json(self.runtime / "status.json", snapshot_document(
                self.state, self.config, os.getpid(),
                Path("/proc/self/stat").read_text().split(") ", 1)[1].split()[19]), 0o644)
        except OSError:
            # Never accept operations with a memory-only pause or an old public
            # snapshot. Transferred client locks survive the broker process.
            log.critical("Cannot persist UART ownership", exc_info=True)
            os._exit(1)

    def poll(self, port):
        key = self.resolve(port)
        # Do not take mutex: pause acknowledgement can be waiting for a poller
        # currently checking this intent under its own serial mutex.
        if any(v["key"] == key for v in list(self.state["leases"].values())):
            raise PortError("UART paused by operation")
        return {"ok": True}

    def acquire(self, port, purpose, pid):
        gate = os.open(self.runtime / "maintenance.lock",
                       os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(gate, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PortError("Update maintenance in progress") from exc
            with self.mutex:
                self.resolve(port)
                service_fds = []
                try:
                    for unit in sorted(set(self.config["ports"][port]) | {"sa02m-modbus-mqtt.service"}):
                        path = self.service_lock_path(unit)
                        service_fds.append(open_locked(path, shared=True))
                    return self._acquire(port, purpose, pid)
                except BlockingIOError as exc:
                    raise PortError("Service mutation in progress") from exc
                finally:
                    for fd in service_fds:
                        os.close(fd)
        finally:
            os.close(gate)

    def service_lock_path(self, unit):
        path = self.lock_dir / ("service-" + unit + ".lock")
        provision(path)
        return path

    def service_mutation(self, unit):
        """Root helper holds returned fd through every actual mutation."""
        canonical = self.system.canonical(unit)
        gate = os.open(self.runtime / "maintenance.lock",
                       os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(gate, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if Path("/var/lib/sa02m-update/maintenance").exists():
                raise PortError("Update recovery is pending")
            with self.mutex:
                if canonical in self.state["services"] or any(
                        canonical in lease["services"] or canonical == "sa02m-modbus-mqtt.service"
                        for lease in self.state["leases"].values()):
                    raise PortError("Service is reserved by a UART operation or restoration")
                fd = open_locked(self.service_lock_path(canonical))
                return fd, canonical
        finally:
            os.close(gate)

    def assert_idle(self):
        if self.state["leases"] or self.state["services"]:
            raise PortError("UART operation or restoration is active")
        for path in self.lock_dir.glob("service-*.lock"):
            fd = open_locked(path, shared=True)
            os.close(fd)

    def _acquire(self, port, purpose, pid):
        if Path("/var/lib/sa02m-update/maintenance").exists():
            raise PortError("Update recovery is pending")
        if purpose not in PURPOSES:
            raise PortError("Invalid operation purpose")
        key = self.resolve(port)
        with self.mutex:
            if any(v["key"] == key for v in self.state["leases"].values()):
                raise PortError("UART already leased")
            token = uuid.uuid4().hex
            self.state["leases"][token] = {
                "id": token, "port": port, "key": key, "purpose": purpose,
                "pid": pid, "services": list(self.config["ports"][port]),
                "phase": "preparing", "error": "",
            }
            self.connected.add(token)
            # Persist membership BEFORE any stop, including services currently
            # inactive because an operation on another port already stopped them.
            self.save()
            try:
                self.holds[token] = self.system.mqtt_pause(port)
                for unit in self.config["ports"][port]:
                    if unit in self.state["services"]:
                        continue
                    if self.system.active(unit):
                        self.state["services"][unit] = {
                            "restore": True, "phase": "stopping", "error": ""}
                        self.save()
                        self.system.stop(unit)
                        self.state["services"][unit]["phase"] = "paused"
                        self.save()
                deadline = time.monotonic() + 5
                while True:
                    if not occupants(port):
                        try:
                            self.fds[token] = lock_device(port, str(self.lock_dir))
                            break
                        except BlockingIOError:
                            pass
                    if time.monotonic() >= deadline:
                        raise PortError("UART has an unknown or uncooperative owner")
                    time.sleep(0.05)
                # No managed opener can pass the lock after this point.
                if occupants(port):
                    raise PortError("UART owner appeared during reservation")
                self.state["leases"][token]["phase"] = "owned"
                self.save()
                return token, self.fds[token], {
                    "ok": True, "lease": self.state["leases"][token]}
            except BaseException as exc:
                self.state["leases"][token]["error"] = str(exc)
                self.release(token)
                raise

    def release(self, token):
        with self.mutex:
            scope = self.state["leases"].get(token, {}).get("services", [])
            self.connected.discard(token)
            fd = self.fds.pop(token, None)
            if fd is not None:
                os.close(fd)
            if token in self.state["leases"]:
                self.state["leases"][token]["phase"] = "recovering"
                self.save()
            self.recover(force=True)
            return {"ok": True, "pending_restore": bool(
                token in self.state["leases"] or
                any(self.state["services"].get(unit, {}).get("error") for unit in scope))}

    def recover(self, force=False):
        with self.mutex:
            now = time.monotonic()
            if not force and now - self.last_retry < 5:
                return
            self.last_retry = now
            for token, lease in list(self.state["leases"].items()):
                if token in self.connected:
                    continue
                try:
                    # Passed lock fds protect surviving clients after broker
                    # death; descriptors protect external child flash tools.
                    if device_key(lease["port"]) != lease["key"]:
                        lease["error"] = "UART mapping changed during recovery"
                        self.save()
                        continue
                    if occupants(lease["port"]):
                        continue
                    fd = lock_device(lease["port"], str(self.lock_dir))
                except (OSError, PortError):
                    continue
                os.close(fd)
                del self.state["leases"][token]
                self.save()
                hold = self.holds.pop(token, None)
                if hold:
                    hold.close()
            for unit, obligation in list(self.state["services"].items()):
                if any(unit in v["services"] for v in self.state["leases"].values()):
                    continue
                try:
                    if not self.system.active(unit):
                        self.system.start(unit)
                    del self.state["services"][unit]
                except Exception as exc:
                    obligation["phase"] = "restore_failed"
                    obligation["error"] = str(exc)
                self.save()


def read_request(conn):
    data = b""
    while b"\n" not in data:
        if len(data) >= 2048:
            raise PortError("Request too large")
        chunk = conn.recv(2048 - len(data))
        if not chunk:
            return None
        data += chunk
    return json.loads(data.split(b"\n", 1)[0])


def reply(conn, value, fd=None):
    data = json.dumps(value).encode() + b"\n"
    ancillary = [] if fd is None else [
        (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))]
    conn.sendmsg([data], ancillary)


def handle(broker, conn, allowed_uids, admin_uids=frozenset({0})):
    token = None
    try:
        pid, uid, gid = struct.unpack("3i", conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid not in allowed_uids:
            raise PortError("Peer is not permitted")
        conn.settimeout(10)
        req = read_request(conn)
        if not isinstance(req, dict):
            raise PortError("Invalid request")
        if req.get("op") == "poll":
            reply(conn, broker.poll(req.get("port")))
            return
        if req.get("op") == "service-mutation" and uid in admin_uids:
            fd, unit = broker.service_mutation(req.get("unit"))
            try:
                reply(conn, {"ok": True, "unit": unit}, fd)
            finally:
                os.close(fd)
            return
        if req.get("op") == "maintenance" and uid in admin_uids:
            with broker.mutex:
                broker.assert_idle()
                reply(conn, {"ok": True})
            return
        if req.get("op") != "acquire":
            raise PortError("Unknown operation")
        token, fd, result = broker.acquire(req.get("port"), req.get("purpose"), pid)
        reply(conn, result, fd)
        conn.settimeout(None)
        req = read_request(conn)
        result = broker.release(token)
        token = None
        if req is not None:
            reply(conn, result)
    except Exception as exc:
        try:
            reply(conn, {"ok": False, "error": str(exc)})
        except OSError:
            pass
    finally:
        if token:
            broker.release(token)
        conn.close()


def serve(broker, path=None, allowed_uids=None, group=None, admin_uids=None):
    # admin_uids: the peers allowed the root-only operations (maintenance,
    # service-mutation). Root in the product; a test run by an unprivileged
    # user passes its own uid so it can play the updater.
    path = path or str(broker.runtime / "control.sock")
    allowed_uids = allowed_uids or {0, pwd.getpwnam("sa02m-flasher").pw_uid}
    admin_uids = frozenset({0} if admin_uids is None else admin_uids)
    if group is None:
        group = pwd.getpwnam("sa02m-flasher").pw_gid
    os.chown(broker.runtime, -1, group)
    os.chown(broker.lock_dir, locks.OWNER_UID, locks.OWNER_GID)
    os.chmod(broker.runtime, 0o755)
    os.chmod(broker.lock_dir, 0o755)
    if os.path.exists(path):
        os.unlink(path)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(path)
    os.chown(path, -1, group)
    os.chmod(path, 0o660)
    listener.listen(16)
    sd_notify("READY=1")

    # Recovery retries on their own thread: an acquire can hold the mutex for
    # tens of seconds (owner sweep, MQTT pause, unit stops), and the accept
    # loop must keep taking connections meanwhile.
    def recover_loop():
        while True:
            time.sleep(1)
            try:
                broker.recover()
            except Exception:
                log.exception("recovery pass failed")
    threading.Thread(target=recover_loop, daemon=True).start()
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=handle, args=(broker, conn, allowed_uids, admin_uids),
                         daemon=True).start()


def main():
    logging.basicConfig(level=logging.INFO)
    runtime = Path("/run/sa02m-port-coordinator")
    runtime.mkdir(exist_ok=True, mode=0o755)
    singleton = os.open(runtime / "broker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open("/etc/sa02m/port-coordinator.json", encoding="utf-8") as fh:
        config = json.load(fh)
    broker = Broker(config)
    serve(broker)


if __name__ == "__main__":
    main()
