"""Exercise both MR type writers and the later poll across a deferred ADC slot."""
import time
import sys
import unittest
from unittest import mock

from test_mr02m_ai_type_write import _poller
from bridge_mqtt import DeviceLiveCache
from bridge_rtu_tcp import RtuTcpClient
from bridge_serial import ModbusSerial, _append_crc


class Module:
    def __init__(self):
        self.magic = 0xA101
        self.flags = 1
        self.accepted = self.completed = 12
        self.registers = {270: 123, 400: 3, 407: 8}
        self.uptime = 3600
        self.writes = []
        self.after_read = lambda reg: None
        self.lost_ack = False

    def read_holding_registers(self, addr, reg, count, **options):
        if reg == 569:
            data = ([self.magic, self.flags, self.accepted, self.completed]
                    if self.magic else [0] * 4)
        else:
            data = [self.registers.get(reg + offset, 0) for offset in range(count)]
        self.after_read(reg)
        return data

    def write_register(self, addr, reg, code, **options):
        self.writes.append((reg, code))
        self.registers[reg] = code
        self.accepted += 1
        self.flags = 5
        if self.lost_ack:
            raise OSError("ACK lost")

    def read_input_registers(self, addr, reg, count, *, deadline):
        assert (reg, count) == (105, 2)
        assert deadline > time.monotonic()
        return [self.uptime & 65535, self.uptime >> 16]

    def finish(self):
        self.flags = 1
        self.completed = self.accepted


class AiApplyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.p, self.pub = _poller()
        self.p._mod_type = 6
        self.p._ai = 6
        self.m = Module()
        self.p.get_port = mock.Mock(return_value=self.m)
        self.p._publish_ai_scale = mock.Mock()
        self.p._persist_ai_sensor_types = mock.Mock()
        self.p._ai_types.update({1: 3, 2: 8})
        for ch, code in self.p._ai_types.items():
            DeviceLiveCache.set_sensor_type(self.p.device_id, ch, code)
        for patch in [mock.patch.object(DeviceLiveCache, 'flush_file'),
                      mock.patch('bridge_mr02m.time.sleep')]:
            patch.start()
            self.addCleanup(patch.stop)
        self.pub.device_online = mock.Mock()

    def unchanged(self):
        self.assertEqual(self.p._ai_types[1], 3)
        self.assertEqual(self.p._ch_cfg("ai", 1)["sensor_type"], 3)
        self.assertEqual(DeviceLiveCache.get_sensor_type(self.p.device_id, 1), 3)
        self.p._persist_ai_sensor_types.assert_not_called()

    def test_pending_echo_never_promotes_on_write_or_following_poll(self):
        for magic in (0, 0xA101):
            with self.subTest(magic=magic):
                self.m.magic = magic
                self.m.finish()
                self.m.writes.clear()
                self.p._writeback_ai_type(1, 41)
                self.assertEqual(self.m.writes, [(400, 41), (407, 41)])
                self.p._poll_ai_ao()
                self.unchanged()
                self.assertIn((self.p.device_id, "ai_1", "w"), self.pub.errors)

    def test_promotes_only_after_generation_completion_and_both_type_reads(self):
        pending_reads = []

        def adc_slot(reg):
            if self.m.flags == 5 and len(self.m.writes) == 2:
                self.unchanged()
                pending_reads.append(reg)
                if len(pending_reads) == 4:
                    self.m.finish()
        self.m.after_read = adc_slot
        self.p._writeback_ai_type(1, 41)
        self.assertEqual(pending_reads, [569] * 4)
        self.assertEqual(self.m.writes, [(400, 41), (407, 41)])
        self.p._persist_ai_sensor_types.assert_called_once_with({1: 41, 2: 41})

    def test_lost_ack_has_no_retry_and_leaves_pair_unconfirmed(self):
        self.m.lost_ack = True
        self.p._writeback_ai_type(1, 41)
        self.assertEqual(self.m.writes, [(400, 41)])
        self.p._poll_ai_ao()
        self.unchanged()

    def test_foreign_generation_during_final_type_read(self):
        def other_writer(reg):
            if self.m.writes:
                if reg == 400:
                    self.m.accepted += 1
                self.m.finish()
        self.m.after_read = other_writer
        self.p._writeback_ai_type(1, 37)
        self.assertEqual(self.m.writes, [(400, 37)])
        self.unchanged()

    def test_restart_after_first_target_stops_the_pair(self):
        def restart(reg):
            if self.m.writes:
                self.m.accepted = self.m.completed = 1
                self.m.flags = 1
        self.m.after_read = restart
        self.p._writeback_ai_type(1, 41)
        self.assertEqual(self.m.writes, [(400, 41)])
        self.unchanged()

    def test_legacy_illegal_status_address_and_broken_link_are_distinct(self):
        read = self.m.read_holding_registers
        for message, expected in (("Modbus exception 2 on FC03", [(400, 37)]),
                                  ("bad CRC", [])):
            def read_status(addr, reg, count, **opts):
                if reg == 569:
                    raise OSError(message)
                return read(addr, reg, count, **opts)
            self.m.read_holding_registers = read_status
            self.m.writes.clear()
            self.p._writeback_ai_type(1, 37)
            self.assertEqual(self.m.writes, expected)
            self.unchanged()

    def test_saturation_startup_and_failed_status_refuse_before_write(self):
        for generation, flags in ((65534, 1), (0, 0), (12, 9), (12, 33)):
            self.m.accepted = self.m.completed = generation
            self.m.flags = flags
            self.p._writeback_ai_type(1, 37)
            self.assertEqual(self.m.writes, [])
            self.unchanged()

    def test_yaml_startup_sends_once_and_never_promotes_delayed_intent(self):
        self.m.registers[400] = 37
        self.m.magic = 0
        self.p._ai_types[1] = 37
        DeviceLiveCache.set_sensor_type(self.p.device_id, 1, 37)
        self.p._apply_configured_ai_sensor_types()
        self.assertEqual(self.m.writes, [(400, 3)])
        self.m.registers[400] = 37
        self.p._apply_configured_ai_sensor_types()
        self.assertEqual(self.m.writes, [(400, 3)])
        self.m.registers[400] = 3
        self.p._poll_ai_ao()
        self.assertEqual(self.p._ai_types[1], 37)
        self.assertEqual(DeviceLiveCache.get_sensor_type(self.p.device_id, 1), 37)
        self.p._persist_ai_sensor_types.assert_not_called()

    def test_startup_pending_before_write_quarantines_poll_and_recovers_only_when_idle(self):
        self.p._ai = 2
        self.p._ai_types[1] = 37
        DeviceLiveCache.set_sensor_type(self.p.device_id, 1, 37)
        self.m.flags, self.m.completed = 5, 11
        self.p._apply_configured_ai_sensor_types()
        self.p._poll_ai_ao()
        self.assertEqual(self.p._ai_types[1], 37)
        self.assertEqual(DeviceLiveCache.get_sensor_type(self.p.device_id, 1), 37)
        self.assertIn(1, self.p._ai_unconfirmed)
        self.assertNotIn((self.p.device_id, "ai_1", ""), self.pub.errors)
        self.m.finish()
        self.p._poll_ai_ao()
        self.assertEqual(self.p._ai_types[1], 3)
        self.assertNotIn(1, self.p._ai_unconfirmed)
        self.assertIn((self.p.device_id, "ai_1", ""), self.pub.errors)
        self.assertFalse(self.m.writes)
        self.p._persist_ai_sensor_types.assert_not_called()

    def test_legacy_startup_matching_yaml_echo_preserves_known_conflicting_type(self):
        self.p._ai = 2
        self.m.magic = 0
        del self.p._ai_types[1]
        DeviceLiveCache.set_sensor_type(self.p.device_id, 1, 37)
        self.p._apply_configured_ai_sensor_types()
        self.p._poll_ai_ao()
        self.assertNotIn(1, self.p._ai_types)
        self.assertEqual(DeviceLiveCache.get_sensor_type(self.p.device_id, 1), 37)
        self.assertIn((self.p.device_id, "ai_1", "w"), self.pub.errors)
        self.assertNotIn((self.p.device_id, "ai_1", ""), self.pub.errors)
        self.assertNotIn(2, self.p._ai_unconfirmed)
        self.assertFalse(self.m.writes)

    def test_startup_recovery_with_reused_generation_and_reboot_keeps_prior_units(self):
        self.m.flags, self.m.completed = 5, 11
        self.p._apply_configured_ai_sensor_types()
        self.m.finish()
        self.m.after_read = lambda reg: setattr(self.m, 'uptime', 1) if reg == 400 else None
        self.p._poll_ai_ao()
        self.assertIn(1, self.p._ai_unconfirmed)
        self.unchanged()


class BoundedTypeTransportTests(unittest.TestCase):
    def test_cold_uart_cannot_send_after_coordinator_stalls(self):
        import bridge_serial
        if sys.platform == "win32":
            # The stalled broker must prevent reaching the POSIX file lock.
            locks = mock.Mock()
            locks.open_locked.side_effect = AssertionError("unexpected UART lock")
            patch = mock.patch.dict(sys.modules, {"sa02m_ports.locks": locks})
            patch.start()
            self.addCleanup(patch.stop)
        bridge_serial._port_client()
        from sa02m_ports import client
        now = [10.0]
        timeout = [None]
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.settimeout.side_effect = lambda seconds: timeout.__setitem__(0, seconds)
        sock.gettimeout.side_effect = lambda: timeout[0]

        def stalled(*args):
            now[0] += timeout[0]
            raise TimeoutError("coordinator stalled")

        sock.recvmsg.side_effect = stalled
        port = ModbusSerial("test-uart", 19200)
        with mock.patch.object(client.socket, "socket", return_value=sock), \
                mock.patch.object(client.socket, "AF_UNIX", 1, create=True), \
                mock.patch.object(client.socket, "CMSG_SPACE", return_value=8, create=True), \
                mock.patch.object(time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(bridge_serial.serial, "Serial") as uart:
            with self.assertRaises(client.CoordinatorUnavailable):
                port.write_register(2, 400, 37, deadline=10.25)
        self.assertEqual(now[0], 10.25)
        self.assertEqual(sock.sendall.call_count, 1)
        uart.assert_not_called()

    def test_rtu_tcp_deadline_write_never_replays_lost_ack(self):
        from test_mr_rtu_tcp_write_once import _ActuatingPeer
        peer = _ActuatingPeer(6, "ack_lost")
        client = RtuTcpClient("127.0.0.1", peer.port, 0.8, refuse_self=False)
        try:
            deadline = time.monotonic() + 2
            self.assertEqual(client.read_holding_registers(2, 33, 1, deadline=deadline), [0])
            self.assertEqual(client.read_input_registers(2, 105, 1, deadline=deadline), [0])
            with self.assertRaises(OSError):
                client.write_register(2, 400, 37, deadline=deadline)
            self.assertEqual([r[1] for r in peer.requests], [3, 4, 6])
        finally:
            client.close()
            peer.stop()

    def test_serial_deadline_reaches_uart_and_expired_request_sends_nothing(self):
        from test_modbus_reply_correlation import _Uart

        class Uart(_Uart):
            write_timeout = None

            def write(self, request):
                self.response = (request if request[1] == 6 else
                                 _append_crc(bytes((2, request[1], 2, 0, 37))))
                super().write(request)
                self.timeout_seen = self.write_timeout
                return len(request)

        port = ModbusSerial("test-uart", 19200, timeout=0.8, inter_frame_delay_s=0)
        peer = Uart(b"")
        with mock.patch.object(port, "_ensure_open", return_value=peer):
            deadline = time.monotonic() + 0.25
            port.write_register(2, 400, 37, deadline=deadline)
            self.assertEqual(port.read_holding_registers(2, 400, 1, deadline=deadline), [37])
            self.assertEqual(port.read_input_registers(2, 105, 1, deadline=deadline), [37])
            self.assertTrue(0 < peer.timeout_seen <= 0.25)
            self.assertIsNone(peer.write_timeout)
            with self.assertRaises(TimeoutError):
                port.write_register(2, 400, 38, deadline=time.monotonic() - 1)
            with port._lock, self.assertRaises(TimeoutError):
                port.write_register(2, 400, 39, deadline=time.monotonic() + 0.02)
        self.assertEqual([r[1] for r in peer.requests], [6, 3, 4])
