"""Boot race: the gateway leases before the coordinator listens; a bounded wait closes it.

A scripted broker on a real Unix socket that appears late and answers the
`acquire` with an SCM_RIGHTS descriptor, as sa02m_ports.broker does.
"""
import array
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sa02m-port-coordinator"))
import serial_gateway as sg  # noqa: E402
from sa02m_ports import broker  # noqa: E402


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


@pytest.mark.parametrize("recovered", ["listening", "stopped"])
def test_gateway_recovers_only_after_mqtt_can_yield(tmp_path, monkeypatch, recovered):
    state = "active"
    pause = str(tmp_path / "pause.sock")
    monkeypatch.setattr(broker, "MQTT_PAUSE_SOCKET", pause)
    monkeypatch.setattr(broker.System, "_state", lambda self, unit: state)
    monkeypatch.setattr(broker.System, "MQTT_PAUSE_RETRIES", 0)
    with pytest.raises(broker.PortError) as refused:
        broker.System().mqtt_pause("/dev/COM4", pause)

    async def run(executor, listener, slave):
        nonlocal state
        pg = sg.PortGateway("COM4", {"enabled": True, "mode": "transparent",
            "bind": "127.0.0.1", "tcp_port": 0}, executor)
        real_open = sg.SerialWorker.open
        def open_pty(worker):
            worker.dev = os.ttyname(slave)
            real_open(worker)
        monkeypatch.setattr(sg.SerialWorker, "open", open_pty)
        attempts = []
        def acquire(*args):
            attempts.append(True)
            pg._lock.error = str(refused.value) if len(attempts) == 1 else ""
            return not pg._lock.error
        monkeypatch.setattr(pg._lock, "acquire", acquire)
        try:
            assert not await pg.start()
            for _ in range(3):
                pg.retry_mqtt_startup()
                await pg._mqtt_retry_task
            assert len(attempts) == 1
            if recovered == "stopped":
                state = "inactive"
            else:
                listener.bind(pause)
                listener.listen(1)
            pg.retry_mqtt_startup()
            task = pg._mqtt_retry_task
            pg.retry_mqtt_startup()
            assert pg._mqtt_retry_task is task
            await task
            assert pg.is_running() and not pg.stats.last_error
            tcp = pg._gw._server.sockets[0].getsockname()
            reader, writer = await asyncio.open_connection(*tcp)
            writer.close()
            await writer.wait_closed()
            pg.retry_mqtt_startup()
            assert pg._mqtt_retry_task is None and len(attempts) == 2
        finally:
            await pg.stop()

    master, slave = os.openpty()
    try:
        with ThreadPoolExecutor() as executor, socket.socket(socket.AF_UNIX) as listener:
            asyncio.run(run(executor, listener, slave))
    finally:
        os.close(master)
        os.close(slave)


def test_retry_teardown_failure_stays_on_its_port(monkeypatch):
    async def run(executor):
        pg = sg.PortGateway("COM4", {"enabled": True, "mode": "transparent"}, executor)
        pg.stats.last_error = "MQTT pause socket is not ready"
        monkeypatch.setattr(pg._lock, "mqtt_ready_for_retry", lambda: True)
        async def fail_close(cancel):
            pg._worker = object()  # teardown failed with the UART still held
            raise RuntimeError("UART close failed")
        monkeypatch.setattr(pg, "start", fail_close)
        pg.retry_mqtt_startup()
        await asyncio.wait([pg._mqtt_retry_task])
        pg.retry_mqtt_startup()
        assert pg.stats.last_error == "UART close failed"
        assert pg._mqtt_retry_task is None and pg._worker is not None
        pg._worker = None
        pg.stats.last_error = "UART already leased"
        pg.retry_mqtt_startup()
        assert pg._mqtt_retry_task is None
    with ThreadPoolExecutor() as executor:
        asyncio.run(run(executor))


def test_stop_drains_pending_readiness_probe_before_reload(monkeypatch):
    async def run(executor):
        pg = sg.PortGateway("COM4", {"enabled": True, "mode": "transparent"}, executor)
        entered, release = threading.Event(), threading.Event()
        def wait_for_socket():
            entered.set()
            assert release.wait(3)
            return True
        pg.stats.last_error = "MQTT pause socket is not ready"
        monkeypatch.setattr(pg._lock, "mqtt_ready_for_retry", wait_for_socket)
        acquired = []
        monkeypatch.setattr(pg._lock, "acquire", lambda: acquired.append(True))
        pg.retry_mqtt_startup()
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            stopping = asyncio.create_task(pg.stop())
            async with asyncio.timeout(2):
                while not pg._mqtt_retry_task.cancelling():
                    await asyncio.sleep(0)
            assert not stopping.done()
        finally:
            release.set()
        await asyncio.wait_for(stopping, 3)
        assert not acquired and pg._mqtt_retry_task is None
    with ThreadPoolExecutor() as executor:
        asyncio.run(run(executor))


def test_stop_interrupts_a_silent_broker_during_background_acquire(socket_path, monkeypatch):
    async def run(executor):
        accepted, disconnected = asyncio.Event(), asyncio.Event()
        requests = []
        async def silent(reader, writer):
            requests.append(json.loads(await reader.readline()))
            accepted.set()
            await reader.read()
            writer.close()
            await writer.wait_closed()
            disconnected.set()

        async with await asyncio.start_unix_server(silent, path=socket_path):
            pg = sg.PortGateway("COM4", {"enabled": True, "mode": "transparent"}, executor)
            pg._lock.socket_path = socket_path
            pg.stats.last_error = "MQTT pause socket is not ready"
            monkeypatch.setattr(pg._lock, "mqtt_ready_for_retry", lambda: True)
            pg.retry_mqtt_startup()
            await asyncio.wait_for(accepted.wait(), 2)
            await asyncio.wait_for(pg.stop(), 2)
            await asyncio.wait_for(disconnected.wait(), 2)
            assert len(requests) == 1 and requests[0]["op"] == "acquire"
            assert not pg.is_running() and pg._lock._lease is None
            assert pg.stats.errors == 0
    with ThreadPoolExecutor() as executor:
        asyncio.run(run(executor))
