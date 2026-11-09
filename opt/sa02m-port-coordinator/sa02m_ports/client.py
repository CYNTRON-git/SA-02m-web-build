"""Client locks survive broker restart through a shared open-file description."""

import array
import json
import math
import os
import socket
import stat
import time
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


def _remaining_timeout(deadline):
    if not math.isfinite(deadline):
        raise ValueError("Coordinator deadline must be finite")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CoordinatorUnavailable("Port coordinator deadline exceeded")
    return min(TIMEOUT, remaining)


def connect(path=None, *, deadline=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(TIMEOUT if deadline is None else _remaining_timeout(deadline))
        sock.connect(SOCKET if path is None else path)
        if deadline is not None:
            sock.settimeout(TIMEOUT)
    except OSError as exc:
        sock.close()
        raise CoordinatorUnavailable("Port coordinator unavailable") from exc
    except BaseException:
        sock.close()
        raise
    return sock


def request(sock, value, *, deadline=None):
    original_timeout = sock.gettimeout() if deadline is not None else None
    fds = []
    try:
        try:
            frame = json.dumps(value).encode() + b"\n"
            if deadline is not None:
                sock.settimeout(_remaining_timeout(deadline))
            sock.sendall(frame)
            if deadline is not None:
                sock.settimeout(_remaining_timeout(deadline))
            payload, ancillary, flags, _ = sock.recvmsg(16384, socket.CMSG_SPACE(4))
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    numbers = array.array("i")
                    numbers.frombytes(data[:len(data) - len(data) % numbers.itemsize])
                    fds.extend(numbers)
            while b"\n" not in payload:
                if len(payload) >= 16384:
                    raise PortError("Oversize coordinator reply")
                if deadline is not None:
                    sock.settimeout(_remaining_timeout(deadline))
                chunk = sock.recv(16384 - len(payload))
                if not chunk:
                    raise PortError("Coordinator disconnected")
                payload += chunk
            if deadline is not None:
                _remaining_timeout(deadline)
            reply = json.loads(payload.split(b"\n", 1)[0])
            if not reply.get("ok"):
                raise PortError(reply.get("error", "Port operation failed"))
            return reply, fds
        finally:
            if deadline is not None:
                sock.settimeout(original_timeout)
    except BaseException as exc:
        for fd in fds:
            os.close(fd)
        if isinstance(exc, socket.timeout):
            raise CoordinatorUnavailable("Port coordinator did not answer") from exc
        raise


class Lease:
    """Exclusive operation. Close only AFTER every UART handle/task is closed."""

    def __init__(self, device, purpose="operation", socket_path=None, *,
                 acquire_deadline=None, release_timeout=None, cancel_acquire=None):
        if release_timeout is not None and (not math.isfinite(release_timeout) or release_timeout < 0):
            raise ValueError("Release timeout must be finite and nonnegative")
        self.device, self.purpose, self.socket_path = device, purpose, socket_path
        self.acquire_deadline, self.release_timeout = acquire_deadline, release_timeout
        self.cancel_acquire = cancel_acquire
        self.sock = None
        self.fd = None
        self.status = {}

    def interrupt_acquire(self):
        """Wake an outstanding acquire before its UART has been handed over."""
        if self.cancel_acquire is not None:
            self.cancel_acquire.set()
            sock = self.sock
            if sock is not None and self.fd is None:
                try:
                    sock.shutdown(socket.SHUT_RD)
                except OSError:
                    pass  # The acquire thread already closed it.

    def __enter__(self):
        if self.sock is not None or self.fd is not None:
            raise PortError("Lease already acquired")
        budget = {} if self.acquire_deadline is None else {"deadline": self.acquire_deadline}
        connect_budget = budget
        if self.cancel_acquire is not None:
            # A UNIX socket with a full backlog can block before we have a
            # connected descriptor to interrupt. Bound only that handshake.
            limit = time.monotonic() + 3
            connect_budget = {"deadline": min(limit, budget.get("deadline", limit))}
        self.sock = connect(self.socket_path, **connect_budget)
        try:
            if self.cancel_acquire is not None and self.cancel_acquire.is_set():
                raise PortError("Lease acquisition cancelled")
            self.status, fds = request(self.sock, {
                "op": "acquire", "port": self.device, "purpose": self.purpose,
            }, **budget)
            if self.cancel_acquire is not None and self.cancel_acquire.is_set():
                for fd in fds:
                    os.close(fd)
                raise PortError("Lease acquisition cancelled")
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

    def close(self, *, deadline=None):
        if self.release_timeout is not None:
            release_deadline = time.monotonic() + self.release_timeout
            deadline = release_deadline if deadline is None else min(deadline, release_deadline)
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.sock is not None:
            sock, self.sock = self.sock, None
            try:
                budget = {} if deadline is None else {"deadline": deadline}
                reply, fds = request(sock, {"op": "release"}, **budget)
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

    def acquire(self, *, deadline=None):
        if self.fd is not None:
            raise PortError("Polling lock already acquired")
        budget = {} if deadline is None else {"deadline": deadline}
        with connect(self.socket_path, **budget) as sock:
            reply, fds = request(sock, {"op": "poll", "port": self.device}, **budget)
            for fd in fds:
                os.close(fd)
        try:
            self.fd = lock_device(self.device, self.lock_dir)
        except OSError as exc:
            raise PortError("UART reserved by another owner") from exc
        # A lease may have started while we obtained the lock. Recheck its intent
        # before opening UART; broker waits for this fd before granting a lease.
        try:
            with connect(self.socket_path, **budget) as sock:
                reply, fds = request(sock, {"op": "poll", "port": self.device}, **budget)
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
