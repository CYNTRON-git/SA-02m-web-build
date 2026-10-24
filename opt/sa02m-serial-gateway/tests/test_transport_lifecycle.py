"""Linux socket + PTY composition; simulated UART, never physical RS-485."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import errno
import os
from pathlib import Path
import select
import socket
import sys
import threading
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serial_gateway as sg


REQ = bytes.fromhex("010300000001840a")
RESP = bytes.fromhex("0103021234b533")


class Bench:
    def __init__(self, mode="rtu_over_tcp"):
        self.master, self.slave = os.openpty()
        self.device = os.ttyname(self.slave)
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.pg = sg.PortGateway("COM1", {
            "enabled": True, "mode": mode, "bind": "127.0.0.1",
            "tcp_port": 0, "baudrate": 115200,
        }, self.executor)
        self.events = []
        self.requests = []
        self.release_reply = threading.Event()
        self.release_reply.set()
        self.received = threading.Event()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.emulate)
        self.pg._lock.acquire = lambda: True
        self.pg._lock.release = lambda: self.events.append("release")
        real_open = sg.SerialWorker.open
        real_close = sg.SerialWorker.close

        def open_pty(worker):
            worker.dev = self.device
            real_open(worker)

        def close_pty(worker):
            self.events.append("close")
            real_close(worker)

        self.open_patch = patch.object(sg.SerialWorker, "open", open_pty)
        self.close_patch = patch.object(sg.SerialWorker, "close", close_pty)

    def emulate(self):
        while not self.done.is_set():
            if not select.select([self.master], [], [], .01)[0]:
                continue
            try:
                data = os.read(self.master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    time.sleep(.01)
                    continue  # no slave descriptor until broker grants the lease
                return
            if not data:
                continue
            self.requests.append(data)
            self.received.set()
            while not self.release_reply.wait(.01):
                if self.done.is_set():
                    return
            self.events.append("reply")
            os.write(self.master, data + RESP)

    async def __aenter__(self):
        self.open_patch.start()
        self.close_patch.start()
        self.thread.start()
        try:
            assert await self.pg.start()
        except BaseException:
            await self.__aexit__()
            raise
        self.port = self.pg._gw._server.sockets[0].getsockname()[1]
        return self

    async def connect(self):
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def __aexit__(self, *exc):
        self.release_reply.set()
        await self.pg.stop()
        self.done.set()
        self.thread.join(2)
        self.executor.shutdown(wait=True)
        self.open_patch.stop()
        self.close_patch.stop()
        os.close(self.master)
        if self.slave >= 0:
            os.close(self.slave)


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


@pytest.mark.parametrize("mode", ["modbus_tcp", "rtu_over_tcp", "transparent"])
def test_bind_failure_never_publishes_running(mode):
    async def run():
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            bench = Bench(mode)
            bench.pg._cfg["tcp_port"] = occupied.getsockname()[1]
            with bench.open_patch, bench.close_patch:
                try:
                    assert not await bench.pg.start()
                    assert not bench.pg.is_running()
                    assert bench.pg._worker is None
                    assert bench.events == ["close", "release"]
                finally:
                    await bench.pg.stop()
                    bench.executor.shutdown(wait=True)
                    os.close(bench.master)
                    os.close(bench.slave)
    asyncio.run(run())


def test_every_fragment_boundary_and_coalesced_requests():
    async def run():
        async with Bench() as bench:
            reader, writer = await bench.connect()
            for split in range(1, len(REQ)):
                before = len(bench.requests)
                writer.write(REQ[:split])
                await writer.drain()
                await asyncio.sleep(.04)
                assert len(bench.requests) == before, "partial request reached UART"
                writer.write(REQ[split:])
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(len(RESP)), 2) == RESP
            writer.write(REQ * 3)
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(len(RESP) * 3), 4) == RESP * 3
            assert bench.requests == [REQ] * 10
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


def test_bad_crc_is_rejected_before_uart():
    async def run():
        async with Bench() as bench:
            reader, writer = await bench.connect()
            writer.write(REQ[:-1] + b"\0")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 2) == b""
            assert bench.requests == []
            assert "CRC" in bench.pg.stats.last_error
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["modbus_tcp", "rtu_over_tcp"])
def test_shutdown_drains_exchange_before_uart_close_and_lease_release(mode):
    async def run():
        async with Bench(mode) as bench:
            bench.release_reply.clear()
            reader, writer = await bench.connect()
            packet = REQ if mode == "rtu_over_tcp" else b"\0\1\0\0\0\6" + REQ[:-2]
            writer.write(packet)
            await writer.drain()
            await wait_until(bench.received.is_set)
            stopping = asyncio.create_task(bench.pg.stop())
            await asyncio.sleep(.05)
            assert not stopping.done(), "stop returned while UART thread remained active"
            assert "close" not in bench.events
            assert "release" not in bench.events
            bench.release_reply.set()
            await asyncio.wait_for(stopping, 2)
            assert bench.events[-3:] == ["reply", "close", "release"]
            assert bench.pg.stats.tcp_clients == 0
            assert await asyncio.wait_for(reader.read(), 1) == b""
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


# Request bodies are independent protocol vectors, including a maximum
# FastModbus by-serial FC16 firmware block (261 bytes including CRC).
PDUS = [
    bytes.fromhex(f"{fc:02x}00000001") for fc in (1, 2, 3, 4, 6)
] + [
    bytes([fc]) for fc in (7, 11, 12, 17)
] + [
    bytes.fromhex("050000ff00"),
    bytes.fromhex("0f00000009020101"),
    bytes.fromhex("10000000020412345678"),
    bytes.fromhex("140706000100000001"),
    bytes.fromhex("1509060001000000011234"),
    bytes.fromhex("160000ffff0000"),
    bytes.fromhex("170000000100000001021234"),
    bytes.fromhex("180000"),
    bytes.fromhex("2b0e0100"),
    bytes.fromhex("100000007bf6") + bytes(246),
    bytes.fromhex("15fb0600010000007a") + bytes(244),
]
REQUESTS = [sg._append_crc(b"\x01" + pdu) for pdu in PDUS] + [
    sg._append_crc(b"\xfd\x46" + bytes([cmd])) for cmd in (1, 2, 4)
] + [
    sg._append_crc(bytes.fromhex("fd460801020304") + pdu) for pdu in PDUS
]


@pytest.mark.parametrize("frame", REQUESTS, ids=lambda r: r[:9].hex())
def test_parser_all_splits_and_coalescing(frame):
    async def run():
        for split in range(1, len(frame)):
            reader = asyncio.StreamReader()
            pending = asyncio.create_task(sg._read_rtu_request(reader))
            reader.feed_data(frame[:split])
            await asyncio.sleep(0)
            assert not pending.done()
            reader.feed_data(frame[split:] + REQ)
            assert await pending == frame
            assert await sg._read_rtu_request(reader) == REQ
    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    b"\x01\x08\0\0", b"\x01\x55", b"\xfd\x46\x09",
    b"\xfd\x47\x01", b"\xfe\x03", b"\x01\x2b\x0d",
    bytes.fromhex("011000000002021234"),
    bytes.fromhex("01170000000100000002021234"),
    bytes.fromhex("011400"),
])
def test_unsupported_or_malformed_never_reaches_uart(payload):
    async def run():
        async with Bench() as bench:
            reader, writer = await bench.connect()
            writer.write(sg._append_crc(payload))
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert not bench.requests
            assert bench.pg.stats.errors == 1
            assert bench.pg.stats.last_error
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


def test_valid_then_bad_coalesced_does_not_forward_bad_or_following_request():
    async def run():
        async with Bench() as bench:
            reader, writer = await bench.connect()
            writer.write(REQ + REQ[:-1] + b"\0" + REQ)
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(len(RESP)), 2) == RESP
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert bench.requests == [REQ]
            assert "CRC" in bench.pg.stats.last_error
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


def test_multiple_clients_disconnect_and_reconnect_keep_uart_serialized():
    async def run():
        async with Bench() as bench:
            bench.release_reply.clear()
            reader1, writer1 = await bench.connect()
            reader2, writer2 = await bench.connect()
            writer1.write(REQ)
            await writer1.drain()
            await wait_until(bench.received.is_set)
            writer1.close()
            await writer1.wait_closed()
            writer2.write(REQ)
            await writer2.drain()
            await asyncio.sleep(.05)
            assert bench.requests == [REQ]
            bench.release_reply.set()
            assert await asyncio.wait_for(reader2.readexactly(len(RESP)), 2) == RESP
            reader3, writer3 = await bench.connect()
            writer3.write(REQ)
            await writer3.drain()
            assert await asyncio.wait_for(reader3.readexactly(len(RESP)), 2) == RESP
            assert bench.requests == [REQ] * 3
            for writer in (writer2, writer3):
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


def test_client_cancellation_keeps_uart_lock_until_thread_finishes():
    async def run():
        async with Bench() as bench:
            bench.release_reply.clear()
            reader1, writer1 = await bench.connect()
            writer1.write(REQ)
            await writer1.drain()
            await wait_until(bench.received.is_set)
            task = next(iter(bench.pg._gw._handlers))
            task.cancel()
            reader2, writer2 = await bench.connect()
            writer2.write(REQ)
            await writer2.drain()
            await asyncio.sleep(.05)
            assert not task.done()
            assert bench.requests == [REQ]
            bench.release_reply.set()
            assert await asyncio.wait_for(reader2.readexactly(len(RESP)), 2) == RESP
            assert await asyncio.wait_for(reader1.read(), 1) == b""
            assert bench.requests == [REQ] * 2
            for writer in (writer1, writer2):
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


def test_cancelling_stop_still_drains_before_releasing_lease():
    async def run():
        async with Bench() as bench:
            bench.release_reply.clear()
            reader, writer = await bench.connect()
            writer.write(REQ)
            await writer.drain()
            await wait_until(bench.received.is_set)
            stopping = asyncio.create_task(bench.pg.stop())
            await asyncio.sleep(.02)
            stopping.cancel()
            await asyncio.sleep(.02)
            stopping.cancel()
            assert "close" not in bench.events
            bench.release_reply.set()
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert bench.events[-3:] == ["reply", "close", "release"]
            assert bench.pg.stats.tcp_clients == 0
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


def test_transparent_shutdown_drains_reader_and_closes_idle_clients():
    async def run():
        bench = Bench("transparent")
        entered = threading.Event()
        finish = threading.Event()
        real_read = sg.SerialWorker.read_pending

        def delayed_read(worker):
            data = real_read(worker)
            entered.set()
            finish.wait(2)
            bench.events.append("read_done")
            return data

        with patch.object(sg.SerialWorker, "read_pending", delayed_read):
            async with bench:
                reader, writer = await bench.connect()
                await wait_until(entered.is_set)
                await wait_until(lambda: bench.pg.stats.tcp_clients == 1)
                stopping = asyncio.create_task(bench.pg.stop())
                await asyncio.sleep(.05)
                assert not stopping.done()
                assert "close" not in bench.events
                finish.set()
                await stopping
                assert bench.events[-3:] == ["read_done", "close", "release"]
                assert bench.pg.stats.tcp_clients == 0
                assert await asyncio.wait_for(reader.read(), 1) == b""
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


def test_partial_frame_timeout_is_reported_without_uart_write():
    async def run():
        async with Bench() as bench:
            reader, writer = await bench.connect()
            writer.write(REQ[:3])
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 6) == b""
            assert bench.requests == []
            assert "timeout" in bench.pg.stats.last_error
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["modbus_tcp", "rtu_over_tcp", "transparent"])
def test_serial_open_failure_releases_lease_and_stays_stopped(mode):
    async def run():
        with ThreadPoolExecutor() as executor:
            pg = sg.PortGateway("COM1", {"enabled": True, "mode": mode}, executor)
            events = []
            pg._lock.acquire = lambda: True
            pg._lock.release = lambda: events.append("release")
            with patch.object(sg.SerialWorker, "open", side_effect=OSError("open failed")):
                assert not await pg.start()
            assert not pg.is_running()
            assert pg._worker is None
            assert events == ["release"]
            assert "open failed" in pg.stats.last_error
    asyncio.run(run())


def test_close_failure_retains_lease_until_successful_retry():
    async def run():
        async with Bench() as bench:
            worker = bench.pg._worker
            with patch.object(worker, "close", side_effect=OSError("close failed")):
                with pytest.raises(OSError, match="close failed"):
                    await bench.pg.stop()
                assert not bench.pg.is_running()
                assert worker._ser.is_open
                assert "release" not in bench.events
                assert "close failed" in bench.pg.stats.last_error
            await bench.pg.stop()
            assert bench.events == ["close", "release"]
    asyncio.run(run())


def test_lease_restoration_failure_is_not_silently_successful():
    async def run():
        async with Bench() as bench:
            with patch.object(bench.pg._lock, "release", side_effect=RuntimeError("restore pending")):
                with pytest.raises(RuntimeError, match="restore pending"):
                    await bench.pg.stop()
                assert not bench.pg.is_running()
                assert bench.pg._worker is None
                assert "restore pending" in bench.pg.stats.last_error
            await bench.pg.stop()
            assert bench.events == ["close", "release"]
    asyncio.run(run())


def test_start_cancellation_drains_lease_acquisition_then_releases():
    async def run():
        with ThreadPoolExecutor() as executor:
            pg = sg.PortGateway("COM1", {"enabled": True, "mode": "modbus_tcp"}, executor)
            entered, finish = threading.Event(), threading.Event()
            events = []

            def acquire():
                entered.set()
                finish.wait(2)
                events.append("acquired")
                return True

            pg._lock.acquire = acquire
            pg._lock.release = lambda: events.append("release")
            pending = asyncio.create_task(pg.start())
            await wait_until(entered.is_set)
            pending.cancel()
            await asyncio.sleep(.03)
            assert events == []
            assert not pending.done()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert events == ["acquired", "release"]
            assert not pg.is_running()
            assert pg._worker is None
    asyncio.run(run())


def test_transparent_preserves_raw_payload_and_fanout():
    async def run():
        async with Bench("transparent") as bench:
            reader1, writer1 = await bench.connect()
            reader2, writer2 = await bench.connect()
            await wait_until(lambda: bench.pg.stats.tcp_clients == 2)
            raw = bytes(range(256))
            writer1.write(raw)
            await writer1.drain()
            # The PTY simulator echoes exactly the bytes it sees plus RESP.
            expected = raw + RESP
            assert await asyncio.wait_for(reader1.readexactly(len(expected)), 2) == expected
            assert await asyncio.wait_for(reader2.readexactly(len(expected)), 2) == expected
            assert b"".join(bench.requests) == raw
            for writer in (writer1, writer2):
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


@pytest.mark.parametrize("frame", [
    sg._append_crc(bytes.fromhex("fd4601")),
    sg._append_crc(bytes.fromhex("fd4608010203040300000001")),
    sg._append_crc(bytes.fromhex("fd460801020304100000007bf6") + bytes(246)),
])
def test_fast_modbus_socket_fragmentation_and_coalescing(frame):
    async def run():
        async with Bench() as bench:
            reader, writer = await bench.connect()
            # Drip every byte separately, including the final CRC split.
            for byte in frame[:-1]:
                writer.write(bytes([byte]))
                await writer.drain()
                await asyncio.sleep(.001)
            assert not bench.requests
            writer.write(frame[-1:] + frame)
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(len(RESP) * 2), 3) == RESP * 2
            assert bench.requests == [frame, frame]
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


def test_real_broker_keeps_physical_lock_until_uart_exchange_and_close_finish():
    # Reuse the coordinator's process fixture. Only systemd and COM->PTY names
    # are simulated; its Unix socket, fd transfer and kernel flock are real.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] /
                           "sa02m-port-coordinator" / "tests"))
    import test_process_ownership as ownership
    import sa02m_ports

    case = ownership.ProcessOwnership()
    case.setUp()
    real_lease = sa02m_ports.Lease
    try:
        async def run():
            bench = Bench()
            os.close(bench.master)
            os.close(bench.slave)
            bench.master = os.dup(case.masters[0])
            bench.slave = -1
            bench.device = case.ports[0]
            bench.pg._lock = sg.PortLock("COM1")

            def mapped_lease(port, purpose):
                assert port == "/dev/COM1"
                assert purpose == "gateway"
                return real_lease(case.ports[0], purpose, case.path)

            def competitor():
                with real_lease(case.ports[0], "scan", case.path):
                    pass

            with patch.object(sa02m_ports, "Lease", mapped_lease):
                async with bench:
                    assert not case.system()["active"]
                    bench.release_reply.clear()
                    reader, writer = await bench.connect()
                    writer.write(REQ)
                    await writer.drain()
                    await wait_until(bench.received.is_set)
                    stopping = asyncio.create_task(bench.pg.stop())
                    await asyncio.sleep(.03)
                    with pytest.raises(sa02m_ports.PortError):
                        await asyncio.to_thread(competitor)
                    assert not case.system()["active"]
                    assert "close" not in bench.events
                    bench.release_reply.set()
                    await stopping
                    assert bench.events[-2:] == ["reply", "close"]
                    assert case.system()["active"]
                    await asyncio.to_thread(competitor)
                    assert case.system()["active"]
                    writer.close()
                    await writer.wait_closed()
        asyncio.run(run())
    finally:
        case.tearDown()


def test_daemon_cancellation_drains_all_ports():
    async def run():
        from unittest.mock import AsyncMock
        async with Bench() as bench:
            daemon = sg.GatewayDaemon(Path("unused.yaml"))
            daemon._ports["COM1"] = bench.pg
            with patch.object(daemon, "_load_config", return_value={}), \
                    patch.object(daemon, "_start_ports", AsyncMock()), \
                    patch.object(sg, "_write_status"), patch.object(sg, "_sd_notify"):
                running = asyncio.create_task(daemon.run())
                await asyncio.sleep(.01)
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            assert not bench.pg.is_running()
            assert bench.events == ["close", "release"]
            assert not daemon._ports
    asyncio.run(run())


def test_daemon_stop_failure_still_stops_other_ports():
    async def run():
        from unittest.mock import AsyncMock, Mock
        daemon = sg.GatewayDaemon(Path("unused.yaml"))
        first = Mock(stop=AsyncMock(side_effect=RuntimeError("close failed")))
        second = Mock(stop=AsyncMock())
        daemon._ports = {"COM1": first, "COM2": second}
        try:
            with pytest.raises(ExceptionGroup, match="failed to stop"):
                await daemon._stop_ports()
            first.stop.assert_awaited_once()
            second.stop.assert_awaited_once()
        finally:
            daemon._executor.shutdown(wait=True)
    asyncio.run(run())


def test_truncated_request_disconnect_is_reported_without_uart_write():
    async def run():
        async with Bench() as bench:
            reader, writer = await bench.connect()
            writer.write(REQ[:3])
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert bench.requests == []
            assert bench.pg.stats.last_error == "Incomplete RTU request"
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())
