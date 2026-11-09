"""An unavailable pause endpoint must not reserve the original poller's UART."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch

from test_process_ownership import FakeSystem, PtyBroker, atomic_json
from sa02m_ports import PollLock, PortError
from sa02m_ports import broker as module

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sa02m-serial-gateway"))
import serial_gateway as gateway


class PauseRollback(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ports = []
        self.masters = []
        for _ in range(2):
            master, slave = os.openpty()
            self.addCleanup(os.close, master)
            self.masters.append(master)
            self.ports.append(os.ttyname(slave))
            os.close(slave)
        self.system = FakeSystem(self.root / "system.json")
        atomic_json(self.system.path, {"active": True, "starts": 0, "stops": 0})
        self.config = {"ports": {p: ["mplc4.service"] for p in self.ports}}
        self.broker = self.new_broker()
        self.addCleanup(self.close_broker)

    def new_broker(self):
        return PtyBroker(self.config, self.root / "state", self.root / "run", self.system)

    def close_broker(self):
        for fd in self.broker.fds.values():
            os.close(fd)
        self.broker.fds.clear()
        for hold in self.broker.holds.values():
            if hold:
                hold.close()

    def owner(self, index=0):
        fd = os.open(self.ports[index], os.O_RDWR | os.O_NOCTTY)
        self.addCleanup(os.close, fd)
        return fd

    def test_refused_pause_releases_only_its_ungranted_intent(self):
        other, _, _ = self.broker.acquire(self.ports[1], "scan", os.getpid())
        self.owner()
        with patch.object(self.system, "mqtt_pause", side_effect=PortError("MQTT pause socket is not ready")):
            with self.assertRaisesRegex(PortError, "pause socket is not ready"):
                self.broker.acquire(self.ports[0], "gateway", os.getpid())
        self.assertEqual(set(self.broker.state["leases"]), {other})
        self.assertEqual(self.broker.connected, {other})
        self.assertEqual(set(self.broker.fds), {other})
        self.assertFalse(self.system.active("mplc4.service"))
        self.assertEqual(self.system.read()["starts"], 0)
        self.assertEqual(set(self.broker.state["services"]), {"mplc4.service"})
        self.broker.release(other)
        self.assertEqual(self.broker.state, {"leases": {}, "services": {}})
        self.assertTrue(self.system.active("mplc4.service"))

    def test_restart_discards_only_the_durable_pre_pause_stage(self):
        self.owner()
        lease = {"id": "prior", "port": self.ports[0],
                 "key": module.device_key(self.ports[0]), "purpose": "gateway",
                 "pid": os.getpid(), "services": ["mplc4.service"], "error": ""}
        for phase in ("pausing", "preparing", "owned", "recovering"):
            with self.subTest(phase=phase):
                atomic_json(self.broker.journal, {"leases": {"prior": dict(lease, phase=phase)},
                    "services": {"mplc4.service": {"restore": True, "phase": "paused", "error": ""}}})
                atomic_json(self.system.path, {"active": False, "starts": 0, "stops": 1})
                restarted = self.new_broker()
                restarted.recover(force=True)
                if phase == "pausing":
                    self.assertEqual(restarted.state, {"leases": {}, "services": {}})
                    self.assertEqual(self.system.read()["starts"], 1)
                else:
                    # Legacy stages cannot prove that no client/child got UART.
                    self.assertIn("prior", restarted.state["leases"])
                    self.assertFalse(self.system.active("mplc4.service"))

    def test_pause_transition_is_durable_before_service_stop_or_fd_grant(self):
        observed = []
        def pause(port):
            observed.append(json.loads(self.broker.journal.read_text())["leases"])
        real_stop = self.system.stop
        def stop(unit):
            observed.append(json.loads(self.broker.journal.read_text())["leases"])
            real_stop(unit)
        with patch.object(self.system, "mqtt_pause", side_effect=pause), patch.object(self.system, "stop", side_effect=stop):
            token, _, _ = self.broker.acquire(self.ports[0], "gateway", os.getpid())
        self.assertEqual([state[token]["phase"] for state in observed], ["pausing", "preparing"])
        self.assertEqual(json.loads(self.broker.journal.read_text())["leases"][token]["phase"], "owned")
        self.broker.release(token)

    def test_gateway_recovers_with_real_broker_and_original_owner_still_open(self):
        async def run(executor):
            endpoint = str(self.root / "pause.sock")
            control = str(self.root / "run/control.sock")
            # Only the endpoint's systemd state is simulated. Requests, UART
            # descriptors, intent checks, flock and SCM_RIGHTS are real.
            real_pause = module.System().mqtt_pause
            other, _, _ = self.broker.acquire(self.ports[1], "scan", os.getpid())
            other_uart = os.open(self.ports[1], os.O_RDWR | os.O_NOCTTY)
            owner = [os.open(self.ports[0], os.O_RDWR | os.O_NOCTTY)]
            poll = [None]
            resumed = asyncio.Event()
            pause_tasks = set()
            async def pause(reader, writer):
                pause_tasks.add(asyncio.current_task())
                try:
                    line = await reader.readline()
                    if not line:  # gateway readiness probe: no pause request
                        return
                    self.assertEqual(json.loads(line), {"via": "com", "port": self.ports[0]})
                    os.close(owner.pop())
                    poll.pop().close()
                    writer.write(b'{"ok":true}\n')
                    await writer.drain()
                    await reader.read()
                    poll.append(await asyncio.to_thread(PollLock(self.ports[0], control,
                        str(self.broker.lock_dir)).acquire))
                    owner.append(os.open(self.ports[0], os.O_RDWR | os.O_NOCTTY))
                    resumed.set()
                finally:
                    writer.close()
                    await writer.wait_closed()
                    pause_tasks.discard(asyncio.current_task())

            listener = socket.socket(socket.AF_UNIX)
            listener.bind(control)
            listener.listen(8)
            listener.settimeout(0.1)
            stopping = threading.Event()
            handlers = []
            def accept():
                while not stopping.is_set():
                    try:
                        conn, _ = listener.accept()
                    except socket.timeout:
                        continue
                    thread = threading.Thread(target=module.handle,
                        args=(self.broker, conn, {os.getuid()}), daemon=True)
                    handlers.append(thread)
                    thread.start()
            server_thread = threading.Thread(target=accept, daemon=True)
            server_thread.start()
            pg = gateway.PortGateway(self.ports[0].removeprefix("/dev/"),
                {"enabled": True, "mode": "transparent", "bind": "127.0.0.1", "tcp_port": 0}, executor)
            pg._lock.socket_path = control
            pause_server = None
            client = None
            try:
                poll[0] = await asyncio.to_thread(PollLock(self.ports[0], control,
                    str(self.broker.lock_dir)).acquire)
                with patch.object(module.System, "_state", return_value="active"), \
                        patch.object(module.System, "MQTT_PAUSE_RETRIES", 0), \
                        patch.object(module, "MQTT_PAUSE_SOCKET", endpoint), \
                        patch.object(self.system, "mqtt_pause", side_effect=lambda port: real_pause(port, endpoint)):
                    self.assertFalse(await pg.start())
                    self.assertEqual(pg.stats.last_error, "MQTT pause socket is not ready")
                    self.assertIn(os.getpid(), module.occupants(self.ports[0]))
                    self.assertEqual(set(self.broker.state["leases"]), {other})
                    pg.retry_mqtt_startup()
                    await pg._mqtt_retry_task
                    self.assertFalse(pg.is_running())
                    pause_server = await asyncio.start_unix_server(pause, endpoint)
                    pg.retry_mqtt_startup()
                    await asyncio.wait_for(pg._mqtt_retry_task, 5)
                    self.assertTrue(pg.is_running())
                    self.assertEqual(pg.stats.last_error, "")
                    self.assertEqual(len(self.broker.state["leases"]), 2)
                    self.assertIn(other, self.broker.connected)
                    self.assertIn(os.getpid(), module.occupants(self.ports[1]))
                    reader, client = await asyncio.open_connection(*pg._gw._server.sockets[0].getsockname())
                    client.write(b"gateway-to-uart")
                    await client.drain()
                    self.assertEqual(await asyncio.wait_for(asyncio.to_thread(os.read,
                        self.masters[0], 64), 3), b"gateway-to-uart")
                    os.write(self.masters[0], b"uart-to-gateway")
                    self.assertEqual(await asyncio.wait_for(reader.readexactly(15), 3), b"uart-to-gateway")
                    client.close()
                    await client.wait_closed()
                    await pg.stop()
                    await asyncio.wait_for(resumed.wait(), 3)
                    self.assertEqual(set(self.broker.state["leases"]), {other})
                    self.assertFalse(self.system.active("mplc4.service"))
            finally:
                if client:
                    client.close()
                    await client.wait_closed()
                await pg.stop()
                if pause_server:
                    pause_server.close()
                    await pause_server.wait_closed()
                if pause_tasks:
                    await asyncio.gather(*pause_tasks)
                for fd in owner:
                    os.close(fd)
                for lock in poll:
                    if lock:
                        lock.close()
                os.close(other_uart)
                self.broker.release(other)
                stopping.set()
                await asyncio.to_thread(server_thread.join, 2)
                listener.close()
                for thread in handlers:
                    await asyncio.to_thread(thread.join, 2)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(self.broker.state, {"leases": {}, "services": {}})
            self.assertTrue(self.system.active("mplc4.service"))

        with ThreadPoolExecutor(max_workers=4) as executor:
            asyncio.run(run(executor))
