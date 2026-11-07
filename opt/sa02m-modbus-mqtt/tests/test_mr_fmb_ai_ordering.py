"""WB INPUT records carry a raw AI sample and no sensor-type generation."""
import itertools
import time
from unittest import mock

import bridge_fmb
import bridge_mr02m
from bridge_serial import _append_crc, FMB_EVT_INPUT
from test_fmb_mr_reply_truth import SerialReplies, reply
from test_mr02m_ai_register_truth import _Base, _poller, DeviceLiveCache, FakeModule
from test_mr_fmb_output_ordering import empty, event


def ai(ch, raw, slave=6):
    return event(raw, slave, 403 + (ch - 1) * 7, FMB_EVT_INPUT)


def reboot_then_sample(slave=6):
    data = bytes.fromhex('000f0000') + ai(1, 1000, slave)[6:-2]
    return _append_crc(bytes([slave, 0x46, 0x11, 7, 2, len(data)]) + data)


class TestAiEventOrdering(_Base):
    def setUp(self):
        super().setUp()
        ticks = itertools.count(1000)
        clock = mock.patch.object(time, "monotonic", side_effect=lambda: next(ticks) / 1000)
        clock.start()
        self.addCleanup(clock.stop)
        for name in ("_sensor_types", "_sensor_type_confirmations", "_units"):
            patcher = mock.patch.object(DeviceLiveCache, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)

    def setup_target(self, code, *, p_enabled=True, n_code=0, cold=False):
        self.module = FakeModule({1: code, 2: n_code})
        self.module.set_value(1, 215)
        self.module.set_value(2, 379)
        self.poller, self.pub = _poller([
            {"ch": 1, "enabled": p_enabled, **({} if cold else {"sensor_type": code})},
            {"ch": 2, "enabled": True},
        ], self.module)
        self.poller._persist_ai_sensor_types = mock.Mock()
        self.manager = bridge_fmb.FastModbusEventPortManager("fixture", 115200)
        self.manager.register_device(6, self.poller.device_id, self.poller.fmb_event_ranges(),
                                     self.poller.fmb_dispatch, self.poller, "mr02m")
        self.manager._devices[6]["configured"] = True
        self.manager._wb_frame_slaves[6] = True
        self.manager.register_device(7, "neighbour", [], lambda *args: None)
        self.manager._devices[7]["configured"] = True
        if not cold:
            self.poller._poll_ai_ao()
            self.burst(empty(6))

    def burst(self, *frames):
        serial = SerialReplies([*frames, b""])
        with mock.patch.object(bridge_fmb, "_get_port", return_value=serial):
            self.manager.event_burst()

    def test_cold_fifo_preserves_failed_read_until_type_sample_and_own_empty(self):
        self.setup_target(37, cold=True)
        self.module.set_value(1, 1000)
        self.pub.pub_error = mock.Mock()
        with mock.patch.object(self.poller, 'read_holding_registers', side_effect=IOError('unreadable')):
            self.poller._poll_ai_ao()
        self.pub.pub_error.assert_any_call(self.poller.device_id, 'ai_1', 'r')
        for retained in (None, 3):
            if retained is not None:
                DeviceLiveCache.set_sensor_type(self.poller.device_id, 1, retained)
            self.pub.pub_error.reset_mock()
            self.burst(ai(1, 1000), empty(6))
            self.assertFalse(self.pub.controls)
            self.pub.pub_error.assert_not_called()
        self.poller._poll_ai_ao()
        self.assertEqual(self.last_value(self.pub, 'ai_1'), '1.0')
        self.pub.controls.clear()
        self.burst(ai(1, 204), empty(7))
        self.burst(ai(1, 205))
        self.assertFalse(self.pub.controls)
        self.burst(empty(6))
        self.burst(ai(1, 1100))
        self.assertEqual(self.pub.controls, [('ai_1', '1.1')])

    def test_unknown_masked_parent_keeps_n_unavailable_while_independent_zero_drains(self):
        self.setup_target(41, p_enabled=False, cold=True)
        self.pub.pub_error = mock.Mock()
        read = self.poller._read_ai_holding_block
        def missing_parent():
            block = read()
            block[:7] = [None]*7
            return block
        with mock.patch.object(self.poller, '_read_ai_holding_block', missing_parent):
            self.poller._poll_ai_ao()
        self.pub.pub_error.assert_any_call(self.poller.device_id, 'ai_2', 'r')
        self.assertNotIn('ai_2', dict(self.pub.controls))
        self.assertEqual(self.poller._ai_types[3], 0)
        self.pub.controls.clear()
        self.pub.pub_error.reset_mock()
        self.burst(empty(6))
        self.burst(ai(1, 123), ai(2, 456), ai(3, 17))
        self.assertEqual(self.pub.controls, [('ai_3', '17.0')])
        self.pub.pub_error.assert_called_once_with(self.poller.device_id, 'ai_3', '')
        self.poller._poll_ai_ao()
        self.assertEqual(self.last_value(self.pub, 'ai_2'), '21.5')
        self.pub.controls.clear()
        self.burst(ai(1, 231), ai(2, 456), ai(3, 19))
        self.assertEqual(self.pub.controls, [('ai_3', '19.0')])
        self.burst(empty(6))
        self.burst(ai(1, 252))
        self.assertEqual(self.pub.controls[-1], ('ai_2', '25.2'))

    def test_unknown_type_or_missing_pair_sample_cannot_reopen_a_channel(self):
        self.setup_target(41, p_enabled=False)
        self.pub.pub_error = mock.Mock()
        for reg in (0, 3):
            with self.subTest(parent_register=reg):
                block = self.poller._read_ai_holding_block()
                block[reg] = 65535 if reg == 0 else None
                with mock.patch.object(self.poller, '_read_ai_holding_block', return_value=block):
                    self.poller._poll_ai_ao()
                self.pub.pub_error.assert_any_call(self.poller.device_id, 'ai_2', 'r')
                self.pub.controls.clear()
                self.burst(empty(6))
                self.burst(ai(1, 250), ai(2, 202))
                self.assertFalse(self.pub.controls)
                self.poller._poll_ai_ao()
                self.burst(empty(6))
                self.burst(ai(1, 250))
                self.assertEqual(self.pub.controls[-1], ('ai_2', '25.0'))

    def test_untyped_old_fifo_after_confirmed_voltage_and_before_own_empty(self):
        self.setup_target(3)
        self.module.set_value(1, 1000)
        self.poller._writeback_ai_type(1, 37)
        self.assertEqual(self.last_value(self.pub, "ai_1"), "1.0")
        self.pub.pub_error = mock.Mock()
        self.burst(ai(1, 204))
        self.assertEqual(self.last_value(self.pub, "ai_1"), "1.0")
        self.pub.pub_error.assert_not_called()
        self.burst(empty(7))
        self.module.set_value(1, 1100)
        self.poller._poll_ai_ao()
        self.burst(ai(1, 205))
        self.assertEqual(self.last_value(self.pub, "ai_1"), "1.1")
        self.assertTrue(self.poller.fmb_needs_own_empty())
        self.burst(empty(6))
        self.burst(ai(1, 1200))
        self.assertEqual(self.last_value(self.pub, "ai_1"), "1.2")

    def test_late_query_cannot_supersede_classic_read(self):
        self.setup_target(3)
        valid = self.manager._poll_once(SerialReplies([ai(1, 204)]))
        self.assertTrue(valid[0])
        observation = self.manager._poll_observation
        self.module.set_value(1, 288)
        self.poller._poll_ai_ao()
        self.manager._dispatch(*valid[1][0], observation=observation)
        self.assertEqual(self.last_value(self.pub, "ai_1"), "28.8")

    def test_reboot_requires_new_type_sample_and_own_empty(self):
        self.setup_target(3)
        self.module.set_type(1, 37)
        self.module.set_value(1, 1000)
        self.pub.controls.clear()
        self.burst(reboot_then_sample(7), ai(1, 201))
        self.assertEqual(self.pub.controls, [('ai_1', '20.1')])
        self.pub.controls.clear()
        self.burst(reboot_then_sample(), empty(6))
        self.burst(ai(1, 1000))
        self.assertEqual(self.pub.controls, [])
        self.poller._poll_ai_ao()
        self.assertEqual(self.last_value(self.pub, 'ai_1'), '1.0')
        serial = SerialReplies([reply(bytes.fromhex('ffffffff0f'), addr=6)])
        with mock.patch.object(bridge_fmb, '_get_port', return_value=serial):
            self.manager.configure_all()
        self.assertTrue(self.manager._devices[6]['configured'])
        self.pub.controls.clear()
        self.burst(empty(7))
        self.burst(ai(1, 1200))
        self.assertEqual(self.pub.controls, [])
        self.burst(empty(6))
        self.burst(ai(1, 1200))
        self.assertEqual(self.pub.controls, [('ai_1', '1.2')])

    def test_reboot_fences_inflight_classic_block_and_keeps_unconfirmed_channel(self):
        self.setup_target(3)
        self.poller._ai_unconfirmed.add(2)
        read = self.poller._read_ai_holding_block

        def reboot_after_read():
            block = read()
            self.module.set_type(1, 37)
            self.module.set_value(1, 1000)
            self.burst(reboot_then_sample())
            return block

        self.pub.controls.clear()
        with mock.patch.object(self.poller, '_read_ai_holding_block', reboot_after_read):
            self.poller._poll_ai_ao()
        self.burst(empty(6))
        self.burst(ai(1, 1000), ai(2, 300))
        self.assertEqual(self.pub.controls, [])
        self.poller._poll_ai_ao()
        self.assertEqual(self.last_value(self.pub, 'ai_1'), '1.0')
        self.assertIn(2, self.poller._ai_unconfirmed)
        self.assertNotIn('ai_2', dict(self.pub.controls))

    def test_reboot_after_command_readback_before_publication(self):
        self.setup_target(3)
        original = bridge_mr02m._AiApply.apply

        def reboot_after_apply(operation, *args):
            result = original(operation, *args)
            self.burst(reboot_then_sample())
            return result

        self.pub.controls.clear()
        with mock.patch.object(bridge_mr02m._AiApply, 'apply', reboot_after_apply):
            self.poller._writeback_ai_type(1, 37)
        self.assertIn(1, self.poller._ai_unconfirmed)
        self.poller._persist_ai_sensor_types.assert_not_called()
        self.assertNotIn('ai_1', dict(self.pub.controls))

    def test_delayed_own_empty_and_delayed_pre_empty_record(self):
        self.setup_target(3)
        self.manager._poll_once(SerialReplies([empty(6)]))
        started, contexts = self.manager._poll_observation
        self.module.set_value(1, 1000)
        self.poller._writeback_ai_type(1, 37)
        self.poller.fmb_empty_observed(query_started_at=started, context=contexts[6])
        self.assertTrue(self.poller.fmb_needs_own_empty())
        _, queued = self.manager._poll_once(SerialReplies([ai(1, 204)]))
        before_empty = self.manager._poll_observation
        self.burst(empty(6))
        self.assertFalse(self.poller.fmb_needs_own_empty())
        self.pub.controls.clear()
        self.manager._dispatch(*queued[0], observation=before_empty)
        self.assertEqual(self.pub.controls, [])
        self.burst(ai(1, 1999))
        self.assertEqual(self.pub.controls, [("ai_1", "1.999")])

    def test_delayed_classic_block_cannot_replace_a_newer_event(self):
        self.setup_target(3)
        read = self.poller._read_ai_holding_block

        def delayed_read():
            block = read()
            self.burst(ai(1, 288))
            return block

        with mock.patch.object(self.poller, "_read_ai_holding_block", delayed_read):
            self.poller._poll_ai_ao()
        self.assertEqual(self.last_value(self.pub, "ai_1"), "28.8")

    def test_independent_n_and_successive_words_in_same_fifo_batch(self):
        self.setup_target(3, n_code=37)
        self.pub.controls.clear()
        self.burst(ai(1, 221))
        self.assertEqual(self.pub.controls, [("ai_1", "22.1")])
        _, queued = self.manager._poll_once(SerialReplies([ai(2, 500)]))
        old = self.manager._poll_observation
        data = ai(2, 750)[6:-2] + ai(2, 1000)[6:-2]
        self.burst(_append_crc(bytes([6, 0x46, 0x11, 7, 2, len(data)]) + data))
        self.manager._dispatch(*queued[0], observation=old)
        self.assertEqual(self.pub.controls[1:], [("ai_2", "0.75"), ("ai_2", "1.0")])

    def test_queued_pair_records_during_type_change_do_not_block_a_third_channel(self):
        self.setup_target(41)
        self.module.set_type(3, 3)
        self.module.set_value(1, 1000)
        self.poller._writeback_ai_type(1, 37)
        self.poller._poll_ai_ao()
        self.burst(empty(6))
        self.module.set_value(1, 215)
        self.poller._writeback_ai_type(1, 41)
        self.pub.controls.clear()
        self.burst(ai(1, 1000), ai(2, 2000), ai(3, 288))
        self.assertEqual(self.pub.controls, [("ai_3", "28.8")])
        self.burst(empty(6))
        self.burst(ai(1, 257), ai(2, 0))
        self.assertEqual(self.pub.controls[-2:], [("ai_1", "25.7"), ("ai_2", "25.7")])

    def test_startup_observed_generation_also_requires_own_empty(self):
        self.setup_target(3)
        self.poller._ai_unconfirmed.update([1])
        self.poller._ai_startup_observe.update([1])
        self.module.set_type(1, 37)
        self.module.set_value(1, 1010)
        self.poller._poll_ai_ao()
        self.assertEqual(self.last_value(self.pub, "ai_1"), "1.01")
        self.burst(ai(1, 215))
        self.assertEqual(self.last_value(self.pub, "ai_1"), "1.01")
        self.burst(empty(6))
        self.burst(ai(1, 1111))
        self.assertEqual(self.last_value(self.pub, "ai_1"), "1.111")

    def test_startup_n_observes_its_parent_without_publishing_hidden_p(self):
        self.setup_target(41, p_enabled=False)
        self.poller._ai_unconfirmed.add(2)
        self.poller._ai_startup_observe.add(2)
        self.module.set_value(1, 300)
        self.poller._poll_ai_ao()
        self.assertEqual(self.last_value(self.pub, "ai_2"), "30.0")
        self.pub.controls.clear()
        self.burst(ai(1, 215))
        self.assertEqual(self.pub.controls, [])
        self.burst(empty(6))
        self.burst(ai(1, 334))
        self.assertEqual(self.pub.controls, [("ai_2", "33.4")])

    def test_parent_measurement_feeds_enabled_pair_and_n_records_are_ignored(self):
        for code in (41, 23):
            for p_enabled in (True, False):
                with self.subTest(code=code, p_enabled=p_enabled):
                    self.setup_target(code, p_enabled=p_enabled)
                    self.pub.controls.clear()
                    self.burst(ai(1, 250), ai(2, 628))
                    expected = [("ai_2", "25.0")]
                    if p_enabled:
                        expected.insert(0, ("ai_1", "25.0"))
                    self.assertEqual(self.pub.controls, expected)
