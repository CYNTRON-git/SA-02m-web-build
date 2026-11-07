"""MR output commands publish observed values after exactly one write."""
from collections import OrderedDict
import contextlib
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge_mr02m
import bridge_mqtt
import bridge_serial
import mqtt_live_snapshot
from test_mr02m_sibling_error import RecPub


class OutputPub(RecPub):
    def __init__(self):
        super().__init__()
        self.forces = []

    def pub_control(self, did, name, value, force=False):
        super().pub_control(did, name, value, force)
        self.forces.append(force)


def make_poller(kind=6, pub=None):
    pub = pub or OutputPub()
    p = bridge_mr02m.MR02mPoller({"id": "mr-readback", "type": "mr02m",
        "module_type": kind, "address": 2, "channels": {}}, pub)
    p._mod_type = kind
    p._do, p._di, p._ao, p._ai = 2, 0, 2, 0
    port = mock.Mock()
    port.read_coils.return_value = [0]
    port.read_holding_registers.return_value = [200]
    p.get_port = lambda: port
    return p, pub, port


class TestMrReadback(unittest.TestCase):
    def setUp(self):
        self.flush = mock.patch.object(bridge_mqtt.DeviceLiveCache, "flush_file").start()
        self.addCleanup(mock.patch.stopall)

    def test_ack_readback_outcome_matrix(self):
        for kind, target, actual in (("do", True, 0), ("ao", 201, 200)):
            for ack_lost in (False, True):
                for matches in (False, True):
                    with self.subTest(kind=kind, lost=ack_lost, matches=matches):
                        p, pub, port = make_poller()
                        observed = int(target) if matches else actual
                        write = port.write_coil if kind == "do" else port.write_register
                        read = port.read_coils if kind == "do" else port.read_holding_registers
                        read.return_value = [observed]
                        if ack_lost:
                            write.side_effect = TimeoutError("ack lost")
                        getattr(p, "_writeback_" + kind)(1, target)
                        self.assertEqual(write.call_count, 1)
                        read.assert_called_once_with(2, 1 if kind == "do" else 33, 1)
                        self.assertEqual(pub.controls, [(p.device_id, kind + "_1", str(observed))])
                        self.assertEqual(pub.forces, [True])
                        self.assertEqual(pub.errors[-1], (p.device_id, kind + "_1",
                                         "w" if ack_lost and not matches else ""))
                        self.flush.assert_called_with(p.device_id)
                        if kind == "do":
                            port.write_register.assert_not_called()

    def test_callbacks_reject_invalid_boolean_and_nonfinite_numbers(self):
        p, pub, port = make_poller()
        p._ai = 1
        callbacks, jobs = {}, []
        pub.subscribe_writeback = lambda did, name, cb: callbacks.update({name: cb})
        p._wb_submit = lambda name, fn: jobs.append((name, fn))
        p._setup_writeback()
        cases = {
            "do_1": (b"", b"garbage", b"2", b"-1", b"\xff", "да".encode()),
            "ao_1": (b"inf", b"-inf", b"1e10000", b"nan", b"\xff"),
            "ai_type_1": (b"inf", b"-inf", b"1e10000", b"nan", b"\xff"),
        }
        for name, payloads in cases.items():
            for payload in payloads:
                with self.subTest(name=name, payload=payload):
                    jobs.clear()
                    callbacks[name](None, None, types.SimpleNamespace(payload=payload, retain=False))
                    self.assertEqual(jobs, [])
                    self.assertEqual(port.mock_calls, [])
        for payload, expected in ((b"1", True), (b" 0 ", False),
                                  (b"TRUE", True), (b"False", False)):
            with self.subTest(payload=payload):
                jobs.clear()
                callbacks["do_1"](None, None, types.SimpleNamespace(payload=payload, retain=False))
                self.assertEqual(len(jobs), 1)
                with mock.patch.object(p, "_writeback_do") as write:
                    jobs[0][1]()
                    self.assertEqual(write.call_args.args[:2], (1, expected))
                jobs.clear()
                callbacks["do_1"](None, None, types.SimpleNamespace(payload=payload, retain=True))
                self.assertEqual(jobs, [])

    def test_bad_read_never_echoes_target_or_zero(self):
        for kind in ("do", "ao"):
            invalid = ([], [1, 2], ["1"], [None], [1.0], [-1], [1001])
            if kind == "do":
                invalid += ([2],)
            for value in invalid + (TimeoutError("no read"),):
                with self.subTest(kind=kind, value=value):
                    p, pub, port = make_poller()
                    read = port.read_coils if kind == "do" else port.read_holding_registers
                    if isinstance(value, Exception):
                        read.side_effect = value
                    else:
                        read.return_value = value
                    getattr(p, "_writeback_" + kind)(1, True if kind == "do" else 201)
                    self.assertEqual(pub.controls, [])
                    self.assertEqual(pub.errors[-1], (p.device_id, kind + "_1", "w"))
                    self.assertEqual((port.write_coil if kind == "do" else
                                      port.write_register).call_count, 1)

    def test_ao_raw_words_all_supported_types(self):
        for kind in (3, 6, 11, 12, 15):
            for raw in (0, 1, 201, 500, 1000):
                with self.subTest(kind=kind, raw=raw):
                    p, pub, port = make_poller(kind)
                    port.read_holding_registers.return_value = [raw]
                    p._writeback_ao(1, raw)
                    port.write_register.assert_called_once_with(2, 33, raw)
                    self.assertEqual(pub.controls[-1][2], str(raw))

    def test_refusal_does_not_reacquire_or_read(self):
        for refusal in (bridge_serial.ScanHold(), PermissionError("reserved"),
                        RuntimeError("coordinator refused")):
            with self.subTest(refusal=type(refusal).__name__):
                p, pub, port = make_poller()
                port.write_coil.side_effect = refusal
                p._writeback_do(1, True)
                port.write_coil.assert_called_once()
                port.read_coils.assert_not_called()
                self.assertEqual(pub.controls, [])

    def test_guards_before_first_actuation(self):
        for reason in ("offline", "stopped", "disabled", "stale_channel"):
            with self.subTest(reason=reason):
                p, pub, port = make_poller()
                if reason == "offline": p._online = False
                if reason == "stopped": p.stop()
                if reason == "disabled": p._channels = {"do": [{"ch": 1, "enabled": False}]}
                if reason == "stale_channel": p._do = 0
                p._writeback_do(1, True)
                port.write_coil.assert_not_called()
                port.read_coils.assert_not_called()
                self.assertEqual(pub.controls, [])

    def test_stop_after_ack_precludes_speculative_read(self):
        p, pub, port = make_poller()
        port.write_coil.side_effect = lambda *args: p.stop()
        p._writeback_do(1, True)
        port.write_coil.assert_called_once()
        port.read_coils.assert_not_called()
        self.assertEqual(pub.controls, [])
        self.assertEqual(pub.errors[-1][2], "w")

    def test_ao_disabled_and_stale_channel_never_write(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                p, pub, port = make_poller()
                if disabled: p._channels = {"ao": [{"ch": 1, "enabled": False}]}
                else: p._ao = 0
                p._writeback_ao(1, 201)
                port.write_register.assert_not_called()
                port.read_holding_registers.assert_not_called()
                self.assertEqual(pub.controls, [])

    def test_fresh_different_poll_is_not_hidden_by_target_grace(self):
        for kind in ("do", "ao"):
            with self.subTest(kind=kind):
                p, pub, port = make_poller()
                p._do = 1 if kind == "do" else 0
                p._ao = 1 if kind == "ao" else 0
                read = port.read_coils if kind == "do" else port.read_holding_registers
                read.return_value = [1 if kind == "do" else 201]
                getattr(p, "_writeback_" + kind)(1, True if kind == "do" else 201)
                read.return_value = [0 if kind == "do" else 200]
                (p._poll_do_di if kind == "do" else p._poll_ai_ao)()
                self.assertEqual(pub.controls[-1][2], "0" if kind == "do" else "200")

    def test_old_observation_cannot_clear_newer_error_or_watermark(self):
        p, pub, _ = make_poller()
        p._publish_output_observation("do_1", 1, 20, 21, error="w")
        self.assertFalse(p._publish_output_observation("do_1", 0, 19, 22))
        self.assertEqual(pub.controls, [(p.device_id, "do_1", "1")])
        self.assertEqual(pub.errors[-1][2], "w")
        self.assertTrue(p._publish_output_observation("do_1", 1, 23, 24))
        self.assertFalse(p._publish_output_observation("do_1", 0, 22, 25))

    def test_classic_bank_and_command_are_serialized(self):
        for kind in ("do", "ao"):
            with self.subTest(kind=kind):
                p, pub, port = make_poller()
                p._do = 1 if kind == "do" else 0
                p._ao = 1 if kind == "ao" else 0
                entered, release, queued = threading.Event(), threading.Event(), threading.Event()
                errors = []
                calls = []
                def read(*args):
                    calls.append("read")
                    if len(calls) == 1:
                        entered.set()
                        if not release.wait(2): raise AssertionError("read barrier timed out")
                        return [0]
                    return [1]
                (port.read_coils if kind == "do" else port.read_holding_registers).side_effect = read
                def run(fn):
                    try: fn()
                    except BaseException as e: errors.append(e)
                poll = threading.Thread(target=run, args=(p._poll_do_di if kind == "do" else p._poll_ai_ao,))
                def command():
                    queued.set()
                    getattr(p, "_writeback_" + kind)(1, True if kind == "do" else 1)
                writer = threading.Thread(target=run, args=(command,))
                poll.start()
                self.assertTrue(entered.wait(2))
                writer.start()
                self.assertTrue(queued.wait(2))
                release.set()
                poll.join(2); writer.join(2)
                self.assertFalse(poll.is_alive() or writer.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(pub.controls[-1][2], "1")

    def test_old_classic_failure_handler_cannot_overwrite_new_confirmation(self):
        p, pub, port = make_poller()
        p._do, p._ao = 1, 0
        port.read_coils.side_effect = [TimeoutError("old poll failed"), [1]]
        catch_entered, confirmed = threading.Event(), threading.Event()
        errors = []
        def log_failure(*args):
            catch_entered.set()
            if not confirmed.wait(2): raise AssertionError("new write could not complete")
        p.log_poll_fail = log_failure
        def poll():
            try: p._poll_do_di()
            except BaseException as e: errors.append(e)
        thread = threading.Thread(target=poll)
        thread.start()
        try:
            self.assertTrue(catch_entered.wait(2))
            p._writeback_do(1, True)
        finally:
            confirmed.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(pub.controls[-1][2], "1")
        self.assertEqual(pub.errors[-1][2], "")


class TestMrPublisherComposition(unittest.TestCase):
    def test_callback_queue_actual_mqtt_cache_and_snapshot(self):
        scratch = Path(__file__).resolve().parents[3] / ".tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder, contextlib.ExitStack() as stack:
            client = mock.Mock()
            stack.enter_context(mock.patch.object(bridge_mqtt.mqtt, "Client", return_value=client))
            stack.enter_context(mock.patch.object(bridge_mqtt, "LIVE_CACHE_DIR", Path(folder)))
            stack.enter_context(mock.patch.object(mqtt_live_snapshot, "CACHE_DIR", Path(folder)))
            for attr in ("_controls", "_units", "_errors", "_sensor_types", "_online"):
                stack.enter_context(mock.patch.object(bridge_mqtt.DeviceLiveCache, attr, {}))
            pub = bridge_mqtt.MQTTPublisher({})
            p, _, port = make_poller(pub=pub)
            callbacks = {}
            pub.subscribe_writeback = lambda did, name, cb: callbacks.update({name: cb})
            # Run the production per-port coalescing queue without creating its
            # permanent daemon thread; execute the exact queued callback job.
            worker = bridge_serial.WritebackWorker.__new__(bridge_serial.WritebackWorker)
            worker._cond = threading.Condition()
            worker._jobs = OrderedDict()
            stack.enter_context(mock.patch.object(bridge_serial.WritebackWorker, "for_port", return_value=worker))
            p._setup_writeback()
            for name, target, actual in (("do_1", "1", "0"), ("ao_1", "201", "200")):
                for retain in (True, False):
                    callbacks[name](None, None, types.SimpleNamespace(payload=target.encode(), retain=retain))
                    self.assertEqual(len(worker._jobs), 0 if retain else 1)
                _, job = worker._jobs.popitem(last=False)
                job()
                data = mqtt_live_snapshot.snapshot(p.device_id)
                self.assertEqual(data["controls"][name], actual)
                self.assertNotIn(name, data["errors"])
                topic = "/devices/" + p.device_id + "/controls/" + name
                values = [c.args[1] for c in client.publish.call_args_list if c.args[0] == topic]
                self.assertEqual(values, [actual])
            port.read_coils.return_value = [1]
            for _ in range(2): p._writeback_do(1, True)
            topic = "/devices/" + p.device_id + "/controls/do_1"
            self.assertEqual([c.args[1] for c in client.publish.call_args_list if c.args[0] == topic], ["0", "1", "1"])
            port.read_coils.side_effect = TimeoutError("readback failed")
            p._writeback_do(1, False)
            data = mqtt_live_snapshot.snapshot(p.device_id)
            self.assertEqual(data["controls"]["do_1"], "1")
            self.assertEqual(data["errors"], {"do_1": "w"})


if __name__ == "__main__":
    unittest.main()
