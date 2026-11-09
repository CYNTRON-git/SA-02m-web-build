"""Writable WB event notifications must not overwrite newer physical state."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from test_template_poller import FakeSerial, _poller, _write_template, bridge
from bridge_serial import FMB_EVT_COIL, FMB_EVT_HOLDING, FMB_EVT_INPUT, ScanHold


class TemplateEventReadback(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        _write_template(root, 'meter', {'channels': [
            {'name': 'trigger_sensitivity', 'reg_type': 'holding', 'address': 4, 'readonly': False},
            {'name': 'relay', 'reg_type': 'coil', 'address': 1, 'readonly': False},
            {'name': 'temperature', 'reg_type': 'input', 'address': 20},
        ]})
        self.p, self.pub = _poller('meter', root, poll_s=3600,
                                   fmb_event_channels=['trigger_sensitivity', 'relay', 'temperature'])
        self.id = self.p.device_id
        self.control = self.p._channels[0]
        self.bank = FakeSerial({4: 3, 20: 42}, coils={1: 0})
        self.bank.read_holding_registers = Mock(wraps=self.bank.read_holding_registers)
        self.bank.read_coils = Mock(wraps=self.bank.read_coils)
        self.bank.read_input_registers = Mock(wraps=self.bank.read_input_registers)
        for override in (patch.object(self.p, 'get_port', return_value=self.bank),
                         patch.object(bridge.DeviceLiveCache, 'flush_file')):
            override.start()
            self.addCleanup(override.stop)
        self.p.poll_io()
        self.neighbor = Mock()
        self.neighbor.in_backoff.return_value = False
        self.scheduler = bridge.PortCycleScheduler('/dev/COM5', 19200, [self.p, self.neighbor], None)
        self.reset()

    def reset(self):
        self.pub.reset_mock()
        for action in (self.bank.read_holding_registers, self.bank.read_coils, self.bank.read_input_registers):
            action.reset_mock()

    def change(self, value):
        self.p._writeback(self.control, str(value))
        self.bank.regs[4] = value

    def slice(self):
        self.scheduler._classic_slice(.2, True)

    def test_queued_prewrite_value_after_matching_classic_read_and_new_external_change(self):
        self.change(8)
        self.p._t_poll -= self.p._poll_s
        self.p.poll_io()
        self.assertNotIn('trigger_sensitivity', self.p._wb_recent)
        self.reset()
        for _ in range(10):
            self.p.fmb_dispatch(FMB_EVT_HOLDING, 4, 3)
        self.p.fmb_dispatch(FMB_EVT_COIL, 1, 1)
        self.p.fmb_dispatch(FMB_EVT_INPUT, 20, 43)
        self.pub.pub_control.assert_called_once_with(self.id, 'temperature', '43')
        self.bank.read_holding_registers.assert_not_called()
        self.slice()
        self.bank.read_holding_registers.assert_called_once_with(self.p.address, 4, 1)
        self.bank.read_coils.assert_not_called()
        self.assertEqual(self.pub.pub_control.call_args, call(self.id, 'trigger_sensitivity', '8'))
        self.assertNotIn(call(self.id, 'trigger_sensitivity', '3'), self.pub.pub_control.call_args_list)
        self.bank.coils[1] = 1
        self.slice()
        self.pub.pub_control.assert_any_call(self.id, 'relay', '1')
        self.bank.regs[4] = 9
        self.p.fmb_dispatch(FMB_EVT_HOLDING, 4, 9)
        self.slice()
        self.assertEqual(self.pub.pub_control.call_args, call(self.id, 'trigger_sensitivity', '9'))
        self.assertEqual(self.neighbor.poll_io.call_count, 3)
        self.bank.read_input_registers.assert_not_called()

    def test_write_during_confirmation_suppresses_stale_reply_and_retries(self):
        def overlap_write(*_):
            self.change(8)
            return [3]

        self.bank.read_holding_registers.side_effect = overlap_write
        self.p.fmb_dispatch(FMB_EVT_HOLDING, 4, 3)
        self.slice()
        self.pub.pub_control.assert_called_once_with(self.id, 'trigger_sensitivity', '8', force=True)
        self.bank.read_holding_registers.side_effect = None
        self.bank.regs[4] = 9
        self.p.fmb_dispatch(FMB_EVT_HOLDING, 4, 9)
        self.slice()
        self.assertEqual(self.bank.read_holding_registers.call_count, 1)
        # Advance the shared monotonic clock past the writeback grace. No new
        # DATA must be needed to recover the real external change.
        with patch('time.monotonic', return_value=time.monotonic() + 2):
            self.slice()
        self.assertEqual(self.pub.pub_control.call_args, call(self.id, 'trigger_sensitivity', '9'))

    def test_scan_hold_retries_and_error_preserves_periodic_fallback(self):
        self.p.fmb_dispatch(FMB_EVT_HOLDING, 4, 25)
        self.bank.read_holding_registers.side_effect = ScanHold()
        with self.assertRaises(ScanHold):
            self.slice()
        self.bank.read_holding_registers.side_effect = IOError('read timeout')
        self.slice()
        self.pub.pub_error.assert_any_call(self.id, 'trigger_sensitivity', 'r')
        self.pub.pub_control.assert_not_called()
        self.bank.read_holding_registers.side_effect = None
        self.bank.regs[4] = 13
        self.p._t_poll -= self.p._poll_s
        self.slice()
        self.pub.pub_control.assert_any_call(self.id, 'trigger_sensitivity', '13')

    def test_write_echo_cannot_pass_a_read_between_its_guard_and_publication(self):
        entering_publish = threading.Event()
        write_finished_on_bus = threading.Event()
        released = threading.Event()
        published = []

        def observe(_device, _name, value, **opts):
            if not opts.get('force'):
                entering_publish.set()
                released.wait(3)
            published.append(value)

        self.pub.pub_control.side_effect = observe
        self.bank.write_register = Mock(side_effect=lambda *_: write_finished_on_bus.set())
        self.p.fmb_dispatch(FMB_EVT_HOLDING, 4, 3)
        read = threading.Thread(target=self.slice)
        write = threading.Thread(target=self.change, args=(8,))
        read.start()
        try:
            self.assertTrue(entering_publish.wait(2))
            write.start()
            self.assertTrue(write_finished_on_bus.wait(2))
        finally:
            released.set()
            read.join(2)
            if write.ident is not None:
                write.join(2)
        self.assertFalse(read.is_alive() or write.is_alive())
        self.assertEqual(published, ['3', '8'])


if __name__ == '__main__':
    unittest.main()
