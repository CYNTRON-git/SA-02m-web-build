"""Exercise template subscriptions, DATA validation and confirmation on an in-memory line."""
from contextlib import ExitStack
from unittest import TestCase
from unittest import mock

from test_fmb_wire_wb import bridge, with_crc


END = bytes.fromhex("fd 46 12 52 5d")
REBOOT = bytes.fromhex("00 0f 00 00")
INPUT = bridge.FMB_EVT_INPUT
COIL = bridge.FMB_EVT_COIL


def data(records, count, *, addr=5, flag=1):
    return with_crc(bytes([addr, 0x46, 0x11, flag, count, len(records)]) + records)


class Line:
    def __init__(self):
        self.frames = []
        self.replies = []

    def fmb_send_recv(self, frame, *_):
        self.frames.append(bytes(frame))
        return self.replies.pop(0)


class Poller:
    def __init__(self):
        self.widths = {(COIL, 10): 1, (INPUT, 100): 4, (INPUT, 250): 2}
        self.covered = False
        self.events = []

    def fmb_event_width(self, kind, address):
        return self.widths.get((kind, address))

    def set_fmb_io_covered(self, covered):
        self.covered = covered


class TemplateLineFraming(TestCase):
    path = "/dev/COMT"

    def configs(self, stopbits):
        return [dict(type="template", id="meter_%d" % addr, address=addr,
                     port=self.path, baudrate=19200, stopbits=bits,
                     template="example", fmb_event_channels=["voltage"])
                for addr, bits in enumerate(stopbits, 1)]

    def test_two_templates_share_one_8n2_handle_for_configure_poll_and_rearm(self):
        import bridge_serial

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(bridge, "_pollers", []))
            stack.enter_context(mock.patch.dict(bridge_serial._port_pool, clear=True))
            create = stack.enter_context(mock.patch.object(bridge_serial, "ModbusSerial",
                                                          side_effect=lambda *a, **kw: Line()))
            buses, managers, refused = bridge.compose_pollers(self.configs([2, 2]), mock.Mock())
            self.assertEqual(refused, [])
            key, = managers
            left, right = buses[key]
            line = left.get_port()
            self.assertIs(right.get_port(), line)
            for p in (left, right):
                stack.enter_context(mock.patch.object(p, "poll_io"))
                stack.enter_context(mock.patch.object(p, "classic_ready_for_fmb", return_value=True))
            mgr = managers[key]
            ack1, ack2 = (with_crc(bytes([addr, 0x46, 0x18, 1, 1])) for addr in (1, 2))
            line.replies = [ack1, ack2, data(REBOOT, 0, addr=2), END, ack2]
            mgr.configure_all(only_ready=True)
            with mock.patch("time.monotonic", return_value=1.0):
                mgr.configure_template_step()
            with mock.patch("time.monotonic", return_value=2.0):
                mgr.configure_template_step()
            self.assertEqual(mgr.event_burst(), 0)
            mgr.reconfigure_pending()
            mgr.configure_template_step()
            self.assertEqual(line.replies, [])
            self.assertEqual([(f[0], f[2]) for f in line.frames],
                             [(1, 0x18), (2, 0x18), (0xfd, 0x10), (0xfd, 0x10), (2, 0x18)])
            create.assert_called_once_with(self.path, 19200, stopbits=2)

    def test_mixed_8n1_and_8n2_keeps_classic_pollers_and_reports_conflict(self):
        with mock.patch.object(bridge, "_pollers", []), self.assertLogs(level="ERROR") as logs:
            buses, managers, refused = bridge.compose_pollers(self.configs([1, 2]), mock.Mock())
        self.assertEqual(len(next(iter(buses.values()))), 2)
        self.assertEqual(refused, [])
        self.assertTrue(any("8N1 and 8N2" in msg for msg in logs.output))
        self.assertEqual(managers, {})


class TemplateProbeBudget(TestCase):
    def test_classic_reads_and_a_second_slave_continue_while_35_subscriptions_time_out(self):
        from test_port_cycle import FakePoller
        from bridge_serial import build_request

        now = 0.0
        classic = {1: [], 2: []}
        attempts = []
        ready = set()

        def advance(seconds):
            nonlocal now
            now += seconds

        def read(addr):
            frame = build_request(addr, 4, 0, 1)
            classic[addr].append((now, frame[1]))
            advance(0.006)
            ready.add(addr)

        def reply(frame, _minimum, _maximum, timeout):
            if frame[2] == 0x10:
                advance(0.004)
                return END
            addr, reg = frame[0], int.from_bytes(frame[5:7], "big")
            attempts.append((now, addr, reg, timeout))
            if addr == 1:
                advance(timeout)
                return b""
            advance(0.004)
            # Reject the first register once; the following register can still subscribe.
            mask = int(reg == 41 or sum(1 for _, a, r, _ in attempts if (a, r) == (2, 40)) > 1)
            return with_crc(bytes((addr, 0x46, 0x18, 1, mask)))

        manager = bridge.FastModbusEventPortManager("/dev/test", 115200)
        pollers = []
        for addr, regs in ((1, range(35)), (2, (40, 41))):
            p = FakePoller(str(addr))
            p.on_poll_io = lambda _p, addr=addr: read(addr)
            p.classic_ready_for_fmb = lambda addr=addr: addr in ready
            p.set_fmb_io_covered = lambda armed: self.assertFalse(armed)
            manager.register_device(addr, p.device_id, [(INPUT, r, 1) for r in regs],
                                    lambda *_: None, p, "template")
            pollers.append(p)
        line = mock.Mock(max_read_regs=None)
        line.fmb_send_recv.side_effect = reply
        loop = bridge.PortCycleScheduler("/dev/test", 115200, pollers, manager)
        with (mock.patch.object(bridge, "get_port", return_value=line),
              mock.patch("time.monotonic", side_effect=lambda: now),
              mock.patch("time.sleep", side_effect=advance),
              mock.patch.object(loop._stop, "is_set", side_effect=lambda: now >= 65.0),
              mock.patch.object(loop._stop, "wait", side_effect=advance)):
            loop.run()
        first = [(t, r) for t, a, r, _ in attempts if a == 1]
        self.assertEqual([(a, r) for _, a, r, _ in attempts[:4]],
                         [(1, 0), (2, 40), (1, 1), (2, 41)])
        self.assertEqual([r for _, r in first[:36]], list(range(35)) + [0])
        self.assertGreaterEqual(first[35][0] - first[34][0], 15)
        self.assertEqual([r for _, a, r, _ in attempts if a == 2], [40, 41, 40])
        self.assertTrue(manager._devices[2]["configured"])
        self.assertFalse(manager._devices[2]["pending"])
        self.assertFalse(manager._devices[1]["configured"])
        self.assertTrue(all(timeout <= 0.1 for _, _, _, timeout in attempts))
        # Ignore the deliberate 300 ms startup settle after warmup.
        for reads in classic.values():
            times = [t for t, fc in reads if fc == 4 and t >= 0.8]
            self.assertLess(times[0], 1.0)
            self.assertGreater(times[-1], 64.5)
            self.assertLess(max(b - a for a, b in zip(times, times[1:])), 0.25)


class TemplateWire(TestCase):
    def setUp(self):
        self.poller = Poller()
        self.mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        self.ranges = [(kind, address, 1) for kind, address in self.poller.widths]
        self.line = Line()
        self.register()

    def register(self, mode="auto"):
        self.mgr.register_device(5, "custom", self.ranges,
                                 lambda *event: self.poller.events.append(event),
                                 self.poller, "template", mode)
        self.dev = self.mgr._devices[5]

    def configure(self, masks):
        self.line.replies = [with_crc(bytes([5, 0x46, 0x18, 1, mask])) for mask in masks]
        return self.mgr._configure_device(self.line, 5, self.dev)

    def poll(self, packet):
        self.line.replies = [packet]
        _, events = self.mgr._poll_once(self.line)
        for event in events:
            self.mgr._dispatch(*event)
        return events

    def test_only_positive_wb_ack_enables_a_range_and_classic_coverage_stays(self):
        self.assertFalse(self.configure([1, 1, 0]))
        self.assertEqual([p[4] for p in self.line.frames], [1, 4, 4])
        self.assertEqual(self.dev["pending"], [(INPUT, 250, 1)])
        records = bytes.fromhex("01 01 00 0a 01 04 04 00 64 78 56 34 12 02 04 00 fa 02 01")
        self.poll(data(records, 3))
        self.assertEqual(self.poller.events, [(COIL, 10, 1), (INPUT, 100, 0x12345678)])
        self.assertTrue(self.configure([1]))
        self.mgr._finish_configure_pass(5, self.dev, 1.0)
        self.assertFalse(self.poller.covered)
        self.mgr.set_insurance(True)
        self.assertFalse(self.poller.covered)
        self.poller.events.clear()
        self.poll(data(records, 3))
        self.assertEqual(self.poller.events[-1], (INPUT, 250, 258))

    def test_template_refuses_legacy_config_and_data(self):
        self.register("legacy")
        self.assertFalse(self.configure([]))
        self.assertEqual(self.line.frames, [])
        self.register()
        self.assertTrue(self.configure([1, 1, 1]))
        self.assertEqual(self.poll(data(bytes.fromhex("03 00 fa 01 02"), 1)), [])
        self.assertFalse(self.mgr._last_poll_answered)
        self.poll(END)
        self.assertEqual(self.line.frames[-1][5:7], bytes([0, 0]))

    def test_no_confirmed_template_controls_before_first_ack(self):
        records = bytes.fromhex("02 04 00 fa 01 00 04 04 00 64 02 00 00 00")
        self.assertEqual(self.poll(data(records, 2)), [])
        self.assertTrue(self.mgr._last_poll_answered)

    def test_bad_width_and_unselected_records_do_not_hide_a_valid_neighbor(self):
        self.assertTrue(self.configure([1, 1, 1]))
        records = bytes.fromhex("02 04 00 64 02 00 09 04 00 97 00 01 02 03 04 05 06 07 08 "
                                "02 03 00 fa 00 00 02 04 00 fa 63 00")
        self.assertEqual(self.poll(data(records, 4)), [(5, INPUT, 250, 99)])
        self.poll(END)
        self.assertEqual(self.line.frames[-1][5:7], bytes([5, 1]))

    def test_canonical_zero_count_reboot_resets_subscriptions_and_is_confirmed(self):
        self.assertTrue(self.configure([1, 1, 1]))
        self.assertEqual(self.poll(data(REBOOT, 0, flag=0)), [(5, bridge.FMB_EVT_REBOOT, 0, -1)])
        self.assertFalse(self.dev["configured"])
        self.assertEqual(self.dev["pending"], self.ranges)
        self.assertNotIn(5, self.mgr._wb_frame_slaves)
        self.poll(END)
        self.assertEqual(self.line.frames[-1][5:7], bytes([5, 0]))
        self.poll(data(bytes.fromhex("02 04 00 fa 01 00"), 1))
        self.assertFalse(self.poller.events)

    def test_only_records_before_a_reboot_use_the_old_subscription(self):
        self.assertTrue(self.configure([1, 1, 1]))
        before = bytes.fromhex("02 04 00 fa 01 00")
        after = bytes.fromhex("02 04 00 fa 02 00")
        self.poll(data(before + REBOOT + after, 3))
        self.assertEqual(self.poller.events, [(INPUT, 250, 1)])
        self.assertEqual(self.dev["pending"], self.ranges)

    def test_zero_count_exceptions_are_limited_to_one_complete_addressed_reboot(self):
        wrong = (data(REBOOT, 0, addr=253), data(REBOOT, 1, addr=248),
                 data(b"", 0), data(REBOOT + REBOOT, 0), data(REBOOT + REBOOT, 1),
                 data(bytes.fromhex("00 0f 00 01"), 0), data(bytes.fromhex("02 04 00 fa 01 00"), 0),
                 data(REBOOT[:-1], 0), data(REBOOT, 0)[:-1] + b"\x00")
        for frame in wrong:
            with self.subTest(frame=frame.hex()):
                self.assertEqual(self.poll(frame), [])
                self.assertFalse(self.mgr._last_poll_answered)
                self.poll(END)
                self.assertEqual(self.line.frames[-1][5:7], bytes([0, 0]))
