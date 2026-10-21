"""Real Unix sockets, SCM_RIGHTS, flock, child death and PTYs; no physical bus."""

import fcntl
import json
import multiprocessing as mp
import os
from pathlib import Path
import pty
import re
import signal
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sa02m_ports import Lease, PollLock, PortError, assert_idle_for_maintenance
from sa02m_ports.broker import Broker, atomic_json, serve

CTX = mp.get_context("spawn")


class FakeSystem:
    """Only systemd is simulated; state survives process restart."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        return json.loads(self.path.read_text())

    def canonical(self, unit):
        if unit in ("codesys.service", "CODESYSControl.service"):
            return "codesyscontrol.service"
        return unit

    def installed(self, unit):
        return unit not in self.read().get("missing", [])

    def active(self, unit):
        return self.read()["active"]

    def stop(self, unit):
        data = self.read()
        data["stops"] += 1
        data["active"] = False
        atomic_json(self.path, data)

    def start(self, unit):
        data = self.read()
        if data.get("fail"):
            raise RuntimeError("injected start failure")
        data["starts"] += 1
        data["active"] = True
        atomic_json(self.path, data)

    def mqtt_pause(self, port):
        path = self.path.parent / "mqtt.sock"
        if path.exists():
            conn = socket.socket(socket.AF_UNIX)
            conn.settimeout(10)
            conn.connect(str(path))
            conn.sendall(json.dumps({"via": "com", "port": port}).encode() + b"\n")
            result = json.loads(conn.recv(4096))
            if not result["ok"]:
                conn.close()
                raise RuntimeError("MQTT pause failed")
            return conn
        return None


class PtyBroker(Broker):
    def validate_config(self):
        # Production accepts only /dev/COM1..5. PTYs are test fixtures only.
        assert all(p.startswith("/dev/pts/") for p in self.config["ports"])


def run_broker(root, ports):
    import sa02m_ports.broker as module
    persist = module.atomic_json

    def write(path, value, mode=0o600):
        if Path(root, "failwrites").exists():
            raise OSError("injected storage failure")
        return persist(path, value, mode)

    module.atomic_json = write
    broker = PtyBroker({"ports": {p: ["mplc4.service"] for p in ports}},
                       root + "/state", root + "/run", FakeSystem(root + "/system.json"))
    serve(broker, allowed_uids={os.getuid()}, group=os.getgid())


def own_until_killed(path, port, ready):
    with Lease(port, "scan", path):
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        ready.send(True)
        while True:
            time.sleep(1)


def run_mqtt(root, ports, ready):
    """Actual bridge UART pool and pause socket, with PTY/path fixture adapters."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sa02m-modbus-mqtt"))
    os.environ["SA02M_MQTT_SCAN_LEASE_LOG"] = root + "/mqtt.log"
    import bridge_serial
    import bridge_scan_lease
    bridge_serial._COM_HOLD_RE = re.compile(r"/dev/pts/[0-9]+\Z")
    bridge_scan_lease._COM_RE = re.compile(r"/dev/pts/[0-9]+\Z")
    bridge_scan_lease._live_schedulers = lambda: []
    bridge_serial._port_client = lambda: (
        lambda port: PollLock(port, root + "/run/control.sock", root + "/run/locks"), Lease)
    handles = [bridge_serial.get_port(p, 9600) for p in ports]
    for handle in handles:
        handle._ensure_open()
    bridge_scan_lease.serve(root + "/mqtt.sock")
    ready.send(True)
    while True:
        for handle in handles:
            try:
                with handle._lock:
                    handle._ensure_open()
            except (PortError, bridge_serial.ScanHold):
                pass
        time.sleep(0.02)


def denied_peer(path, port, result):
    os.setgid(65534)
    os.setuid(65534)
    try:
        with Lease(port, "scan", path):
            result.send("unexpected success")
    except PortError as exc:
        result.send(str(exc))


class ProcessOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.system_file = Path(self.root, "system.json")
        atomic_json(self.system_file, {"active": True, "starts": 0, "stops": 0})
        self.masters = []
        self.ports = []
        for _ in range(2):
            master, slave = pty.openpty()
            self.masters.append(master)
            self.ports.append(os.ttyname(slave))
            os.close(slave)
        self.path = self.root + "/run/control.sock"
        self.children = []
        # systemd's notify socket, modelled: the broker must send READY=1 only
        # once control.sock accepts (Type=notify ordering for the consumers).
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.notify.bind(self.root + "/notify.sock")
        self.notify.settimeout(10)
        os.environ["NOTIFY_SOCKET"] = self.root + "/notify.sock"
        self.start_broker()

    def start_broker(self):
        self.broker = CTX.Process(target=run_broker, args=(self.root, self.ports))
        self.broker.start()
        self.children.append(self.broker)
        self.wait(lambda: Path(self.path).exists() and self.broker.is_alive())
        # Socket may exist from the preceding process; wait until accept works.
        self.wait(self.can_connect)

    def can_connect(self):
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.connect(self.path)
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def wait(self, predicate, timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                if predicate():
                    return
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(0.05)
        self.fail("condition did not become true")

    def state(self):
        return json.loads(Path(self.root, "state/state.json").read_text())

    def system(self):
        return json.loads(self.system_file.read_text())

    def lease(self, index=0):
        return Lease(self.ports[index], "scan", self.path)

    def tearDown(self):
        for child in self.children:
            if child.is_alive():
                child.kill()
            child.join(3)
        for fd in self.masters:
            os.close(fd)
        self.notify.close()
        os.environ.pop("NOTIFY_SOCKET", None)
        self.tmp.cleanup()

    def maintenance(self):
        """The updater's real shape: EX flock on maintenance.lock, then the idle check."""
        fd = os.open(self.root + "/run/maintenance.lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert_idle_for_maintenance(self.path)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def test_ready_is_notified_only_after_the_control_socket_accepts(self):
        # The broker was started by setUp; READY=1 must arrive, and at that
        # instant a connection must already succeed (never READY before listen).
        message = self.notify.recv(64)
        self.assertEqual(message, b"READY=1")
        self.assertTrue(self.can_connect())

    def test_config_naming_an_uninstalled_service_refuses_to_start(self):
        # A6: a scope naming a unit this board does not have would protect
        # nothing while looking mapped — the broker fails closed instead.
        # Control: the shipped template (empty scopes) constructs without error.
        Broker({"ports": {}}, self.root + "/state2", self.root + "/run2",
               FakeSystem(self.system_file))
        data = self.system()
        data["missing"] = ["vplc.service"]
        atomic_json(self.system_file, data)
        with self.assertRaises(PortError) as caught:
            PtyBroker({"ports": {self.ports[0]: ["vplc.service"]}}, self.root + "/state3",
                      self.root + "/run3", FakeSystem(self.system_file))
        self.assertIn("not installed: vplc.service", str(caught.exception))

    def test_overlap_both_completion_orders_and_inactive_policy(self):
        for reverse in (False, True):
            a, b = self.lease(0), self.lease(1)
            a.__enter__()
            b.__enter__()
            first, second = (b, a) if reverse else (a, b)
            first.close()
            self.assertFalse(self.system()["active"])
            second.close()
            self.assertTrue(self.system()["active"])
        self.assertEqual((self.system()["starts"], self.system()["stops"]), (2, 2))
        data = self.system()
        data["active"] = False
        atomic_json(self.system_file, data)
        with self.lease():
            pass
        self.assertFalse(self.system()["active"])
        self.assertEqual(self.system()["starts"], 2)

    def test_conflict_and_normal_poller_lock(self):
        with self.lease() as owner:
            with self.assertRaises(PortError):
                owner.__enter__()
            with self.assertRaises(PortError):
                with self.lease():
                    pass
            with self.assertRaises(PortError):
                PollLock(self.ports[0], self.path, self.root + "/run/locks").acquire()
        poller = PollLock(self.ports[0], self.path, self.root + "/run/locks").acquire()
        try:
            with self.assertRaises(PortError):
                poller.acquire()
            with self.assertRaises(PortError):
                with self.lease():
                    pass
        finally:
            poller.close()
        self.wait(lambda: not self.state()["leases"])
        self.wait(lambda: self.system()["active"])

    def test_broker_restart_keeps_client_lock_and_restore_obligation(self):
        lease = self.lease()
        lease.__enter__()
        self.broker.kill()
        self.broker.join()
        self.start_broker()
        time.sleep(1.2)
        self.assertFalse(self.system()["active"])
        self.assertTrue(self.state()["leases"])
        with self.assertRaises(PortError):
            with self.lease():
                pass
        with self.assertRaises(PortError):
            lease.close()
        self.wait(lambda: self.system()["active"])
        self.assertFalse(self.state()["leases"])

    def test_sigkill_client_releases_uart_and_restores(self):
        parent, child = CTX.Pipe()
        proc = CTX.Process(target=own_until_killed, args=(self.path, self.ports[0], child))
        proc.start()
        self.children.append(proc)
        self.assertTrue(parent.poll(10))
        self.assertTrue(parent.recv())
        self.assertFalse(self.system()["active"])
        proc.kill()
        proc.join(3)
        self.wait(lambda: self.system()["active"])
        self.assertFalse(self.state()["leases"])

    def test_failed_restore_persists_across_restart_then_retries(self):
        lease = self.lease()
        lease.__enter__()
        data = self.system()
        data["fail"] = True
        atomic_json(self.system_file, data)
        with self.assertRaises(PortError):
            lease.close()
        self.assertEqual(self.state()["services"]["mplc4.service"]["phase"], "restore_failed")
        self.broker.kill()
        self.broker.join()
        self.start_broker()
        data["fail"] = False
        atomic_json(self.system_file, data)
        self.wait(lambda: self.system()["active"] and not self.state()["services"])
        self.assertEqual(self.state()["services"], {})

    def test_maintenance_blocks_new_leases_and_rejects_active_one(self):
        with self.lease():
            with self.assertRaises(PortError):
                self.maintenance()
        gate = self.maintenance()
        try:
            with self.assertRaises(PortError):
                with self.lease():
                    pass
        finally:
            os.close(gate)
        with self.lease():
            pass

    def test_unknown_descriptor_blocks_until_closed(self):
        uart = os.open(self.ports[0], os.O_RDWR | os.O_NOCTTY)
        try:
            with self.assertRaises(PortError):
                with self.lease():
                    pass
            self.assertFalse(self.system()["active"])
            self.assertTrue(self.state()["leases"])
        finally:
            os.close(uart)
        self.wait(lambda: self.system()["active"])

    def test_actual_mqtt_pause_one_port_and_resume_with_pty_exchange(self):
        import serial
        from sa02m_ports.broker import occupants
        parent, child = CTX.Pipe()
        proc = CTX.Process(target=run_mqtt, args=(self.root, self.ports, child))
        proc.start()
        self.children.append(proc)
        self.assertTrue(parent.poll(10))
        self.assertTrue(parent.recv())
        self.assertIn(proc.pid, occupants(self.ports[0]))
        self.assertIn(proc.pid, occupants(self.ports[1]))
        with self.lease():
            self.assertNotIn(proc.pid, occupants(self.ports[0]))
            self.assertIn(proc.pid, occupants(self.ports[1]))
            with serial.Serial(self.ports[0], 9600, timeout=1, exclusive=True) as uart:
                uart.write(b"\x01\x03\x00\x00\x00\x01\x84\x0a")
                self.assertEqual(os.read(self.masters[0], 8),
                                 b"\x01\x03\x00\x00\x00\x01\x84\x0a")
                os.write(self.masters[0], b"\x01\x03\x02\x00\x09\x78\x42")
                self.assertEqual(uart.read(7), b"\x01\x03\x02\x00\x09\x78\x42")
        self.wait(lambda: proc.pid in occupants(self.ports[0]))

    def test_alias_uses_same_kernel_lock(self):
        from sa02m_ports.client import lock_device
        alias = Path(self.root, "alias")
        alias.symlink_to(self.ports[0])
        first = lock_device(self.ports[0], self.root + "/run/locks")
        try:
            with self.assertRaises(BlockingIOError):
                lock_device(str(alias), self.root + "/run/locks")
        finally:
            os.close(first)

    def test_persistence_failure_cannot_report_success_or_forget_pause(self):
        lease = self.lease()
        lease.__enter__()
        Path(self.root, "failwrites").touch()
        with self.assertRaises(PortError):
            lease.close()
        self.broker.join(5)
        self.assertEqual(self.broker.exitcode, 1)
        self.assertTrue(self.state()["leases"])
        self.assertFalse(self.system()["active"])
        Path(self.root, "failwrites").unlink()
        self.start_broker()
        self.wait(lambda: self.system()["active"])
        self.assertFalse(self.state()["leases"])

    @unittest.skipUnless(os.getuid() == 0, "setuid peer test requires Linux root")
    def test_peer_credentials_even_if_socket_permissions_are_widened(self):
        os.chmod(self.root, 0o755)
        os.chmod(self.path, 0o666)
        parent, child = CTX.Pipe()
        proc = CTX.Process(target=denied_peer, args=(self.path, self.ports[0], child))
        proc.start()
        self.children.append(proc)
        self.assertTrue(parent.poll(10))
        self.assertIn("Peer is not permitted", parent.recv())
        self.assertFalse(self.state()["leases"])


if __name__ == "__main__":
    unittest.main()
