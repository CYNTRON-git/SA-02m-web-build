"""An intentional scan interruption is not a failed device read."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge_device as device
import bridge_dtv_ce as ce
import bridge_fmb as fmb
import bridge_mr02m as mr
import bridge_serial as serial
import bridge_spodes as spodes


def state(p):
    return (p._online, p._fail_count, p._backoff_until,
            p._classic_ok_count, list(p.pub.mock_calls))


class TestPauseAvailability(unittest.TestCase):
    def test_pause_log_is_debug_while_real_timeout_stays_warning(self):
        p = device.DevicePoller({"id": "log-pause"}, mock.Mock())
        p.log = mock.Mock()
        p.log_poll_fail("read", serial.ScanHold())
        p.log.warning.assert_not_called()
        p.log.debug.assert_called_once_with("%s: scan hold", "read")
        p.log_poll_fail("read", TimeoutError("timeout"))
        p.log.warning.assert_called_once()

    def test_unknown_healthy_and_offline_are_unchanged_until_real_success(self):
        for online, fails, deadline in ((None, 0, 0), (True, 1, 0), (False, 7, 900)):
            with self.subTest(online=online), mock.patch.object(
                    device.DeviceLiveCache, "set_online") as cache:
                p = device.DevicePoller({"id": "pause"}, mock.Mock())
                p._online, p._fail_count, p._backoff_until = online, fails, deadline
                before = state(p)
                for _ in range(4):
                    with self.assertRaises(serial.ScanHold):
                        p._io(mock.Mock(side_effect=serial.ScanHold))
                self.assertEqual(state(p), before)
                cache.assert_not_called()
                self.assertEqual(p._io(lambda: [42]), [42])
                self.assertIs(p._online, True)
                self.assertEqual((p._fail_count, p._backoff_until), (0, 0))
                cache.assert_called_once_with("pause", True)

    def test_real_timeout_still_counts_even_if_hold_armed_during_read(self):
        p = device.DevicePoller({"id": "real-fault"}, mock.Mock())
        sched = device.PortCycleScheduler("/dev/COM4", 115200, [p])
        def fail():
            sched._scan_hold.set()
            raise TimeoutError("wire timeout")
        with mock.patch.object(device.time, "monotonic", return_value=100):
            for _ in range(3):
                with self.assertRaises(TimeoutError):
                    p._io(fail)
        self.assertEqual(p._fail_count, 3)
        self.assertFalse(p._online)
        self.assertEqual(p._backoff_until, 102)
        p.pub.device_online.assert_called_once_with("real-fault", False)


class TestStatefulPause(unittest.TestCase):
    def test_read_leaf_handlers_propagate_pause_without_followup_reads(self):
        import bridge_carel
        cases = [
            (mr.MR02mPoller, "_init_module", {}),
            (mr.MR02mPoller, "_apply_configured_ai_sensor_types", {"_ai": 1}),
            (mr.MR02mPoller, "_poll_do_di", {"_do": 1, "_di": 1}),
            (mr.MR02mPoller, "_poll_do_di", {"_do": 0, "_di": 1}),
            (mr.MR02mPoller, "_poll_di_counters", {"_di": 1}),
            (mr.MR02mPoller, "_refresh_di_modes", {"_di": 1}),
            (mr.MR02mPoller, "_poll_uptime", {}),
            (mr.MR02mPoller, "_poll_diag", {"_di": 1}),
            (ce.DTVPoller, "_try_autodetect", {}),
            (ce.DTVPoller, "_poll_sensors", {}),
            (ce.DTVPoller, "_poll_coils", {}),
            (ce.DTVPoller, "_poll_uptime", {}),
            (ce.DTVPoller, "_poll_diag", {}),
            (ce.DTVPoller, "_poll_ext_temp_mode", {}),
            (ce.CE02M3Poller, "_poll_power", {}),
            (ce.CE02M3Poller, "_read_ct_ratios", {}),
            (ce.CE02M3Poller, "_poll_energy", {"_fw_version": (1, 0, 8, 0), "_en_ener": True}),
            (ce.CE02M3Poller, "_poll_uptime", {}),
            (ce.CE02M3Poller, "_poll_diag", {}),
            (ce.CE02M3Poller, "_read_fw_version", {"_fw_version": (1, 0, 8, 0)}),
            (bridge_carel.CarelPoller, "_identify", {}),
            (bridge_carel.CarelPoller, "_read_snapshot", {}),
        ]
        for cls, method, attrs in cases:
            with self.subTest(poller=cls.__name__, method=method):
                p = cls({"id": "read-pause", "channels": {"ai": [{"ch": 1, "sensor_type": 1}]}}, mock.Mock())
                p.__dict__.update(attrs)
                if method == "_apply_configured_ai_sensor_types":
                    p._ai_effective_sensor_type = lambda _: 1
                port = mock.Mock()
                for name in ("read_input_registers", "read_holding_registers", "read_coils", "report_slave_id"):
                    getattr(port, name).side_effect = serial.ScanHold
                p.get_port = mock.Mock(return_value=port)
                before = state(p)
                with self.assertRaises(serial.ScanHold):
                    getattr(p, method)()
                self.assertEqual(state(p), before)
                self.assertEqual(len(port.mock_calls), 1)
                if method == "_read_fw_version":
                    self.assertEqual(p._fw_version, (1, 0, 8, 0))
                    self.assertFalse(p._fw_read_warned)

    def test_mr_each_diagnostic_branch_stops_on_pause(self):
        for channel in ("serial", "mcu_vdd", "op_days", "mcu_ram_free"):
            with self.subTest(channel=channel):
                p, port = self.mr_poller(serial.ScanHold)
                p._sys_enabled = lambda name: name == channel
                with self.assertRaises(serial.ScanHold):
                    p._poll_diag()
                self.assertEqual(len(port.mock_calls), 1)
                p.pub.pub_control.assert_not_called()
                self.assertEqual(p._fail_count, 0)

    def test_ce_cycle_probe_pause_does_not_create_meter_link_fault(self):
        p = ce.CE02M3Poller({"id": "ce-probe"}, mock.Mock())
        p._fw_version = (1, 0, 8, 0)
        p._cycles_sample = 12
        p.get_port = mock.Mock()
        p.get_port.return_value.read_input_registers.side_effect = serial.ScanHold
        with self.assertRaises(serial.ScanHold):
            p._meter_link_check(True)
        self.assertEqual(p._cycles_sample, 12)
        self.assertFalse(p._meter_link_lost)
        p.pub.pub_error.assert_not_called()

    def test_ce_per_phase_pause_keeps_completed_totals_without_false_unit_marker(self):
        p = ce.CE02M3Poller({"id": "ce-phases"}, mock.Mock())
        p._fw_version = (1, 0, 8, 0)
        p._per_phase_energy = True
        p.get_port = mock.Mock()
        p.get_port.return_value.read_input_registers.side_effect = [[1] * 20, serial.ScanHold()]
        with self.assertRaises(serial.ScanHold):
            p._poll_energy()
        self.assertEqual(p._energy_converted, {"totals"})
        self.assertFalse(p._energy_unit_announced)
        self.assertEqual(p._fail_count, 0)
        p.pub.pub_error.assert_not_called()

    def test_fmb_firmware_read_pause_is_not_an_unknown_firmware_failure(self):
        p = ce.CE02M3Poller({"id": "ce-firmware"}, mock.Mock())
        p.get_port = mock.Mock()
        p.get_port.return_value.read_holding_registers.side_effect = serial.ScanHold
        mgr = fmb.FastModbusEventPortManager("/dev/COM4", 115200)
        mgr.register_device(14, p.device_id, [(3, 500, 1)], mock.Mock(), poller=p, dev_type="ce02m3")
        dev = mgr._devices[14]
        with mock.patch.object(fmb, "_get_port", return_value=mock.Mock()):
            with self.assertRaises(serial.ScanHold):
                mgr.configure_all()
        self.assertFalse(dev["fw_getter_logged"])
        self.assertEqual((dev["fail_passes"], dev["retry_at"]), (0, 0))

    def test_led_required_snapshot_pause_propagates(self):
        import test_led_poller as led_tests
        p, pub, port = led_tests._poller()
        with mock.patch.object(port, "read_holding_registers", side_effect=serial.ScanHold) as read:
            with self.assertRaises(serial.ScanHold):
                p._read_snapshot()
        self.assertEqual(read.call_count, 1)
        pub.pub_error.assert_not_called()

    def test_template_single_pause_does_not_publish_or_change_probe(self):
        import test_template_poller as template_tests
        p, pub = template_tests._poller("example", template_tests.TEMPLATES_DIR)
        unit = p._units[0]
        before = (p._fresh_fails, unit.alive, unit.sweep_at, p._probing)
        with mock.patch.object(p, "_read_channel", side_effect=serial.ScanHold):
            with self.assertRaises(serial.ScanHold):
                p._poll_single(unit.channels[0], unit)
        self.assertEqual((p._fresh_fails, unit.alive, unit.sweep_at, p._probing), before)
        pub.pub_error.assert_not_called()

    def test_template_block_pause_does_not_start_single_sweep(self):
        import test_template_poller as template_tests
        import bridge_template
        p, pub = template_tests._poller("example", template_tests.TEMPLATES_DIR)
        unit = bridge_template._ReadUnit(p._channels[:2], p._channels[0].reg_type, 0)
        with mock.patch.object(p, "_read_span", side_effect=serial.ScanHold), \
                mock.patch.object(p, "_read_channel", return_value=[0] * 8) as single:
            with self.assertRaises(serial.ScanHold):
                p._poll_unit(unit, [])
        self.assertIsNone(unit.sweep_at)
        self.assertEqual(p._fresh_fails, 0)
        single.assert_not_called()
        pub.pub_error.assert_not_called()

    def test_led_optional_pause_does_not_paint_aux_and_text_faults(self):
        import test_led_poller as led_tests
        p, pub, port = led_tests._poller()
        with mock.patch.object(port, "read_holding_registers", side_effect=serial.ScanHold) as read:
            with self.assertRaises(serial.ScanHold):
                p.poll_slow_if_due(1000000)
        self.assertEqual(read.call_count, 1)
        pub.pub_error.assert_not_called()

    def test_carel_pause_does_not_retry_alarm_block(self):
        import bridge_carel
        p = bridge_carel.CarelPoller({"id": "carel-pause"}, mock.Mock())
        with mock.patch.object(p, "read_input_registers", side_effect=serial.ScanHold) as read:
            with self.assertRaises(serial.ScanHold):
                p._read_alarm_packs(1)
        self.assertEqual(read.call_count, 1)

    def mr_poller(self, exc):
        p = mr.MR02mPoller({"id": "mr-pause"}, mock.Mock())
        port = mock.Mock(max_read_regs=7)
        port.read_holding_registers.side_effect = exc
        port.read_input_registers.side_effect = exc
        p.get_port = mock.Mock(return_value=port)
        return p, port

    def test_ai_pause_no_channel_error_no_retry_or_next_chunk(self):
        p, port = self.mr_poller(serial.ScanHold)
        p._ai = 2
        with self.assertRaises(serial.ScanHold):
            p._poll_ai_ao()
        self.assertEqual(port.read_holding_registers.call_count, 1)
        p.pub.pub_error.assert_not_called()
        p.pub.pub_control.assert_not_called()
        self.assertEqual(p._fail_count, 0)

    def test_ai_real_timeout_still_marks_missing_channels(self):
        p, _ = self.mr_poller(TimeoutError)
        p._ai = 1
        with mock.patch.object(mr.time, "sleep"):
            p._poll_ai_ao()
        p.pub.pub_error.assert_called_with(p.device_id, "ai_1", "r")
        self.assertEqual(p._fail_count, mr.MR02M_AI_READ_RETRIES)

    def test_ao_pause_stops_retries(self):
        p, port = self.mr_poller(serial.ScanHold)
        p._ao = 1
        with self.assertRaises(serial.ScanHold):
            p._poll_ai_ao()
        self.assertEqual(port.read_holding_registers.call_count, 1)
        p.pub.pub_control.assert_not_called()

    def test_di_press_pause_preserves_existing_failure_count(self):
        p, port = self.mr_poller(serial.ScanHold)
        p._di_button = {1}
        p._press_fails = 2
        with self.assertRaises(serial.ScanHold):
            p._poll_di_press_counters()
        self.assertEqual(p._press_fails, 2)
        self.assertFalse(p._press_disabled)
        self.assertEqual(port.read_input_registers.call_count, 1)
        p.pub.pub_control.assert_not_called()

    def test_di_press_real_timeout_keeps_disable_threshold(self):
        p, _ = self.mr_poller(TimeoutError)
        p._di_button = {1}
        p._press_fails = 2
        p._poll_di_press_counters()
        self.assertTrue(p._press_disabled)
        self.assertEqual(p._press_fails, 3)

    def test_ce_chunk_pause_is_neutral_but_timeout_is_one_failed_block(self):
        for exc in (serial.ScanHold, TimeoutError):
            with self.subTest(exc=exc), mock.patch.object(ce.time, "sleep"):
                p = ce.CE02M3Poller({"id": "ce-pause"}, mock.Mock())
                port = mock.Mock()
                port.read_input_registers.side_effect = exc
                p.get_port = mock.Mock(return_value=port)
                with self.assertRaises(exc):
                    p._read_power_input_block()
                paused = exc is serial.ScanHold
                self.assertEqual(p._fail_count, 0 if paused else 1)
                self.assertEqual(port.read_input_registers.call_count, 1 if paused else 3)
                p.pub.pub_control.assert_not_called()

    def test_spodes_power_energy_and_profile_pause_propagate(self):
        for method in ("poll_io", "_poll_energy", "_poll_profiles"):
            with self.subTest(method=method), mock.patch.object(spodes.time, "monotonic", return_value=100):
                p = spodes.SpodesPoller({"id": "sp-pause", "type": "spodes"}, mock.Mock())
                p._open = mock.Mock(side_effect=serial.ScanHold)
                p._drop = mock.Mock()
                before = state(p)
                with self.assertRaises(serial.ScanHold):
                    getattr(p, method)()
                self.assertEqual(state(p), before)
                p._drop.assert_not_called()

    def test_spodes_real_timeout_still_counts(self):
        for method in ("poll_io", "_poll_energy"):
            with self.subTest(method=method), mock.patch.object(spodes.time, "monotonic", return_value=100):
                p = spodes.SpodesPoller({"id": "sp-fault", "type": "spodes"}, mock.Mock())
                p._open = mock.Mock(side_effect=TimeoutError)
                p._drop = mock.Mock()
                getattr(p, method)()
                self.assertEqual(p._fail_count, 1)
                p._drop.assert_called_once()

    def test_fmb_configure_pause_preserves_pending_ack_and_backoff(self):
        for method in ("configure_all", "reconfigure_pending"):
            with self.subTest(method=method):
                mgr = fmb.FastModbusEventPortManager("/dev/COM4", 115200)
                mgr.register_device(10, "mr", [(0, 1, 6), (1, 1, 5)], mock.Mock(), wire_mode="legacy")
                dev = mgr._devices[10]
                dev["fail_passes"] = 2
                mgr._ack_slave, mgr._ack_flag = 9, 1
                before = copy.deepcopy(dev)
                port = mock.Mock()
                port.fmb_send_recv.side_effect = serial.ScanHold
                with mock.patch.object(fmb, "_get_port", return_value=port), mock.patch.object(fmb.time, "sleep"):
                    with self.assertRaises(serial.ScanHold):
                        getattr(mgr, method)()
                for key in ("pending", "ranges", "configured", "retry_at", "fail_passes"):
                    self.assertEqual(dev[key], before[key], key)
                self.assertEqual((mgr._ack_slave, mgr._ack_flag), (9, 1))
                self.assertEqual(port.fmb_send_recv.call_count, 1)

    def test_fmb_pause_after_ack_preserves_only_unacknowledged_ranges(self):
        mgr = fmb.FastModbusEventPortManager("/dev/COM4", 115200)
        ranges = [(0, 1, 6), (1, 1, 5)]
        mgr.register_device(10, "mr", ranges, mock.Mock(), wire_mode="legacy")
        dev = mgr._devices[10]
        port = mock.Mock()
        port.fmb_send_recv.side_effect = [bytes.fromhex("0a4618010028de"), serial.ScanHold()]
        with mock.patch.object(fmb, "_get_port", return_value=port):
            with self.assertRaises(serial.ScanHold):
                mgr.configure_all()
        self.assertEqual(dev["ranges"], ranges)
        self.assertEqual(dev["pending"], ranges[1:])
        self.assertTrue(dev["configured"])
        self.assertEqual((dev["fail_passes"], dev["retry_at"]), (0, 0))
        self.assertEqual(mgr._wb_frame_slaves, {10: False})
        port.fmb_send_recv.side_effect = None
        port.fmb_send_recv.return_value = bytes.fromhex("0a4618010028de")
        with mock.patch.object(fmb, "_get_port", return_value=port):
            mgr.reconfigure_pending()
        self.assertEqual(dev["pending"], [])
        self.assertEqual(port.fmb_send_recv.call_count, 3)

    def test_fmb_post_configure_pause_keeps_real_ack_without_failure(self):
        poller = mock.Mock()
        poller.fmb_event_ranges.return_value = [(0, 1, 6)]
        poller.poll_io.side_effect = serial.ScanHold
        mgr = fmb.FastModbusEventPortManager("/dev/COM4", 115200)
        mgr.register_device(10, "mr", [(0, 1, 6)], mock.Mock(), poller=poller, wire_mode="legacy")
        port = mock.Mock()
        port.fmb_send_recv.return_value = bytes.fromhex("0a4618010028de")
        with mock.patch.object(fmb, "_get_port", return_value=port):
            with self.assertRaises(serial.ScanHold):
                mgr.configure_all()
        dev = mgr._devices[10]
        self.assertEqual(dev["pending"], [])
        self.assertTrue(dev["configured"])
        self.assertEqual((dev["fail_passes"], dev["retry_at"]), (0, 0))
        poller.set_fmb_io_covered.assert_called_once_with(True)


class TestSchedulerPause(unittest.TestCase):
    def setUp(self):
        global FakeFmb, FakePoller
        from test_port_cycle import FakeFmb, FakePoller

    def test_hold_between_classic_pollers_stops_fast_and_slow_work(self):
        a, b = FakePoller("a"), FakePoller("b")
        sched = device.PortCycleScheduler("/dev/COM4", 115200, [a, b])
        a.on_poll_io = lambda _: sched._scan_hold.set()
        with self.assertRaises(serial.ScanHold):
            sched._classic_slice(1, True)
        self.assertEqual((a.io_count, b.io_count, a.slow_count, b.slow_count), (1, 0, 0, 0))

    def test_slow_path_hold_interrupts_remaining_slow_pollers(self):
        a, b = FakePoller("a"), FakePoller("b")
        sched = device.PortCycleScheduler("/dev/COM4", 115200, [a, b])
        def pause(_):
            sched._scan_hold.set()
            raise serial.ScanHold()
        a.poll_slow_if_due = pause
        with self.assertRaises(serial.ScanHold):
            sched._classic_slice(0, False)
        self.assertEqual(b.slow_count, 0)

    def test_event_pause_never_disarms_insurance(self):
        mgr = FakeFmb()
        sched = device.PortCycleScheduler("/dev/COM4", 115200, [], mgr)
        def burst(me):
            if me.burst_count == 12:
                sched.stop()
            raise serial.ScanHold()
        mgr.on_burst = burst
        with mock.patch.object(device.time, "sleep"), mock.patch.object(sched._stop, "wait", return_value=False):
            sched.run()
        self.assertEqual(mgr.burst_count, 12)
        self.assertNotIn(("set_insurance", False), mgr.calls)

    def test_initial_fmb_pause_survives_and_remains_retryable(self):
        mgr = FakeFmb()
        mgr.configure_all = mock.Mock(side_effect=serial.ScanHold)
        sched = device.PortCycleScheduler("/dev/COM4", 115200, [], mgr)
        mgr.on_burst = lambda _: sched.stop()
        with mock.patch.object(device.time, "sleep"), mock.patch.object(sched._stop, "wait", return_value=False):
            sched.run()
        self.assertEqual(mgr.burst_count, 1)

    def test_setup_pause_does_not_clear_errors_until_setup_completes(self):
        for cls in (device.PortCycleScheduler, device.HdlcPortScheduler):
            with self.subTest(cls=cls):
                p = FakePoller()
                p.clear_stale_control_errors = mock.Mock()
                sched = (cls("/dev/COM4", 115200, [p]) if cls is device.PortCycleScheduler
                         else cls("/dev/COM4", [p]))
                attempts = []
                def setup():
                    attempts.append(1)
                    if len(attempts) == 1:
                        sched._scan_hold.set()
                        raise serial.ScanHold()
                p.setup = setup
                p.on_poll_io = lambda _: sched.stop()
                def parked(_):
                    p.clear_stale_control_errors.assert_not_called()
                    sched._scan_hold.clear()
                    return False
                with mock.patch.object(device.time, "sleep"), mock.patch.object(sched._stop, "wait", side_effect=parked):
                    sched.run()
                self.assertEqual(len(attempts), 2)
                p.clear_stale_control_errors.assert_called_once()

    def test_hdlc_hold_between_pollers_skips_slow_and_next_device(self):
        a, b = FakePoller("a"), FakePoller("b")
        sched = device.HdlcPortScheduler("/dev/COM4", [a, b])
        a.on_poll_io = lambda _: sched._scan_hold.set()
        parks = []
        def wait(delay):
            parks.append(delay)
            self.assertEqual((b.io_count, a.slow_count, b.slow_count), (0, 0, 0))
            if len(parks) == 3:
                sched._scan_hold.clear()
                a.on_poll_io = lambda _: sched.stop()
            return False
        with mock.patch.object(sched._stop, "wait", side_effect=wait):
            sched.run()
        self.assertEqual(a.io_count, 2)
        self.assertEqual(parks, [0.05] * 3)

    def test_mid_read_race_parks_and_resumes_without_backoff(self):
        p = device.DevicePoller({"id": "race"}, mock.Mock())
        other = FakePoller("other")
        other_sched = device.PortCycleScheduler("/dev/COM3", 115200, [other])
        sched = device.PortCycleScheduler("/dev/COM4", 115200, [p])
        calls, parked = [], []
        def read():
            calls.append(1)
            if len(calls) == 2:
                sched._scan_hold.set()
                raise serial.ScanHold()
            if len(calls) == 3:
                sched.stop()
            return [7]
        p.poll_io = lambda: p._io(read)
        def wait(_):
            if sched._scan_hold.is_set():
                self.assertEqual(len(calls), 2)
                self.assertEqual((p._fail_count, p._backoff_until, p._online), (0, 0, True))
                other_sched._classic_slice(1, True)
                parked.append(1)
                if len(parked) == 3:
                    sched._scan_hold.clear()
            return False
        with mock.patch.object(device.time, "sleep"), mock.patch.object(sched._stop, "wait", side_effect=wait):
            sched.run()
        self.assertEqual((len(calls), len(parked), other.io_count), (3, 3, 3))
        p.pub.device_online.assert_called_once_with("race", True)


def entry_probe():
    """Fresh script-mode entry, real composition/serve/handler, mocked wire only."""
    import ast
    import json
    import socket
    import tempfile
    import threading
    import bridge_scan_lease as lease

    assert "modbus_mqtt_bridge" not in sys.modules
    path = Path(__file__).resolve().parents[1] / "modbus_mqtt_bridge.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body.pop()  # Defer only the final main() call to install startup seams.
    ns = {"__name__": "__main__", "__file__": str(path)}
    exec(compile(tree, str(path), "exec"), ns)
    pub = mock.Mock()
    reads = []
    class Poller(device.DevicePoller):
        def __init__(self, cfg, publisher):
            super().__init__(cfg, publisher)
            if self.device_id == "broken":
                self._online, self._fail_count, self._backoff_until = False, 7, 1e20

        def poll_io(self):
            self._io(lambda: reads.append(self.device_id))
            self.pub.pub_control(self.device_id, "value", "7")
    cfg = {"devices": [
        {"id": "target", "type": "template", "port": "/dev/COM4", "address": 10},
        {"id": "other", "type": "template", "port": "/dev/COM3", "address": 11},
        {"id": "broken", "type": "template", "port": "/dev/COM4", "address": 12},
    ]}
    ns.update(load_config=lambda: cfg, MQTTPublisher=lambda _: pub,
              sd_notify=lambda _: None, write_bridge_roster=lambda *_: None)
    ns["POLLER_CLASSES"] = {"template": Poller}
    ns["_stop_ev"].set()
    actual_serve = lease.serve
    actual_socket = socket.socket
    sockets = []
    def record_socket(*args, **kwargs):
        sock = actual_socket(*args, **kwargs)
        sockets.append(sock)
        return sock
    with tempfile.TemporaryDirectory() as tmp:
        def serve(**kwargs):
            return actual_serve(str(Path(tmp) / "scan.sock"), **kwargs)
        with mock.patch.object(lease, "serve", side_effect=serve), \
                mock.patch.object(threading, "Thread"), mock.patch.object(socket, "socket", side_effect=record_socket), \
                mock.patch.object(device.time, "sleep"), mock.patch.object(ns["signal"], "signal"):
            ns["main"]()
        try:
            target, other = ns["_port_schedulers"]
            assert lease._live_schedulers() == [target, other]
            assert "modbus_mqtt_bridge" not in sys.modules
            target._classic_slice(1, True)
            healthy = state(target._pollers[0])
            broken = state(target._pollers[1])[:4]
            a, b = socket.socketpair()
            with mock.patch.object(serial, "hold_com", return_value=True), \
                    mock.patch.object(serial, "release_com"), mock.patch.object(lease, "trace"):
                worker = threading.Thread(target=lease.handle, args=(b,), daemon=True)
                worker.start()
                a.settimeout(2)
                a.sendall(b'{"via":"com","port":"/dev/COM4"}\n')
                assert json.loads(a.recv(1024))["held"]
                assert target._scan_hold.is_set() and not other._scan_hold.is_set()
                second, second_server = socket.socketpair()
                second_worker = threading.Thread(target=lease.handle, args=(second_server,), daemon=True)
                second_worker.start()
                second.settimeout(2)
                second.sendall(b'{"via":"com","port":"/dev/COM4"}\n')
                assert json.loads(second.recv(1024))["held"]
                for _ in range(4):
                    try:
                        target._classic_slice(1, True)
                    except serial.ScanHold:
                        pass
                    other._classic_slice(1, True)
                assert reads.count("target") == 1 and reads.count("other") == 4
                # Shared publisher includes other-line values; target availability is unchanged.
                assert state(target._pollers[0])[:4] == healthy[:4]
                assert mock.call.device_online("target", False) not in pub.mock_calls
                a.close()
                worker.join(2)
                assert not worker.is_alive() and target._scan_hold.is_set()
                second.close()
                second_worker.join(2)
                assert not second_worker.is_alive() and not target._scan_hold.is_set()
            target._classic_slice(1, True)
            assert reads.count("target") == 2
            assert "broken" not in reads and state(target._pollers[1])[:4] == broken
            assert pub.pub_control.call_args == mock.call("target", "value", "7")
        finally:
            for sock in sockets:
                sock.close()


class TestEntryComposition(unittest.TestCase):
    def test_script_mode_uses_live_registry_and_only_target_line_pauses(self):
        import subprocess
        result = subprocess.run([sys.executable, __file__, "--entry-probe"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    if sys.argv[1:] == ["--entry-probe"]:
        entry_probe()
    else:
        unittest.main()
