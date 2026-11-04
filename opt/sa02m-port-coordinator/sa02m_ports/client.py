"""Client locks survive broker restart through a shared open-file description."""

import array
import json
import os
import socket
import stat
from .locks import open_locked

SOCKET = "/run/sa02m-port-coordinator/control.sock"
LOCK_DIR = "/run/sa02m-port-coordinator/locks"
# One reply bound: a lease acquire legitimately waits for the broker's own
# bounded steps (5 s owner sweep + 20 s MQTT pause + 25 s per unit stop).
TIMEOUT = 45


class PortError(RuntimeError):
    pass


class CoordinatorUnavailable(PortError):
    """No broker answered: socket absent, connection refused, or a reply timed out.

    Distinct from a refusal (a real lease/maintenance conflict) so a caller can
    retry or name the outage; the service-ctl wrapper maps it to
    `coordinator_unavailable` while every refusal stays `port_busy`.
    """


def device_key(device):
    info = os.stat(device)
    if not stat.S_ISCHR(info.st_mode):
        raise PortError("UART must be a character device")
    return "%d-%d" % (os.major(info.st_rdev), os.minor(info.st_rdev))


def lock_device(device, directory=LOCK_DIR):
    path = os.path.join(directory, device_key(device) + ".lock")
    return open_locked(path)


def connect(path=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    try:
        sock.connect(SOCKET if path is None else path)
    except OSError as exc:
        sock.close()
        raise CoordinatorUnavailable("Port coordinator unavailable") from exc
    return sock


def request(sock, value):
    try:
        sock.sendall(json.dumps(value).encode() + b"\n")
        payload, ancillary, flags, _ = sock.recvmsg(16384, socket.CMSG_SPACE(4))
    except socket.timeout as exc:
        raise CoordinatorUnavailable("Port coordinator did not answer") from exc
    fds = []
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            numbers = array.array("i")
            numbers.frombytes(data[:len(data) - len(data) % numbers.itemsize])
            fds.extend(numbers)
    try:
        while b"\n" not in payload:
            if len(payload) >= 16384:
                raise PortError("Oversize coordinator reply")
            try:
                chunk = sock.recv(16384 - len(payload))
            except socket.timeout as exc:
                raise CoordinatorUnavailable("Port coordinator did not answer") from exc
            if not chunk:
                raise PortError("Coordinator disconnected")
            payload += chunk
        reply = json.loads(payload.split(b"\n", 1)[0])
        if not reply.get("ok"):
            raise PortError(reply.get("error", "Port operation failed"))
        return reply, fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


class Lease:
    """Exclusive operation. Close only AFTER every UART handle/task is closed."""

    def __init__(self, device, purpose="operation", socket_path=None):
        self.device, self.purpose, self.socket_path = device, purpose, socket_path
        self.sock = None
        self.fd = None
        self.status = {}

    def __enter__(self):
        if self.sock is not None or self.fd is not None:
            raise PortError("Lease already acquired")
        self.sock = connect(self.socket_path)
        try:
            self.status, fds = request(self.sock, {
                "op": "acquire", "port": self.device, "purpose": self.purpose,
            })
            if len(fds) != 1:
                for fd in fds:
                    os.close(fd)
                raise PortError("Coordinator did not transfer UART lock")
            self.fd = fds[0]
            os.set_inheritable(self.fd, False)
            return self
        except BaseException:
            self.sock.close()
            self.sock = None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.sock is not None:
            sock, self.sock = self.sock, None
            try:
                reply, fds = request(sock, {"op": "release"})
                for fd in fds:
                    os.close(fd)
                self.status = reply
                if reply.get("pending_restore"):
                    raise PortError("Operation ended; service restoration pending")
            except (OSError, ValueError, CoordinatorUnavailable) as exc:
                # The broker recovers a lease whose connection ends without a
                # reply (its own death included) — the outcome is the same.
                raise PortError("Coordinator recovery pending") from exc
            finally:
                sock.close()

    def __exit__(self, typ, value, traceback):
        self.close()


class PollLock:
    """Non-preempting lock for normal polling; coordinator outage fails closed."""

    def __init__(self, device, socket_path=None, lock_dir=LOCK_DIR):
        self.device, self.socket_path, self.lock_dir = device, socket_path, lock_dir
        self.fd = None

    def acquire(self):
        if self.fd is not None:
            raise PortError("Polling lock already acquired")
        with connect(self.socket_path) as sock:
            reply, fds = request(sock, {"op": "poll", "port": self.device})
            for fd in fds:
                os.close(fd)
        try:
            self.fd = lock_device(self.device, self.lock_dir)
        except OSError as exc:
            raise PortError("UART reserved by another owner") from exc
        # A lease may have started while we obtained the lock. Recheck its intent
        # before opening UART; broker waits for this fd before granting a lease.
        try:
            with connect(self.socket_path) as sock:
                reply, fds = request(sock, {"op": "poll", "port": self.device})
                for fd in fds:
                    os.close(fd)
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.close()


def assert_idle_for_maintenance(socket_path=None):
    """Caller must already hold the exclusive maintenance.lock.

    The updater (install-maintenance.py, sa02m-update-runner.sh) takes that
    flock itself and calls this once; there is no client-side wrapper class.
    """
    with connect(socket_path) as sock:
        result, fds = request(sock, {"op": "maintenance"})
        for fd in fds:
            os.close(fd)
    return result


class ServiceMutation:
    """Root-only service guard, independent of unrelated whole-service scopes."""

    def __init__(self, unit, socket_path=None):
        self.unit, self.socket_path, self.fd = unit, socket_path, None

    def __enter__(self):
        with connect(self.socket_path) as sock:
            result, fds = request(sock, {"op": "service-mutation", "unit": self.unit})
        if len(fds) != 1:
            for fd in fds:
                os.close(fd)
            raise PortError("Missing service mutation guard")
        self.fd, self.unit = fds[0], result["unit"]
        os.set_inheritable(self.fd, False)
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
