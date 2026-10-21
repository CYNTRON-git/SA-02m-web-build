"""Boot race: the gateway leases before the coordinator listens; a bounded wait closes it.

A scripted broker on a real Unix socket that appears late and answers the
`acquire` with an SCM_RIGHTS descriptor, as sa02m_ports.broker does.
"""
import array
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serial_gateway as sg  # noqa: E402


def late_broker(path, delay, log):
    """Bind after `delay`; grant one lease; then answer the release."""
    def run():
        time.sleep(delay)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen(1)
        log.append(("listening", time.monotonic()))
        conn, _ = listener.accept()
        try:
            request = json.loads(conn.recv(2048).split(b"\n", 1)[0])
            log.append(("request", request))
            lock_fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                reply = json.dumps({"ok": True, "lease": {"port": request["port"], "services": []}}).encode() + b"\n"
                conn.sendmsg([reply], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [lock_fd]))])
            finally:
                os.close(lock_fd)
            conn.recv(2048)
            conn.sendall(b'{"ok": true, "pending_restore": false}\n')
        finally:
            conn.close()
            listener.close()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def socket_path(tmp_path):
    return str(tmp_path / "control.sock")


def test_without_retries_the_boot_race_loses(socket_path):
    # The pre-fix shape: one attempt, the broker not yet listening ⇒ start fails
    # and GatewayDaemon never retries a port until SIGHUP.
    lock = sg.PortLock("COM1", socket_path=socket_path)
    lock.RETRIES = 0
    assert lock.acquire() is False
    assert "unavailable" in lock.error


def test_lease_waits_for_a_late_coordinator_within_the_bound(socket_path):
    log = []
    late_broker(socket_path, 0.35, log)
    lock = sg.PortLock("COM1", socket_path=socket_path)
    lock.RETRY_DELAY = 0.1
    started = time.monotonic()
    assert lock.acquire() is True
    assert lock.error == ""
    assert log[0][0] == "listening" and log[1][1]["op"] == "acquire"
    assert log[1][1]["purpose"] == "gateway" and log[1][1]["port"] == "/dev/COM1"
    assert time.monotonic() - started < 3
    lock.release()
    assert lock._lease is None


def test_the_wait_is_bounded(socket_path):
    lock = sg.PortLock("COM1", socket_path=socket_path)
    lock.RETRIES = 3
    lock.RETRY_DELAY = 0.05
    started = time.monotonic()
    assert lock.acquire() is False
    assert 0.15 <= time.monotonic() - started < 1.5
    assert "unavailable" in lock.error


def test_a_refusal_is_not_retried(socket_path):
    # A real conflict answers at once; waiting on it would only delay the error.
    def refuse():
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(socket_path)
        listener.listen(1)
        conn, _ = listener.accept()
        conn.recv(2048)
        conn.sendall(b'{"ok": false, "error": "UART already leased"}\n')
        conn.close()
        listener.close()
    threading.Thread(target=refuse, daemon=True).start()
    time.sleep(0.1)
    lock = sg.PortLock("COM1", socket_path=socket_path)
    lock.RETRY_DELAY = 5
    started = time.monotonic()
    assert lock.acquire() is False
    assert lock.error == "UART already leased"
    assert time.monotonic() - started < 2
